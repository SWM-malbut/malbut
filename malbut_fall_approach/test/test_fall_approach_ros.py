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

from malbut_fall_approach.fall_approach_node import FallApproachNode  # noqa: E402


class FakeNav2:
    """Straight-line planner; driving, backing and turning are recorded.

    ``follow`` lists the outcomes of successive drives (then they succeed).
    """

    def __init__(self, node, *, plan=True, backup=True, follow=()):
        self.plan, self.driven, self.turns, self.backups = plan, [], [], []
        self.backup, self.follow = backup, list(follow)
        self.planned, self.navigated = [], []
        self.navigate_succeeds = True
        group = ReentrantCallbackGroup()
        ActionServer(node, nav2.ComputePathToPose, 'compute_path_to_pose', self._plan,
                     callback_group=group)
        ActionServer(node, nav2.FollowPath, 'follow_path', self._follow, callback_group=group)
        ActionServer(node, nav2.Spin, 'spin', self._spin, callback_group=group)
        ActionServer(node, nav2.NavigateToPose, 'navigate_to_pose', self._navigate,
                     callback_group=group)
        ActionServer(node, nav2.BackUp, 'backup', self._back_up, callback_group=group)

    def _plan(self, handle):
        self.planned.append(handle.request)
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
        if self.follow and not self.follow.pop(0):
            handle.abort()  # Like controller_server's "Failed to make progress".
        else:
            handle.succeed()
        return nav2.FollowPath.Result()

    def _spin(self, handle):
        self.turns.append(handle.request.target_yaw)
        handle.succeed()
        return nav2.Spin.Result()

    def _navigate(self, handle):
        self.navigated.append(handle.request.pose)
        if self.navigate_succeeds:
            handle.succeed()
        else:
            handle.abort()
        return nav2.NavigateToPose.Result()

    def _back_up(self, handle):
        self.backups.append((handle.request.target.x, handle.request.speed))
        if self.backup:
            handle.succeed()
        else:
            handle.abort()  # Something behind the robot in the local costmap.
        return nav2.BackUp.Result()


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
    planned, driven = len(nav.planned), len(nav.driven)
    result = send(request_id='q1', phase='return', x=0.0, y=0.0, standoff_m=1.0)
    assert result.outcome == 'returned'
    assert len(nav.planned) == planned and len(nav.driven) == driven
    assert len(nav.navigated) == 1
    assert nav.navigated[0].header.frame_id == 'map'
    assert nav.navigated[0].pose.position.x == pytest.approx(0.0)
    assert nav.navigated[0].pose.position.y == pytest.approx(0.0)
    time.sleep(0.2)
    assert statuses[-1]['phase'] == 'return' and statuses[-1]['outcome'] == 'returned'


def test_return_uses_the_saved_pose_and_keeps_it_when_navigation_fails(ros):
    from malbut_fall_approach.fall_approach import Pose2D
    node, fake, send, _ = ros
    nav = FakeNav2(fake)
    node.starts.remember('saved', Pose2D(1.2, -0.4, 0.7))
    nav.navigate_succeeds = False
    result = send(request_id='saved', phase='return', x=99.0, y=99.0, standoff_m=1.0)
    assert result.outcome == 'failed'
    assert node.starts.get('saved') is not None
    assert nav.navigated[-1].pose.position.x == pytest.approx(1.2)
    assert nav.navigated[-1].pose.position.y == pytest.approx(-0.4)
    assert nav.navigated[-1].pose.orientation.z == pytest.approx(math.sin(0.7 / 2))
    assert not nav.planned and not nav.driven
    nav.navigate_succeeds = True
    result = send(request_id='saved', phase='return', x=99.0, y=99.0, standoff_m=1.0)
    assert result.outcome == 'returned'
    assert node.starts.get('saved') is None
    assert nav.turns[-1] == pytest.approx(0.7)


def test_no_path_is_reported_and_a_return_without_a_start_fails(ros):
    node, fake, send, _ = ros
    FakeNav2(fake, plan=False)
    blocked = send(request_id='q2', phase='approach', x=3.0, y=0.0, standoff_m=1.0)
    assert blocked.outcome == 'no_path'
    unknown = send(request_id='unknown', phase='return', x=0.0, y=0.0, standoff_m=1.0)
    assert unknown.outcome == 'failed'
    assert send(request_id='q3', phase='drive', x=3.0, y=0.0, standoff_m=1.0) is None


def test_view_backs_up_facing_the_spot_without_turning_away(ros):
    _, fake, send, statuses = ros
    nav = FakeNav2(fake)
    # The fake robot stands at the origin facing +x, 1 m short of the spot.
    result = send(request_id='v1', phase='view', x=1.0, y=0.0, standoff_m=2.5)
    assert result.outcome == 'arrived'
    assert nav.backups == [pytest.approx((1.5, 0.1))]
    assert nav.driven == [] and nav.turns == []
    time.sleep(0.2)
    assert statuses[-1]['phase'] == 'view' and statuses[-1]['outcome'] == 'arrived'


def test_view_tries_the_next_side_after_a_failed_drive(ros):
    _, fake, send, _ = ros
    nav = FakeNav2(fake, backup=False, follow=[False])
    result = send(request_id='v2', phase='view', x=1.0, y=0.0, standoff_m=2.5)
    assert result.outcome == 'arrived'
    assert len(nav.backups) == 1 and len(nav.driven) == 2
    side = math.pi + math.radians(45.0)  # The second view point, around the spot.
    end = nav.driven[-1].poses[-1].pose.position
    assert (end.x, end.y) == pytest.approx(
        (1.0 + 2.5 * math.cos(side), 2.5 * math.sin(side)), abs=1e-6)


def test_view_fails_only_after_every_side_failed(ros):
    _, fake, send, _ = ros
    nav = FakeNav2(fake, backup=False, follow=[False] * 5)
    result = send(request_id='v3', phase='view', x=1.0, y=0.0, standoff_m=2.5)
    assert result.outcome == 'failed'
    assert len(nav.driven) == 5
