"""Uncertain suspicions are looked at from 1 m before anyone is asked."""

import math

import pytest

from malbut_agent_server.fall_rules.fall_confirmation import (
    CHECK_WAIT_S, FallConfirmationCoordinator,
)
from test_fall_rules_confirmation import event

TARGET = dict(x=2.5, y=1.0, frame='map')


def scene(**changes):
    return event(confirmation_scope='scene', subject_key=None, approach_target=TARGET, **changes)


def approaching():
    coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    assert coordinator.receive(scene())
    kind, request = coordinator.next_work(0.0)
    assert kind == 'approach' and request.approach_target == (2.5, 1.0)
    return coordinator, request


def looked(reason):
    return event(kind='person_check_completed', confirmation_scope='scene',
                 subject_key=None, reason=reason)


def checked(coordinator, reason):
    return coordinator.receive(looked(reason))


def test_without_a_target_the_question_goes_out_as_before():
    coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    assert coordinator.receive(event())
    kind, request = coordinator.next_work(0.0)
    assert kind == 'confirm' and request.approach_target is None


@pytest.mark.parametrize('target', [
    dict(x=1, y=2), dict(x=1, y=2, frame='odom'), dict(x='1', y=2, frame='map'),
    dict(x=math.inf, y=2, frame='map'), dict(x=True, y=2, frame='map'), [1, 2],
])
def test_malformed_targets_are_refused(target):
    coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    assert not coordinator.receive(event(approach_target=target))
    assert not coordinator.requests


def test_arrival_waits_for_the_look_and_a_person_is_then_asked():
    coordinator, request = approaching()
    assert coordinator.approach_done(request, 'arrived', 10.0)
    (decision,) = coordinator.drain_commands()
    assert decision == dict(action='approach_result', boot_id='boot-1', incident_id='incident',
                            question_id='question', evidence_revision=1, outcome='arrived')
    assert coordinator.next_work(10.0 + CHECK_WAIT_S - 1) is None
    assert checked(coordinator, 'person')
    assert coordinator.next_work(12.0) == ('confirm', request)


def test_no_person_closes_without_a_question_and_drives_back():
    coordinator, request = approaching()
    coordinator.approach_done(request, 'arrived', 10.0)
    coordinator.drain_commands()
    assert checked(coordinator, 'not_a_person')
    assert not coordinator.requests
    kind, job = coordinator.next_work(11.0)
    assert kind == 'return' and job.question_id == 'question'
    # The runtime then closes the case; the return trip is not cancelled by it.
    assert coordinator.receive(event(kind='incident_resolved', confirmation_scope='scene',
                                     subject_key=None, reason='not_a_person'))
    assert coordinator.next_work(11.5) == ('return', job)
    assert coordinator.return_done(job, 'returned')
    (decision,) = coordinator.drain_commands()
    assert decision['action'] == 'return_result' and decision['outcome'] == 'returned'
    assert coordinator.next_work(12.0) is None
    # A replayed question for the closed case does not start another approach.
    coordinator.receive(scene())
    assert coordinator.next_work(12.5) is None


@pytest.mark.parametrize('outcome', ['no_map', 'no_path', 'timeout', 'failed', 'rejected', 'odd'])
def test_not_getting_there_asks_from_where_the_robot_is(outcome):
    coordinator, request = approaching()
    assert coordinator.approach_done(request, outcome, 5.0)
    (decision,) = coordinator.drain_commands()
    assert decision['outcome'] == (outcome if outcome != 'odd' else 'failed')
    assert coordinator.next_work(5.0) == ('confirm', request)


def test_no_answer_from_the_look_within_15_s_asks_anyway():
    coordinator, request = approaching()
    coordinator.approach_done(request, 'arrived', 0.0)
    assert coordinator.next_work(CHECK_WAIT_S) == ('confirm', request)
    # A late look result no longer changes the stage.
    assert checked(coordinator, 'not_a_person')
    assert coordinator.requests and not coordinator.returns


def test_look_results_for_other_questions_or_stages_are_ignored():
    coordinator, request = approaching()
    assert checked(coordinator, 'not_a_person')  # Still driving: not a check stage.
    assert coordinator.requests and not coordinator.returns
    assert not coordinator.receive(event(kind='person_check_completed', confirmation_scope='scene',
                                         subject_key=None, reason='maybe'))


# ---------------------------------------------------------------- transport

from concurrent.futures import Future  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from threading import RLock  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import Mock  # noqa: E402

from malbut_agent_server.fall_rules.fall_confirmation_link import (  # noqa: E402
    FallConfirmationLink,
)


@pytest.fixture
def link(monkeypatch):
    monkeypatch.setitem(sys.modules, 'action_msgs.msg', SimpleNamespace(
        GoalStatus=SimpleNamespace(STATUS_SUCCEEDED=4, STATUS_ABORTED=6)))
    now = SimpleNamespace(value=0.0)
    item = object.__new__(FallConfirmationLink)
    item.node, item.lock, item.clock = Mock(), RLock(), lambda: now.value
    item.goal_response_timeout_s, item.result_timeout_s = 5.0, 610.0
    item.server_loss_timeout_s = 5.0
    item.coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    item.action_type = SimpleNamespace(Goal=SimpleNamespace)
    item.message_type = SimpleNamespace
    item.client, item.decisions, item.timer, item.agent_presence = Mock(), Mock(), Mock(), Mock()
    item.client.server_is_ready.return_value = True
    item.agent_presence.server_is_ready.return_value = True
    item.goals = []

    def send(goal, feedback_callback=None):
        future = Future()
        fields = json.loads(goal.arguments_yaml) | {'capability': goal.capability_id}
        item.goals.append((fields, future))
        item.feedback = feedback_callback
        return future
    item.client.send_goal_async.side_effect = send
    item.manager_state = SimpleNamespace(
        active_foreground_missions=[], active_background_missions=[],
        pending_missions=[], suspended_missions=[])
    item.request = item.goal_future = item.handle = None
    item.sent_at = item.next_attempt = 0.0
    item.accepted_at = item.server_missing_since = None
    item.now = now
    yield item
    item.close()


def finish(link, outcome, *, accepted=True, status=4, message=''):
    goal, future = link.goals[-1]
    result = Future()
    handle = Mock(accepted=accepted)
    handle.get_result_async.return_value = result
    future.set_result(handle)
    if accepted:
        result.set_result(SimpleNamespace(status=status, result=SimpleNamespace(
            message=message, result_yaml=json.dumps(dict(outcome=outcome)) if status == 4 else '')))
    return goal


def sent(link):
    return [json.loads(c.args[0].data) for c in link.decisions.publish.call_args_list]


def test_link_drives_there_looks_and_drives_back_without_asking(link):
    link.on_event(SimpleNamespace(data=scene()))
    goal = finish(link, 'arrived')
    assert goal == dict(capability='fall_approach', request_id='question', phase='approach',
                        x=2.5, y=1.0, standoff_m=1.0)
    assert [d['action'] for d in sent(link)] == ['approach_result']
    assert len(link.goals) == 1  # Waits for the runtime's look, no question yet.
    link.on_event(SimpleNamespace(data=looked('not_a_person')))
    goal = finish(link, 'returned')
    assert goal['capability'] == 'fall_approach' and goal['phase'] == 'return'
    assert [d['action'] for d in sent(link)] == ['approach_result', 'return_result']
    assert sent(link)[-1]['outcome'] == 'returned'
    assert all(g['capability'] != 'fall_confirmation' for g, _ in link.goals)


def test_link_asks_after_a_person_is_seen(link):
    link.on_event(SimpleNamespace(data=scene()))
    finish(link, 'arrived')
    link.on_event(SimpleNamespace(data=looked('person')))
    assert link.goals[-1][0]['capability'] == 'fall_confirmation'


@pytest.mark.parametrize('how', ['no_map', 'no_path', 'aborted'])
def test_link_asks_in_place_when_the_robot_cannot_go(link, how):
    link.on_event(SimpleNamespace(data=scene()))
    if how == 'no_map':
        finish(link, None, status=6, message='select a saved map first; only mapping is available')
    elif how == 'no_path':
        finish(link, 'no_path')
    else:
        finish(link, None, status=6)
    expected = {'no_map': 'no_map', 'no_path': 'no_path', 'aborted': 'failed'}[how]
    assert sent(link)[0]['outcome'] == expected
    link.tick()
    assert link.goals[-1][0]['capability'] == 'fall_confirmation'


def test_link_waits_when_the_manager_does_not_take_the_drive(link):
    """Not accepted (starting or stopping): the same drive goes again (2026-10-12)."""
    link.on_event(SimpleNamespace(data=scene()))
    finish(link, None, accepted=False)
    assert sent(link) == [] and link.coordinator.waiting_for == 'busy'
    link.now.value = 1.0
    link.tick()
    assert link.goals[-1][0] == link.goals[0][0]
    assert len(link.goals) == 2


def test_link_waits_while_a_fall_mission_still_runs_in_the_manager(link):
    running = [SimpleNamespace(capability_id='fall_approach')]
    link.manager_state.active_foreground_missions = running
    link.on_event(SimpleNamespace(data=scene()))
    assert not link.goals


# ------------------------------------------------- a HIGH mission the whole check

GOAL_ID = bytes(range(16))


def hold(link, outcome):
    """The drive reports its outcome as feedback and keeps holding (2026-10-13)."""
    _, future = link.goals[-1]
    result = Future()
    handle = Mock(accepted=True)
    handle.goal_id = SimpleNamespace(uuid=GOAL_ID)
    handle.get_result_async.return_value = result
    future.set_result(handle)
    link.feedback(SimpleNamespace(feedback=SimpleNamespace(
        state='RUNNING', feedback_yaml=json.dumps(dict(phase='HOLDING', outcome=outcome)))))
    return handle, result


def holding_in_manager(link):
    # The Manager lists the holding drive: ours, not another fall mission.
    link.manager_state.active_foreground_missions = [
        SimpleNamespace(capability_id='fall_approach', mission_id=GOAL_ID.hex())]


def test_the_drive_holds_through_the_look_and_the_question_takes_over(link):
    """2026-10-13: no gap for an app move between the drive, the look and the question."""
    link.on_event(SimpleNamespace(data=scene()))
    held, result = hold(link, 'arrived')
    assert [d['action'] for d in sent(link)] == ['approach_result']
    assert link.held[1] is held and link.request is None
    holding_in_manager(link)
    link.tick()
    assert len(link.goals) == 1  # Looking: the drive keeps the wheels, nothing new.
    link.on_event(SimpleNamespace(data=looked('person')))
    assert link.goals[-1][0]['capability'] == 'fall_confirmation'
    held.cancel_goal_async.assert_not_called()  # Replaced in the Manager, not let go.
    assert link.held is None and link.mode == 'confirm'
    # The replaced drive ending late is not the question's result.
    result.set_result(SimpleNamespace(status=6, result=SimpleNamespace(
        message='mission preempted by a replacement request', result_yaml='')))
    assert link.request is not None and link.mode == 'confirm'
    assert [d['action'] for d in sent(link)] == ['approach_result']


def test_the_way_back_takes_over_the_hold(link):
    link.on_event(SimpleNamespace(data=scene()))
    held, _ = hold(link, 'arrived')
    holding_in_manager(link)
    link.on_event(SimpleNamespace(data=looked('not_a_person')))
    assert link.goals[-1][0]['phase'] == 'return'
    held.cancel_goal_async.assert_not_called()
    assert link.held is None


def test_the_hold_is_let_go_when_the_check_ends_with_nothing_next(link):
    link.on_event(SimpleNamespace(data=scene()))
    held, _ = hold(link, 'arrived')
    link.on_voice(SimpleNamespace(data=json.dumps({'kind': 'stop'})))
    assert sent(link)[-1]['action'] == 'check_stopped'
    held.cancel_goal_async.assert_called_once()
    assert link.held is None and len(link.goals) == 1  # Stopped: no question.


def test_a_hold_ended_by_manual_driving_is_forgotten(link):
    link.on_event(SimpleNamespace(data=scene()))
    held, result = hold(link, 'arrived')
    result.set_result(SimpleNamespace(status=6, result=SimpleNamespace(
        message='mission preempted by a replacement request', result_yaml='')))
    assert link.held is None
    link.manager_state.active_foreground_missions = [SimpleNamespace(capability_id='manual_drive')]
    link.on_event(SimpleNamespace(data=looked('person')))
    assert len(link.goals) == 1 and link.coordinator.waiting_for == 'manual_drive'
    link.manager_state.active_foreground_missions = []
    link.on_state(link.manager_state)  # 직접 움직이기 ended: at once, not a second later.
    assert link.goals[-1][0]['capability'] == 'fall_confirmation'
