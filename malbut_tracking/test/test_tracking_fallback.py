"""Check short fallback paths without touching a real motion server."""

from types import SimpleNamespace
from unittest.mock import Mock

from nav_msgs.msg import Path
import pytest

from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.follow_policy import decide_follow_motion, FollowCommand, FollowSettings
from malbut_tracking.geometry import Point2D
from malbut_tracking.navigation import MotionMode
from malbut_tracking.person_follower_node import FollowState, PersonFollowerNode


def _fixture(wall_x=None, map_time=20.0):
    costs = [0] * 800
    if wall_x is not None:
        for row in range(20):
            costs[row * 40 + wall_x] = 254
    grid = CostmapGrid('map', map_time, 0.1, 40, 20, Point2D(0.0, 0.0), 0.0, costs)
    settings = FollowSettings(1.0, 0.2, 0.1, 0.1, 0.4, 1.5, 0.75)
    parameters = {
        'sensor_transform_queue_timeout_s': 0.3,
        'goal_safe_search_radius_m': 1.0,
        'goal_maximum_cost': 80,
    }
    from builtin_interfaces.msg import Time
    follower = SimpleNamespace(
        _latest_global_costmap=grid, _now_seconds=lambda: 20.0,
        _settings=settings, _global_frame='map', _tracking_source='camera',
        _last_motion_source_stamp_ns=20_000_000_000,
        _static_job_context=object(), _line_fallback_pending=True,
        _path_planner=Mock(busy=True), _nav2=Mock(),
        _dispatch_tracking_path=Mock(return_value=True),
        _schedule_tracking_navigation_retry=Mock(), _warn_periodically=Mock(),
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


def test_fixed_map_updates_are_not_ignored():
    """An updated occupancy map replaces the first received snapshot."""
    old, new = object(), object()
    follower = SimpleNamespace(
        _latest_static_map=old, _occupancy_grid=Mock(return_value=new),
        _warn_periodically=Mock(), get_logger=Mock(),
    )
    PersonFollowerNode._on_static_map(follower, object())
    assert follower._latest_static_map is new


def test_follower_static_search_uses_raw_corridor_without_extra_padding():
    """The hint may pass a narrow gap; actual robot clearance remains with Nav2."""
    costs = tuple(0 if row == 2 else 100 for row in range(5) for _ in range(20))
    grid = CostmapGrid('map', 0.0, 0.05, 20, 5, Point2D(0.0, 0.0), 0.0, costs)
    path = PersonFollowerNode._compute_static_path(
        object(), grid, Point2D(0.125, 0.125), Point2D(0.875, 0.125), 65, 0.02,
    )
    assert path is not None


def test_unsafe_retreat_goal_schedules_retry_instead_of_recursing(monkeypatch):
    """An unprojectable retreat must not enter the forward-line fallback loop."""
    follower = _fixture()
    follower.get_parameter = lambda key: SimpleNamespace(value=1.0)
    follower._nav2.mode = MotionMode.NAVIGATE
    follower._line_fallback_pending = False
    follower._plan_latest_observation_if_pending = Mock()
    monkeypatch.setattr(
        'malbut_tracking.person_follower_node.project_navigation_goal',
        Mock(return_value=None),
    )
    robot, target = Point2D(1.25, 0.55), Point2D(1.35, 0.55)
    decision = decide_follow_motion(robot, target, follower._settings)
    assert decision.command == FollowCommand.RETREAT

    PersonFollowerNode._request_tracking_path(
        follower, robot, target, decision, False, None, 'camera', 20_000_000_000, 1,
    )

    follower._schedule_tracking_navigation_retry.assert_called_once_with()
    follower._plan_latest_observation_if_pending.assert_not_called()
    follower._path_planner.compute.assert_not_called()
    follower._nav2.cancel.assert_called_once_with()
    assert not follower._line_fallback_pending


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
