"""Drive 1 m in front of an uncertain fall suspicion, facing it, or back.

The Manager runs this as the HIGH `fall_approach` mission holding BASE: it
takes over a patrol or a drive, never 직접 움직이기 (URGENT).
Nav2 plans around keepout zones (GridBased planner) to a place on the 1 m circle,
on the side the suspicion was first seen from. No question or alert here: the
Agent's fall rules ask the fall runtime to look once the robot has arrived.
"""

import json
import math
import signal
import threading
import time
from datetime import datetime, timezone

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Duration as DurationMsg
from geometry_msgs.msg import PoseStamped
from malbut_interfaces.action import FallApproach
from nav2_msgs.action import BackUp, ComputePathToPose, FollowPath, NavigateToPose, Spin
from nav2_msgs.msg import Costmap
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data, ReliabilityPolicy
from rclpy.time import Time
from std_msgs.msg import String
from tf2_ros import Buffer, TransformException, TransformListener

from malbut_fall_approach.fall_approach import (
    APPROACH_LIMIT_S, BACKUP_SPEED_MPS, CAMERA_HFOV_RAD,
    HOLD_LIMIT_S, HOLDING_PHASES, RETURN_LIMIT_S, VIEW_LIMIT_S, CostGrid,
    Pose2D, StartPoses, TorsoWatch, already_near, approach_places, backup_distance, can_stand,
    facing_turn, first_seen, goal_error, returning_turn, sight_clear, target_pose, UNSEEN_VIEW_M,
)

STATUS_TOPIC = '/malbut/falls/approach/status'
SPIN_ALLOWANCE_S = 10
BACKUP_SLACK_S = 5
CANCEL_SETTLE_S = 5.0
# Watching at a stop: long enough for the torso to stay in view half a second
# at the detector's five frames a second.
VIEW_LOOK_S = 1.5
# An approach plans at most the 1 m circle and one wider circle, first-seen side first.
APPROACH_PLANS = 24
MAX_POSES_BYTES = 262144
SEEN = object()  # A backup stopped because the torso came into view.


class Timeout(Exception):
    pass


class Canceled(Exception):
    pass


class Seen(Exception):
    pass


def _yaw(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


class FallApproachNode(Node):
    def __init__(self):
        super().__init__('fall_approach')
        settings = {
            'global_frame': 'map', 'robot_frame': 'base_footprint',
            # GridBased honours the keepout mask; the follower's A* is for people.
            'planner_id': 'GridBased', 'controller_id': 'FollowPath',
            'goal_checker_id': 'general_goal_checker',
            'compute_path_action': 'compute_path_to_pose', 'follow_path_action': 'follow_path',
            'spin_action': 'spin', 'navigate_action': 'navigate_to_pose',
            'backup_action': 'backup',
            # Where the whole body can be seen from (walls, furniture, free floor).
            'costmap_topic': '/global_costmap/costmap_raw',
            # The pose detector, watched while the view moves (2026-10-13).
            'poses_topic': '/homecam/person_poses',
            'camera_horizontal_fov_rad': CAMERA_HFOV_RAD,
        }
        self.settings = {name: self.declare_parameter(name, value).value
                         for name, value in settings.items()}
        self.group = ReentrantCallbackGroup()
        self.tf = Buffer()
        self.tf_listener = TransformListener(self.tf, self)
        self.planner = ActionClient(self, ComputePathToPose, self.settings['compute_path_action'],
                                    callback_group=self.group)
        self.follower = ActionClient(self, FollowPath, self.settings['follow_path_action'],
                                     callback_group=self.group)
        self.spinner = ActionClient(self, Spin, self.settings['spin_action'],
                                    callback_group=self.group)
        self.navigator = ActionClient(self, NavigateToPose, self.settings['navigate_action'],
                                      callback_group=self.group)
        self.backer = ActionClient(self, BackUp, self.settings['backup_action'],
                                   callback_group=self.group)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        self.status = self.create_publisher(String, STATUS_TOPIC, latched)
        self.grid = None
        self.create_subscription(Costmap, self.settings['costmap_topic'], self._on_costmap,
                                 latched, callback_group=self.group)
        self.watch = TorsoWatch()
        self.watch_lock = threading.Lock()
        self.watch_target = None
        self.create_subscription(String, self.settings['poses_topic'], self._on_poses,
                                 qos_profile_sensor_data, callback_group=self.group)
        self.starts = StartPoses()
        self.busy = threading.Lock()
        self.closing = threading.Event()  # Shutdown ends a hold at once.
        self.child = None
        self.server = ActionServer(
            self, FallApproach, '/malbut/falls/approach', execute_callback=self._execute,
            goal_callback=self._goal, cancel_callback=lambda _goal: CancelResponse.ACCEPT,
            callback_group=self.group)

    def _goal(self, request):
        if goal_error(request.phase, request.x, request.y, request.standoff_m, request.reason,
                      request.from_x, request.from_y):
            return GoalResponse.REJECT
        if not request.request_id or len(request.request_id) > 200 or self.busy.locked():
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _execute(self, handle):
        request = handle.request
        with self.busy:
            limit = {'approach': APPROACH_LIMIT_S, 'view': VIEW_LIMIT_S}.get(
                request.phase, RETURN_LIMIT_S)
            deadline = time.monotonic() + limit
            canceled = False
            phase = {'approach': self._approach, 'view': self._view,
                     'hold': lambda *_: ('arrived', 'holding')}.get(request.phase, self._return)
            try:
                outcome, detail = phase(handle, request, deadline)
            except Timeout:
                outcome, detail = 'timeout', f'not done within {limit:.0f} s'
            except Canceled:
                outcome, detail, canceled = 'failed', 'canceled', True
            self._settle_child()
            if request.phase != 'hold':
                self._publish(request, outcome)
            if not canceled and request.phase in HOLDING_PHASES:
                # The step is done but the check is not: report it and keep the
                # wheels until the next step replaces this goal or it is canceled.
                handle.publish_feedback(FallApproach.Feedback(
                    phase='HOLDING', outcome=outcome, detail=detail))
                canceled = self._hold(handle)
            result = FallApproach.Result(outcome=outcome, detail=detail)
            if canceled:
                handle.canceled()
            else:
                handle.succeed()
            return result

    def _hold(self, handle):
        """Stand still until canceled; True when canceled, False at the bound."""
        end = time.monotonic() + HOLD_LIMIT_S
        while rclpy.ok() and not self.closing.is_set() and time.monotonic() < end:
            if handle.is_cancel_requested:
                return True
            time.sleep(0.05)
        return handle.is_cancel_requested

    # ------------------------------------------------------------ phases

    def _approach(self, handle, request, deadline):
        """Stop on the circle around the spot, on the side it was first seen from.

        The same side, so the photos before and after can be compared (the
        user, 2026-10-17): places on the standoff circle go round from that
        side, then the wider circles of the view. A place has to stand clear and
        see the spot on the costmap; a failed drive moves on to the next one.
        With none reached the robot faces the spot from where it is, and the
        fall rules decide what to do there. Already inside the 1 m circle, it only
        faces the spot (the user, 2026-10-19).
        """
        robot = self._pose()
        if robot is None:
            return 'no_map', 'no robot pose on the map'
        self.starts.remember(request.request_id, robot)
        target = (request.x, request.y)
        if already_near(robot, target, request.standoff_m):
            if self._turn_to(handle, target, deadline) is not True:
                return 'arrived', 'could not turn to face the spot'
            gap = math.dist((robot.x, robot.y), target)
            return 'arrived', f'already {gap:.1f} m from the spot'
        places = approach_places(first_seen(request.from_x, request.from_y, robot), target,
                                 request.standoff_m)
        grid = self.grid
        if grid is not None:
            places = [p for p in places if can_stand(grid, p) and sight_clear(grid, p, target)]
        planned = False
        for place in places[:APPROACH_PLANS]:
            path = self._plan(handle, robot, place, deadline,
                              yaw=math.atan2(target[1] - place[1], target[0] - place[0]))
            if path is None:
                continue
            planned = True
            if len(path.poses) >= 2:
                self._feedback(handle, 'DRIVING')
                if not self._drive(handle, path, deadline):
                    continue
            radius = math.dist(place, target)
            if self._turn_to(handle, target, deadline) is not True:
                return 'arrived', 'could not turn to face the spot'
            if abs(radius - request.standoff_m) < 0.01:
                return 'arrived', ''
            return 'arrived', f'{radius:.1f} m from the spot'
        self._turn_to(handle, target, deadline)
        if planned:
            return 'failed', 'Nav2 did not reach a place around the spot; facing it from here'
        return 'no_path', 'no place around the spot; facing it from here'

    def _view(self, handle, request, deadline):
        """Back away facing the spot until the person's torso is in view.

        Watching the pose detector (2026-10-13): stay if it is already in view,
        else back straight away, camera on the spot, and stop once it is, at most
        standoff_m from the spot (2.5 m while the person was never picked out).
        Never to places around the spot (the user, 2026-10-19): turning round to
        drive there failed in narrow rooms (10-12 16:31, 10-13 16:51, 10-19 22:33).
        Still out of view, Cloud judges from where it stopped.
        """
        robot = self._pose()
        if robot is None:
            return 'no_map', 'no robot pose on the map'
        self.starts.remember(request.request_id, robot)  # Normally kept from the approach.
        target = (request.x, request.y)
        with self.watch_lock:
            self.watch.reset()
            self.watch_target = target
        try:
            if self._look(handle, deadline):
                return 'arrived', 'torso in view'
            return self._view_backing(handle, request, robot, target, deadline)
        finally:
            with self.watch_lock:
                self.watch_target = None

    def _view_backing(self, handle, request, robot, target, deadline):
        seen = self._torso_seen
        if not backup_distance(robot, target, request.standoff_m) and facing_turn(robot, target):
            self._turn_to(handle, target, deadline)  # Back away facing the spot.
            robot = self._pose() or robot
        back = backup_distance(robot, target, request.standoff_m)
        if back:
            self._feedback(handle, 'DRIVING')
            done = self._back_up(handle, back, deadline,
                                 stop=lambda: seen() or self._unseen_far_enough(target))
            if seen() or (done is True and self._look(handle, deadline)):
                return 'arrived', 'torso in view'
        self._turn_to(handle, target, deadline)
        with self.watch_lock:
            picked_out = self.watch.best is not None
        if not picked_out:
            return 'arrived', 'the person was not picked out: backed away only'
        return 'arrived', 'the torso never came into view: backed away only'

    def _turn_to(self, handle, target, deadline):
        robot = self._pose()
        turn = facing_turn(robot, target) if robot is not None else 0.0
        if not turn:
            return True
        self._feedback(handle, 'TURNING')
        return self._spin(handle, turn, deadline)

    def _look(self, handle, deadline):
        """Stand still a moment watching for the torso."""
        self._feedback(handle, 'LOOKING')
        end = min(deadline, time.monotonic() + VIEW_LOOK_S)
        while time.monotonic() < end:
            if handle.is_cancel_requested or not rclpy.ok():
                raise Canceled()
            if self._torso_seen():
                return True
            time.sleep(0.05)
        return self._torso_seen()

    def _unseen_far_enough(self, target):
        """Never picked out yet and already as far as a blind view goes."""
        with self.watch_lock:
            if self.watch.best is not None:
                return False
        robot = self._pose()
        return robot is not None and math.dist((robot.x, robot.y), target) >= UNSEEN_VIEW_M

    def _torso_seen(self):
        now = self.get_clock().now().nanoseconds * 1e-9
        with self.watch_lock:
            return self.watch.seen(now)

    def _return(self, handle, request, deadline):
        start = self.starts.get(request.request_id)
        if start is None:
            return 'failed', 'no start pose for this check'
        robot = self._pose()
        if robot is None:
            return 'no_map', 'no robot pose on the map'
        # Return to a fixed pose through Nav2's normal replanning/recovery tree.
        # Only approach/view need to calculate and trim a path themselves.
        goal = NavigateToPose.Goal()
        goal.pose = self._pose_stamped((start.x, start.y), start.yaw)
        self._feedback(handle, 'DRIVING')
        result = self._call(self.navigator, goal, handle, deadline)
        if result is None or result.status != GoalStatus.STATUS_SUCCEEDED:
            return 'failed', 'Nav2 did not finish the path back'
        robot = self._pose() or robot
        turn = returning_turn(robot, start)
        if turn:
            self._feedback(handle, 'TURNING')
            self._spin(handle, turn, deadline)
        self.starts.forget(request.request_id)
        return 'returned', ''

    # ------------------------------------------------------------ Nav2

    def _plan(self, handle, robot, goal_xy, deadline, *, yaw=None):
        self._feedback(handle, 'PLANNING')
        goal = ComputePathToPose.Goal()
        goal.goal = self._pose_stamped(goal_xy, yaw if yaw is not None else math.atan2(
            goal_xy[1] - robot.y, goal_xy[0] - robot.x))
        goal.planner_id = self.settings['planner_id']
        goal.use_start = False
        result = self._call(self.planner, goal, handle, deadline)
        if result is None or result.status != GoalStatus.STATUS_SUCCEEDED:
            return None
        path = result.result.path
        return path if path.poses else None

    def _drive(self, handle, path, deadline):
        goal = FollowPath.Goal()
        goal.path = path
        goal.controller_id = self.settings['controller_id']
        goal.goal_checker_id = self.settings['goal_checker_id']
        return self._succeeded(self._call(self.follower, goal, handle, deadline))

    def _spin(self, handle, turn, deadline):
        goal = Spin.Goal()
        goal.target_yaw = float(turn)
        goal.time_allowance = DurationMsg(sec=SPIN_ALLOWANCE_S)
        return self._succeeded(self._call(self.spinner, goal, handle, deadline))

    def _back_up(self, handle, distance, deadline, stop=None):
        """Nav2 BackUp checks the local costmap behind the robot as it goes."""
        goal = BackUp.Goal()
        goal.target.x = float(distance)
        goal.speed = BACKUP_SPEED_MPS
        goal.time_allowance = DurationMsg(
            sec=int(math.ceil(distance / BACKUP_SPEED_MPS)) + BACKUP_SLACK_S)
        return self._succeeded(self._call(self.backer, goal, handle, deadline, stop))

    @staticmethod
    def _succeeded(result):
        if result is SEEN:
            return SEEN
        return result is not None and result.status == GoalStatus.STATUS_SUCCEEDED

    def _call(self, client, goal, handle, deadline, stop=None):
        if not client.wait_for_server(timeout_sec=min(2.0, max(0.1, deadline - time.monotonic()))):
            return None
        self.child = None
        sent = client.send_goal_async(goal)
        try:
            goal_handle = self._wait(sent, handle, deadline)
        except (Timeout, Canceled):
            # An acceptance after cancellation still owns motion until the
            # accepted Nav2 goal actually ends, not merely its cancel ACK.
            goal_handle = self._wait_motion(sent, 'late Nav2 acceptance')
            if goal_handle is not None and goal_handle.accepted:
                self.child = goal_handle
                self._settle_child()
            raise
        if goal_handle is None or not goal_handle.accepted:
            return None
        self.child = goal_handle
        try:
            result = self._wait(goal_handle.get_result_async(), handle, deadline, stop)
        except Seen:
            self._settle_child()  # Stop here: the torso is in view.
            return SEEN
        self.child = None
        return result

    def _wait(self, future, handle, deadline, stop=None):
        while not future.done():
            if handle.is_cancel_requested or not rclpy.ok():
                raise Canceled()
            if time.monotonic() > deadline:
                raise Timeout()
            if stop is not None and stop():
                raise Seen()
            time.sleep(0.02)
        return future.result()

    def _settle_child(self):
        """Never leave Nav2 driving after this mission ended."""
        child = self.child
        if child is None:
            return
        child.cancel_goal_async()
        self._wait_motion(child.get_result_async(), 'Nav2 cancellation')
        self.child = None

    def _wait_motion(self, future, label):
        warn_at = time.monotonic() + CANCEL_SETTLE_S
        warned = False
        while not future.done() and rclpy.ok():
            if not warned and time.monotonic() >= warn_at:
                self.get_logger().warning(f'{label} is unconfirmed; retaining motion ownership')
                warned = True
            time.sleep(0.02)
        return future.result() if future.done() else None

    # ------------------------------------------------------------ helpers

    def _on_costmap(self, message):
        if message.header.frame_id != self.settings['global_frame']:
            return  # Positions here are on the map; another frame cannot be compared.
        meta = message.metadata
        q = meta.origin.orientation
        width, height = int(meta.size_x), int(meta.size_y)
        if meta.resolution <= 0.0 or len(message.data) != width * height:
            return
        self.grid = CostGrid(float(meta.resolution), float(meta.origin.position.x),
                             float(meta.origin.position.y), _yaw(q), width, height,
                             bytes(message.data))

    def _on_poses(self, message):
        """A pose detector frame: note the person at the spot while a view watches."""
        with self.watch_lock:
            target = self.watch_target
        if target is None or len(message.data) > MAX_POSES_BYTES:
            return
        try:
            frame = json.loads(message.data)
        except ValueError:
            return
        if not isinstance(frame, dict) or frame.get('status') != 'ok':
            return
        robot = self._pose()
        if robot is None:
            return
        pose = target_pose(frame.get('persons'), robot, target,
                           float(self.settings['camera_horizontal_fov_rad']))
        # Timed on arrival: the camera's clock need not be this node's, and the
        # robot pose paired with the frame is the one now.
        now = self.get_clock().now().nanoseconds * 1e-9
        with self.watch_lock:
            if self.watch_target != target:
                return
            self.watch.observe(now, robot, pose)

    def _pose(self):
        try:
            t = self.tf.lookup_transform(self.settings['global_frame'],
                                         self.settings['robot_frame'], Time())
        except TransformException:
            return None
        return Pose2D(t.transform.translation.x, t.transform.translation.y,
                      _yaw(t.transform.rotation))

    def _pose_stamped(self, xy, yaw):
        pose = PoseStamped()
        pose.header.frame_id = self.settings['global_frame']
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x, pose.pose.position.y = float(xy[0]), float(xy[1])
        pose.pose.orientation.z, pose.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        return pose

    def _feedback(self, handle, phase):
        handle.publish_feedback(FallApproach.Feedback(phase=phase))

    def _publish(self, request, outcome):
        status = dict(request_id=request.request_id, phase=request.phase, outcome=outcome,
                      at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        if request.phase == 'return' and request.reason:
            status['reason'] = request.reason
        self.status.publish(String(data=json.dumps(status)))


def main(args=None):
    rclpy.init(args=args)

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    node = FallApproachNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.closing.set()
        node._settle_child()
        executor.shutdown()
        node.server.destroy()
        node.destroy_node()
        signal.signal(signal.SIGTERM, previous)
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
