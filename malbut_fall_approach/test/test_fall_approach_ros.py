"""The approach node with fake Nav2 servers and a fixed map pose (real ROS)."""

import json
import math
import threading
import time
from types import SimpleNamespace

import pytest

rclpy = pytest.importorskip('rclpy')
nav2 = pytest.importorskip('nav2_msgs.action')
tf2_ros = pytest.importorskip('tf2_ros')
interfaces = pytest.importorskip('malbut_interfaces.action')
if not hasattr(interfaces, 'FallApproach'):
    pytest.skip('malbut_interfaces without FallApproach', allow_module_level=True)

from geometry_msgs.msg import PoseStamped, TransformStamped  # noqa: E402
from nav2_msgs.msg import Costmap  # noqa: E402
from nav_msgs.msg import Path  # noqa: E402
from rclpy.action import ActionClient, ActionServer, CancelResponse  # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import (  # noqa: E402
    DurabilityPolicy, QoSProfile, qos_profile_sensor_data, ReliabilityPolicy,
)
from std_msgs.msg import String  # noqa: E402

from malbut_fall_approach import fall_approach_node  # noqa: E402
from malbut_fall_approach.fall_approach_node import FallApproachNode  # noqa: E402


class FakeNav2:
    """Straight-line planner; driving, backing and turning are recorded.

    ``follow`` lists the outcomes of successive drives (then they succeed);
    ``detour(x, y)`` makes the path to that goal go 3 m out of the way first;
    a backup takes ``backup_s`` unless canceled, like the robot backing slowly.
    """

    def __init__(self, node, *, plan=True, backup=True, follow=(), detour=None, backup_s=0.0):
        self.plan, self.driven, self.turns, self.backups = plan, [], [], []
        self.backup, self.follow, self.detour = backup, list(follow), detour
        self.backup_s, self.backup_canceled = backup_s, False
        self.planned, self.navigated = [], []
        self.navigate_succeeds = True
        group = ReentrantCallbackGroup()
        ActionServer(node, nav2.ComputePathToPose, 'compute_path_to_pose', self._plan,
                     callback_group=group)
        ActionServer(node, nav2.FollowPath, 'follow_path', self._follow, callback_group=group)
        ActionServer(node, nav2.Spin, 'spin', self._spin, callback_group=group)
        ActionServer(node, nav2.NavigateToPose, 'navigate_to_pose', self._navigate,
                     callback_group=group)
        ActionServer(node, nav2.BackUp, 'backup', self._back_up, callback_group=group,
                     cancel_callback=lambda _goal: CancelResponse.ACCEPT)

    def _plan(self, handle):
        self.planned.append(handle.request)
        goal = handle.request.goal.pose.position
        result = nav2.ComputePathToPose.Result()
        if not self.plan:
            handle.abort()
            return result
        result.path = Path()
        result.path.header.frame_id = 'map'
        corners = [(0.0, 0.0), (goal.x, goal.y)]
        if self.detour is not None and self.detour(goal.x, goal.y):
            corners.insert(1, (goal.x, goal.y + 3.0))
        for (x0, y0), (x1, y1) in zip(corners, corners[1:]):
            steps = max(1, int(math.hypot(x1 - x0, y1 - y0) / 0.1))
            for i in range(steps + 1):
                pose = PoseStamped()
                pose.header.frame_id = 'map'
                pose.pose.position.x = x0 + (x1 - x0) * i / steps
                pose.pose.position.y = y0 + (y1 - y0) * i / steps
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
        if not self.backup:
            handle.abort()  # Something behind the robot in the local costmap.
            return nav2.BackUp.Result()
        end = time.monotonic() + self.backup_s
        while time.monotonic() < end:
            if handle.is_cancel_requested:
                self.backup_canceled = True
                handle.canceled()
                return nav2.BackUp.Result()
            time.sleep(0.02)
        handle.succeed()
        return nav2.BackUp.Result()


@pytest.fixture
def ros():
    rclpy.init()
    node = FallApproachNode()
    fake = Node('fake_nav2_and_tf')
    broadcaster = tf2_ros.StaticTransformBroadcaster(fake)

    def place(x=0.0, y=0.0):
        """Put the fake robot at (x, y) on the map, facing +x."""
        pose = TransformStamped()
        pose.header.frame_id, pose.child_frame_id = 'map', 'base_footprint'
        pose.transform.translation.x, pose.transform.translation.y = x, y
        pose.transform.rotation.w = 1.0
        broadcaster.sendTransform(pose)

    place()
    fake.place = place
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

    def send(*, hold_s=0.0, **fields):
        """Send a goal; once it holds after its drive, wait hold_s and cancel it."""
        goal = interfaces.FallApproach.Goal(**fields)
        feedback = []
        handle = client.send_goal_async(
            goal, feedback_callback=lambda message: feedback.append(message.feedback))
        end = time.monotonic() + 10
        while not handle.done() and time.monotonic() < end:
            time.sleep(0.02)
        accepted = handle.result()
        if not accepted.accepted:
            return None
        result = accepted.get_result_async()
        held, still_running = None, None
        while not result.done() and time.monotonic() < end:
            if held is None:
                held = next((f for f in feedback if f.phase == 'HOLDING'), None)
                if held is not None:
                    time.sleep(hold_s)
                    still_running = not result.done()
                    accepted.cancel_goal_async()
            time.sleep(0.02)
        if not result.done():
            accepted.cancel_goal_async()  # Never leave a hold behind a failed test.
            stop = time.monotonic() + 5
            while not result.done() and time.monotonic() < stop:
                time.sleep(0.02)
        response = result.result()
        return SimpleNamespace(
            outcome=response.result.outcome, detail=response.result.detail,
            status=response.status, held=held is not None,
            held_outcome=held.outcome if held is not None else None,
            still_running=still_running)

    latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         reliability=ReliabilityPolicy.RELIABLE)
    costmaps = fake.create_publisher(Costmap, '/global_costmap/costmap_raw', latched)

    def costmap(cells):
        """A free 10 m x 10 m map around the robot; ``cells`` maps (x, y) in m to a cost."""
        message = Costmap()
        message.header.frame_id = 'map'
        message.metadata.resolution = 0.1
        message.metadata.size_x = message.metadata.size_y = 100
        message.metadata.origin.position.x = message.metadata.origin.position.y = -5.0
        message.metadata.origin.orientation.w = 1.0
        data = bytearray(100 * 100)
        for (x, y), cost in cells.items():
            data[int((y + 5.0) / 0.1) * 100 + int((x + 5.0) / 0.1)] = cost
        message.data = bytes(data)
        node.grid = None
        costmaps.publish(message)
        end = time.monotonic() + 5
        while node.grid is None and time.monotonic() < end:
            time.sleep(0.02)
        assert node.grid is not None

    deadline = time.monotonic() + 5
    while node._pose() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    yield node, fake, send, statuses, costmap
    node.closing.set()
    executor.shutdown()
    node.destroy_node()
    fake.destroy_node()
    rclpy.shutdown()


def test_drives_to_one_metre_before_the_spot_faces_it_and_goes_back(ros):
    node, fake, send, statuses, _ = ros
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
    node, fake, send, _, _ = ros
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
    node, fake, send, _, _ = ros
    FakeNav2(fake, plan=False)
    blocked = send(request_id='q2', phase='approach', x=3.0, y=0.0, standoff_m=1.0)
    assert blocked.outcome == 'no_path'
    unknown = send(request_id='unknown', phase='return', x=0.0, y=0.0, standoff_m=1.0)
    assert unknown.outcome == 'failed'
    assert send(request_id='q3', phase='drive', x=3.0, y=0.0, standoff_m=1.0) is None


def test_the_stop_is_on_the_side_the_spot_was_first_seen_from(ros):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake)
    # Seen from above the spot (3, 0) while the robot now stands at the origin.
    result = send(request_id='side', phase='approach', x=3.0, y=0.0, standoff_m=1.0,
                  from_x=3.0, from_y=2.0)
    assert (result.outcome, result.detail) == ('arrived', '')
    assert _end(nav) == pytest.approx((3.0, 1.0), abs=0.01)
    planned = nav.planned[0].goal.pose
    assert math.atan2(planned.orientation.z, planned.orientation.w) * 2 == pytest.approx(
        -math.pi / 2)  # Facing the spot from there.


def test_without_a_first_seen_point_the_robot_side_comes_first(ros):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake)
    result = send(request_id='unknown-side', phase='approach', x=3.0, y=0.0, standoff_m=1.0,
                  from_x=math.nan, from_y=math.nan)
    assert result.outcome == 'arrived'
    assert _end(nav) == pytest.approx((2.0, 0.0), abs=0.01)


def test_a_failed_drive_moves_on_round_the_circle(ros):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake, follow=[False])
    result = send(request_id='round', phase='approach', x=3.0, y=0.0, standoff_m=1.0)
    assert result.outcome == 'arrived'
    assert len(nav.driven) == 2
    side = math.radians(210)  # 30 degrees round from the robot's side.
    assert _end(nav) == pytest.approx((3.0 + math.cos(side), math.sin(side)), abs=0.01)


def test_a_blocked_standoff_circle_looks_from_the_nearest_wider_one(ros):
    from malbut_fall_approach.fall_approach import INSCRIBED_COST, ring_places
    _, fake, send, _, costmap = ros
    nav = FakeNav2(fake)
    # A patch around each place: the message's float32 resolution moves cell edges.
    steps = [i * 0.05 for i in range(-3, 4)]
    costmap({(x + dx, y + dy): INSCRIBED_COST
             for x, y in ring_places((0.0, 0.0), (3.0, 0.0), 1.0)
             for dx in steps for dy in steps})
    result = send(request_id='wider', phase='approach', x=3.0, y=0.0, standoff_m=1.0)
    assert (result.outcome, result.detail) == ('arrived', '1.5 m from the spot')
    assert _end(nav) == pytest.approx((1.5, 0.0), abs=0.01)


def test_with_no_place_reached_it_faces_the_spot_from_where_it_is(ros):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake, follow=[False] * 24)
    result = send(request_id='stuck', phase='approach', x=0.0, y=3.0, standoff_m=1.0)
    assert result.outcome == 'failed' and 'facing it from here' in result.detail
    assert nav.turns[-1] == pytest.approx(math.pi / 2, abs=1e-3)


def test_already_inside_the_1_m_circle_it_only_faces_the_spot(ros):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake)
    # 2026-10-19 22:32: 0.6 m away, driving back out to 1 m failed in a narrow room.
    result = send(request_id='near', phase='approach', x=0.0, y=0.6, standoff_m=1.0)
    assert (result.outcome, result.detail) == ('arrived', 'already 0.6 m from the spot')
    assert not nav.planned and not nav.driven
    assert nav.turns == [pytest.approx(math.pi / 2, abs=1e-3)]
    assert result.held


def test_looking_again_from_farther_where_it_was_first_seen_still_drives_there(ros):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake)
    result = send(request_id='again', phase='approach', x=0.8, y=0.0, standoff_m=2.0,
                  from_x=-1.2, from_y=0.0)
    assert (result.outcome, result.detail) == ('arrived', '')
    assert _end(nav) == pytest.approx((-1.2, 0.0), abs=0.01)


NAMES = ('nose', 'left_eye', 'right_eye', 'left_ear', 'right_ear', 'left_shoulder',
         'right_shoulder', 'left_elbow', 'right_elbow', 'left_wrist', 'right_wrist', 'left_hip',
         'right_hip', 'left_knee', 'right_knee', 'left_ankle', 'right_ankle')
TORSO = NAMES
LEGS = ('left_hip', 'left_knee', 'right_knee', 'left_ankle', 'right_ankle')


def person(seen, box=(0.3, 0.2, 0.75, 0.9)):
    """A detected person ahead (the spot is straight ahead) with ``seen`` keypoints."""
    return {'pose': {'boxConfidence': 0.8,
                     'box': dict(zip(('left', 'top', 'right', 'bottom'), box)),
                     'keypoints': [{'name': n, 'x': 0.5, 'y': 0.5,
                                    'confidence': 0.9 if n in seen else 0.05} for n in NAMES]}}


@pytest.fixture
def camera(ros, monkeypatch):
    """The pose detector: ``camera['show']()`` gives the persons of each frame (10 a second)."""
    monkeypatch.setattr(fall_approach_node, 'VIEW_LOOK_S', 0.8)
    _, fake, *_ = ros
    publisher = fake.create_publisher(String, '/homecam/person_poses', qos_profile_sensor_data)
    state, stop = {'show': lambda: []}, threading.Event()

    def frames():
        while not stop.is_set():
            people = state['show']()
            if people is not None:
                now = fake.get_clock().now().nanoseconds
                publisher.publish(String(data=json.dumps({
                    'status': 'ok', 'persons': people,
                    'captureStamp': {'sec': now // 10**9, 'nanosec': now % 10**9}})))
            time.sleep(0.1)

    thread = threading.Thread(target=frames, daemon=True)
    thread.start()
    yield state
    stop.set()
    thread.join(timeout=2)


def _end(nav, index=-1):
    end = nav.driven[index].poses[-1].pose.position
    return end.x, end.y


def _around(turn_deg, radius=1.5):
    """A view place around the spot (1, 0), the robot at the origin on its 180-degree side."""
    side = math.pi + math.radians(turn_deg)
    return 1.0 + radius * math.cos(side), radius * math.sin(side)


def view(send, request_id='v1', standoff_m=3.0):
    # The spot is 1 m straight ahead of the fake robot, as after an approach.
    return send(request_id=request_id, phase='view', x=1.0, y=0.0, standoff_m=standoff_m)


def test_a_torso_already_in_view_needs_no_move(ros, camera):
    _, fake, send, statuses, _ = ros
    nav = FakeNav2(fake)
    camera['show'] = lambda: [person(TORSO)]
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', 'torso in view')
    assert nav.backups == [] and nav.driven == [] and nav.turns == []
    time.sleep(0.2)
    assert statuses[-1]['phase'] == 'view' and statuses[-1]['outcome'] == 'arrived'


def test_backing_away_stops_once_the_torso_is_in_view(ros, camera):
    """2026-10-13 16:51: the whole body showed from 1.23 m while backing toward 2.5 m."""
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake, backup_s=10.0)
    started = []

    def show():
        if nav.backups and not started:
            started.append(time.monotonic())
        return [person(TORSO if started and time.monotonic() - started[0] > 1.0 else LEGS)]

    camera['show'] = show
    began = time.monotonic()
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', 'torso in view')
    assert nav.backups == [pytest.approx((2.0, 0.1))]  # Up to 3 m from the spot ...
    assert nav.backup_canceled and time.monotonic() - began < 6.0  # ... but stopped early.
    assert nav.driven == []


def test_a_blocked_backup_tries_places_nearest_first_and_a_failed_drive_moves_on(ros, camera):
    _, fake, send, _, costmap = ros
    costmap({})
    nav = FakeNav2(fake, backup=False, follow=[False])
    camera['show'] = lambda: [person(TORSO if len(nav.driven) >= 2 else LEGS)]
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', 'torso in view')
    assert len(nav.backups) == 1 and len(nav.driven) == 2
    assert _end(nav, 0) == pytest.approx(_around(0.0), abs=1e-6)  # Straight back, 1.5 m.
    # The drive failed; the next nearest is 30 degrees round, the first side.
    assert _end(nav) == pytest.approx(_around(30.0), abs=1e-6)


def test_places_a_wall_hides_the_spot_from_are_skipped(ros, camera):
    _, fake, send, _, costmap = ros
    # A cupboard between the places straight back and the spot.
    costmap({(-0.2, y / 10): 254 for y in range(-3, 4)})
    nav = FakeNav2(fake, backup=False)
    camera['show'] = lambda: [person(TORSO if nav.driven else LEGS)]
    assert view(send).outcome == 'arrived'
    assert len(nav.driven) == 1
    assert _end(nav) == pytest.approx(_around(30.0), abs=1e-6)


def test_places_more_than_twice_the_straight_line_away_are_skipped(ros, camera):
    _, fake, send, _, costmap = ros
    costmap({(-0.2, y / 10): 254 for y in range(-3, 4)})
    # The way to every place on the first side (y < 0) goes 3 m round: the other side.
    nav = FakeNav2(fake, backup=False, detour=lambda x, y: y < 0.0)
    camera['show'] = lambda: [person(TORSO if nav.driven else LEGS)]
    assert view(send).outcome == 'arrived'
    assert len(nav.driven) == 1
    assert _end(nav) == pytest.approx(_around(-30.0), abs=1e-6)


def test_never_in_view_goes_back_to_where_the_body_was_seen_best(ros, camera, monkeypatch):
    monkeypatch.setattr(fall_approach_node, 'VIEW_PLANS', 2)
    _, fake, send, _, costmap = ros
    costmap({})
    nav = FakeNav2(fake, backup=False)

    placed = {}

    def show():
        drives = len(nav.driven)
        if drives in (1, 2) and drives not in placed:
            fake.place(*((-0.5, 0.0) if drives == 1 else (-0.3, 0.75)))
            placed[drives] = time.monotonic()
        if drives == 1 and time.monotonic() - placed[1] > 0.3:
            return [person(LEGS)]  # At the first place the legs were seen best.
        return [person(('left_ankle',))]

    camera['show'] = show
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', 'where the body was best in view')
    assert len(nav.driven) == 3
    assert _end(nav) == pytest.approx((-0.5, 0.0), abs=1e-6)


UNSEEN = 'the person was not picked out: backed away only'


def test_a_person_never_picked_out_only_backs_away(ros, camera):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake)  # The detector runs but sees no one.
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', UNSEEN)  # Cloud judges there.
    assert nav.backups == [pytest.approx((2.0, 0.1))] and nav.driven == []


def test_backing_away_unseen_stops_at_2_5_m_from_the_spot(ros, camera):
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake, backup_s=10.0)

    def move():
        while not nav.backups:
            time.sleep(0.02)
        time.sleep(0.5)
        fake.place(-1.5, 0.0)  # 2.5 m from the spot (1, 0).

    threading.Thread(target=move, daemon=True).start()
    began = time.monotonic()
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', UNSEEN)
    assert nav.backup_canceled and time.monotonic() - began < 6.0


def test_a_person_never_picked_out_sends_the_robot_nowhere_else(ros, camera):
    """2026-10-13 16:51: blind, a place off to the side only lost the person."""
    _, fake, send, _, costmap = ros
    costmap({})
    nav = FakeNav2(fake, backup=False)  # Blocked right behind.
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', UNSEEN)
    assert nav.driven == [] and nav.planned == []


def test_without_detector_frames_the_view_only_backs_away(ros):
    _, fake, send, _, costmap = ros
    costmap({})
    nav = FakeNav2(fake)
    result = view(send)
    assert (result.outcome, result.detail) == ('arrived', UNSEEN)
    assert nav.backups == [pytest.approx((2.0, 0.1))] and nav.driven == []


def test_a_drive_reports_its_outcome_then_holds_the_wheels_until_replaced(ros):
    """2026-10-13: a fall check keeps a HIGH mission between its steps."""
    from action_msgs.msg import GoalStatus
    _, fake, send, statuses, _ = ros
    FakeNav2(fake)
    result = send(request_id='h1', phase='approach', x=3.0, y=1.0, standoff_m=1.0, hold_s=0.3)
    assert result.held and result.held_outcome == 'arrived'
    assert result.still_running, 'the goal must keep holding after its drive'
    assert result.outcome == 'arrived' and result.status == GoalStatus.STATUS_CANCELED
    time.sleep(0.2)
    assert statuses[-1]['outcome'] == 'arrived'  # Reported when the drive ended.
    back = send(request_id='h1', phase='return', x=0.0, y=0.0, standoff_m=1.0)
    assert not back.held and back.outcome == 'returned'  # The way back ends the check.


def test_hold_only_holds_the_wheels_until_canceled(ros):
    from action_msgs.msg import GoalStatus
    _, fake, send, _, _ = ros
    nav = FakeNav2(fake)
    result = send(request_id='h2', phase='hold', hold_s=0.3)
    assert result.held and result.still_running and result.status == GoalStatus.STATUS_CANCELED
    assert nav.driven == [] and nav.turns == [] and nav.backups == []
