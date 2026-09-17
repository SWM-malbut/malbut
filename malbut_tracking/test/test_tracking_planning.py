"""Check worker isolation with a real follower and ROS executor, without motion."""

from threading import Event
from time import monotonic
from unittest.mock import Mock

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor

from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.geometry import Point2D
from malbut_tracking.person_follower_node import FollowState, PersonFollowerNode


@pytest.mark.parametrize('cancel_before_completion', [False, True])
def test_slow_search_does_not_block_ros_or_queue_every_observation(
    cancel_before_completion,
):
    """TF/timers remain runnable while one search retains only the latest input."""
    rclpy.init()
    node = PersonFollowerNode()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    release = Event()
    entered = Event()
    try:
        # The test never sends a robot action, even if a server exists locally.
        node._nav2.destroy()
        node._path_planner.destroy()
        node._nav2 = Mock(mode=None)
        node._path_planner = Mock(busy=False)
        node._request_tracking_path = Mock()
        node._active_goal = object()
        node._settings = node._default_settings()
        node._state = FollowState.TRACKING
        node._tracking_source = 'camera'
        node._latest_static_map = CostmapGrid(
            'map', 0.0, 0.1, 40, 40, Point2D(0.0, 0.0), 0.0,
            (0,) * 1600,
        )
        node._latest_global_costmap = node._latest_static_map
        original_compute = node._compute_static_path

        def slow_search(*args):
            entered.set()
            assert release.wait(2.0), 'test did not release worker'
            return original_compute(*args)

        node._compute_static_path = Mock(side_effect=slow_search)
        robot = Point2D(0.5, 1.0)
        node._tracking_retry_pending = True
        node._tracking_retry_timer.reset()
        before = monotonic()
        node._apply_tracking_motion(robot, Point2D(2.0, 1.0), node._now_seconds())
        assert monotonic() - before < 0.1
        assert not node._tracking_retry_pending
        assert entered.wait(1.0)
        first_job = node._static_job
        for offset in range(20):
            latest = Point2D(2.0 + offset * 0.02, 1.0)
            node._apply_tracking_motion(robot, latest, node._now_seconds())
        assert node._static_job is first_job
        assert node._last_motion_target == latest
        assert node._compute_static_path.call_count == 1

        ticks = []
        node.create_timer(0.005, lambda: ticks.append(monotonic()))
        deadline = monotonic() + 0.5
        while len(ticks) < 3 and monotonic() < deadline:
            executor.spin_once(timeout_sec=0.01)
        assert len(ticks) >= 3
        assert not first_job.done()
        assert node.executor is executor  # TF listener never steals the node.

        if cancel_before_completion:
            node._active_goal = None
            node._state = FollowState.STOPPED
        release.set()
        deadline = monotonic() + 1.0
        while node._static_job is not None and monotonic() < deadline:
            executor.spin_once(timeout_sec=0.01)
        assert node._static_job is None
        if cancel_before_completion:
            node._request_tracking_path.assert_not_called()
        else:
            node._request_tracking_path.assert_called_once()
        node._nav2.follow_path.assert_not_called()
    finally:
        release.set()
        executor.remove_node(node)
        node.destroy_node()
        executor.shutdown()
        rclpy.shutdown()
