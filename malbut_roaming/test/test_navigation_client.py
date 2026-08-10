"""State-machine tests for the Nav2 action adapter."""

from types import SimpleNamespace

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Time
from geometry_msgs.msg import PoseStamped
import pytest

import malbut_roaming.navigation_client as navigation_module
from malbut_roaming.navigation_client import (
    NavigationClient,
    NavigationOutcome,
    make_pose,
)


class FakeFuture:
    """Minimal controllable future used by the action-client tests."""

    def __init__(self):
        self._callbacks = []
        self._result = None
        self._error = None
        self._done = False

    def add_done_callback(self, callback):
        if self._done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def set_result(self, result):
        self._result = result
        self._done = True
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)

    def set_exception(self, error):
        self._error = error
        self.set_result(None)

    def result(self):
        if self._error is not None:
            raise self._error
        return self._result


class FakeGoalHandle:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.result_future = FakeFuture()
        self.cancel_count = 0

    def get_result_async(self):
        return self.result_future

    def cancel_goal_async(self):
        self.cancel_count += 1
        return FakeFuture()


class FakeActionClient:
    instances = {}

    def __init__(self, _node, action_type, action_name):
        self.action_type = action_type
        self.action_name = action_name
        self.ready = True
        self.sent = []
        self.destroyed = False
        self.instances[action_name] = self

    def server_is_ready(self):
        return self.ready

    def send_goal_async(self, goal, feedback_callback=None):
        future = FakeFuture()
        self.sent.append((goal, feedback_callback, future))
        return future

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def client(monkeypatch):
    FakeActionClient.instances = {}
    monkeypatch.setattr(navigation_module, 'ActionClient', FakeActionClient)
    starts = []
    results = []
    adapter = NavigationClient(
        object(),
        'compute_path_to_pose',
        'navigate_to_pose',
        starts.append,
        lambda request, outcome, detail: results.append(
            (request, outcome, detail)
        ),
    )
    return adapter, starts, results


def _wrapped_result(status, result=None):
    return SimpleNamespace(status=status, result=result)


def _complete_valid_plan(planner_future, pose_count=2):
    handle = FakeGoalHandle()
    planner_future.set_result(handle)
    path = SimpleNamespace(poses=[object()] * pose_count)
    handle.result_future.set_result(
        _wrapped_result(
            GoalStatus.STATUS_SUCCEEDED,
            SimpleNamespace(path=path),
        )
    )
    return handle


def test_make_pose_sets_frame_timestamp_position_and_heading():
    """Coordinate requests must produce a complete map-frame PoseStamped."""
    clock = SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=12, nanosec=34))
    )
    node = SimpleNamespace(get_clock=lambda: clock)
    pose = make_pose(node, 'map', 1.25, -2.5, 1.0)
    assert pose.header.frame_id == 'map'
    assert pose.header.stamp == Time(sec=12, nanosec=34)
    assert pose.pose.position.x == pytest.approx(1.25)
    assert pose.pose.position.y == pytest.approx(-2.5)
    norm = sum(
        value * value
        for value in (
            pose.pose.orientation.x,
            pose.pose.orientation.y,
            pose.pose.orientation.z,
            pose.pose.orientation.w,
        )
    )
    assert norm == pytest.approx(1.0)


def test_request_requires_both_nav2_action_servers(client):
    adapter, _starts, _results = client
    FakeActionClient.instances['navigate_to_pose'].ready = False
    assert adapter.request(PoseStamped(), 'roaming') is None
    assert not FakeActionClient.instances['compute_path_to_pose'].sent


def test_validated_path_runs_navigation_and_reports_success(client):
    """Planning must succeed with a non-empty path before motion is requested."""
    adapter, starts, results = client
    pose = PoseStamped()
    request = adapter.request(pose, 'roaming')
    planner = FakeActionClient.instances['compute_path_to_pose']
    navigator = FakeActionClient.instances['navigate_to_pose']
    planner_goal, _feedback, planner_future = planner.sent[0]
    assert planner_goal.goal is pose
    assert planner_goal.use_start is False

    _complete_valid_plan(planner_future)
    assert len(navigator.sent) == 1
    navigation_goal, feedback_callback, navigation_future = navigator.sent[0]
    assert navigation_goal.pose is pose
    assert feedback_callback is not None

    navigation_handle = FakeGoalHandle()
    navigation_future.set_result(navigation_handle)
    assert starts == [request]
    navigation_handle.result_future.set_result(
        _wrapped_result(GoalStatus.STATUS_SUCCEEDED)
    )
    assert results[0][0] is request
    assert results[0][1] == NavigationOutcome.SUCCEEDED
    assert adapter.active_request is None


@pytest.mark.parametrize('pose_count', [0, 1])
def test_empty_or_single_pose_plan_is_rejected_before_navigation(
    client,
    pose_count,
):
    adapter, _starts, results = client
    adapter.request(PoseStamped(), 'roaming')
    planner_future = FakeActionClient.instances[
        'compute_path_to_pose'
    ].sent[0][2]
    _complete_valid_plan(planner_future, pose_count=pose_count)
    assert not FakeActionClient.instances['navigate_to_pose'].sent
    assert results[0][1] == NavigationOutcome.FAILED
    assert 'empty path' in results[0][2]


def test_new_request_invalidates_a_late_old_planner_response(client):
    """Preemption must prevent a stale plan from moving the robot."""
    adapter, starts, results = client
    first = adapter.request(PoseStamped(), 'roaming')
    planner = FakeActionClient.instances['compute_path_to_pose']
    first_future = planner.sent[0][2]
    second = adapter.request(PoseStamped(), 'external')
    assert second.token > first.token

    stale_handle = FakeGoalHandle()
    first_future.set_result(stale_handle)
    assert stale_handle.cancel_count == 1
    assert adapter.active_request is second
    assert not starts
    assert not results


def test_cancel_stops_active_navigation_and_ignores_its_late_result(client):
    adapter, starts, results = client
    adapter.request(PoseStamped(), 'roaming')
    planner_future = FakeActionClient.instances[
        'compute_path_to_pose'
    ].sent[0][2]
    _complete_valid_plan(planner_future)
    navigation_future = FakeActionClient.instances[
        'navigate_to_pose'
    ].sent[0][2]
    handle = FakeGoalHandle()
    navigation_future.set_result(handle)
    assert starts

    adapter.cancel()
    assert handle.cancel_count == 1
    handle.result_future.set_result(
        _wrapped_result(GoalStatus.STATUS_CANCELED)
    )
    assert not results
    assert adapter.active_request is None


def test_destroy_releases_both_action_clients(client):
    adapter, _starts, _results = client
    adapter.destroy()
    assert all(
        action_client.destroyed
        for action_client in FakeActionClient.instances.values()
    )
