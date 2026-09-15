"""Transport-free deadline and ownership checks for Nav2 path requests."""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path
import pytest
from rclpy.clock import ClockType

from malbut_tracking import navigation
from malbut_tracking.navigation import Nav2PathClient, PATH_TIMEOUT_PREFIX


class GoalHandle:
    """Keep cancellation acknowledgement distinct from actual completion."""

    def __init__(self, accepted=True):
        self.accepted = accepted
        self.result = Future()
        self.cancel_response = Future()
        self.cancel_goal_async = Mock(return_value=self.cancel_response)

    def get_result_async(self):
        return self.result

    def finish(self, status=GoalStatus.STATUS_SUCCEEDED):
        path = Path()
        path.header.frame_id = 'map'
        self.result.set_result(SimpleNamespace(
            status=status, result=SimpleNamespace(path=path),
        ))
        return path


class Client:
    """Deliver request responses in the order selected by the test."""

    def __init__(self):
        self.responses = []
        self.goals = []
        self.destroy = Mock()

    def server_is_ready(self):
        return True

    def send_goal_async(self, goal):
        self.goals.append(goal)
        response = Future()
        self.responses.append(response)
        return response

    def accept(self, accepted=True):
        handle = GoalHandle(accepted)
        self.responses[-1].set_result(handle)
        return handle


class Node:
    """Record ROS timer configuration without starting an executor."""

    def __init__(self):
        self.timers = []
        self.destroy_timer = Mock()

    def create_timer(self, period, callback, *, clock):
        timer = SimpleNamespace(period=period, callback=callback, clock=clock, cancel=Mock())
        self.timers.append(timer)
        return timer


@pytest.fixture
def planner(monkeypatch):
    """Use the real client state machine and a controllable monotonic clock."""
    now = [100.0]
    monkeypatch.setattr(navigation, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    client, node = Client(), Node()
    monkeypatch.setattr(navigation, 'ActionClient', lambda *args: client)
    result, idle = Mock(), Mock()
    adapter = Nav2PathClient(node, 'compute_path', on_idle=idle)
    return SimpleNamespace(
        adapter=adapter, client=client, node=node, now=now, result=result, idle=idle,
    )


def compute(planner, timeout=0.20):
    return planner.adapter.compute(
        PoseStamped(), 'GridBased', planner.result, timeout_seconds=timeout,
    )


def expire(planner):
    planner.now[0] += 0.21
    planner.node.timers[-1].callback()


def test_success_before_deadline_returns_path_and_destroys_timer(planner):
    assert compute(planner)
    assert planner.node.timers[0].clock.clock_type == ClockType.STEADY_TIME
    handle = planner.client.accept()
    path = handle.finish()
    planner.result.assert_called_once_with(path, 'Nav2 path planning succeeded')
    planner.idle.assert_not_called()
    assert not planner.adapter.busy
    planner.node.destroy_timer.assert_called_once_with(planner.node.timers[0])


def test_no_path_failure_is_distinct_from_timeout(planner):
    assert compute(planner)
    handle = planner.client.accept()
    handle.finish(GoalStatus.STATUS_ABORTED)
    assert planner.result.call_args.args[0] is None
    assert not planner.result.call_args.args[1].startswith(PATH_TIMEOUT_PREFIX)
    assert not planner.adapter.busy
    planner.idle.assert_not_called()


def test_timeout_notifies_once_but_owns_planner_until_terminal(planner):
    assert compute(planner)
    handle = planner.client.accept()
    expire(planner)
    assert planner.result.call_args.args[0] is None
    assert planner.result.call_args.args[1].startswith(PATH_TIMEOUT_PREFIX)
    handle.cancel_goal_async.assert_called_once()
    assert planner.adapter.busy
    assert not compute(planner)
    handle.cancel_response.set_result(object())
    assert planner.adapter.busy
    planner.idle.assert_not_called()
    handle.finish()
    planner.result.assert_called_once()
    planner.idle.assert_called_once()
    assert not planner.adapter.busy
    assert len(planner.client.goals) == 1


def test_timeout_before_acceptance_cancels_late_goal_without_overlap(planner):
    assert compute(planner)
    expire(planner)
    assert planner.adapter.busy
    assert not compute(planner)
    handle = planner.client.accept()
    handle.cancel_goal_async.assert_called_once()
    planner.idle.assert_not_called()
    handle.finish(GoalStatus.STATUS_CANCELED)
    planner.idle.assert_called_once()
    planner.result.assert_called_once()


def test_cancel_before_acceptance_retains_and_then_releases_ownership(planner):
    assert compute(planner)
    planner.adapter.cancel()
    assert planner.adapter.busy
    assert not compute(planner)
    handle = planner.client.accept()
    handle.cancel_goal_async.assert_called_once()
    handle.finish(GoalStatus.STATUS_CANCELED)
    planner.result.assert_not_called()
    planner.idle.assert_called_once()
    assert not planner.adapter.busy


def test_cancel_active_request_suppresses_result_and_cancels_once(planner):
    assert compute(planner)
    handle = planner.client.accept()
    planner.adapter.cancel()
    planner.adapter.cancel()
    handle.cancel_goal_async.assert_called_once()
    handle.finish()
    planner.result.assert_not_called()
    planner.idle.assert_called_once()


def test_overdue_result_cannot_beat_a_delayed_timer_callback(planner):
    assert compute(planner)
    handle = planner.client.accept()
    planner.now[0] += 0.21
    handle.finish()
    planner.result.assert_called_once()
    assert planner.result.call_args.args[0] is None
    assert planner.result.call_args.args[1].startswith(PATH_TIMEOUT_PREFIX)
    planner.idle.assert_called_once()


def test_request_transport_failure_does_not_claim_planner_is_idle(planner):
    assert compute(planner)
    planner.client.responses[0].set_exception(RuntimeError('reply lost'))
    planner.result.assert_called_once()
    assert 'unconfirmed' in planner.result.call_args.args[1]
    assert planner.adapter.busy
    assert not compute(planner)
    planner.idle.assert_not_called()


def test_result_transport_failure_does_not_claim_planner_is_idle(planner):
    assert compute(planner)
    handle = planner.client.accept()
    handle.result.set_exception(RuntimeError('result lost'))
    handle.cancel_goal_async.assert_called_once()
    assert planner.adapter.busy
    assert not compute(planner)
    planner.idle.assert_not_called()


def test_unknown_goal_status_does_not_release_planning_ownership(planner):
    assert compute(planner)
    handle = planner.client.accept()
    handle.finish(GoalStatus.STATUS_UNKNOWN)
    assert planner.adapter.busy
    assert 'unconfirmed' in planner.result.call_args.args[1]
    planner.idle.assert_not_called()


def test_send_transport_exception_is_reported_without_an_overlapping_retry(planner):
    planner.client.send_goal_async = Mock(side_effect=RuntimeError('send failed'))
    assert compute(planner)
    planner.result.assert_called_once()
    assert planner.adapter.busy
    assert not compute(planner)
    planner.idle.assert_not_called()


def test_late_rejection_after_timeout_releases_for_one_idle_callback(planner):
    assert compute(planner)
    expire(planner)
    planner.client.accept(accepted=False)
    planner.result.assert_called_once()
    planner.idle.assert_called_once()
    assert not planner.adapter.busy


def test_idle_callback_can_start_latest_request_after_old_one_really_ends(planner):
    assert compute(planner)
    handle = planner.client.accept()
    old_timer = planner.node.timers[0]
    expire(planner)
    planner.idle.side_effect = lambda: compute(planner)
    handle.finish()
    assert len(planner.client.goals) == 2
    assert planner.adapter.busy
    old_timer.callback()
    assert planner.adapter.busy
    assert planner.result.call_count == 1
    planner.idle.assert_called_once()


def test_legacy_request_without_deadline_does_not_create_timer(planner):
    assert compute(planner, None)
    assert not planner.node.timers
    handle = planner.client.accept()
    handle.finish()
    planner.result.assert_called_once()


@pytest.mark.parametrize('timeout', [0.0, -1.0, float('nan'), float('inf')])
def test_invalid_deadline_is_rejected_without_sending(planner, timeout):
    with pytest.raises(ValueError):
        compute(planner, timeout)
    assert not planner.adapter.busy
    assert not planner.client.goals


def test_destroy_suppresses_result_and_idle_callback_for_late_terminal(planner):
    assert compute(planner)
    handle = planner.client.accept()
    planner.adapter.destroy()
    handle.cancel_goal_async.assert_called_once()
    handle.finish()
    planner.result.assert_not_called()
    planner.idle.assert_not_called()
