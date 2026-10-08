"""Drive 1 m in front of an uncertain fall suspicion, facing it, or back.

The Manager runs this as the URGENT `fall_approach` mission holding BASE.
Nav2 plans around keepout zones (GridBased planner); the follower's path
standoff rule stops the robot on the 1 m circle. No question or alert here: the
fall coordinator asks the fall runtime to look once the robot has arrived.
"""

from copy import deepcopy
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
from nav2_msgs.action import ComputePathToPose, FollowPath, Spin
from nav_msgs.msg import Path
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from std_msgs.msg import String
from tf2_ros import Buffer, TransformException, TransformListener

from malbut_fall_coordinator.fall_approach import (
    APPROACH_LIMIT_S, RETURN_LIMIT_S, Pose2D, StartPoses, facing_turn,
    goal_error, plan_targets, returning_turn, route_length, standoff_route,
)

STATUS_TOPIC = '/malbut/falls/approach/status'
SPIN_ALLOWANCE_S = 10
CANCEL_SETTLE_S = 5.0


class Timeout(Exception):
    pass


class Canceled(Exception):
    pass


def _yaw(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def route_path(path, route, target):
    """Nav2's poses along the route, the last one moved onto its end facing the spot."""
    cut = Path()
    cut.header = deepcopy(path.header)
    cut.poses = [deepcopy(pose) for pose in path.poses[:len(route)]]
    last = cut.poses[-1].pose
    last.position.x, last.position.y = route[-1]
    yaw = math.atan2(target[1] - route[-1][1], target[0] - route[-1][0])
    last.orientation.x = last.orientation.y = 0.0
    last.orientation.z, last.orientation.w = math.sin(yaw * 0.5), math.cos(yaw * 0.5)
    return cut


class FallApproachNode(Node):
    def __init__(self):
        super().__init__('fall_approach')
        settings = {
            'global_frame': 'map', 'robot_frame': 'base_footprint',
            # GridBased honours the keepout mask; the follower's A* is for people.
            'planner_id': 'GridBased', 'controller_id': 'FollowPath',
            'goal_checker_id': 'general_goal_checker',
            'compute_path_action': 'compute_path_to_pose', 'follow_path_action': 'follow_path',
            'spin_action': 'spin',
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
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        self.status = self.create_publisher(String, STATUS_TOPIC, latched)
        self.starts = StartPoses()
        self.busy = threading.Lock()
        self.child = None
        self.server = ActionServer(
            self, FallApproach, '/malbut/falls/approach', execute_callback=self._execute,
            goal_callback=self._goal, cancel_callback=lambda _goal: CancelResponse.ACCEPT,
            callback_group=self.group)

    def _goal(self, request):
        if goal_error(request.phase, request.x, request.y, request.standoff_m):
            return GoalResponse.REJECT
        if not request.request_id or len(request.request_id) > 200 or self.busy.locked():
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _execute(self, handle):
        request = handle.request
        with self.busy:
            limit = APPROACH_LIMIT_S if request.phase == 'approach' else RETURN_LIMIT_S
            deadline = time.monotonic() + limit
            canceled = False
            try:
                outcome, detail = (self._approach(handle, request, deadline)
                                   if request.phase == 'approach'
                                   else self._return(handle, request, deadline))
            except Timeout:
                outcome, detail = 'timeout', f'not done within {limit:.0f} s'
            except Canceled:
                outcome, detail, canceled = 'failed', 'canceled', True
            self._settle_child()
            self._publish(request, outcome)
            result = FallApproach.Result(outcome=outcome, detail=detail)
            if canceled:
                handle.canceled()
            else:
                handle.succeed()
            return result

    # ------------------------------------------------------------ phases

    def _approach(self, handle, request, deadline):
        robot = self._pose()
        if robot is None:
            return 'no_map', 'no robot pose on the map'
        self.starts.remember(request.request_id, robot)
        target = (request.x, request.y)
        path = None
        for goal in plan_targets((robot.x, robot.y), target):
            path = self._plan(handle, robot, goal, deadline)
            if path is not None:
                break
        if path is None:
            return 'no_path', 'no path near the spot'
        route = standoff_route([(p.pose.position.x, p.pose.position.y) for p in path.poses],
                               target, request.standoff_m)
        if len(route) >= 2 and route_length(route) > 0.05:
            self._feedback(handle, 'DRIVING')
            if not self._drive(handle, route_path(path, route, target), deadline):
                return 'failed', 'Nav2 did not finish the path'
        robot = self._pose() or robot
        turn = facing_turn(robot, target)
        if turn:
            self._feedback(handle, 'TURNING')
            if not self._spin(handle, turn, deadline):
                return 'arrived', 'could not turn to face the spot'
        return 'arrived', ''

    def _return(self, handle, request, deadline):
        start = self.starts.get(request.request_id)
        if start is None:
            return 'failed', 'no start pose for this check'
        robot = self._pose()
        if robot is None:
            return 'no_map', 'no robot pose on the map'
        path = self._plan(handle, robot, (start.x, start.y), deadline, yaw=start.yaw)
        if path is None:
            return 'no_path', 'no path back'
        self._feedback(handle, 'DRIVING')
        if len(path.poses) >= 2 and not self._drive(handle, path, deadline):
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
        result = self._call(self.follower, goal, handle, deadline)
        return result is not None and result.status == GoalStatus.STATUS_SUCCEEDED

    def _spin(self, handle, turn, deadline):
        goal = Spin.Goal()
        goal.target_yaw = float(turn)
        goal.time_allowance = DurationMsg(sec=SPIN_ALLOWANCE_S)
        result = self._call(self.spinner, goal, handle, deadline)
        return result is not None and result.status == GoalStatus.STATUS_SUCCEEDED

    def _call(self, client, goal, handle, deadline):
        if not client.wait_for_server(timeout_sec=min(2.0, max(0.1, deadline - time.monotonic()))):
            return None
        self.child = None
        sent = client.send_goal_async(goal)
        try:
            goal_handle = self._wait(sent, handle, deadline)
        except (Timeout, Canceled):
            # A late acceptance must not leave Nav2 driving on its own.
            sent.add_done_callback(lambda done: done.result() and done.result().accepted
                                   and done.result().cancel_goal_async())
            raise
        if goal_handle is None or not goal_handle.accepted:
            return None
        self.child = goal_handle
        result = self._wait(goal_handle.get_result_async(), handle, deadline)
        self.child = None
        return result

    def _wait(self, future, handle, deadline):
        while not future.done():
            if handle.is_cancel_requested or not rclpy.ok():
                raise Canceled()
            if time.monotonic() > deadline:
                raise Timeout()
            time.sleep(0.02)
        return future.result()

    def _settle_child(self):
        """Never leave Nav2 driving after this mission ended."""
        child, self.child = self.child, None
        if child is None:
            return
        future = child.cancel_goal_async()
        end = time.monotonic() + CANCEL_SETTLE_S
        while not future.done() and time.monotonic() < end and rclpy.ok():
            time.sleep(0.02)

    # ------------------------------------------------------------ helpers

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
        self.status.publish(String(data=json.dumps(dict(
            request_id=request.request_id, phase=request.phase, outcome=outcome,
            at=datetime.now(timezone.utc).isoformat(timespec='seconds')))))


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
        node._settle_child()
        executor.shutdown()
        node.server.destroy()
        node.destroy_node()
        signal.signal(signal.SIGTERM, previous)
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
