"""The approach node with fake Nav2 servers and a fixed map pose (real ROS)."""

import json
import math
import threading
import time

import pytest

rclpy = pytest.importorskip('rclpy')
nav2 = pytest.importorskip('nav2_msgs.action')
tf2_ros = pytest.importorskip('tf2_ros')
interfaces = pytest.importorskip('malbut_interfaces.action')
if not hasattr(interfaces, 'FallApproach'):
    pytest.skip('malbut_interfaces without FallApproach', allow_module_level=True)

from geometry_msgs.msg import PoseStamped, TransformStamped  # noqa: E402
from nav_msgs.msg import Path  # noqa: E402
from rclpy.action import ActionClient, ActionServer  # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from std_msgs.msg import String  # noqa: E402

from malbut_fall_coordinator.fall_approach_node import FallApproachNode  # noqa: E402


class FakeNav2:
    """Straight-line planner; driving and turning always succeed and are recorded."""

    def __init__(self, node, *, plan=True):
        self.plan, self.driven, self.turns = plan, [], []
        group = ReentrantCallbackGroup()
        ActionServer(node, nav2.ComputePathToPose, 'compute_path_to_pose', self._plan,
                     callback_group=group)
        ActionServer(node, nav2.FollowPath, 'follow_path', self._follow, callback_group=group)
        ActionServer(node, nav2.Spin, 'spin', self._spin, callback_group=group)

    def _plan(self, handle):
        goal = handle.request.goal.pose.position
        result = nav2.ComputePathToPose.Result()
        if not self.plan:
            handle.abort()
            return result
        result.path = Path()
        result.path.header.frame_id = 'map'
        steps = max(1, int(math.hypot(goal.x, goal.y) / 0.1))
        for i in range(steps + 1):
            pose = PoseStamped()
            pose.header.frame_id = 'map'
            pose.pose.position.x, pose.pose.position.y = goal.x * i / steps, goal.y * i / steps
            result.path.poses.append(pose)
        handle.succeed()
        return result

    def _follow(self, handle):
        self.driven.append(handle.request.path)
        handle.succeed()
        return nav2.FollowPath.Result()

    def _spin(self, handle):
        self.turns.append(handle.request.target_yaw)
        handle.succeed()
        return nav2.Spin.Result()


@pytest.fixture
def ros():
    rclpy.init()
    node = FallApproachNode()
    fake = Node('fake_nav2_and_tf')
    broadcaster = tf2_ros.StaticTransformBroadcaster(fake)
    pose = TransformStamped()
    pose.header.frame_id, pose.child_frame_id = 'map', 'base_footprint'
    pose.transform.rotation.w = 1.0
    broadcaster.sendTransform(pose)
    statuses = []
    fake.create_subscription(String, '/malbut/falls/approach/status',
                             lambda m: statuses.append(json.loads(m.data)),
                             QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                        reliability=ReliabilityPolicy.RELIABLE))
    executor = MultiThreadedExecutor(num_threads=6)
    executor.add_node(node)
    executor.add_node(fake)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    client = ActionClient(fake, interfaces.FallApproach, '/malbut/falls/approach')
    assert client.wait_for_server(timeout_sec=5)

    def send(**fields):
        goal = interfaces.FallApproach.Goal(**fields)
        handle = client.send_goal_async(goal)
        end = time.monotonic() + 10
        while not handle.done() and time.monotonic() < end:
            time.sleep(0.02)
        accepted = handle.result()
        if not accepted.accepted:
            return None
        result = accepted.get_result_async()
        while not result.done() and time.monotonic() < end:
            time.sleep(0.02)
        return result.result().result

    deadline = time.monotonic() + 5
    while node._pose() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    yield node, fake, send, statuses
    executor.shutdown()
    node.destroy_node()
    fake.destroy_node()
    rclpy.shutdown()


def test_drives_to_one_metre_before_the_spot_faces_it_and_goes_back(ros):
    node, fake, send, statuses = ros
    nav = FakeNav2(fake)
    result = send(request_id='q1', phase='approach', x=3.0, y=1.0, standoff_m=1.0)
    assert result.outcome == 'arrived'
    end = nav.driven[-1].poses[-1].pose.position
    assert math.hypot(3.0 - end.x, 1.0 - end.y) == pytest.approx(1.0, abs=0.05)
    # The fake robot did not move: it still has to turn toward (3, 1).
    assert nav.turns and nav.turns[-1] == pytest.approx(math.atan2(1.0, 3.0), abs=1e-3)
    result = send(request_id='q1', phase='return', x=0.0, y=0.0, standoff_m=1.0)
    assert result.outcome == 'returned'
    time.sleep(0.2)
    assert statuses[-1]['phase'] == 'return' and statuses[-1]['outcome'] == 'returned'


def test_no_path_is_reported_and_a_return_without_a_start_fails(ros):
    node, fake, send, _ = ros
    FakeNav2(fake, plan=False)
    blocked = send(request_id='q2', phase='approach', x=3.0, y=0.0, standoff_m=1.0)
    assert blocked.outcome == 'no_path'
    unknown = send(request_id='unknown', phase='return', x=0.0, y=0.0, standoff_m=1.0)
    assert unknown.outcome == 'failed'
    assert send(request_id='q3', phase='drive', x=3.0, y=0.0, standoff_m=1.0) is None
