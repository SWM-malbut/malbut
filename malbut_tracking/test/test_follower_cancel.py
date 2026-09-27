"""Verify outer cancellation retains ownership until Nav2 motion terminates."""

from concurrent.futures import Future
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from action_msgs.msg import GoalStatus
from nav_msgs.msg import Path
import pytest
from rclpy.action import CancelResponse, GoalResponse
from rclpy.task import Future as RosFuture

from malbut_tracking import navigation
from malbut_tracking.navigation import Nav2MotionClient
from malbut_tracking.person_follower_node import PersonFollowerNode


class GoalHandle:
    """Separate acceptance, cancel acknowledgement, and motion completion."""

    def __init__(self, accepted=True):
        self.accepted = accepted
        self.result = Future()
        self.cancel_response = Future()
        self.cancel_goal_async = Mock(return_value=self.cancel_response)

    def get_result_async(self):
        """Return the still-pending terminal result."""
        return self.result

    def finish(self, status=GoalStatus.STATUS_CANCELED):
        """Confirm that this particular motion has ended."""
        self.result.set_result(SimpleNamespace(status=status))


class Client:
    """Allow tests to choose acceptance and terminal ordering."""

    def __init__(self):
        self.responses = []

    def server_is_ready(self):
        """Keep transport availability independent of ownership."""
        return True

    def send_goal_async(self, goal, feedback_callback=None):
        """Leave each motion request waiting for acceptance."""
        response = Future()
        self.responses.append(response)
        return response

    def accept(self, accepted=True):
        """Deliver an acceptance response for the latest request."""
        handle = GoalHandle(accepted)
        self.responses[-1].set_result(handle)
        return handle


@pytest.fixture
def follow(monkeypatch):
    """Connect real cancellation methods to the real motion ownership adapter."""
    path, spin = Client(), Client()
    monkeypatch.setattr(
        navigation, 'ActionClient',
        lambda node, action, name: path if name == 'path' else spin,
    )
    goal = Mock(status=GoalStatus.STATUS_EXECUTING)
    goal.canceled.side_effect = lambda: setattr(goal, 'status', GoalStatus.STATUS_CANCELED)
    result = RosFuture()
    node = SimpleNamespace(
        _active_goal=goal,
        _result_future=result,
        _cancel_requested_goal=None,
        _cancel_guard=Mock(),
        _path_planner=Mock(busy=True),
        _reset_speed_limit=Mock(),
        _set_state=Mock(),
        _obstacle_tracker=Mock(),
        _camera_estimator=Mock(),
        _loss_timer=Mock(),
        _pending_detection_timer=Mock(),
        _cancel_tracking_retry=Mock(),
        _reset_goal_pullback=Mock(),
        _reset_recovery=Mock(),
        _settings_for_goal=Mock(),
        _validate_target_request=Mock(),
        get_logger=Mock(return_value=Mock()),
        _canceled_follow_result=PersonFollowerNode._canceled_follow_result,
    )
    for name in (
        '_cancel_callback', '_on_cancel_guard', '_cancel_follow_action',
        '_complete_follow_cancel', '_execute_callback', '_goal_callback',
    ):
        setattr(node, name, MethodType(getattr(PersonFollowerNode, name), node))
    node._nav2 = Nav2MotionClient(
        None, 'path', 'spin', Mock(), on_idle=node._cancel_guard.trigger,
    )
    return SimpleNamespace(node=node, goal=goal, result=result, path=path, spin=spin)


def request_motion(follow, mode):
    """Request one motion without acknowledging it yet."""
    if mode == 'path':
        assert follow.node._nav2.follow_path(Path(), 'controller', 'checker')
    else:
        assert follow.node._nav2.spin(1.0, 3.0)
    return getattr(follow, mode)


def cancel_follow(follow):
    """Accept cancel, then run the guard after ROS enters CANCELING."""
    assert follow.node._cancel_callback(follow.goal) == CancelResponse.ACCEPT
    follow.goal.status = GoalStatus.STATUS_CANCELING
    follow.node._on_cancel_guard()
    follow.node._cancel_guard.trigger.reset_mock()


@pytest.mark.parametrize('mode', ['path', 'spin'])
@pytest.mark.parametrize('pending_acceptance', [False, True])
@pytest.mark.parametrize('status', [
    GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_ABORTED,
])
def test_cancel_waits_for_motion_terminal(follow, mode, pending_acceptance, status):
    """Neither cancel acceptance nor its acknowledgement may release BASE."""
    client = request_motion(follow, mode)
    handle = None if pending_acceptance else client.accept()
    cancel_follow(follow)
    assert follow.node._active_goal is None
    assert follow.node._cancel_requested_goal is follow.goal
    assert not follow.result.done()
    assert follow.node._goal_callback(object()) == GoalResponse.REJECT
    PersonFollowerNode._on_detections(follow.node, object())
    if handle is None:
        handle = client.accept()
    handle.cancel_goal_async.assert_called_once()
    handle.cancel_response.set_result(object())
    follow.node._on_cancel_guard()
    assert not follow.result.done()
    follow.goal.canceled.assert_not_called()
    handle.finish(status)
    follow.node._cancel_guard.trigger.assert_called_once()
    follow.node._on_cancel_guard()
    assert follow.result.done()
    follow.goal.canceled.assert_called_once()
    assert follow.node._goal_callback(object()) == GoalResponse.ACCEPT


def test_cancel_waits_for_all_inflight_path_requests(follow):
    """A pending replacement still owns motion after the first path ends."""
    request_motion(follow, 'path')
    first = follow.path.accept()
    request_motion(follow, 'path')
    cancel_follow(follow)
    first.finish()
    follow.node._cancel_guard.trigger.assert_not_called()
    assert not follow.result.done()
    second = follow.path.accept()
    second.cancel_goal_async.assert_called_once()
    second.finish()
    follow.node._cancel_guard.trigger.assert_called_once()
    follow.node._on_cancel_guard()
    assert follow.result.done()


def test_rejected_pending_motion_allows_cancel_to_finish(follow):
    """A rejected goal cannot keep running and needs no terminal result."""
    request_motion(follow, 'spin')
    cancel_follow(follow)
    follow.spin.accept(accepted=False)
    follow.node._on_cancel_guard()
    assert follow.result.done()


@pytest.mark.parametrize('failure_stage', ['acceptance', 'result'])
def test_unconfirmed_motion_keeps_outer_cancel_pending(follow, failure_stage):
    """Transport uncertainty must never be presented as a completed stop."""
    request_motion(follow, 'spin')
    future = (
        follow.spin.responses[0]
        if failure_stage == 'acceptance' else follow.spin.accept().result
    )
    cancel_follow(follow)
    future.set_exception(RuntimeError('response lost'))
    follow.node._on_cancel_guard()
    assert follow.node._nav2.busy
    assert not follow.result.done()
    follow.goal.canceled.assert_not_called()
    follow.node._cancel_guard.trigger.assert_not_called()
    assert follow.node._goal_callback(object()) == GoalResponse.REJECT


def test_cancel_without_motion_does_not_wait_for_read_only_planner(follow):
    """Invalidating planner callbacks suffices when no motion is owned."""
    cancel_follow(follow)
    follow.node._path_planner.cancel.assert_called_once()
    assert follow.result.done()
    assert follow.result.result().message == 'follow action canceled'
    follow.goal.canceled.assert_called_once()


def test_execute_after_cancel_started_waits_for_same_result(follow):
    """Execution may start after the cancel guard, before motion terminates."""
    request_motion(follow, 'spin')
    handle = follow.spin.accept()
    cancel_follow(follow)
    execution = follow.node._execute_callback(follow.goal)
    assert execution.send(None) is follow.result
    handle.finish()
    follow.node._on_cancel_guard()
    with pytest.raises(StopIteration) as stopped:
        execution.send(None)
    assert stopped.value.value is follow.result.result()
    follow.goal.abort.assert_not_called()


def test_execute_after_immediate_cancel_returns_canceled_result(follow):
    """An already-canceled goal must not be aborted by its late execution."""
    cancel_follow(follow)
    execution = follow.node._execute_callback(follow.goal)
    with pytest.raises(StopIteration) as stopped:
        execution.send(None)
    assert stopped.value.value.message == 'follow action canceled'
    follow.goal.abort.assert_not_called()


def test_unrelated_cancel_cannot_replace_pending_cancel_handle(follow):
    """Only the owned follow action may alter the retained cancellation."""
    request_motion(follow, 'spin')
    cancel_follow(follow)
    assert follow.node._cancel_callback(object()) == CancelResponse.REJECT
    assert follow.node._cancel_requested_goal is follow.goal
