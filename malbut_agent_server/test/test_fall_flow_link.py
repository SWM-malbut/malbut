"""FallFlow through the Manager (SWM25-261): fake futures, no ROS or DDS graph.

Unit tests of the link's wiring, not robot checks."""

from concurrent.futures import Future
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_agent_server.fall_rules.fall_flow_link import (
    LOOK_TIMEOUT_S, FallFlowLink, parse_people, parse_sighting,
)


def state(*manual):
    return SimpleNamespace(
        active_foreground_missions=[SimpleNamespace(capability_id=c) for c in manual],
        active_background_missions=[], pending_missions=[], suspended_missions=[])


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setitem(sys.modules, 'action_msgs.msg', SimpleNamespace(
        GoalStatus=SimpleNamespace(STATUS_SUCCEEDED=4, STATUS_ABORTED=6)))
    now = SimpleNamespace(value=100.0)
    link = object.__new__(FallFlowLink)
    link.node, link.runtime_id = Mock(), 'vlm'
    link.goal_response_timeout_s, link.result_timeout_s = 5.0, 610.0
    link.server_loss_timeout_s = 5.0
    link.action_type = SimpleNamespace(Goal=lambda **kw: SimpleNamespace(**kw))
    link.message_type = SimpleNamespace
    link.client, link.decisions, link.status_publisher, link.timer = Mock(), Mock(), Mock(), Mock()
    link.client.server_is_ready.return_value = True
    goals = []

    def send(goal, **kwargs):
        future = Future()
        goals.append(SimpleNamespace(goal=goal, future=future,
                                     feedback=kwargs.get('feedback_callback')))
        return future
    link.client.send_goal_async.side_effect = send
    link.agent_presence = Mock()
    link.agent_presence.server_is_ready.return_value = True
    link.init_state(lambda: now.value)
    link.manager_state = state()
    yield SimpleNamespace(link=link, now=now, goals=goals)


def event(link, **data):
    link.on_event(SimpleNamespace(data=json.dumps(dict(data, runtime_id='vlm', boot_id='b'))))


def decisions(link, action=None):
    out = [json.loads(call.args[0].data) for call in link.decisions.publish.call_args_list]
    link.decisions.publish.reset_mock()
    return [d for d in out if action is None or d['action'] == action]


def sighting(kind='lying', **fields):
    data = dict(sighting_id='s1', at=99.0, kind=kind, track='pose:1:a', point=[3.0, 1.0],
                box=[0.3, 0.5, 0.7, 0.9], view=0, robot=[0.0, 0.0], on_floor=None,
                photo='p1', fall_moment=None)
    data.update(fields)
    return data


def goal_arguments(entry):
    return entry.goal.capability_id, json.loads(entry.goal.arguments_yaml)


def accept(rig, entry):
    handle = Mock(accepted=True)
    result = Future()
    handle.get_result_async.return_value = result
    entry.future.set_result(handle)
    return handle, result


def succeed(result, **fields):
    result.set_result(SimpleNamespace(status=4, result=SimpleNamespace(
        message='', result_yaml=json.dumps(fields))))


def answer(rig, what, verdict, **fields):
    look, = decisions(rig.link, 'look')
    assert look['what'] == what
    key = 'sighting_id' if what == 'filter' else 'incident_id'
    event(rig.link, kind='fall_look_result', look_id=look['look_id'], what=what,
          verdict=verdict, **{key: look[key]}, **fields)


def open_incident(rig):
    event(rig.link, kind='fall_sighting', sighting=sighting())
    answer(rig, 'filter', 'suspect')
    return rig.link.flow.status()['incident_id']


def test_a_sighting_is_filtered_then_driven_to_from_the_side_first_seen(rig):
    incident = open_incident(rig)
    records = [d['event'] for d in decisions(rig.link, 'record')]
    assert records == ['incident_opened', 'analysis_completed', 'approach_started']
    capability, arguments = goal_arguments(rig.goals[-1])
    assert capability == 'fall_approach' and arguments['phase'] == 'approach'
    assert arguments['request_id'] == incident and arguments['standoff_m'] == 1.0
    assert (arguments['x'], arguments['y'], arguments['from_x'], arguments['from_y']) == (
        3.0, 1.0, 0.0, 0.0)


def test_the_whole_check_runs_through_the_manager_until_the_question(rig):
    incident = open_incident(rig)
    decisions(rig.link)
    drive = rig.goals[-1]
    handle, _ = accept(rig, drive)
    drive.feedback(SimpleNamespace(feedback=SimpleNamespace(
        state='RUNNING', feedback_yaml=json.dumps(dict(outcome='arrived')))))
    assert rig.link.held[1] is handle  # The drive holds the wheels until the next step.
    answer(rig, 'arrival', 'same_person')
    capability, arguments = goal_arguments(rig.goals[-1])
    assert arguments['phase'] == 'view' and arguments['standoff_m'] == 3.0
    _, result = accept(rig, rig.goals[-1])
    succeed(result, outcome='arrived')
    answer(rig, 'posture', 'floor')
    _, result = accept(rig, rig.goals[-1])
    succeed(result, outcome='arrived')  # Back to 1 m before asking.
    capability, arguments = goal_arguments(rig.goals[-1])
    assert capability == 'fall_confirmation' and arguments['request_id'] != incident
    assert '낙상이 의심' in arguments['summary']
    decisions(rig.link)
    _, result = accept(rig, rig.goals[-1])
    succeed(result, situation_assessment='confirmed_incident', help_needed=True)
    out = decisions(rig.link)
    done = next(d for d in out if d.get('event') == 'confirmation_completed')
    assert (done['state'], done['answer'], done['reason']) == (
        'help_required', 'help_request', 'confirmed_incident')
    assert [d['reason'] for d in out if d['action'] == 'notify'] == ['confirmation_help_required']
    assert rig.link.step is None and rig.link.held is None
    handle.cancel_goal_async.assert_not_called()  # The next step replaced the hold.


def test_a_refused_step_waits_counts_and_goes_again(rig):
    open_incident(rig)
    decisions(rig.link)
    rig.link.on_state(state('manual_drive'))
    _, result = accept(rig, rig.goals[-1])
    result.set_result(SimpleNamespace(status=6, result=SimpleNamespace(
        message='superseded before execution by manual_drive', result_yaml='')))
    assert [d['reason'] for d in decisions(rig.link, 'record')] == ['manual_drive']
    status = rig.link.status()
    assert (status['phase'], status['waiting_for'], status['step']) == (
        'wait', 'manual_drive', 'wait')
    sent = len(rig.goals)
    rig.now.value += 2
    rig.link.tick()
    assert len(rig.goals) == sent  # 직접 움직이기 is still on: not asked every second.
    rig.link.on_state(state())
    assert len(rig.goals) == sent + 1  # It ended: the same step goes now.


def test_an_unanswered_look_is_failed_after_its_timeout(rig):
    event(rig.link, kind='fall_sighting', sighting=sighting())
    decisions(rig.link)
    rig.now.value += LOOK_TIMEOUT_S
    rig.link.tick()
    records = [d['event'] for d in decisions(rig.link, 'record')]
    assert 'incident_opened' in records  # A failed filter opens the incident (design 4).


def test_hello_until_the_vlm_is_ready_then_open_incidents_are_checked_again(rig):
    rig.link.tick()
    assert decisions(rig.link, 'flow_hello')
    rig.now.value += 1
    rig.link.tick()
    assert not decisions(rig.link, 'flow_hello')
    event(rig.link, kind='flow_ready', flow=True, open_incidents=[dict(
        incident_id='i-open', first=sighting(), check_round=1, fall_moments=[])], recent=[])
    started = [d for d in decisions(rig.link, 'record') if d['event'] == 'recheck_started']
    assert started[0]['reason'] == 'agent_restart' and started[0]['check_round'] == 2
    assert goal_arguments(rig.goals[-1])[1]['request_id'] == 'i-open'
    rig.now.value += 10
    rig.link.tick()
    assert not decisions(rig.link, 'flow_hello')


def test_another_runtime_or_a_switch_mismatch_is_ignored(rig):
    rig.link.on_event(SimpleNamespace(data=json.dumps(dict(
        kind='fall_sighting', sighting=sighting(), runtime_id='someone-else'))))
    assert not decisions(rig.link)
    event(rig.link, kind='flow_ready', flow=False)
    assert not rig.link.ready
    rig.link.node.get_logger().error.assert_called_with('fall_flow_switch_mismatch')


def test_stop_said_during_the_check_cancels_the_drive_without_a_question(rig):
    open_incident(rig)
    decisions(rig.link)
    handle, _ = accept(rig, rig.goals[-1])
    rig.link.on_voice(SimpleNamespace(data=json.dumps(dict(kind='stop'))))
    handle.cancel_goal_async.assert_called_once()
    out = decisions(rig.link)
    assert [d['event'] for d in out if d['action'] == 'record'] == ['check_stopped']
    assert [d['reason'] for d in out if d['action'] == 'notify'] == ['check_stopped_by_user']
    assert rig.link.step is None and not rig.link.status()['active']


def test_status_names_the_step_and_why_for_the_map(rig):
    open_incident(rig)
    status = rig.link.status()
    assert (status['active'], status['phase'], status['step'], status['purpose']) == (
        True, 'approach', 'approach', 'approach')
    assert status['target'] == dict(x=3.0, y=1.0) and status['check_round'] == 1


def test_a_malformed_sighting_is_refused():
    for bad in (dict(sighting(), kind='dance'), dict(sighting(), point=[1, 'x']),
                dict(sighting(), sighting_id=None), dict(sighting(), box=[0, 0, 2, 1])):
        with pytest.raises(ValueError):
            parse_sighting(bad)


def test_the_marks_of_the_check_video_go_to_the_vlm(rig):
    incident = open_incident(rig)
    assert not decisions(rig.link, 'clip')
    drive = rig.goals[-1]
    accept(rig, drive)
    drive.feedback(SimpleNamespace(feedback=SimpleNamespace(state='RUNNING', feedback_yaml='')))
    mark, = decisions(rig.link, 'clip')
    assert mark == dict(action='clip', incident_id=incident, mark='start', check_round=1)


def test_pose_frames_go_to_the_person_tracking_and_its_numbers_to_the_vlm(rig):
    """SWM25-267: who is who is decided here; the VLM is told, for the people boxes."""
    event(rig.link, kind='fall_people', at=99.0, view=0, stationary=True, robot=[0.0, 0.0],
          people=[dict(track='pose:1:a', box=[0.3, 0.5, 0.7, 0.9], point=[3.0, 1.0],
                       state='suspected')])
    numbers, = decisions(rig.link, 'people')
    assert numbers['tracks'] == {'pose:1:a': rig.link.flow.tracker.person_of('pose:1:a')}
    incident = open_incident(rig)  # Its sighting is pose:1:a at the same place.
    bound, = [d for d in decisions(rig.link, 'people') if 'incidents' in d]
    assert bound['incidents'] == {incident: numbers['tracks']['pose:1:a']}


def test_a_malformed_pose_frame_is_refused():
    ok = dict(at=1.0, view=0, people=[dict(track='a', box=None, point=None, state='clear')])
    assert parse_people(ok).people[0].state == 'clear'
    for bad in (dict(ok, at='x'), dict(ok, view=-1), dict(ok, people='a'),
                dict(ok, people=[dict(track='a', state='lying')]),
                dict(ok, people=[dict(track='a', box=[0, 0, 2, 1], state='clear')])):
        with pytest.raises(ValueError):
            parse_people(bad)
    with pytest.raises(ValueError):
        parse_sighting(dict(sighting_id='s1', at=1.0, kind='seen'))  # Frames carry this now.
