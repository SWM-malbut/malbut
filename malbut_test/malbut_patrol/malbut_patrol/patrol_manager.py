"""Run one map-driven camera patrol through cancellable Nav2 actions."""

import json
import math
from pathlib import Path
import signal
import threading
import time

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from malbut_interfaces.action import Patrol
from map_msgs.msg import OccupancyGridUpdate
from nav2_msgs.action import NavigateToPose, Spin
from nav_msgs.msg import OccupancyGrid
import numpy as np
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image
from std_msgs.msg import String
from tf2_ros import Buffer, TransformException, TransformListener

from malbut_patrol.coverage import CoverageGrid, CoveragePlanner, CoverageProfile


class PatrolInterrupted(RuntimeError):
    """A patrol must stop and settle its outstanding Nav2 command."""


class ChildAction:
    """Track a Nav2 goal from send through its terminal result, including races."""

    def __init__(self, client, request):
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.handle = None
        self.result = None
        self.error = None
        self.settled = False
        self.cancel_requested = False
        self.cancel_sent = False
        self.sent_at = time.monotonic()
        try:
            client.send_goal_async(request).add_done_callback(self._accepted)
        except Exception as error:
            self.error = error
            self.settled = True

    def _accepted(self, future):
        with self.lock:
            try:
                self.handle = future.result()
                if not self.handle.accepted:
                    self.settled = True
                else:
                    self.handle.get_result_async().add_done_callback(
                        self._finished)
                    if self.cancel_requested:
                        self.cancel()
            except Exception as error:
                # A transport failure does not prove that the robot stopped.
                self.error = error
            self.wake.set()

    def _finished(self, future):
        with self.lock:
            try:
                self.result = future.result()
                self.settled = True
            except Exception as error:
                self.error = error
            self.wake.set()

    def cancel(self):
        """Cancel now, or cancel immediately when a delayed acceptance arrives."""
        with self.lock:
            self.cancel_requested = True
            if (self.handle is not None and self.handle.accepted
                    and not self.settled and not self.cancel_sent):
                self.cancel_sent = True
                try:
                    self.handle.cancel_goal_async()
                except Exception as error:
                    self.error = error
            self.wake.set()


def _yaw(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                      1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def _grid(message):
    origin = message.info.origin
    return CoverageGrid(
        np.asarray(message.data, dtype=np.int16).reshape(
            message.info.height, message.info.width),
        message.info.resolution, origin.position.x, origin.position.y,
        _yaw(origin.orientation))


class PatrolManager(Node):
    """Observe a saved map once at the requested inspection thoroughness."""

    def __init__(self, **kwargs):
        super().__init__('patrol_manager', **kwargs)
        defaults = {
            'map_topic': '/map',
            'costmap_topic': '/global_costmap/costmap',
            'camera_image_topic': '/camera/color/image_raw',
            'camera_info_topic': '/camera/color/camera_info',
            'camera_optical_frame': '',
            'room_map_file': '',
            'base_frame': 'base_footprint',
            'nav2_action_name': 'navigate_to_pose',
            'spin_action_name': 'spin',
            'robot_clearance_m': 0.26,
            'observation_ranges_m': [4.0, 3.0, 2.0],
            'coverage_targets': [0.80, 0.90, 0.95],
            'candidate_spacing_m': [1.5, 1.0, 0.65],
            'observation_hz': 5.0,
            'sensor_timeout_s': 3.0,
            'costmap_timeout_s': 5.0,
            'goal_response_timeout_s': 5.0,
            'cancel_completion_timeout_s': 5.0,
            'navigation_timeout_s': 120.0,
            'spin_time_allowance_s': 60.0,
            'maximum_goal_cost': 80,
        }
        for name, value in defaults.items():
            self.declare_parameter(name, value)
        self.settings = {name: self.get_parameter(name).value for name in defaults}
        for name in ('observation_hz', 'sensor_timeout_s', 'costmap_timeout_s',
                     'goal_response_timeout_s', 'cancel_completion_timeout_s',
                     'navigation_timeout_s', 'spin_time_allowance_s',
                     'robot_clearance_m'):
            if not math.isfinite(self.settings[name]) or self.settings[name] <= 0:
                raise ValueError(f'{name} must be positive and finite')
        for name in ('observation_ranges_m', 'coverage_targets',
                     'candidate_spacing_m'):
            if len(self.settings[name]) != 3:
                raise ValueError(f'{name} must contain LIGHT, NORMAL, THOROUGH')
        if not 0 <= self.settings['maximum_goal_cost'] < 99:
            raise ValueError('maximum_goal_cost must be in 0..98')
        self.profiles = [CoverageProfile(*values) for values in zip(
            self.settings['observation_ranges_m'],
            self.settings['coverage_targets'],
            self.settings['candidate_spacing_m'])]
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.busy = False
        self.child = None
        self.map_message = None
        self.map_changed = False
        self.costmap = None
        self.costmap_received = 0.0
        self.image = None
        self.image_received = 0.0
        self.camera_info = None
        self.planner = None
        self.phase = 'IDLE'
        self.visited = 0
        self.last_image_stamp = None
        self.last_observation = 0.0
        self.last_pump = 0.0
        self.last_feedback = 0.0
        self.group = ReentrantCallbackGroup()
        self.tf = Buffer(cache_time=Duration(seconds=10.0))
        self.tf_listener = TransformListener(self.tf, self)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(OccupancyGrid, self.settings['map_topic'],
                                 self._receive_map, latched,
                                 callback_group=self.group)
        self.create_subscription(OccupancyGrid, self.settings['costmap_topic'],
                                 self._receive_costmap, latched,
                                 callback_group=self.group)
        self.create_subscription(OccupancyGridUpdate,
                                 self.settings['costmap_topic'] + '_updates',
                                 self._receive_costmap_update, 10,
                                 callback_group=self.group)
        self.create_subscription(Image, self.settings['camera_image_topic'],
                                 self._receive_image, qos_profile_sensor_data,
                                 callback_group=self.group)
        self.create_subscription(CameraInfo, self.settings['camera_info_topic'],
                                 self._receive_info, qos_profile_sensor_data,
                                 callback_group=self.group)
        self.navigation = ActionClient(self, NavigateToPose,
                                       self.settings['nav2_action_name'],
                                       callback_group=self.group)
        self.spin_client = ActionClient(self, Spin,
                                        self.settings['spin_action_name'],
                                        callback_group=self.group)
        self.status = self.create_publisher(String, 'patrol/status', latched)
        self.server = ActionServer(
            self, Patrol, 'patrol', execute_callback=self._execute,
            goal_callback=self._goal, cancel_callback=self._cancel,
            callback_group=self.group)
        self._publish_status('idle', 'Waiting for a patrol goal')

    def _receive_map(self, message):
        with self.lock:
            if self.busy and self.map_message is not None:
                old = self.map_message
                if (old.header.frame_id != message.header.frame_id
                        or old.info.resolution != message.info.resolution
                        or old.info.width != message.info.width
                        or old.info.height != message.info.height
                        or old.info.origin != message.info.origin
                        or old.data != message.data):
                    self.map_changed = True
            self.map_message = message

    def _receive_costmap(self, message):
        with self.lock:
            self.costmap = message
            self.costmap_received = time.monotonic()

    def _receive_costmap_update(self, update):
        # Nav2 can publish patches instead of a new full grid every cycle.
        with self.lock:
            grid = self.costmap
            if (grid is None or grid.header.frame_id != update.header.frame_id
                    or update.x < 0 or update.y < 0
                    or update.width == 0 or update.height == 0
                    or update.x + update.width > grid.info.width
                    or update.y + update.height > grid.info.height
                    or len(update.data) != update.width * update.height):
                return
            for row in range(update.height):
                start = (row + update.y) * grid.info.width + update.x
                patch = row * update.width
                grid.data[start:start + update.width] = (
                    update.data[patch:patch + update.width])
            grid.header.stamp = update.header.stamp
            self.costmap_received = time.monotonic()

    def _receive_image(self, message):
        # Keep only the latest image. No image processing or coverage work idle.
        if message.width and message.height and len(message.data):
            with self.lock:
                self.image = message
                self.image_received = time.monotonic()

    def _receive_info(self, message):
        with self.lock:
            self.camera_info = message

    def _robot_xy(self):
        transform = self.tf.lookup_transform(
            self.map_message.header.frame_id, self.settings['base_frame'], Time())
        p = transform.transform.translation
        return p.x, p.y

    def _ready(self):
        now = time.monotonic()
        if self.map_message is None or not self.map_message.header.frame_id:
            return 'Saved /map is not available'
        if not self.map_message.data or self.map_message.info.resolution <= 0:
            return 'Saved /map is empty or invalid'
        if (self.image is None or self.camera_info is None
                or self.camera_info.k[0] <= 0 or not self.camera_info.width):
            return 'RGB image and calibrated CameraInfo are required'
        if now - self.image_received > self.settings['sensor_timeout_s']:
            return 'RGB camera is stale'
        if (self.costmap is None or now - self.costmap_received
                > self.settings['costmap_timeout_s']):
            return 'Global costmap is not available or stale'
        if self.costmap.header.frame_id != self.map_message.header.frame_id:
            return 'Global costmap and saved map must use the same frame'
        if (not self.navigation.server_is_ready()
                or not self.spin_client.server_is_ready()):
            return 'Nav2 NavigateToPose and Spin servers must be active'
        try:
            self._robot_xy()
        except TransformException:
            return 'Robot localization TF is not available'
        return ''

    def _goal(self, goal):
        with self.lock:
            reason = ''
            if goal.thoroughness not in (Patrol.Goal.LIGHT, Patrol.Goal.NORMAL,
                                         Patrol.Goal.THOROUGH):
                reason = 'Unknown thoroughness'
            elif self.busy or (self.child is not None and not self.child.settled):
                reason = 'Previous patrol or Nav2 cancellation is still active'
            else:
                reason = self._ready()
            if reason:
                self.get_logger().warning(f'Patrol rejected: {reason}')
                return GoalResponse.REJECT
            self.busy = True
            self.stop_event.clear()
            self.map_changed = False
            return GoalResponse.ACCEPT

    def _cancel(self, _goal_handle):
        # ROS changes to CANCELING after this callback returns ACCEPT.
        # The execute loop tests is_cancel_requested after the transition.
        if self.child is not None:
            self.child.cancel()
        return CancelResponse.ACCEPT

    def request_shutdown(self):
        """Cancel outstanding robot motion before shutting down the node."""
        self.stop_event.set()
        if self.child is not None:
            self.child.cancel()

    def _observe(self):
        with self.lock:
            image = self.image
            info = self.camera_info
        stamp = (image.header.stamp.sec, image.header.stamp.nanosec)
        if stamp == self.last_image_stamp:
            return
        image_time = Time.from_msg(image.header.stamp)
        age = (self.get_clock().now() - image_time).nanoseconds / 1e9
        if age < -0.1 or age > self.settings['sensor_timeout_s']:
            return
        optical_frame = self.settings['camera_optical_frame']
        if not optical_frame and image.header.frame_id != info.header.frame_id:
            return
        try:
            transform = self.tf.lookup_transform(
                self.map_message.header.frame_id,
                optical_frame or image.header.frame_id,
                image_time)
        except TransformException:
            return
        q = transform.transform.rotation
        # CameraInfo uses an optical frame: +Z is the viewing direction.
        forward_x = 2.0 * (q.x * q.z + q.w * q.y)
        forward_y = 2.0 * (q.y * q.z - q.w * q.x)
        if math.hypot(forward_x, forward_y) < 1e-6:
            return
        fov = 2.0 * math.atan2(info.width, 2.0 * info.k[0])
        p = transform.transform.translation
        self.planner.mark_observed(p.x, p.y, math.atan2(forward_y, forward_x), fov)
        self.last_image_stamp = stamp
        self.last_observation = time.monotonic()

    def _pump(self, handle):
        if self.stop_event.is_set() or handle.is_cancel_requested or not rclpy.ok():
            raise PatrolInterrupted('Patrol canceled')
        if self.map_changed:
            raise PatrolInterrupted('Saved map changed; start a new patrol on that map')
        reason = self._ready()
        if reason:
            raise PatrolInterrupted(reason)
        now = time.monotonic()
        if now - self.last_pump >= 1.0 / self.settings['observation_hz']:
            self.last_pump = now
            self._observe()
        if now - self.last_observation > self.settings['sensor_timeout_s']:
            raise PatrolInterrupted('No current camera frame with matching TF')
        if now - self.last_feedback >= 1.0:
            self.last_feedback = now
            feedback = Patrol.Feedback()
            feedback.state = self.phase
            feedback.coverage_ratio = float(self.planner.coverage_ratio)
            feedback.viewpoints_visited = self.visited
            handle.publish_feedback(feedback)
            self._publish_status(self.phase.lower(), '')

    def _allowed(self, x, y):
        message = self.costmap
        origin = message.info.origin
        angle = _yaw(origin.orientation)
        dx, dy = x - origin.position.x, y - origin.position.y
        resolution = message.info.resolution
        if resolution <= 0:
            return False
        col = math.floor((math.cos(angle) * dx + math.sin(angle) * dy) / resolution)
        row = math.floor((-math.sin(angle) * dx + math.cos(angle) * dy) / resolution)
        if not (0 <= col < message.info.width and 0 <= row < message.info.height):
            return False
        cost = message.data[row * message.info.width + col]
        return 0 <= cost <= self.settings['maximum_goal_cost']

    def _run_child(self, client, request, handle, timeout):
        self.child = ChildAction(client, request)
        operation = self.child
        while not operation.settled:
            self._pump(handle)
            elapsed = time.monotonic() - operation.sent_at
            if operation.error is not None:
                raise PatrolInterrupted(f'Nav2 communication failed: {operation.error}')
            if (operation.handle is None and elapsed
                    > self.settings['goal_response_timeout_s']):
                raise PatrolInterrupted('Nav2 goal response timeout')
            if elapsed > timeout:
                if not self._settle_child():
                    raise PatrolInterrupted('Nav2 did not confirm cancellation')
                return False
            operation.wake.wait(1.0 / self.settings['observation_hz'])
            operation.wake.clear()
        self._pump(handle)
        if operation.result is None:
            return False
        if operation.result.status == GoalStatus.STATUS_CANCELED:
            raise PatrolInterrupted('Nav2 goal was canceled externally')
        return operation.result.status == GoalStatus.STATUS_SUCCEEDED

    def _settle_child(self):
        if self.child is None or self.child.settled:
            return True
        self.phase = 'CANCELING'
        self._publish_status('stopping', 'Waiting for Nav2 to stop')
        self.child.cancel()
        deadline = time.monotonic() + self.settings['cancel_completion_timeout_s']
        warned = False
        while not self.child.settled and rclpy.ok():
            if not warned and time.monotonic() > deadline:
                warned = True
                self.get_logger().error(
                    'Nav2 stop is unconfirmed; retaining patrol ownership')
                self._publish_status('stopping',
                                     'Nav2 stop unconfirmed; retaining ownership')
            self.child.wake.wait(0.05)
            self.child.wake.clear()
        return self.child.settled

    def _execute(self, handle):
        self.visited = 0
        self.planner = None
        self.phase = 'PLANNING'
        self.last_image_stamp = None
        self.last_feedback = 0.0
        self.last_observation = time.monotonic()
        success = False
        detail = ''
        try:
            self._publish_status('planning', 'Computing reachable viewpoints')
            rooms = None
            room_file = self.settings['room_map_file']
            if room_file:
                rooms = json.loads(Path(room_file).expanduser().read_text(encoding='utf-8'))
            info = self.camera_info
            self.planner = CoveragePlanner(
                _grid(self.map_message), self.profiles[handle.request.thoroughness],
                self._robot_xy(), self.settings['robot_clearance_m'], rooms,
                2.0 * math.atan2(info.width, 2.0 * info.k[0]))
            self.last_observation = time.monotonic()
            while True:
                self._pump(handle)
                if self.planner.complete:
                    success = True
                    detail = 'Requested coverage and reachable room visits completed'
                    break
                viewpoint = self.planner.select(self._robot_xy(), self._allowed)
                if viewpoint is None:
                    detail = 'No usable untried viewpoint remains; partial coverage returned'
                    break
                self.planner.mark_attempted(viewpoint.index)
                request = NavigateToPose.Goal()
                request.pose = PoseStamped()
                request.pose.header.frame_id = self.map_message.header.frame_id
                request.pose.header.stamp = self.get_clock().now().to_msg()
                request.pose.pose.position.x = float(viewpoint.x)
                request.pose.pose.position.y = float(viewpoint.y)
                request.pose.pose.orientation.z = math.sin(viewpoint.yaw * 0.5)
                request.pose.pose.orientation.w = math.cos(viewpoint.yaw * 0.5)
                self.phase = 'NAVIGATING'
                if not self._run_child(self.navigation, request, handle,
                                       self.settings['navigation_timeout_s']):
                    continue
                self.visited += 1
                self.phase = 'OBSERVING'
                request = Spin.Goal()
                request.target_yaw = 2.0 * math.pi
                allowance = self.settings['spin_time_allowance_s']
                request.time_allowance = Duration(seconds=allowance).to_msg()
                self._run_child(self.spin_client, request, handle, allowance + 5.0)
        except (PatrolInterrupted, ValueError, OSError, TransformException) as error:
            detail = str(error)
        except Exception as error:
            self.get_logger().error(f'Patrol failed: {error}')
            detail = f'Patrol failed: {error}'
        finally:
            settled = self._settle_child()
            result = Patrol.Result()
            result.success = success and settled and not handle.is_cancel_requested
            result.coverage_ratio = float(self.planner.coverage_ratio) if self.planner else 0.0
            result.viewpoints_visited = self.visited
            result.message = detail
            if not settled:
                result.message += '; Nav2 stop unconfirmed; new patrols blocked until settled'
            if handle.is_cancel_requested and settled:
                handle.canceled()
                state = 'idle'
            elif result.success:
                handle.succeed()
                state = 'completed'
            else:
                handle.abort()
                state = 'aborted'
            self._publish_status(state, result.message)
            with self.lock:
                self.busy = False
            return result

    def _publish_status(self, state, detail):
        message = String()
        message.data = json.dumps({
            'state': state, 'detail': detail,
            'coverage_ratio': float(self.planner.coverage_ratio) if self.planner else 0.0,
            'viewpoints_visited': self.visited,
            'unvisited_rooms': list(self.planner.unvisited_room_names)
            if self.planner else [],
            'inaccessible_rooms': list(self.planner.inaccessible_room_names)
            if self.planner else [],
        })
        self.status.publish(message)


def main(args=None):
    """Keep the Action server idle until requested and cancel on shutdown."""
    # Keep ROS communication alive long enough to cancel Nav2 on SIGINT/TERM.
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, interrupt)
    node = PatrolManager()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.request_shutdown()
    finally:
        node.request_shutdown()
        while rclpy.ok() and (node.busy or (
                node.child is not None and not node.child.settled)):
            executor.spin_once(timeout_sec=0.1)
        executor.shutdown()
        node.server.destroy()
        node.destroy_node()
        signal.signal(signal.SIGTERM, previous_term)
        if rclpy.ok():
            rclpy.shutdown()
