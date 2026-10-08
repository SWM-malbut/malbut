"""Uncertain suspicions are looked at from 1 m before anyone is asked."""

import math

import pytest

from malbut_fall_coordinator.fall_approach import (
    PULLBACK_STEP_M, Pose2D, StartPoses, facing_turn, goal_error, plan_targets, returning_turn,
)
from malbut_fall_coordinator.fall_confirmation import CHECK_WAIT_S, FallConfirmationCoordinator
from test_fall_confirmation import event

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


def test_pullback_targets_never_pass_the_robot():
    assert plan_targets((5.0, 0.0), (0.0, 0.0)) == [
        (0.0, 0.0), (PULLBACK_STEP_M, 0.0), (2 * PULLBACK_STEP_M, 0.0)]
    assert plan_targets((1.4, 0.0), (0.0, 0.0)) == [(0.0, 0.0)]


def test_turns_face_the_spot_and_restore_the_start_heading():
    robot = Pose2D(1.0, 1.0, 0.0)
    assert facing_turn(robot, (1.0, 3.0)) == pytest.approx(math.pi / 2)
    assert facing_turn(robot, (3.0, 1.05)) == 0.0  # Within tolerance.
    assert returning_turn(robot, Pose2D(0.0, 0.0, -math.pi / 2)) == pytest.approx(-math.pi / 2)


def test_goal_validation_and_start_poses_are_bounded():
    assert goal_error('approach', 1.0, 2.0, 1.0) is None
    assert goal_error('return', 0.0, 0.0, 1.0) is None
    for bad in (('drive', 1.0, 2.0, 1.0), ('approach', math.nan, 2.0, 1.0),
                ('approach', 1.0, 2.0, 0.1), ('approach', True, 2.0, 1.0)):
        assert goal_error(*bad)
    starts = StartPoses(limit=2)
    starts.remember('a', Pose2D(0, 0, 0))
    starts.remember('a', Pose2D(9, 9, 0))
    starts.remember('b', Pose2D(1, 1, 0))
    starts.remember('c', Pose2D(2, 2, 0))
    assert starts.get('a') is None and starts.get('b') == Pose2D(1, 1, 0)
    starts.forget('b')
    assert starts.get('b') is None


# ---------------------------------------------------------------- transport

from concurrent.futures import Future  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from threading import RLock  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import Mock  # noqa: E402

from malbut_fall_coordinator.fall_confirmation_link import FallConfirmationLink  # noqa: E402


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

    def send(goal):
        future = Future()
        fields = json.loads(goal.arguments_yaml) | {'capability': goal.capability_id}
        item.goals.append((fields, future))
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


def finish(link, outcome, *, accepted=True, status=4):
    goal, future = link.goals[-1]
    result = Future()
    handle = Mock(accepted=accepted)
    handle.get_result_async.return_value = result
    future.set_result(handle)
    if accepted:
        result.set_result(SimpleNamespace(status=status, result=SimpleNamespace(
            message='', result_yaml=json.dumps(dict(outcome=outcome)))))
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


@pytest.mark.parametrize('how', ['manager_rejects', 'no_path', 'aborted'])
def test_link_asks_in_place_when_the_robot_cannot_go(link, how):
    link.on_event(SimpleNamespace(data=scene()))
    if how == 'manager_rejects':
        finish(link, None, accepted=False)
    elif how == 'no_path':
        finish(link, 'no_path')
    else:
        finish(link, None, status=6)
    expected = {'manager_rejects': 'rejected', 'no_path': 'no_path', 'aborted': 'failed'}[how]
    assert sent(link)[0]['outcome'] == expected
    link.tick()
    assert link.goals[-1][0]['capability'] == 'fall_confirmation'


def test_link_waits_while_a_fall_mission_still_runs_in_the_manager(link):
    running = [SimpleNamespace(capability_id='fall_approach')]
    link.manager_state.active_foreground_missions = running
    link.on_event(SimpleNamespace(data=scene()))
    assert not link.goals


# ---------------------------------------------------------------- standoff route

from malbut_fall_coordinator.fall_approach import route_length, standoff_route  # noqa: E402


def test_the_route_stops_where_it_first_enters_the_standoff_circle():
    route = standoff_route([(0.0, 0.0), (2.0, 0.0), (4.0, 0.0)], (4.0, 0.0), 1.0)
    assert route == [(0.0, 0.0), (2.0, 0.0), pytest.approx((3.0, 0.0))]
    assert route_length(route) == pytest.approx(3.0)


def test_a_route_already_inside_or_never_inside_is_kept_as_is():
    assert standoff_route([(3.5, 0.0), (5.0, 0.0)], (4.0, 0.0), 1.0) == [(3.5, 0.0)]
    far = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    assert standoff_route(far, (2.0, 5.0), 1.0) == far
    assert standoff_route([], (0.0, 0.0), 1.0) == []
