"""Exercise the planning deadline through ROS, without any motion action server."""

from threading import Thread
from time import monotonic, sleep
from unittest.mock import Mock

from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import ComputePathToPose
from nav_msgs.msg import Path
import rclpy
from rclpy.action import ActionServer
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node

from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.geometry import Point2D, distance
from malbut_tracking.navigation import Nav2PathClient
from malbut_tracking.person_follower_node import FollowState, PersonFollowerNode


def test_slow_ros_planner_uses_short_fallback_before_its_late_result(monkeypatch):
    """A timed-out, still-owned compute does not block or later replace fallback."""
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    context = Context()
    rclpy.init(context=context, domain_id=196)
    monkeypatch.setattr('rclpy.node.get_default_context', lambda: context)
    follower = PersonFollowerNode()
    server_node = Node('delayed_test_planner', context=context)
    executor = SingleThreadedExecutor(context=context)
    server_executor = SingleThreadedExecutor(context=context)
    executor.add_node(follower)
    server_executor.add_node(server_node)
    calls, motions, sources = [], [], []
    running = [0]
    peak_running = [0]

    def delayed_plan(handle):
        calls.append(monotonic())
        running[0] += 1
        peak_running[0] = max(peak_running[0], running[0])
        # Like a synchronous planner, do not finish early on cancellation.
        sleep(0.40)
        result = ComputePathToPose.Result()
        result.path = Path()
        result.path.header.frame_id = 'map'
        for x in (0.5, 9.0):
            pose = PoseStamped()
            pose.pose.position.x = x
            pose.pose.position.y = 1.0
            pose.pose.orientation.w = 1.0
            result.path.poses.append(pose)
        handle.succeed()
        running[0] -= 1
        return result

    action_name = '/latency_test_compute_path'
    server = ActionServer(server_node, ComputePathToPose, action_name, delayed_plan)
    thread = Thread(target=server_executor.spin, daemon=True)
    thread.start()
    try:
        # Replace every motion transport before making the follower active.
        follower._nav2.destroy()
        follower._nav2 = Mock(mode=None, busy=False, stopping=False)
        follower._nav2.follow_path.side_effect = lambda path, *_: (
            motions.append((monotonic(), path)) or True
        )
        follower._publish_command_trace = Mock(
            side_effect=lambda _stamp, source, *_: sources.append(source),
        )
        follower._path_planner.destroy()
        follower._path_planner = Nav2PathClient(
            follower, action_name, on_idle=follower._on_path_planner_idle,
        )
        follower._active_goal = Mock()
        follower._settings = follower._default_settings()
        follower._state = FollowState.TRACKING
        follower._tracking_source = 'camera'
        robot = Point2D(0.5, 1.0)
        follower._robot_pose = Mock(return_value=(robot, 0.0))
        follower._latest_static_map = CostmapGrid(
            'map', follower._now_seconds(), 0.1, 40, 40,
            Point2D(0.0, 0.0), 0.0, (0,) * 1600,
        )
        follower._latest_global_costmap = follower._latest_static_map
        deadline = monotonic() + 2.0
        while not follower._path_planner._client.server_is_ready():
            assert monotonic() < deadline, 'isolated test action server was not discovered'
            executor.spin_once(timeout_sec=0.01)

        def observe():
            follower._apply_tracking_motion(
                robot, Point2D(3.0, 1.0), follower._now_seconds(),
                source_stamp_ns=follower.get_clock().now().nanoseconds,
            )

        observe()
        observation_timer = follower.create_timer(0.025, observe)
        deadline = monotonic() + 1.0
        while not motions and monotonic() < deadline:
            executor.spin_once(timeout_sec=0.005)
        observation_timer.cancel()
        assert motions, 'the ROS deadline did not dispatch a line fallback'
        assert calls and running[0] == 1, 'fallback must run before the slow planner ends'
        assert 0.15 <= motions[0][0] - calls[0] < 0.35
        assert sources == ['camera:line_fallback']
        assert follower._path_planner.busy

        # End this one planning cycle; do not start another normal cycle on idle.
        # Keep the active goal/state valid, so a wrongly forwarded late path fails.
        idle = Mock()
        follower._path_planner._on_idle = idle
        while follower._path_planner.busy and monotonic() < deadline:
            executor.spin_once(timeout_sec=0.005)
        assert not follower._path_planner.busy
        idle.assert_called_once_with()
        assert len(calls) == peak_running[0] == 1
        assert len(motions) == 1
        for _, path in motions:
            endpoint = path.poses[-1].pose.position
            assert distance(robot, Point2D(endpoint.x, endpoint.y)) <= 1.0 + 1e-6
            assert all(pose.pose.position.x < 9.0 for pose in path.poses)
    finally:
        server_executor.shutdown(timeout_sec=1.0)
        thread.join(timeout=1.0)
        executor.shutdown()
        follower.destroy_node()
        server.destroy()
        server_node.destroy_node()
        context.shutdown()
