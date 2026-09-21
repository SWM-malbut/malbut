"""Check short fallback paths without touching a real motion server."""

import math
from types import SimpleNamespace
from unittest.mock import Mock

from nav_msgs.msg import Path
import pytest

from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.follow_policy import decide_follow_motion, FollowSettings
from malbut_tracking.geometry import Point2D
from malbut_tracking.person_follower_node import FollowState, PersonFollowerNode


def _fixture(wall_x=None, map_time=20.0):
    costs = [0] * 800
    if wall_x is not None:
        for row in range(20):
            costs[row * 40 + wall_x] = 254
    grid = CostmapGrid('map', map_time, 0.1, 40, 20, Point2D(0.0, 0.0), 0.0, costs)
    settings = FollowSettings(1.0, 0.2, 0.1, 0.75)
    parameters = {
        'sensor_transform_queue_timeout_s': 0.3,
        'goal_safe_search_radius_m': 1.0,
        'goal_maximum_cost': 80,
        'goal_pullback_step_m': 0.5,
        'planner_id': 'GridBased',
        'nav2_planning_timeout_s': 0.2,
        'tracking_controller_id': 'FollowPath',
        'retreat_controller_id': 'FollowPathReverse',
        'goal_checker_id': 'general_goal_checker',
    }
    from builtin_interfaces.msg import Time
    follower = SimpleNamespace(
        _latest_global_costmap=grid, _now_seconds=lambda: 20.0,
        _settings=settings, _global_frame='map', _tracking_source='camera',
        _last_motion_source_stamp_ns=20_000_000_000,
        _line_fallback_pending=True,
        _goal_pullback_m=0.0, _goal_pullback_anchor=None,
        _path_planner=Mock(busy=True), _nav2=Mock(),
        _dispatch_tracking_path=Mock(return_value=True),
        _schedule_tracking_navigation_retry=Mock(), _warn_periodically=Mock(),
        _schedule_recovery_navigation_retry=Mock(), get_logger=Mock(),
        get_parameter=lambda key: SimpleNamespace(value=parameters[key]),
        get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=20)),
        ),
    )
    return follower


@pytest.mark.parametrize('wall_x', [None, 7])
def test_fallback_does_not_wait_for_global_planner_or_cross_blocked_cells(wall_x):
    """A checked short segment can be followed while a read-only plan drains."""
    follower = _fixture(wall_x)
    robot, target = Point2D(0.25, 0.55), Point2D(3.25, 0.55)
    decision = decide_follow_motion(robot, target, follower._settings)
    PersonFollowerNode._request_line_fallback(follower, robot, target, decision)
    follower._dispatch_tracking_path.assert_called_once()
    path, endpoint, travel = follower._dispatch_tracking_path.call_args.args[:3]
    assert 0.1 < travel <= 1.0
    assert endpoint.x < 0.7 if wall_x is not None else endpoint.x == 1.25
    for pose in path.poses:
        point = Point2D(pose.pose.position.x, pose.pose.position.y)
        assert follower._latest_global_costmap.cost(
            *follower._latest_global_costmap.world_to_cell(point),
        ) <= 80
    assert len(path.poses) >= 2
    assert follower._line_fallback_pending  # Refresh while the old plan drains.
    follower._path_planner.compute.assert_not_called()
    follower._nav2.cancel.assert_not_called()
    assert follower._dispatch_tracking_path.call_args.args[4] == 'camera:line_fallback'
    start_ns, end_ns = follower._dispatch_tracking_path.call_args.args[6:8]
    assert start_ns == end_ns  # Do not misreport this as Nav2 planning time.


@pytest.mark.parametrize('wall_x,map_time', [(2, 20.0), (None, 17.0)])
@pytest.mark.parametrize('planner_busy', [False, True])
def test_unsafe_or_stale_fallback_cancels_previous_motion(wall_x, map_time, planner_busy):
    """No admissible segment means holding, not keeping an obsolete path alive."""
    follower = _fixture(wall_x, map_time)
    follower._path_planner.busy = planner_busy
    robot, target = Point2D(0.25, 0.55), Point2D(3.25, 0.55)
    decision = decide_follow_motion(robot, target, follower._settings)
    PersonFollowerNode._request_line_fallback(follower, robot, target, decision)
    follower._dispatch_tracking_path.assert_not_called()
    follower._nav2.cancel.assert_called_once()
    assert follower._line_fallback_pending == planner_busy
    if planner_busy:
        # A newly clear/fresh grid can be retried before the old plan finishes.
        follower._latest_global_costmap = _fixture()._latest_global_costmap
        PersonFollowerNode._request_line_fallback(follower, robot, target, decision)
        follower._dispatch_tracking_path.assert_called_once()


def test_fallback_preserves_requested_person_standoff():
    """The quick path does not advance into the requested one-meter distance."""
    follower = _fixture()
    robot, target = Point2D(0.25, 0.55), Point2D(1.7, 0.55)
    decision = decide_follow_motion(robot, target, follower._settings)
    PersonFollowerNode._request_line_fallback(follower, robot, target, decision)
    endpoint = follower._dispatch_tracking_path.call_args.args[1]
    assert endpoint.x == pytest.approx(0.7)


@pytest.mark.parametrize('path', [None, Path()])
def test_failed_or_empty_retreat_plan_uses_retry_backoff(path):
    """A failed retreat result cannot immediately issue another identical plan."""
    follower = _fixture()
    follower._active_goal = object()
    follower._state = FollowState.TRACKING
    follower._observation_is_current = Mock(return_value=True)
    follower._navigation_failure_count = 0
    follower._line_fallback_pending = False
    follower._plan_latest_observation_if_pending = Mock()
    follower._cancel_tracking_retry = Mock()

    PersonFollowerNode._on_tracking_path(
        follower, path, 'retreat unavailable', None, 'camera', False,
        20_000_000_000, 1, 1,
    )

    follower._schedule_tracking_navigation_retry.assert_called_once_with()
    follower._plan_latest_observation_if_pending.assert_not_called()
    follower._dispatch_tracking_path.assert_not_called()
    follower._cancel_tracking_retry.assert_not_called()
    assert not follower._line_fallback_pending


def test_successful_forward_plan_is_dispatched_with_requested_standoff():
    """The actual callback passes the trimmed route, not just a helper result."""
    from geometry_msgs.msg import PoseStamped
    follower = _fixture()
    follower._active_goal = object()
    follower._state = FollowState.TRACKING
    follower._observation_is_current = Mock(return_value=True)
    follower._plan_latest_observation_if_pending = Mock()
    path = Path()
    for x in (0.25, 1.25, 2.25, 3.25):
        pose = PoseStamped()
        pose.pose.position.x = x
        pose.pose.position.y = 0.55
        path.poses.append(pose)
    PersonFollowerNode._on_tracking_path(
        follower, path, 'planned', Point2D(3.25, 0.55), 'camera', False,
        20_000_000_000, 1, 1,
    )
    selected, endpoint, travel = follower._dispatch_tracking_path.call_args.args[:3]
    assert endpoint == Point2D(2.25, 0.55)
    assert selected.poses[-1].pose.position.x == 2.25
    assert travel == pytest.approx(2.0)


def _bind(follower, *names):
    from types import MethodType
    for name in names:
        setattr(follower, name, MethodType(getattr(PersonFollowerNode, name), follower))
    return follower


def test_forward_plan_targets_the_person_itself_without_a_costmap():
    """Nav2's planner tolerance, not the follower, resolves the person's cells."""
    follower = _bind(_fixture(), '_pulled_back_goal')
    follower._latest_global_costmap = None
    follower._path_planner = Mock(busy=False)
    follower._path_planner.compute.return_value = True
    robot, target = Point2D(0.25, 0.55), Point2D(3.25, 2.55)
    decision = decide_follow_motion(robot, target, follower._settings)
    PersonFollowerNode._request_tracking_path(
        follower, robot, target, decision, False, 'camera', 20_000_000_000, 1,
    )
    pose = follower._path_planner.compute.call_args.args[0]
    assert (pose.pose.position.x, pose.pose.position.y) == (3.25, 2.55)
    assert pose.header.frame_id == 'map'
    yaw = 2 * math.atan2(pose.pose.orientation.z, pose.pose.orientation.w)
    assert yaw == pytest.approx(math.atan2(2.0, 3.0))
    assert follower._path_planner.compute.call_args.args[1] == 'GridBased'
    assert follower._path_planner.compute.call_args.kwargs['timeout_seconds'] == 0.2
    follower._warn_periodically.assert_not_called()


def test_pulled_back_goal_moves_toward_the_robot_and_stops_at_the_standoff():
    """Each failed cycle steps one planner tolerance closer, never past the standoff."""
    follower = _bind(_fixture(), '_raise_goal_pullback', '_pulled_back_goal')
    robot, target = Point2D(0.0, 0.0), Point2D(3.0, 0.0)
    assert follower._pulled_back_goal(robot, target) == target
    assert follower._raise_goal_pullback(target)
    assert follower._pulled_back_goal(robot, target) == Point2D(2.5, 0.0)
    assert follower._raise_goal_pullback(target)
    assert follower._goal_pullback_m == pytest.approx(1.0)  # The standoff itself.
    assert follower._pulled_back_goal(robot, target) == Point2D(2.0, 0.0)
    assert not follower._raise_goal_pullback(target)
    assert follower._goal_pullback_m == pytest.approx(1.0)
    # A person closer than the pullback still gets a goal on the robot's side.
    near = follower._pulled_back_goal(robot, Point2D(0.5, 0.0))
    assert 0.0 < near.x < 0.5


@pytest.mark.parametrize('detail,pullback_before,expect_pullback', [
    ('Nav2 path planning finished with status 6', 0.0, True),   # No path: step closer.
    ('Nav2 path planning finished with status 6', 1.0, False),  # Standoff reached.
    ('Nav2 path planning timed out; waiting for Nav2 to finish cancellation', 0.0, False),
])
def test_no_path_to_the_person_pulls_the_goal_back_before_the_line_fallback(
    detail, pullback_before, expect_pullback,
):
    """Only an actual "no path" answer moves the goal; timeouts keep the fallback."""
    follower = _bind(_fixture(), '_raise_goal_pullback')
    follower._goal_pullback_m = pullback_before
    follower._active_goal = object()
    follower._state = FollowState.TRACKING
    follower._observation_is_current = Mock(return_value=True)
    follower._navigation_failure_count = 0
    follower._line_fallback_pending = False
    follower._line_fallback_immediate = False
    follower._plan_latest_observation_if_pending = Mock()
    follower._cancel_tracking_retry = Mock()
    PersonFollowerNode._on_tracking_path(
        follower, None, detail, Point2D(3.25, 0.55), 'camera', False,
        20_000_000_000, 1, 1,
    )
    follower._plan_latest_observation_if_pending.assert_called_once_with(-1)
    follower._cancel_tracking_retry.assert_called_once_with()
    if expect_pullback:
        assert follower._goal_pullback_m == pytest.approx(0.5)
        assert follower._goal_pullback_anchor == Point2D(3.25, 0.55)
        assert not follower._line_fallback_pending
    else:
        assert follower._goal_pullback_m == pullback_before
        assert follower._line_fallback_pending and follower._line_fallback_immediate
    follower._dispatch_tracking_path.assert_not_called()


def test_retreat_paths_use_the_reverse_controller_when_configured():
    """Backing away must not pass through a controller that penalizes reverse."""
    follower = _fixture()
    follower._nav2 = Mock()
    follower._nav2.follow_path.return_value = True
    for name in ('_cancel_tracking_retry', '_publish_command_trace', '_publish_track_markers'):
        setattr(follower, name, Mock())
    follower._goal_dispatch_count = 0
    follower._recovery_navigation_active = False
    path = Path()
    PersonFollowerNode._dispatch_tracking_path(
        follower, path, Point2D(0.0, 0.0), 0.3, 'full retreat path', 'camera',
        20_000_000_000, 1, 2, False, reverse=True,
    )
    assert follower._nav2.follow_path.call_args.args[1:] == (
        'FollowPathReverse', 'general_goal_checker')
    PersonFollowerNode._dispatch_tracking_path(
        follower, path, Point2D(0.0, 0.0), 0.3, 'safe tracking goal', 'camera',
        20_000_000_000, 1, 2, False,
    )
    assert follower._nav2.follow_path.call_args.args[1] == 'FollowPath'
    parameters = {'tracking_controller_id': 'FollowPath', 'retreat_controller_id': '',
                  'goal_checker_id': 'general_goal_checker'}
    follower.get_parameter = lambda key: SimpleNamespace(value=parameters[key])
    PersonFollowerNode._dispatch_tracking_path(
        follower, path, Point2D(0.0, 0.0), 0.3, 'full retreat path', 'camera',
        20_000_000_000, 1, 2, False, reverse=True,
    )
    assert follower._nav2.follow_path.call_args.args[1] == 'FollowPath'  # Empty reuses it.


def test_successful_retreat_plan_is_dispatched_in_reverse():
    """The actual result callback marks retreat routes for the reverse controller."""
    from geometry_msgs.msg import PoseStamped
    follower = _fixture()
    follower._active_goal = object()
    follower._state = FollowState.TRACKING
    follower._observation_is_current = Mock(return_value=True)
    follower._plan_latest_observation_if_pending = Mock()
    path = Path()
    for x in (0.25, 0.0):
        pose = PoseStamped()
        pose.pose.position.x = x
        path.poses.append(pose)
    PersonFollowerNode._on_tracking_path(
        follower, path, 'planned', None, 'camera', False, 20_000_000_000, 1, 1,
    )
    assert follower._dispatch_tracking_path.call_args.kwargs == {'reverse': True}
    assert follower._dispatch_tracking_path.call_args.args[3] == 'full retreat path'
