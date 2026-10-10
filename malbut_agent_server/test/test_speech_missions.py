"""Voice dispatch ownership, correlation and target freshness without ROS."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
from types import SimpleNamespace

import pytest

from malbut_agent_server.speech_missions import SpeechMissions


class FakeManager:
    """Expose separately observable Goal submission and terminal outcomes."""

    def __init__(self):
        self.submissions = []
        self.cancellations = []
        self.records = {}
        self.submit_error = None
        self.on_event = lambda event: None
        self.initial_state = 'SUBMITTING'
        self.initial_kind = 'submitted'

    def submit(self, capability, arguments, request_id):
        self.submissions.append((capability, arguments, request_id))
        self.records[request_id] = {
            'request_id': request_id, 'capability_id': capability,
            'state': self.initial_state, 'kind': self.initial_kind,
            'terminal': self.initial_state in {'UNAVAILABLE', 'REJECTED'},
        }
        self.on_event(dict(self.records[request_id]))
        if self.submit_error is not None:
            raise self.submit_error
        return request_id

    def snapshot(self, request_id):
        return dict(self.records[request_id])

    def cancel(self, request_id):
        self.cancellations.append(request_id)
        self.records[request_id]['kind'] = 'cancel_requested'
        self.on_event(self.snapshot(request_id))
        return self.snapshot(request_id)


class Targets:
    """Represent a mutable on-disk destination catalog using fixed fixtures."""

    def __init__(self):
        self.digest = 'original-config-and-map-digest'
        self.arguments = {'pose': {'test': 1}, 'behavior_tree': ''}
        self.lookups = []
        self.error = None

    def resolve(self, location, active_map):
        self.lookups.append((location, active_map))
        if self.error:
            raise self.error
        return SimpleNamespace(
            arguments=self.arguments, digest=self.digest, map_path=active_map,
        )


@pytest.mark.parametrize('tool, arguments, capability, expected', [
    ('request_mapping', {'map_name': 'new_home'}, 'autoslam', {'map_name': 'new_home'}),
    ('request_relocalization', {'method': 'auto'}, 'relocalize', {'method': 0}),
    ('request_relocalization', {'method': 'global_search'}, 'relocalize', {'method': 2}),
    ('request_manual_control', {}, 'manual_drive', {'time_allowance': {'sec': 0, 'nanosec': 0}}),
    ('request_recovery', {}, 'recovery', {}),
])
def test_added_commands_submit_only_the_existing_manifest(tool, arguments, capability, expected):
    manager = FakeManager()
    dispatcher = SpeechMissions(manager)
    dispatcher.dispatch(dispatcher.prepare('leaf-request', tool, arguments))
    assert len(manager.submissions) == 1
    assert manager.submissions[0][:2] == (capability, expected)


def test_cancel_uses_existing_foreground_cancel_and_remembers_unaccepted_voice_goal():
    manager = FakeManager()
    all_foreground = []
    dispatcher = SpeechMissions(manager, cancel_foreground=lambda: all_foreground.append(True))
    dispatcher.dispatch(dispatcher.prepare('follow', 'request_follow_person', {}))
    dispatcher.dispatch(dispatcher.prepare('cancel', 'cancel_voice_mission', {}))
    assert len(manager.cancellations) == 1 and all_foreground == [True]


@pytest.fixture
def harness():
    manager = FakeManager()
    targets = Targets()
    dispatcher = SpeechMissions(manager, targets, clock=lambda: 42.0)
    manager.on_event = dispatcher.handle
    dispatcher.observe_localization({
        'mode': 'LOCALIZATION', 'map': '/maps/home.yaml',
    })
    return SimpleNamespace(
        manager=manager, targets=targets, dispatcher=dispatcher,
    )


def test_preparation_is_immutable_and_submission_uses_ros_owner(harness):
    """Worker preparation cannot cause execution or claim completion."""
    dispatcher, manager = harness.dispatcher, harness.manager
    with ThreadPoolExecutor(max_workers=1) as pool:
        proposal = pool.submit(
            dispatcher.prepare, 'turn-1', 'request_follow_person', {},
        ).result()
        with pytest.raises(RuntimeError, match='ROS owner'):
            pool.submit(dispatcher.dispatch, proposal).result()
    assert manager.submissions == []
    assert proposal.prepared_at == 42.0
    with pytest.raises(FrozenInstanceError):
        proposal.tool_name = 'arbitrary_action'
    response = dispatcher.dispatch(proposal)
    assert response == '실행 요청을 보냈어요. 접수 결과를 확인할게요.'
    assert manager.submissions == [(
        'follow_person', {
            'target_mode': 0, 'target_person_id': '',
            'desired_distance_m': 1.0,
        }, 'speech-mission:' + hashlib.sha256(b'turn-1').hexdigest(),
    )]


@pytest.mark.parametrize('level, expected', [
    ('light', 0), ('normal', 1), ('thorough', 2),
])
def test_patrol_uses_only_manifest_thoroughness(harness, level, expected):
    dispatcher = harness.dispatcher
    proposal = dispatcher.prepare(
        'patrol', 'request_patrol', {'thoroughness': level},
    )
    dispatcher.dispatch(proposal)
    assert harness.manager.submissions[0][:2] == (
        'patrol', {'thoroughness': expected},
    )


@pytest.mark.parametrize('tool, arguments', [
    ('navigate_to_pose', {}),
    ('request_follow_person', {'target_person_id': 'somebody'}),
    ('request_follow_person', {'desired_distance_m': 0.1}),
    ('request_patrol', {'thoroughness': 1}),
    ('request_patrol', {'thoroughness': 'normal', 'behavior_tree': '/tmp/bt'}),
    ('request_navigation', {'location': '거실', 'x': 1}),
    ('request_navigation', {'location': ''}),
    ('cancel_voice_mission', {'request_id': 'foreign'}),
])
def test_model_cannot_supply_other_capabilities_or_arguments(
    harness, tool, arguments,
):
    with pytest.raises(ValueError):
        harness.dispatcher.prepare('invalid', tool, arguments)
    assert harness.manager.submissions == []


def test_duplicate_ids_never_submit_again_even_after_submit_failure(harness):
    dispatcher, manager = harness.dispatcher, harness.manager
    proposal = dispatcher.prepare('duplicate', 'request_follow_person', {})
    manager.submit_error = RuntimeError('transport may have sent the Goal')
    response = dispatcher.dispatch(proposal)
    manager.submit_error = None
    assert dispatcher.dispatch(proposal) == response
    other_input = dispatcher.prepare(
        'duplicate', 'request_patrol', {'thoroughness': 'normal'},
    )
    assert dispatcher.dispatch(other_input) == response
    assert len(manager.submissions) == 1
    assert '확인할 수 없어요' in response


@pytest.mark.parametrize('state, kind, text', [
    ('UNAVAILABLE', 'unavailable', '요청을 보내지 못했어요'),
    ('REJECTED', 'rejected', '접수를 거절했어요'),
    ('UNKNOWN', 'unknown', '확인할 수 없어요'),
    ('ACCEPTED', 'accepted', '요청을 접수했어요'),
])
def test_immediate_response_uses_actual_manager_observation(
    harness, state, kind, text,
):
    harness.manager.initial_state = state
    harness.manager.initial_kind = kind
    proposal = harness.dispatcher.prepare(
        'follow', 'request_follow_person', {},
    )
    response = harness.dispatcher.dispatch(proposal)
    assert text in response
    assert '완료했' not in response


def test_cancel_includes_only_owned_accepted_and_unknown_missions(harness):
    dispatcher, manager = harness.dispatcher, harness.manager
    ids = []
    for index in range(3):
        dispatcher.dispatch(dispatcher.prepare(
            f'follow-{index}', 'request_follow_person', {},
        ))
        ids.append(manager.submissions[-1][2])
    for request_id, state in zip(ids, ['ACCEPTED', 'UNKNOWN', 'SUCCEEDED']):
        event = dict(manager.records[request_id], state=state)
        assert dispatcher.handle(event)
        manager.records[request_id] = event
    assert not dispatcher.handle({
        'request_id': 'web-owned', 'capability_id': 'follow_person',
        'state': 'RUNNING',
    })
    # A terminal event for an unrelated capability cannot release ownership.
    assert not dispatcher.handle({
        'request_id': ids[0], 'capability_id': 'patrol', 'state': 'SUCCEEDED',
    })
    proposal = dispatcher.prepare('stop', 'cancel_voice_mission', {})
    response = dispatcher.dispatch(proposal)
    assert manager.cancellations == ids[:2]
    assert '종료 여부를 확인할게요' in response
    dispatcher.dispatch(proposal)
    assert manager.cancellations == ids[:2]


def test_unknown_submit_can_still_be_canceled(harness):
    harness.manager.submit_error = RuntimeError('possibly sent')
    dispatcher = harness.dispatcher
    dispatcher.dispatch(dispatcher.prepare(
        'follow', 'request_follow_person', {},
    ))
    dispatcher.dispatch(dispatcher.prepare(
        'stop', 'cancel_voice_mission', {},
    ))
    assert harness.manager.cancellations == [
        harness.manager.submissions[0][2],
    ]


def test_cancellation_does_not_claim_no_motion_after_unknown_cancel(harness):
    dispatcher = harness.dispatcher
    dispatcher.dispatch(dispatcher.prepare(
        'follow', 'request_follow_person', {},
    ))

    def unavailable(request_id):
        raise RuntimeError('no Goal handle available')

    harness.manager.cancel = unavailable
    response = dispatcher.dispatch(dispatcher.prepare(
        'stop', 'cancel_voice_mission', {},
    ))
    assert '종료된 것으로 판단하지 않을게요' in response


def test_navigation_resolves_and_rechecks_server_binding(harness):
    dispatcher = harness.dispatcher
    proposal = dispatcher.prepare(
        'navigate', 'request_navigation', {'location': '거실'},
    )
    assert harness.manager.submissions == []
    dispatcher.dispatch(proposal)
    assert harness.targets.lookups == [('거실', '/maps/home.yaml')] * 2
    assert harness.manager.submissions[0][:2] == (
        'navigate_to_pose', harness.targets.arguments,
    )


@pytest.mark.parametrize('change', ['digest', 'arguments', 'unavailable'])
def test_navigation_config_change_blocks_dispatch(harness, change):
    dispatcher = harness.dispatcher
    proposal = dispatcher.prepare(
        'navigate', 'request_navigation', {'location': '거실'},
    )
    if change == 'digest':
        harness.targets.digest = 'different-map-image-or-config'
    elif change == 'arguments':
        harness.targets.arguments['pose']['test'] = 99
    else:
        harness.targets.error = OSError('configuration was removed')
    response = dispatcher.dispatch(proposal)
    assert '변경되어 이동하지 않았어요' in response
    assert harness.manager.submissions == []


def test_map_switch_away_and_back_invalidates_prepared_navigation(harness):
    dispatcher = harness.dispatcher
    proposal = dispatcher.prepare(
        'navigate', 'request_navigation', {'location': '거실'},
    )
    dispatcher.observe_localization({'mode': 'MAPPING', 'map': None})
    dispatcher.observe_localization({
        'mode': 'LOCALIZATION', 'map': '/maps/home.yaml',
    })
    assert '변경되어 이동하지 않았어요' in dispatcher.dispatch(proposal)
    assert harness.manager.submissions == []


@pytest.mark.parametrize('payload', [
    '{invalid', {'mode': 'MAPPING', 'map': None},
    {'mode': 'SWITCHING', 'map': '/maps/home.yaml'},
    {'mode': 'ERROR', 'map': '/maps/home.yaml'},
    {'mode': 'LOCALIZATION', 'map': 'relative.yaml'},
    {'mode': 'LOCALIZATION', 'map': None},
])
def test_unusable_map_state_blocks_named_navigation(harness, payload):
    dispatcher = harness.dispatcher
    dispatcher.observe_localization(payload)
    proposal = dispatcher.prepare(
        'navigate', 'request_navigation', {'location': '거실'},
    )
    assert proposal.blocked
    assert '저장 지도를 확인할 수 없어' in dispatcher.dispatch(proposal)
    assert harness.manager.submissions == []


def test_latched_localization_has_no_artificial_heartbeat_expiry(harness):
    dispatcher = harness.dispatcher
    dispatcher._clock = lambda: 1000000.0
    proposal = dispatcher.prepare(
        'navigate', 'request_navigation', {'location': '거실'},
    )
    dispatcher.observe_localization(
        '{"mode":"LOCALIZATION","map":"/maps/home.yaml"}',
    )
    dispatcher.dispatch(proposal)
    assert len(harness.manager.submissions) == 1


def test_missing_active_map_refuses_without_guessing():
    manager = FakeManager()
    dispatcher = SpeechMissions(manager)
    proposal = dispatcher.prepare(
        'navigate', 'request_navigation', {'location': '거실'},
    )
    assert proposal.blocked
    assert '사용 중인 저장 지도' in dispatcher.dispatch(proposal)
    assert manager.submissions == []


def test_other_dispatcher_cannot_consume_prepared_proposal(harness):
    proposal = harness.dispatcher.prepare(
        'follow', 'request_follow_person', {},
    )
    other = SpeechMissions(harness.manager)
    with pytest.raises(ValueError, match='another dispatcher'):
        other.dispatch(proposal)
    assert harness.manager.submissions == []


def test_guard_runs_after_navigation_io_and_blocks_expired_proposal(harness):
    dispatcher = harness.dispatcher
    proposal = dispatcher.prepare(
        'navigate', 'request_navigation', {'location': '거실'},
    )
    valid = [True]
    original_resolve = harness.targets.resolve

    def slow_resolve(location, active_map):
        target = original_resolve(location, active_map)
        valid[0] = False
        return target

    refusal = '요청이 만료되어 실행 요청을 보내지 않았어요.'
    harness.targets.resolve = slow_resolve
    response = dispatcher.dispatch(
        proposal, guard=lambda: None if valid[0] else refusal,
    )
    assert response == refusal
    assert harness.manager.submissions == []
    # Expiration consumes the original command even if a later guard allows it.
    assert dispatcher.dispatch(proposal, guard=lambda: None) == refusal
    assert harness.manager.submissions == []


@pytest.mark.parametrize('tool, arguments', [
    ('request_follow_person', {}),
    ('request_patrol', {'thoroughness': 'normal'}),
    ('cancel_voice_mission', {}),
])
def test_guard_exception_blocks_before_manager_calls(harness, tool, arguments):
    dispatcher = harness.dispatcher
    if tool == 'cancel_voice_mission':
        dispatcher.dispatch(dispatcher.prepare(
            'initial-follow', 'request_follow_person', {},
        ))
    previous_submissions = list(harness.manager.submissions)
    proposal = dispatcher.prepare('guarded', tool, arguments)

    def invalidated():
        raise RuntimeError('conversation or memory state changed')

    response = dispatcher.dispatch(proposal, guard=invalidated)
    assert response == '요청 조건을 확인할 수 없어 요청을 보내지 않았어요.'
    assert harness.manager.submissions == previous_submissions
    assert harness.manager.cancellations == []


def test_allowing_guard_keeps_submission_and_cancellation_working(harness):
    dispatcher = harness.dispatcher
    calls = []

    def valid():
        calls.append('checked')
        return None

    dispatcher.dispatch(dispatcher.prepare(
        'follow', 'request_follow_person', {},
    ), guard=valid)
    dispatcher.dispatch(dispatcher.prepare(
        'stop', 'cancel_voice_mission', {},
    ), guard=valid)
    assert calls == ['checked', 'checked']
    assert len(harness.manager.submissions) == 1
    assert len(harness.manager.cancellations) == 1
