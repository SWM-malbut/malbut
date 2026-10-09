"""Exercise fixed voice workflows against independently completed transports."""

import json
import time
from types import SimpleNamespace

import pytest

from malbut_agent_server.robot_operations import (
    OperationProposal, RobotOperations, WorkflowJournal,
)
from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.tools import validate_tool_arguments


class Device:
    def __init__(self, journal):
        self.journal = journal
        self.calls, self.stops, self.cancels = [], [], []
        self.observed = {}

    def send(self, request_id, operation, arguments, callback):
        # This assertion detects a send-before-journal regression.
        assert self.journal.db.execute(
            'SELECT state FROM agent_robot_steps WHERE request_id=?',
            (request_id,),
        ).fetchone()[0] == 'sent'
        self.calls.append((request_id, operation, arguments, callback))
        if operation == 'result_publish':
            callback(dict(success=True, result={'links': ['/robots/result/' + request_id]}))

    def stop(self, request_id, callback, *, confirmed_ids=None):
        self.stops.append((request_id, confirmed_ids, callback))

    def cancel(self, request_id):
        self.cancels.append(request_id)

    def snapshot(self, request_id):
        return self.observed.get(request_id, {'done': False, 'code': None})

    def close(self):
        pass


class Manager:
    def __init__(self):
        self.calls = []
        self.cancels = []
        self.records = {}

    def submit(self, capability, arguments, request_id, **confirmation):
        self.calls.append((capability, arguments, request_id, confirmation))
        self.records[request_id] = dict(request_id=request_id, capability_id=capability,
                                       state='RUNNING', kind='accepted', terminal=False)
        return request_id

    def snapshot(self, request_id):
        return self.records[request_id]

    def cancel(self, request_id):
        self.cancels.append(request_id)
        return self.snapshot(request_id)


def proposal(request_id, tool, arguments=None, utterance='요청해 줘', user='speaker', generation=1):
    return OperationProposal(request_id, tool, json.dumps(arguments or {}), json.dumps({
        'user_id': user, 'conversation_id': 'conversation',
        'generation': generation, 'utterance': utterance,
    }))


def status(*, ready=True, pose_ready=True, active=()):
    return {'runtime': {'state': 'RUNNING' if ready else 'STOPPED', 'ready': ready,
                        'mode': 'navigation', 'map': 'home.yaml',
                        'localization': {'mode': 'LOCALIZATION', 'map': 'home.yaml',
                                         'pose_ready': pose_ready,
                                         'runtime_id': 'r1', 'transition_id': 1}},
            'maps': [{'id': 'home.yaml', 'name': 'home', 'revision': 'revision-one'}],
            'last_selected_map': 'home.yaml',
            'system': {'active_foreground_missions': [{'mission_id': item} for item in active],
                       'movement_runtime_id': 'manager-lifetime', 'movement_epoch': 3},
            'battery': None}


@pytest.fixture
def rig(tmp_path):
    path = tmp_path / 'agent.db'
    journal = WorkflowJournal(str(path))
    device, manager, notices, clock = Device(journal), Manager(), [], [100.0]
    runner = RobotOperations(manager, device, journal, notify=notices.append, clock=lambda: clock[0])
    value = SimpleNamespace(runner=runner, journal=journal, device=device, manager=manager,
                            notices=notices, clock=clock, path=path)
    yield value
    if not runner.closed:
        runner.close()


def respond(rig, operation, value=None, *, success=True, code='completed', message='확인했어요.'):
    request_id, _, arguments, callback = next(
        call for call in reversed(rig.device.calls) if call[1] == operation)
    value = value or {}
    if operation == 'runtime_start' and success:
        value.setdefault('preparation_movement_binding', {'runtime_id': 'manager-lifetime', 'epoch': 3})
    callback(dict(success=success, code=code, result=value, message=message))
    rig.runner.tick()
    return arguments


def test_query_reports_observed_state_and_publishes_stable_result(rig):
    rig.runner.dispatch(proposal('state', 'get_robot_status'))
    respond(rig, 'status', status())
    assert rig.manager.calls == [] and rig.device.stops == []
    job = rig.runner.jobs['state']
    assert job['state'] == 'succeeded'
    assert '배터리 값은 아직 확인되지 않았어요' in job['message']
    publications = [item for item in rig.device.calls if item[1] == 'result_publish']
    assert [item[2]['state'] for item in publications] == ['accepted', 'succeeded']
    assert publications[0][0] != publications[1][0]
    assert job['publication']['links']


@pytest.mark.parametrize('runtime_state,voice_ready,expected', [
    ('STOPPED', True, '로봇 기능은 꺼져 있고, 음성 대화는 대기 중이에요.'),
    ('STOPPED', False, '로봇 기능은 꺼져 있어요.'),
    ('STOPPED', None, '로봇 기능은 꺼져 있어요.'),
    ('STARTING', True, '로봇 기능을 켜고 있어요.'),
    ('RUNNING', True, '로봇 기능이 켜져 있어요.'),
    ('STOPPING', True, '로봇 기능을 끄고 있어요.'),
    ('ERROR', True, '로봇 기능에 오류가 있어 확인이 필요해요.'),
    ('FUTURE_STATE', True, '로봇 기능의 현재 상태를 아직 확인하지 못했어요.'),
    (None, True, '로봇 기능의 현재 상태를 아직 확인하지 못했어요.'),
])
def test_spoken_status_uses_korean_and_only_observed_voice_readiness(runtime_state, voice_ready, expected):
    value = {'runtime': {'state': runtime_state}, 'system': {}, 'battery': None}
    if voice_ready is not None:
        value['voice'] = {'ready': voice_ready}
    message = RobotOperations._describe({'tool': 'get_robot_status'}, value, None)
    assert message == expected + ' 확인된 진행 작업은 0개예요. 배터리 값은 아직 확인되지 않았어요.'


def test_preparation_observes_runtime_and_pose_before_manager_send(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(ready=False, pose_ready=False))
    assert rig.manager.calls == []
    args = respond(rig, 'runtime_start', {'accepted': True})
    assert args == {'mode': 'navigation', 'map': 'home.yaml',
                    'movement_runtime_id': 'manager-lifetime', 'movement_epoch': 3}
    assert rig.manager.calls == []
    respond(rig, 'status', status())
    assert rig.manager.calls[0][0:2] == ('follow_person', {
        'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 1.0})
    confirmation = rig.manager.calls[0][3]
    assert confirmation['require_preemption_confirmation'] is True
    assert confirmation['confirmed_preemption_mission_ids'] == ()
    row = rig.journal.db.execute('SELECT goal_id FROM agent_robot_steps WHERE request_id=?',
                                (rig.manager.calls[0][2],)).fetchone()
    assert row['goal_id'] == confirmation['goal_uuid'].hex


def test_transition_receipt_without_pose_is_not_readiness(rig):
    rig.runner.dispatch(proposal('patrol', 'request_patrol', {'thoroughness': 'thorough'}))
    respond(rig, 'status', status(pose_ready=False))
    request_id = rig.manager.calls[0][2]
    rig.runner.handle(dict(request_id=request_id, terminal=True, state='SUCCEEDED',
                           result_yaml='{"success":true}'))
    rig.runner.tick()
    respond(rig, 'status', status(pose_ready=False))
    assert [call[0] for call in rig.manager.calls] == ['relocalize']
    assert rig.runner.jobs['patrol']['state'] == 'failed'


@pytest.mark.parametrize('initially_ready', [False, True])
def test_autoslam_owns_mapping_after_default_map_runtime_preparation(rig, initially_ready):
    """Default-map startup is sufficient; only the AutoSLAM mission starts SLAM."""
    observed = status(ready=initially_ready)
    observed.update(maps=[], last_selected_map=None)
    observed['runtime']['map'] = None
    observed['runtime']['localization']['map'] = 'default_map.yaml'
    rig.runner.dispatch(proposal('mapping', 'request_mapping', {'map_name': 'new'}))
    respond(rig, 'status', observed)
    if not initially_ready:
        assert rig.manager.calls == []
        assert respond(rig, 'runtime_start') == {
            'mode': 'mapping', 'movement_runtime_id': 'manager-lifetime', 'movement_epoch': 3}
        observed['runtime'].update(state='RUNNING', ready=True)
        respond(rig, 'status', observed)
    else:
        assert not any(call[1] == 'runtime_start' for call in rig.device.calls)
    assert [call[:2] for call in rig.manager.calls] == [('autoslam', {'map_name': 'new'})]
    binding = rig.manager.calls[0][3]
    assert binding['expected_localization_runtime_id'] == 'r1'
    assert binding['expected_localization_transition_id'] == 1
    assert binding['require_movement_epoch'] is True
    assert binding['movement_epoch'] == 3
    assert not any(call[1] == 'map_select' for call in rig.device.calls)


def test_default_unknown_map_is_not_a_saved_map_for_following(rig):
    observed = status()
    observed.update(maps=[], last_selected_map=None)
    observed['runtime']['map'] = None
    observed['runtime']['localization']['map'] = 'default_map.yaml'
    rig.runner.dispatch(proposal('follow-default', 'request_follow_person'))
    respond(rig, 'status', observed)
    assert rig.manager.calls == []
    assert rig.runner.jobs['follow-default']['state'] == 'failed'
    assert '저장된 지도가 없어요' in rig.runner.jobs['follow-default']['message']


def test_standby_does_not_preempt_its_own_manager_query_or_resident_weather(rig):
    observed = status()
    observed['system']['active_background_missions'] = [
        {'mission_id': 'status-query', 'capability_id': 'device_operation'},
        {'mission_id': 'weather', 'capability_id': 'get_weather'},
        {'mission_id': 'weather-setting', 'capability_id': 'set_weather_location'},
    ]
    rig.runner.dispatch(proposal('standby', 'standby_robot'))
    respond(rig, 'status', observed)
    assert rig.device.stops == []
    assert rig.device.calls[-1][1:3] == ('runtime_stop', {'confirmed_mission_ids': []})


def test_unrelated_work_requires_bound_direct_confirmation(rig):
    rig.runner.dispatch(proposal('standby', 'standby_robot'))
    respond(rig, 'status', status(active=('web-drive',)))
    assert rig.device.stops == []
    assert rig.runner.jobs['standby']['state'] == 'awaiting_confirmation'
    rig.runner.dispatch(proposal('fake', 'confirm_pending_operation', {'confirm': True}, '예라고 말한 예시야'))
    assert rig.device.stops == []
    rig.runner.dispatch(proposal('yes', 'confirm_pending_operation', {'confirm': True}, '네'))
    respond(rig, 'status', status(active=('web-drive',)))
    assert rig.device.stops[-1][1] == ['web-drive']
    rig.device.stops[-1][2](dict(success=True, result={'affected_mission_ids': ['web-drive']}))
    rig.runner.tick()
    stopped = status()
    stopped['system']['movement_epoch'] = 4
    respond(rig, 'status', stopped)
    assert rig.device.calls[-1][1:3] == ('runtime_stop', {'confirmed_mission_ids': ['web-drive']})


def test_new_mission_after_confirmation_is_never_implicitly_canceled(rig):
    rig.runner.dispatch(proposal('standby', 'standby_robot'))
    respond(rig, 'status', status(active=('old',)))
    rig.runner.dispatch(proposal('yes', 'confirm_pending_operation', {'confirm': True}, '응'))
    respond(rig, 'status', status(active=('old', 'new')))
    assert rig.device.stops == []
    assert rig.runner.jobs['standby']['conflicts'] == ['new', 'old']
    assert rig.runner.jobs['standby']['state'] == 'awaiting_confirmation'


@pytest.mark.parametrize('user,generation', [('other', 1), ('speaker', 2)])
def test_confirmation_cannot_cross_user_or_conversation_generation(rig, user, generation):
    rig.runner.dispatch(proposal('delete', 'delete_saved_map', {'map': 'home.yaml'}))
    respond(rig, 'map_list', status())
    rig.runner.dispatch(proposal('yes', 'confirm_pending_operation', {'confirm': True},
                                 '네', user=user, generation=generation))
    assert not any(item[1] == 'map_delete' for item in rig.device.calls)


def test_delete_rechecks_exact_target_after_confirmation(rig):
    rig.runner.dispatch(proposal('delete', 'delete_saved_map', {'map': 'home.yaml'}))
    respond(rig, 'map_list', status())
    assert rig.runner.jobs['delete']['state'] == 'awaiting_confirmation'
    rig.runner.dispatch(proposal('yes', 'confirm_pending_operation', {'confirm': True}, '네'))
    respond(rig, 'map_list', {'maps': [{'id': 'home.yaml', 'name': 'home', 'revision': 'replaced'}]})
    assert not any(item[1] == 'map_delete' for item in rig.device.calls)
    assert rig.runner.jobs['delete']['state'] == 'failed'


def test_zone_update_uses_observed_revision_only(rig):
    rig.runner.dispatch(proposal('zone', 'update_map_zone', {
        'map': 'home.yaml', 'index': 2, 'name': None, 'behavior': 'restricted'}))
    respond(rig, 'zones_get', {'map': 'home.yaml', 'revision': 'observed-revision', 'zones': [{}, {}, {}]})
    assert rig.device.calls[-1][1:3] == ('zones_update', {
        'map': 'home.yaml', 'index': 2, 'behavior': 'restricted', 'revision': 'observed-revision'})
    assert rig.manager.calls == []


def test_stop_cancels_pending_preparation_and_late_result_cannot_move(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    pending_status = next(item for item in rig.device.calls if item[1] == 'status')
    rig.runner.dispatch(proposal('stop', 'stop_robot_movement'))
    assert rig.device.stops[-1][1] is None
    pending_status[3](dict(success=True, result=status()))
    rig.runner.tick()
    assert rig.manager.calls == []
    assert rig.runner.jobs['follow']['state'] == 'canceled'


def test_restart_never_replays_previously_sent_or_waiting_operation(rig):
    request = proposal('start', 'wake_robot', {'map': None})
    rig.runner.dispatch(request)
    rig.runner.close()
    journal = WorkflowJournal(str(rig.path))
    device = Device(journal)
    restored = RobotOperations(Manager(), device, journal)
    try:
        message = restored.dispatch(request)
        assert '자동으로 다시 요청하지 않았어요' in message
        assert device.calls == []
        assert journal.recent()[0]['state'] == 'unknown'
    finally:
        restored.close()


def test_duplicate_does_not_send_and_changed_input_is_rejected(rig):
    request = proposal('same', 'request_patrol', {'thoroughness': 'normal'})
    rig.runner.dispatch(request)
    calls = len(rig.device.calls)
    rig.runner.dispatch(request)
    assert len(rig.device.calls) == calls
    with pytest.raises(ValueError):
        rig.runner.dispatch(proposal('same', 'request_mapping', {'map_name': 'new'}))


def test_guard_failure_never_writes_dispatch_or_contacts_transports(rig):
    assert rig.runner.dispatch(proposal('expired', 'wake_robot', {'map': None}),
                               guard=lambda: '만료됐어요') == '만료됐어요'
    assert rig.journal.recent() == []
    assert rig.device.calls == []


def test_preparation_failure_does_not_submit_manager(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(ready=False))
    respond(rig, 'runtime_start', success=False, code='unavailable', message='준비 실패')
    assert rig.manager.calls == []
    assert rig.runner.jobs['follow']['message'] == '준비 실패'


def test_homecam_denial_does_not_alter_consent_or_retry(rig):
    rig.runner.dispatch(proposal('camera', 'update_homecam_settings', {
        'cameraEnabled': True, 'microphoneEnabled': None,
        'monitoringEnabled': None, 'fallEnabled': None}))
    args = respond(rig, 'homecam_settings', success=False, code='delegation_required',
                   message='소유자 웹 설정에서 기기 위임을 켜 주세요.')
    assert args == {'cameraEnabled': True}
    assert rig.runner.jobs['camera']['state'] == 'failed'
    assert len([item for item in rig.device.calls if item[1] == 'homecam_settings']) == 1


def test_running_follow_is_not_canceled_by_preparation_deadline(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status())
    rig.clock[0] += 1000
    rig.runner.tick()
    assert rig.runner.jobs['follow']['state'] == 'running'
    assert rig.device.cancels == []


@pytest.mark.parametrize('tool,args', [
    ('update_homecam_settings', {'cameraEnabled': 'yes', 'microphoneEnabled': None,
                                'monitoringEnabled': None, 'fallEnabled': None}),
    ('get_homecam_events', {'limit': 21, 'event_type': None}),
    ('confirm_pending_operation', {'confirm': True, 'mission_ids': ['foreign']}),
    ('update_map_zone', {'map': 'home.yaml', 'index': True, 'name': None, 'behavior': None}),
])
def test_unbounded_or_authority_injecting_arguments_fail(tool, args):
    with pytest.raises(ValidationError):
        validate_tool_arguments(tool, args)


def test_expired_preparation_result_cannot_send_next_step(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    rig.clock[0] += 601
    respond(rig, 'status', status())
    assert rig.manager.calls == []
    assert rig.runner.jobs['follow']['state'] == 'unknown'


def test_fall_confirmation_invalidates_pending_prepare_without_replay(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    rig.runner.preempt_preparations()
    respond(rig, 'status', status())
    assert rig.manager.calls == []
    assert rig.runner.jobs['follow']['state'] == 'canceled'


def test_observation_context_is_same_user_session_and_retains_links(rig):
    rig.runner.dispatch(proposal('maps', 'list_saved_maps'))
    respond(rig, 'map_list', status())
    context = rig.runner.context('speaker', 'conversation')
    assert context[0]['result']['maps'][0]['id'] == 'home.yaml'
    assert context[0]['publication']['links']
    assert rig.runner.context('other', 'conversation') == []
    assert rig.runner.context('speaker', 'other') == []


def test_settings_saved_is_not_runtime_verified(rig):
    rig.runner.dispatch(proposal('camera', 'update_homecam_settings', {
        'cameraEnabled': True, 'microphoneEnabled': None,
        'monitoringEnabled': None, 'fallEnabled': None}))
    respond(rig, 'homecam_settings', {'saved': True, 'receiptState': 'waiting', 'runtimeVerified': False})
    assert '아직 확인 전' in rig.runner.jobs['camera']['message']


def test_only_map_without_explicit_or_prior_choice_is_not_selected(rig):
    rig.runner.dispatch(proposal('wake', 'wake_robot', {'map': None}))
    current = status(ready=False)
    current['runtime']['map'] = None
    current['last_selected_map'] = None
    respond(rig, 'status', current)
    assert '사용할 지도를 말씀' in rig.runner.jobs['wake']['message']
    assert not any(call[1] == 'runtime_start' for call in rig.device.calls)


@pytest.mark.parametrize('method,expected', [('auto', 0), ('global_search', 2)])
def test_explicit_relocalization_accepts_lost_pose_and_honors_method(rig, method, expected):
    rig.runner.dispatch(proposal('locate', 'request_relocalization', {'method': method}))
    respond(rig, 'status', status(pose_ready=False))
    assert rig.manager.calls[0][0:2] == ('relocalize', {'method': expected, 'initial_pose': {}})
    assert not any(call[1] == 'map_select' for call in rig.device.calls)


def test_relocalization_reuses_auto_preparation_result(rig):
    rig.runner.dispatch(proposal('locate', 'request_relocalization', {'method': 'auto'}))
    respond(rig, 'status', status(ready=False, pose_ready=False))
    respond(rig, 'runtime_start', {'localization': {'pose_ready': True}})
    respond(rig, 'status', status())
    assert rig.manager.calls == []
    assert rig.runner.jobs['locate']['state'] == 'succeeded'


def test_confirmation_names_actual_conflicting_work(rig):
    rig.runner.dispatch(proposal('standby', 'standby_robot'))
    current = status(active=('web-drive',))
    current['system']['active_foreground_missions'][0]['capability_id'] = 'manual_drive'
    respond(rig, 'status', current)
    assert 'manual_drive [web-drive]' in rig.runner.jobs['standby']['message']


def test_delete_forwards_only_server_bound_revision(rig):
    rig.runner.dispatch(proposal('delete', 'delete_saved_map', {'map': 'home.yaml'}))
    respond(rig, 'map_list', status())
    rig.runner.dispatch(proposal('yes', 'confirm_pending_operation', {'confirm': True}, '네'))
    respond(rig, 'map_list', status())
    assert rig.device.calls[-1][1:3] == ('map_delete', {
        'map': 'home.yaml', 'confirmed': True, 'revision': 'revision-one'})


@pytest.mark.parametrize('success', [True, False])
def test_homecam_publication_preserves_delegated_kind_without_denied_payload(rig, success):
    rig.runner.dispatch(proposal('camera', 'get_homecam_status'))
    respond(rig, 'homecam_status', {'cameraEnabled': True}, success=success)
    publications = [call for call in rig.device.calls if call[1] == 'result_publish']
    assert len(publications) == int(success)
    if success:
        assert publications[0][2]['kind'] == 'homecam'
        assert 'referenceId' not in publications[0][2]


def test_homecam_status_reads_flat_saved_settings_and_current_revision_receipts(rig):
    rig.runner.dispatch(proposal('camera', 'get_homecam_status'))
    respond(rig, 'homecam_status', {
        'cameraEnabled': True, 'microphoneEnabled': False,
        'monitoringEnabled': False, 'fallEnabled': True,
        'settingsRevision': '7', 'mediaSettingsRevision': '3', 'runtimeVerified': False,
        'mediaApplyReceipt': {
            'runtimeId': 'media-a', 'sequence': '2', 'requestedRevision': '3',
            'cameraEnabled': True, 'microphoneEnabled': False, 'monitoringEnabled': False,
            'applied': True, 'state': 'reported_applied', 'reasonCode': 'applied',
            'reportAgeS': 2, 'fresh': True, 'runtimeVerified': False,
        },
        'fallApplyReceipt': {
            'runtimeId': 'fall-a', 'requestedRevision': '7', 'appliedRevision': '7',
            'applied': True, 'enabled': True, 'cameraEnabled': True, 'cloudConsent': False,
            'state': 'reported_applied', 'reportAgeS': 1, 'fresh': True, 'runtimeVerified': False,
        },
    })
    message = rig.runner.jobs['camera']['message']
    assert '저장된 설정은 카메라 켜짐, 마이크 꺼짐, 감시 꺼짐, 낙상 감시 켜짐이에요.' in message
    assert '홈캠 설정을 적용했다는 최근 회신이 있어요.' in message
    assert '낙상 감시 설정을 적용했다는 최근 회신이 있어요.' in message
    assert '현재 정상 동작 여부는 별도 확인이 필요해요.' in message
    assert '실제 기기 적용 여부는 아직 확인 전' not in message
    published = [call[2] for call in rig.device.calls if call[1] == 'result_publish']
    assert published[-1]['summary'] == message


@pytest.mark.parametrize('receipt,expected', [
    ({'state': 'waiting', 'runtimeVerified': False}, '홈캠 설정의 적용 회신을 기다리고 있어요.'),
    ({'state': 'no_response', 'runtimeVerified': False}, '홈캠 설정의 적용 회신은 아직 없어요.'),
    ({'state': 'reported_applied', 'applied': True, 'fresh': False, 'reportAgeS': 61},
     '홈캠 설정을 적용했다는 오래된 회신이 있어요.'),
    ({'state': 'reported_failed', 'applied': False, 'fresh': True, 'reportAgeS': 0,
      'reasonCode': 'local_media_unavailable'}, '홈캠 설정 적용에 실패했다는 최근 회신이 있어요.'),
    ({}, '홈캠 설정의 적용 회신 상태는 아직 확인되지 않았어요.'),
])
def test_homecam_status_does_not_confuse_receipt_state_or_freshness(receipt, expected):
    message = RobotOperations._describe({'tool': 'get_homecam_status'}, {
        'cameraEnabled': True, 'microphoneEnabled': True, 'monitoringEnabled': False,
        'fallEnabled': False, 'runtimeVerified': False, 'mediaApplyReceipt': receipt,
        'fallApplyReceipt': {'state': 'waiting', 'runtimeVerified': False},
    }, None)
    assert expected in message
    assert '낙상 감시 설정의 적용 회신을 기다리고 있어요.' in message
    assert '현재 정상 동작 여부는 별도 확인이 필요해요.' in message


def test_past_observations_never_become_current_identity_claim(rig):
    rig.runner.dispatch(proposal('look', 'get_robot_observations'))
    respond(rig, 'status', {'observations': {'person': {'current': False, 'person_id': 'alice'}},
                            'tracking': {'current': False}})
    message = rig.runner.jobs['look']['message']
    assert '과거 관측' in message and 'alice' not in message


def test_stop_does_not_claim_running_manager_work_already_canceled(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status())
    rig.runner.dispatch(proposal('stop', 'stop_robot_movement'))
    assert rig.runner.jobs['follow']['state'] == 'running'
    assert rig.manager.cancels == [rig.manager.calls[0][2]]


def test_global_stop_waits_for_owned_late_manager_acceptance_to_end(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status())
    request_id = rig.manager.calls[0][2]
    rig.manager.records[request_id].update(state='SUBMITTING', terminal=False)
    rig.runner.dispatch(proposal('stop', 'stop_robot_movement'))
    rig.device.stops[-1][2](dict(success=True, result={'affected_mission_ids': []}))
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'running'
    assert rig.manager.cancels == [request_id]
    rig.manager.records[request_id].update(state='CANCELED', terminal=True)
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'succeeded'


def test_unresolved_late_manager_acceptance_never_claims_global_stop(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status())
    rig.runner.dispatch(proposal('stop', 'stop_robot_movement'))
    rig.device.stops[-1][2](dict(success=True, result={}))
    rig.runner.tick()
    rig.clock[0] += 31
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'unknown'


def test_global_stop_waits_for_late_runtime_start_cancellation(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(ready=False))
    runtime_request = rig.runner.jobs['follow']['pending_id']
    rig.runner.dispatch(proposal('stop', 'stop_robot_movement'))
    rig.device.stops[-1][2](dict(success=True, result={}))
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'running'
    rig.device.observed[runtime_request] = {'done': True, 'code': 'canceled'}
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'succeeded'
    assert rig.manager.calls == []


@pytest.mark.parametrize('code', ['rejected', 'movement_epoch_changed'])
def test_global_stop_completes_when_manager_never_dispatched_late_preparation(rig, code):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(ready=False))
    runtime_request = rig.runner.jobs['follow']['pending_id']
    rig.runner.dispatch(proposal('stop', 'stop_robot_movement'))
    rig.device.stops[-1][2](dict(success=True, result={}))
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'running'
    rig.device.observed[runtime_request] = {'done': True, 'code': code, 'not_dispatched': True}
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'succeeded'
    assert rig.manager.calls == []


@pytest.mark.parametrize('code', ['rejected', 'movement_epoch_changed', 'unknown', 'timeout', 'stop_unconfirmed'])
def test_global_stop_does_not_assume_backend_failure_means_no_dispatch(rig, code):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(ready=False))
    runtime_request = rig.runner.jobs['follow']['pending_id']
    rig.runner.dispatch(proposal('stop', 'stop_robot_movement'))
    rig.device.stops[-1][2](dict(success=True, result={}))
    rig.runner.tick()
    rig.device.observed[runtime_request] = {'done': True, 'code': code}
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'running'
    rig.clock[0] += 31
    rig.runner.tick()
    assert rig.runner.jobs['stop']['state'] == 'unknown'
    assert rig.manager.calls == []


def test_same_selected_map_lost_pose_runs_one_auto_before_final_motion(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(pose_ready=False))
    assert rig.manager.calls[0][0] == 'relocalize'
    assert rig.manager.calls[0][3]['expected_localization_runtime_id'] == 'r1'
    assert rig.manager.calls[0][3]['expected_localization_transition_id'] == 1
    assert not any(call[1] == 'map_select' for call in rig.device.calls)
    request_id = rig.manager.calls[0][2]
    rig.runner.handle(dict(request_id=request_id, terminal=True, state='SUCCEEDED',
                           result_yaml='{"success":true}'))
    rig.runner.tick()
    assert [call[0] for call in rig.manager.calls] == ['relocalize']
    respond(rig, 'status', status())
    assert [call[0] for call in rig.manager.calls] == ['relocalize', 'follow_person']


def test_current_map_identity_is_required_before_bound_mission_send(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    current = status()
    del current['runtime']['localization']['runtime_id']
    respond(rig, 'status', current)
    assert rig.manager.calls == []
    assert rig.runner.jobs['follow']['state'] == 'failed'


def test_movement_epoch_is_persisted_before_wire_and_missing_epoch_blocks(rig):
    rig.runner.dispatch(proposal('manual', 'request_manual_control'))
    current = status()
    del current['system']['movement_epoch']
    respond(rig, 'status', current)
    assert rig.runner.jobs['manual']['state'] == 'unknown'
    assert rig.manager.calls == []
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status())
    policy = rig.manager.calls[0][3]
    assert policy['require_movement_epoch'] is True
    assert policy['movement_runtime_id'] == 'manager-lifetime'
    assert policy['movement_epoch'] == 3
    row = rig.journal.db.execute('SELECT arguments FROM agent_robot_steps WHERE request_id=?',
                                (rig.manager.calls[0][2],)).fetchone()
    assert json.loads(row[0])['movement_epoch'] == 3


def test_external_stop_epoch_during_pose_preparation_blocks_final_mission(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(pose_ready=False))
    request_id = rig.manager.calls[0][2]
    rig.runner.handle(dict(request_id=request_id, terminal=True, state='SUCCEEDED', result_yaml='{}'))
    rig.runner.tick()
    changed = status()
    changed['system']['movement_epoch'] = 4
    respond(rig, 'status', changed)
    assert [call[0] for call in rig.manager.calls] == ['relocalize']
    assert rig.runner.jobs['follow']['state'] == 'failed'


def test_map_transition_sends_original_preparation_epoch_on_wire(rig):
    rig.runner.dispatch(proposal('choose', 'select_saved_map', {'map': 'other.yaml'}))
    current = status()
    current['maps'].append({'id': 'other.yaml', 'name': 'other', 'revision': 'other-revision'})
    respond(rig, 'status', current)
    call = next(call for call in rig.device.calls if call[1] == 'map_select')
    assert call[2] == {'map': 'other.yaml', 'movement_runtime_id': 'manager-lifetime',
                       'movement_epoch': 3}
    row = rig.journal.db.execute('SELECT arguments FROM agent_robot_steps WHERE request_id=?',
                                (call[0],)).fetchone()
    assert json.loads(row[0]) == call[2]


def test_new_manager_startup_binding_cannot_adopt_a_later_stop_epoch(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(ready=False))
    respond(rig, 'runtime_start', {'preparation_movement_binding': {'runtime_id': 'new-lifetime', 'epoch': 0}})
    changed = status()
    changed['system'].update(movement_runtime_id='new-lifetime', movement_epoch=1)
    respond(rig, 'status', changed)
    assert rig.manager.calls == []
    assert rig.runner.jobs['follow']['state'] == 'failed'


@pytest.mark.parametrize('downstream_terminal', [False, True])
def test_restart_reconciles_exact_retained_goal_without_resending(rig, downstream_terminal):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status())
    goal_id = rig.runner.jobs['follow']['goal_id']
    rig.runner.close()
    journal = WorkflowJournal(str(rig.path))
    device, manager = Device(journal), Manager()
    restored = RobotOperations(manager, device, journal)
    try:
        record = dict(mission_id=goal_id, capability_id='follow_person', state='ABORTED',
                      downstream_terminal=downstream_terminal, message='결과', observed_at='2026-10-07T12:00:00Z')
        restored.observe_device_state({'recent_results': [dict(record, mission_id='wrong')]})
        assert journal.recent()[0]['state'] == 'unknown'
        restored.observe_device_state({'recent_results': [record]})
        assert journal.recent()[0]['state'] == ('failed' if downstream_terminal else 'unknown')
        assert manager.calls == []
        assert all(call[1] == 'result_publish' for call in device.calls)
    finally:
        restored.close()


def test_recovered_preparation_result_does_not_claim_final_motion_succeeded(rig):
    rig.runner.dispatch(proposal('follow', 'request_follow_person'))
    respond(rig, 'status', status(pose_ready=False))
    goal_id = rig.runner.jobs['follow']['goal_id']
    rig.runner.close()
    journal = WorkflowJournal(str(rig.path))
    device, manager = Device(journal), Manager()
    restored = RobotOperations(manager, device, journal)
    try:
        restored.observe_device_state({'recent_results': [dict(
            mission_id=goal_id, capability_id='relocalize', state='SUCCEEDED',
            downstream_terminal=True, message='pose ready')]})
        job = journal.recent()[0]
        assert job['state'] == 'unknown'
        assert '원래 요청한 동작은 자동으로 재개하지 않았어요' in job['message']
        assert device.calls == manager.calls == []
    finally:
        restored.close()


def test_dialogue_commits_then_dispatches_and_next_turn_reads_observed_result(rig):
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator
    from malbut_agent_server.schemas import AgentDecision, ProviderResult
    from malbut_agent_server.speech_dialogue import DialogueWorker
    from malbut_agent_server.speech_mission_policy import configure_speech_missions

    contexts = []

    class Provider:
        supports_memory = True

        def complete(self, request, memories, history, tools, conversation_summary=None,
                     *, memory_context=None):
            contexts.append(memory_context)
            decision = (AgentDecision(type='tool_call', tool_name='get_robot_status',
                                      arguments={}, message='') if len(contexts) == 1
                        else AgentDecision(type='message', message='관측 결과를 확인했어요.'))
            return ProviderResult(decision=decision, provider='fixed', model='fixed', latency_ms=0)

    def factory():
        runtime = build_orchestrator(Settings(database_path=str(rig.path)))
        runtime.provider = Provider()
        runtime.robot_operation_context = rig.runner.context
        configure_speech_missions(runtime, device_operations=True)
        return runtime

    def collect(worker):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            replies = [reply for reply in worker.drain() if reply['kind'] == 'answer']
            if replies:
                return replies[0]
            time.sleep(0.002)
        raise AssertionError('dialogue did not complete')

    worker = DialogueWorker(factory, 'speaker', missions=rig.runner)
    try:
        assert worker.submit('state-turn', '로봇 상태 알려 줘')
        reply = collect(worker)
        assert rig.device.calls == []
        worker.publish_reply(reply, lambda text: True)
        assert rig.device.calls[0][1] == 'status'
        respond(rig, 'status', status())
        assert worker.submit('next-turn', '방금 결과가 뭐였지?')
        collect(worker)
        observed = contexts[-1]['robot_operation_results'][0]
        assert observed['state'] == 'succeeded'
        assert observed['result']['battery'] is None
        assert observed['publication']['links']
    finally:
        worker.close()
