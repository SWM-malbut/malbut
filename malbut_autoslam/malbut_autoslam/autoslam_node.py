"""Expose existing frontier exploration as one safely cancellable Action."""

import math
from pathlib import Path
import signal
import threading
import time

from action_msgs.msg import GoalStatus, GoalStatusArray
from geometry_msgs.msg import Twist
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from malbut_interfaces.action import AutoSlam
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from nav2_msgs.srv import SaveMap
from nav_msgs.msg import OccupancyGrid, Odometry
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from tf2_ros import Buffer, TransformException, TransformListener

from malbut_autoslam.frontier import (
    blocked_approach, map_grid_from_message, map_statistics, path_avoids_blocks,
    path_is_known_free, point_has_clearance, search_frontiers,
)
from malbut_autoslam.runtime import (
    DEFAULT_READY_TIMEOUT_S, OwnedRuntime, RuntimeGraph, missing_components,
)
from malbut_autoslam.saved_pose import write_mapping_pose


class Interrupted(RuntimeError):
    """Execution was canceled or the server is shutting down."""


def _pose_xy_yaw(position, orientation):
    quaternion = (orientation.x, orientation.y, orientation.z, orientation.w)
    norm = math.hypot(*quaternion)
    if (not all(math.isfinite(value) for value in (*quaternion, position.x, position.y, norm))
            or norm < 1e-6):
        raise ValueError('invalid planar pose')
    x, y, z, w = (value / norm for value in quaternion)
    return position.x, position.y, math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def map_base(directory, name):
    """Resolve a filename without allowing traversal or replacing saved maps."""
    if (not name or name in ('.', '..') or '/' in name or '\\' in name
            or '\x00' in name or name.endswith(('.yaml', '.pgm'))):
        raise ValueError('map_name must be a filename without a path or extension')
    base = Path(directory).expanduser().resolve() / name
    if any(Path(str(base) + suffix).exists() for suffix in ('.yaml', '.pgm', '.pose.yaml')):
        raise ValueError(f'map already exists: {base}; choose another map_name')
    return base


class Navigation:
    """Retain a child goal until its terminal result, including late acceptance."""

    def __init__(self, client, request):
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.handle = None
        self.result = None
        self.error = None
        self.cancel_requested = False
        self.cancel_sent = False
        client.send_goal_async(request).add_done_callback(self._accepted)

    def _accepted(self, future):
        with self.lock:
            try:
                self.handle = future.result()
                if self.handle.accepted:
                    self.handle.get_result_async().add_done_callback(self._finished)
                    if self.cancel_requested:
                        self.cancel()
                else:
                    self.done.set()
            except Exception as error:
                # An uncertain transport result must not release BASE ownership.
                self.error = error

    def _finished(self, future):
        with self.lock:
            try:
                self.result = future.result()
                self.done.set()
            except Exception as error:
                self.error = error

    def cancel(self):
        """Request cancellation now, or immediately after a pending acceptance."""
        with self.lock:
            self.cancel_requested = True
            if (self.handle is not None and self.handle.accepted
                    and not self.done.is_set() and not self.cancel_sent):
                self.cancel_sent = True
                try:
                    self.handle.cancel_goal_async()
                except Exception as error:
                    self.error = error


class AutoSlamNode(Node):
    """Explore a live map through Nav2; save it using the standard map saver."""

    def __init__(self, **kwargs):
        super().__init__('autoslam', **kwargs)
        defaults = {
            'map_topic': '/map', 'base_frame': 'base_footprint',
            'navigation_action': '/navigate_to_pose',
            'planning_action': '/compute_path_to_pose',
            'save_map_service': '/autoslam_map_saver/save_map',
            'map_directory': str(Path.home() / '.ros/malbut/maps'),
            'minimum_frontier_cells': 8, 'robot_clearance_m': 0.30,
            'minimum_goal_distance_m': 0.45,
            'exploration_period_s': 1.0, 'completion_delay_s': 12.0,
            'map_timeout_s': 10.0, 'tf_timeout_s': 3.0,
            'ready_timeout_s': DEFAULT_READY_TIMEOUT_S, 'navigation_timeout_s': 90.0,
            'progress_timeout_s': 5.0, 'progress_distance_m': 0.05,
            'progress_angle_rad': 0.15,
            'max_exploration_time_s': 1200.0,
            'auto_start': False,
            'scan_topic': '/scan_raw', 'odom_topic': '/odom',
            'cmd_vel_topic': '/cmd_vel', 'progress_odom_topic': '/odom_rf2o',
            'runtime_directory': str(Path.home() / '.ros/malbut/autoslam'),
            'sensor_timeout_s': 3.0,
        }
        for name, default in defaults.items():
            self.declare_parameter(name, default)
        self.settings = {name: self.get_parameter(name).value for name in defaults}
        for name in defaults:
            if name.endswith(('_s', '_m', '_rad', '_cells')):
                value = self.settings[name]
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f'{name} must be positive and finite')
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.busy = False
        self.save_uncertain = False
        self.message = None
        self.received_at = 0.0
        self.map_revision = 0
        self.child = None
        self.planned_path = []
        self.blocked_approaches = []
        self.blocked_targets = []
        self.runtime = None
        self.scan_received_at = 0.0
        self.odom_received_at = 0.0
        self.command = None
        self.command_received_at = 0.0
        self.progress_odom = None
        self.progress_odom_received_at = 0.0
        self.navigation_busy = False
        self.known_area_m2 = 0.0
        self.frontier_count = 0
        self.group = ReentrantCallbackGroup()
        self.create_subscription(
            Twist, self.settings['cmd_vel_topic'], self._receive_command,
            qos_profile_sensor_data, callback_group=self.group)
        self.create_subscription(
            Odometry, self.settings['progress_odom_topic'], self._receive_progress_odom,
            qos_profile_sensor_data, callback_group=self.group)
        self.lifecycle_clients = {}
        if self.settings['auto_start']:
            if self.get_parameter('use_sim_time').value:
                raise ValueError(
                    'auto_start requires real hardware; use auto_start:=false in simulation')
            self.create_subscription(
                LaserScan, self.settings['scan_topic'], self._receive_scan,
                qos_profile_sensor_data, callback_group=self.group)
            self.create_subscription(
                Odometry, self.settings['odom_topic'], self._receive_odom,
                qos_profile_sensor_data, callback_group=self.group)
            self.create_subscription(
                GoalStatusArray, self.settings['navigation_action'] + '/_action/status',
                self._receive_navigation_status,
                QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
                callback_group=self.group)
            for name in ('controller_server', 'planner_server', 'bt_navigator'):
                self.lifecycle_clients[name] = self.create_client(
                    GetState, f'/{name}/get_state', callback_group=self.group)
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self)
        self.create_subscription(
            OccupancyGrid, self.settings['map_topic'], self._receive_map,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
            callback_group=self.group,
        )
        self.navigation = ActionClient(
            self, NavigateToPose, self.settings['navigation_action'],
            callback_group=self.group,
        )
        self.planner = ActionClient(
            self, ComputePathToPose, self.settings['planning_action'],
            callback_group=self.group,
        )
        self.saver = self.create_client(
            SaveMap, self.settings['save_map_service'], callback_group=self.group)
        self.server = ActionServer(
            self, AutoSlam, '/autoslam', execute_callback=self._execute,
            goal_callback=self._goal, cancel_callback=self._cancel,
            callback_group=self.group,
        )

    def _receive_map(self, message):
        with self.lock:
            self.message = message
            self.received_at = time.monotonic()
            self.map_revision += 1
        self.wake.set()

    def _receive_scan(self, _message):
        self.scan_received_at = time.monotonic()

    def _receive_odom(self, _message):
        self.odom_received_at = time.monotonic()

    def _receive_command(self, message):
        with self.lock:
            self.command, self.command_received_at = message, time.monotonic()

    def _receive_progress_odom(self, message):
        with self.lock:
            self.progress_odom, self.progress_odom_received_at = message, time.monotonic()

    def _command_mode(self):
        with self.lock:
            command, received = self.command, self.command_received_at
        if command is None or time.monotonic() - received > self.settings['sensor_timeout_s']:
            return None
        x, y, yaw = command.linear.x, command.linear.y, command.angular.z
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            return None
        # Ignore zero/tiny commands: a deliberate Nav2 wait is not physical blockage.
        timeout = self.settings['progress_timeout_s']
        if math.hypot(x, y) * timeout >= self.settings['progress_distance_m']:
            return 'translation'
        if abs(yaw) * timeout >= self.settings['progress_angle_rad']:
            return 'rotation'
        return None

    def _progress_pose(self, map_frame, fallback_pose):
        # Reuse the driver's scan odometry; do not start a second estimator/TF publisher.
        with self.lock:
            odom, received = self.progress_odom, self.progress_odom_received_at
        timeout = self.settings['sensor_timeout_s']
        if (odom is not None and odom.header.frame_id and odom.child_frame_id
                and time.monotonic() - received <= timeout):
            age = (self.get_clock().now() - Time.from_msg(odom.header.stamp)).nanoseconds / 1e9
            if abs(age) <= timeout:
                try:
                    pose = _pose_xy_yaw(odom.pose.pose.position, odom.pose.pose.orientation)
                    return ('odom', odom.header.frame_id, odom.child_frame_id), pose
                except ValueError:
                    pass
        return ('tf', map_frame), fallback_pose

    def _receive_navigation_status(self, message):
        self.navigation_busy = any(status.status in (
            GoalStatus.STATUS_ACCEPTED, GoalStatus.STATUS_EXECUTING,
            GoalStatus.STATUS_CANCELING) for status in message.status_list)

    def _runtime_graph(self):
        def full_name(name, namespace):
            return namespace.rstrip('/') + '/' + name

        def publishers(topic):
            return tuple(sorted(full_name(info.node_name, info.node_namespace)
                                for info in self.get_publishers_info_by_topic(topic)))

        return RuntimeGraph(
            nodes=tuple(sorted(full_name(name, namespace)
                               for name, namespace in self.get_node_names_and_namespaces())),
            map_publishers=publishers(self.settings['map_topic']),
            scan_publishers=publishers(self.settings['scan_topic']),
            odom_publishers=publishers(self.settings['odom_topic']),
            navigation_present=self.navigation.server_is_ready(),
        )

    def _prepare_runtime(self, handle):
        if not self.settings['auto_start']:
            return
        self.runtime = OwnedRuntime(self.settings['runtime_directory'])
        self.runtime.acquire()
        # DDS discovery is asynchronous. Take a settled graph snapshot rather
        # than launching a second stack immediately after this server starts.
        previous = None
        stable_since = time.monotonic()
        deadline = stable_since + self.settings['ready_timeout_s']
        while time.monotonic() < deadline:
            self._check(handle)
            graph = self._runtime_graph()
            if graph != previous:
                previous = graph
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= 1.0:
                break
            self._feedback(handle, 'WAITING')
            self._pause()
        else:
            raise RuntimeError('ROS graph did not settle; mapping startup was not attempted')
        if self.navigation_busy:
            raise RuntimeError('Nav2 has an active goal; cancel its owner before AutoSLAM')
        components = missing_components(graph)
        if components['start_slam']:
            with self.lock:
                self.message = None  # Never reuse a map from the previous owned session.
        self.runtime.start(
            components, self.settings['scan_topic'], self.settings['odom_topic'])
        if self.runtime.log_path is not None:
            self.get_logger().info(f'Mapping prerequisite log: {self.runtime.log_path}')

    def _wait_active_navigation(self, handle, deadline):
        if not self.settings['auto_start']:
            return
        for name, client in self.lifecycle_clients.items():
            future = None
            requested_at = 0.0
            try:
                while True:
                    self._check(handle)
                    now = time.monotonic()
                    if now >= deadline:
                        raise RuntimeError(
                            f'mapping prerequisites not ready: {name} is not active')
                    if (future is not None and not future.done()
                            and now - requested_at >= self.settings['sensor_timeout_s']):
                        # GetState is read-only: retry a lost reply within the
                        # existing startup deadline, without restarting Nav2.
                        client.remove_pending_request(future)
                        future.cancel()
                        future = None
                    if future is None and client.service_is_ready():
                        future = client.call_async(GetState.Request())
                        requested_at = now
                    if future is not None and future.done():
                        response = future.result()
                        if response.current_state.id == State.PRIMARY_STATE_ACTIVE:
                            break
                        future = None
                    self._feedback(handle, 'WAITING')
                    self._pause()
            finally:
                if future is not None and not future.done():
                    client.remove_pending_request(future)
                    future.cancel()

    def _check_sensor_updates(self):
        if not self.settings['auto_start']:
            return
        now = time.monotonic()
        if (now - self.scan_received_at > self.settings['sensor_timeout_s']
                or now - self.odom_received_at > self.settings['sensor_timeout_s']):
            raise RuntimeError('waiting for fresh LiDAR and odometry')

    def _goal(self, request):
        with self.lock:
            if self.busy or self.stopping.is_set() or self.save_uncertain:
                return GoalResponse.REJECT
            try:
                map_base(self.settings['map_directory'], request.map_name)
            except ValueError as error:
                self.get_logger().warning(str(error))
                return GoalResponse.REJECT
            self.busy = True
        return GoalResponse.ACCEPT

    def _cancel(self, _handle):
        self.wake.set()
        return CancelResponse.ACCEPT

    def _check(self, handle):
        if handle.is_cancel_requested or self.stopping.is_set():
            raise Interrupted('automatic mapping canceled')
        if self.runtime is not None:
            self.runtime.check()

    def _pause(self):
        self.wake.wait(0.2)
        self.wake.clear()

    def _snapshot(self, with_heading=False):
        self._check_sensor_updates()
        with self.lock:
            message, received = self.message, self.received_at
        if message is None or not message.header.frame_id:
            raise RuntimeError('waiting for a live SLAM map')
        if time.monotonic() - received > self.settings['map_timeout_s']:
            raise RuntimeError('SLAM map updates stopped')
        transform = self.tf.lookup_transform(
            message.header.frame_id, self.settings['base_frame'], Time())
        stamp = Time.from_msg(transform.header.stamp)
        age = (self.get_clock().now() - stamp).nanoseconds / 1e9
        if abs(age) > self.settings['tf_timeout_s']:
            raise RuntimeError('robot transform is stale or too far in the future')
        position = transform.transform.translation
        if not all(math.isfinite(value) for value in (position.x, position.y)):
            raise RuntimeError('robot position is not finite')
        if with_heading:
            return message, _pose_xy_yaw(position, transform.transform.rotation)
        return message, (position.x, position.y)

    def _feedback(self, handle, state, grid=None, frontier_count=None):
        feedback = AutoSlam.Feedback()
        feedback.state = state
        if frontier_count is not None:
            self.frontier_count = frontier_count
        feedback.frontier_count = self.frontier_count
        if grid is not None:
            self.known_area_m2 = map_statistics(grid)['known_area_m2']
        feedback.known_area_m2 = self.known_area_m2
        handle.publish_feedback(feedback)

    def _settle_child(self, handle):
        child = self.child
        if child is None:
            return
        child.cancel()
        deadline = time.monotonic() + self.settings['ready_timeout_s']
        while not child.done.wait(0.2):
            self._feedback(handle, 'CANCELING')
            if (time.monotonic() >= deadline and self.runtime is not None
                    and self.runtime.components.get('start_navigation')):
                # Only a Nav2 process group owned by this request can be stopped
                # when its Action transport no longer returns a terminal result.
                try:
                    self.runtime.stop()
                    break
                except RuntimeError as error:
                    self.get_logger().error(str(error))
                    self.stopping.set()
                    # An unkillable owned process must not release the Action's
                    # BASE resource. Retain ownership until cleanup can finish.
        self.child = None

    def _close_runtime(self, handle):
        if self.runtime is None:
            return
        while True:
            if self.runtime.process is not None:
                self._feedback(handle, 'CANCELING')
            try:
                self.runtime.close()
                self.runtime = None
                return
            except RuntimeError as error:
                self.get_logger().error(str(error))
                self.stopping.set()
                # Do not send a terminal Action result while an owned process
                # could still publish commands. Each shutdown attempt is bounded.
                self._pause()

    def _target_pose(self, frontier, frame):
        request = NavigateToPose.Goal()
        request.pose.header.frame_id = frame
        request.pose.header.stamp = self.get_clock().now().to_msg()
        request.pose.pose.position.x = frontier.x
        request.pose.pose.position.y = frontier.y
        request.pose.pose.orientation.z = math.sin(frontier.yaw / 2.0)
        request.pose.pose.orientation.w = math.cos(frontier.yaw / 2.0)
        return request.pose

    def _can_reach(self, handle, frontier, frame):
        # Let the actual Nav2 costmap/robot footprint decide path feasibility.
        # Planning is read-only: an uncertain planning result never sends motion.
        self.planned_path = []
        request = ComputePathToPose.Goal()
        request.goal = self._target_pose(frontier, frame)
        request.use_start = False
        self._check(handle)
        planning = Navigation(self.planner, request)
        deadline = time.monotonic() + self.settings['ready_timeout_s']
        try:
            while not planning.done.wait(0.2):
                self._check(handle)
                self._snapshot()
                if planning.error:
                    raise RuntimeError(f'Nav2 planning transport error: {planning.error}')
                if time.monotonic() >= deadline:
                    raise RuntimeError('Nav2 path planning did not respond in time')
                self._feedback(handle, 'EXPLORING')
            result = planning.result
            self._check(handle)
            if result is None:
                raise RuntimeError('Nav2 planner rejected the planning request')
            if result.status != GoalStatus.STATUS_SUCCEEDED:
                return False
            path = result.result.path
            if path.header.frame_id != frame or not path.poses:
                return False
            points = [(pose.pose.position.x, pose.pose.position.y) for pose in path.poses]
            # Navfn may accept an endpoint up to its tolerance away from the
            # request. Do not mistake a path ending on this side of a wall for
            # a route to the frontier on the other side.
            message, robot = self._snapshot()
            if message.header.frame_id != frame:
                return False
            grid = map_grid_from_message(message)
            if math.hypot(points[-1][0] - frontier.x, points[-1][1] - frontier.y) > max(
                    grid.resolution, self.settings['robot_clearance_m']):
                return False
            # SLAM may have discovered a wall while the planner was running.
            # Recheck both the requested goal and Navfn's tolerated endpoint;
            # a free center cell alone does not preserve the approach margin.
            if not all(point_has_clearance(grid, point, self.settings['robot_clearance_m'])
                       for point in (points[-1], (frontier.x, frontier.y))):
                return False
            points = [robot, *points, (frontier.x, frontier.y)]
            if (not path_is_known_free(grid, points)
                    or not path_avoids_blocks(points, self.blocked_approaches)):
                return False
            self.planned_path = points
            return True
        finally:
            planning.cancel()  # Also cancels a goal that is accepted after timeout.

    def _navigate(self, handle, frontier, frame, run_deadline):
        request = NavigateToPose.Goal()
        request.pose = self._target_pose(frontier, frame)
        self._check(handle)
        self.child = Navigation(self.navigation, request)
        deadline = min(run_deadline,
                       time.monotonic() + self.settings['navigation_timeout_s'])
        baseline = None
        baseline_key = None
        last_progress = time.monotonic()
        while not self.child.done.wait(0.2):
            self._check(handle)
            message, pose = self._snapshot(with_heading=True)
            if message.header.frame_id != frame:
                raise RuntimeError('SLAM frame changed during navigation')
            if self.child.error:
                raise RuntimeError(f'Nav2 transport error: {self.child.error}')
            self._feedback(handle, 'NAVIGATING')
            now = time.monotonic()
            mode = self._command_mode()
            source, pose = self._progress_pose(frame, pose)
            key = (source, mode)
            # A bump alone does not matter. Compare the requested type of motion
            # with observed progress only while fresh nonzero commands persist.
            if mode is None or self.child.handle is None or not self.child.handle.accepted:
                baseline = None
            else:
                if baseline_key != key:
                    baseline = None  # Never compare RF2O and map-frame origins.
                    if baseline_key is None or baseline_key[0] != source:
                        self.get_logger().info(f'Motion progress source: {source}')
                baseline_key = key
                angle = (0.0 if baseline is None else
                         math.atan2(math.sin(pose[2] - baseline[2]),
                                    math.cos(pose[2] - baseline[2])))
                if (baseline is None
                        or (mode == 'translation' and math.dist(pose[:2], baseline[:2])
                            >= self.settings['progress_distance_m'])
                        or (mode == 'rotation' and abs(angle)
                            >= self.settings['progress_angle_rad'])):
                    baseline, last_progress = pose, now
                elif now - last_progress >= self.settings['progress_timeout_s']:
                    self.get_logger().warning(
                        f'Commanded {mode} without progress for {now - last_progress:.1f}s '
                        f'({source}); '
                        'canceling and excluding this approach for the current mapping run')
                    child = self.child
                    self._settle_child(handle)
                    self._check(handle)
                    if (child.result is not None
                            and child.result.status == GoalStatus.STATUS_SUCCEEDED):
                        return True  # Arrival raced with the cancellation request.
                    self.blocked_targets.append((frontier.x, frontier.y))
                    stopped_map, stopped_pose = self._snapshot()
                    if stopped_map.header.frame_id != frame:
                        raise RuntimeError('SLAM frame changed while stopping navigation')
                    block = blocked_approach(
                        self.planned_path, stopped_pose, self.settings['robot_clearance_m'])
                    if block is not None:
                        self.blocked_approaches.append(block)
                    return False
            if time.monotonic() >= deadline:
                self._settle_child(handle)
                return False
        result = self.child.result
        self.child = None
        self._check(handle)
        return result is not None and result.status == GoalStatus.STATUS_SUCCEEDED

    def _observe(self, handle):
        # A Nav2 result does not mean SLAM has published the observations from
        # arrival yet. Wait for a new map before deciding whether this visit
        # discovered anything or the run has finished.
        with self.lock:
            revision = self.map_revision
        started = time.monotonic()
        while True:
            self._check(handle)
            message, pose = self._snapshot()
            now = time.monotonic()
            with self.lock:
                updated = self.map_revision > revision
            if updated and now - started >= self.settings['exploration_period_s']:
                return message, pose
            if now - started >= self.settings['map_timeout_s']:
                raise RuntimeError('SLAM did not publish a new map after navigation')
            self._feedback(handle, 'EXPLORING')
            self._pause()

    def _save(self, handle, base):
        self._check(handle)
        map_base(base.parent, base.name)  # Recheck after a potentially long run.
        base.parent.mkdir(parents=True, exist_ok=True)
        request = SaveMap.Request()
        request.map_topic = self.settings['map_topic']
        request.map_url = str(base)
        request.image_format = 'pgm'
        request.map_mode = 'trinary'
        request.free_thresh = 0.196
        request.occupied_thresh = 0.65
        future = self.saver.call_async(request)
        done = threading.Event()
        future.add_done_callback(lambda _future: done.set())
        deadline = time.monotonic() + self.settings['ready_timeout_s']
        while not done.wait(0.2):
            # Saving is a non-cancellable Service. Wait for its actual response.
            if self.stopping.is_set() or time.monotonic() >= deadline:
                # Parent launch also stops map_saver on SIGINT, so a response
                # may never arrive. Still run owned mapping cleanup in finally.
                self.saver.remove_pending_request(future)
                future.cancel()
                # Keep ROS alive to deliver the failure result, but reject new
                # goals until an operator resolves the possibly delayed write.
                self.save_uncertain = True
                self.get_logger().error(
                    'Map save result is unconfirmed; restart after checking files')
                raise Interrupted(
                    'map save result is unconfirmed; check map files before restarting AutoSLAM')
            self._feedback(handle, 'CANCELING' if handle.is_cancel_requested
                           else 'SAVING')
        try:
            response = future.result()
        except Exception as error:
            self.save_uncertain = True
            raise RuntimeError(
                'map save result is unconfirmed; '
                'check map files before restarting AutoSLAM') from error
        if not response.result:
            raise RuntimeError('Nav2 map saver failed')
        yaml_path = Path(str(base) + '.yaml')
        if not yaml_path.is_file() or not Path(str(base) + '.pgm').is_file():
            raise RuntimeError('map saver returned without the expected map files')
        return str(yaml_path)

    def _save_pose(self, map_yaml):
        # Read after SaveMap completes, while the SLAM/odometry runtime is alive.
        transform = self.tf.lookup_transform('map', self.settings['base_frame'], Time())
        stamp = Time.from_msg(transform.header.stamp)
        age = (self.get_clock().now() - stamp).nanoseconds / 1e9
        if abs(age) > self.settings['tf_timeout_s']:
            raise RuntimeError('robot transform is stale or too far in the future')
        position = transform.transform.translation
        rotation = transform.transform.rotation
        components = (rotation.x, rotation.y, rotation.z, rotation.w)
        if not all(math.isfinite(value) for value in (
                position.x, position.y, position.z, *components)):
            raise ValueError('robot transform is not finite')
        norm = math.hypot(*components)
        if not math.isfinite(norm) or norm == 0.0:
            raise ValueError('robot transform has an invalid quaternion')
        x, y, z, w = (value / norm for value in components)
        yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
        write_mapping_pose(map_yaml, position.x, position.y, yaw)

    def _finish_mapping(self, handle, result, base, reason, grid):
        # A practical mapping run can finish with inaccessible/low-confidence
        # space left over. Save what was observed and report why exploration
        # stopped instead of claiming every part of the building was covered.
        self._feedback(handle, 'SAVING', grid)
        result.known_area_m2 = map_statistics(grid)['known_area_m2']
        result.map_yaml = self._save(handle, base)
        result.message = f'{reason}; navigation map saved'
        if self.frontier_count:
            result.message += f'; {self.frontier_count} frontier regions remain'
        try:
            self._save_pose(result.map_yaml)
            result.message += '; initial robot pose saved'
        except (OSError, ValueError, RuntimeError, TransformException) as error:
            result.message += (
                f'; WARNING: initial robot pose not saved ({error}); '
                'set RViz 2D Pose Estimate before navigation')
            self.get_logger().warning(result.message)
        self._check(handle)
        result.success = True

    def _explore(self, handle, result):
        base = map_base(self.settings['map_directory'], handle.request.map_name)
        self._prepare_runtime(handle)
        deadline = time.monotonic() + self.settings['ready_timeout_s']
        while True:
            self._check(handle)
            try:
                message, _pose = self._snapshot()
                map_grid_from_message(message)
                if (not self.navigation.server_is_ready()
                        or not self.planner.server_is_ready()
                        or not self.saver.service_is_ready()):
                    raise RuntimeError('waiting for Nav2 navigation/planner and map saver')
                break
            except (RuntimeError, ValueError, TransformException) as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'mapping prerequisites not ready: {error}')
                self._feedback(handle, 'WAITING')
                self._pause()

        self._wait_active_navigation(handle, deadline)

        blacklist = []
        retried = False
        empty_since = None
        empty_revision = None
        next_plan = 0.0
        known_high_water = map_statistics(map_grid_from_message(message))['known_cells']
        last_progress = time.monotonic()
        run_deadline = last_progress + self.settings['max_exploration_time_s']
        while True:
            self._check(handle)
            message, pose = self._snapshot()
            now = time.monotonic()
            if now < next_plan:
                self._pause()
                continue
            next_plan = now + self.settings['exploration_period_s']
            grid = map_grid_from_message(message)
            statistics = map_statistics(grid)
            result.known_area_m2 = statistics['known_area_m2']
            if (statistics['known_cells']
                    >= known_high_water + self.settings['minimum_frontier_cells']):
                known_high_water = statistics['known_cells']
                last_progress = now
                empty_since = None
            search = search_frontiers(
                grid, pose,
                minimum_cells=self.settings['minimum_frontier_cells'],
                minimum_clearance_m=self.settings['robot_clearance_m'],
                minimum_goal_distance_m=self.settings['minimum_goal_distance_m'],
                blacklisted=tuple(blacklist + self.blocked_targets),
            )
            candidates = search.candidates
            self._feedback(handle, 'EXPLORING', grid, search.frontier_count)
            if now >= run_deadline or now - last_progress >= self.settings['navigation_timeout_s']:
                reason = ('Exploration time budget reached' if now >= run_deadline
                          else 'No new mapped space within the progress limit')
                self._finish_mapping(handle, result, base, reason, grid)
                return
            if not candidates:
                if empty_since is None:
                    empty_since = now
                    with self.lock:
                        empty_revision = self.map_revision
                with self.lock:
                    updated = self.map_revision > empty_revision
                if updated and now - empty_since >= self.settings['completion_delay_s']:
                    # A transient obstacle may clear while other regions are
                    # explored. Retry ordinary failures once. Suspected blocked
                    # approaches remain excluded until the next AutoSLAM request.
                    if search.frontier_count and blacklist and not retried:
                        blacklist.clear()
                        retried = True
                        empty_since = None
                        continue
                    reason = (
                        'No remaining reachable frontiers' if not search.frontier_count
                        else 'Remaining frontiers have no usable approach or made no progress')
                    self._finish_mapping(handle, result, base, reason, grid)
                    return
                self._pause()
                continue
            empty_since = None
            target = candidates[0]
            if not self._can_reach(handle, target, message.header.frame_id):
                self.get_logger().info(
                    f'Skipping unreachable frontier ({target.x:.2f}, {target.y:.2f})')
                blacklist.append((target.x, target.y))
                continue
            if time.monotonic() >= run_deadline:
                self._finish_mapping(
                    handle, result, base, 'Exploration time budget reached', grid)
                return
            reached = self._navigate(handle, target, message.header.frame_id, run_deadline)
            observed, _pose = self._observe(handle)
            after = map_statistics(map_grid_from_message(observed))['known_cells']
            gained = after >= known_high_water + self.settings['minimum_frontier_cells']
            if gained:
                known_high_water = after
                last_progress = time.monotonic()
            if not reached or not gained:
                # Do not drop old failures after 32 entries: that resurrected
                # unreachable goals and allowed loops across a large map.
                blacklist.append((target.x, target.y))

    def _execute(self, handle):
        result = AutoSlam.Result()
        self.known_area_m2 = 0.0
        self.frontier_count = 0
        self.planned_path = []
        self.blocked_approaches = []
        self.blocked_targets = []
        try:
            self._explore(handle, result)
        except Interrupted as error:
            result.message = str(error)
        except Exception as error:
            result.message = str(error)
            self.get_logger().error(result.message)
        finally:
            # Do not report completion while an accepted or pending goal can move.
            self._settle_child(handle)
            self._close_runtime(handle)
            with self.lock:
                if handle.is_cancel_requested:
                    result.success = False
                    handle.canceled()
                elif result.success:
                    handle.succeed()
                else:
                    handle.abort()
                self.busy = False
        return result


def main(args=None):
    """Allow graceful Ctrl+C to cancel Nav2 while the executor is still alive."""
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = AutoSlamNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)

    def stop(_signum, _frame):
        node.stopping.set()
        node.wake.set()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        while rclpy.ok() and (not node.stopping.is_set() or node.busy):
            executor.spin_once(timeout_sec=0.2)
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
