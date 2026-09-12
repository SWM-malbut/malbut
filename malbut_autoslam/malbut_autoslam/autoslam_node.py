"""Expose existing frontier exploration as one safely cancellable Action."""

import math
from pathlib import Path
import signal
import threading
import time

from action_msgs.msg import GoalStatus, GoalStatusArray
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from malbut_interfaces.action import AutoSlam
from nav2_msgs.action import NavigateToPose
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
    find_frontiers, map_grid_from_message, map_statistics,
)
from malbut_autoslam.runtime import (
    DEFAULT_READY_TIMEOUT_S, OwnedRuntime, RuntimeGraph, missing_components,
)


class Interrupted(RuntimeError):
    """Execution was canceled or the server is shutting down."""


def map_base(directory, name):
    """Resolve a filename without allowing traversal or replacing saved maps."""
    if (not name or name in ('.', '..') or '/' in name or '\\' in name
            or '\x00' in name or name.endswith(('.yaml', '.pgm'))):
        raise ValueError('map_name must be a filename without a path or extension')
    base = Path(directory).expanduser().resolve() / name
    if any(Path(str(base) + suffix).exists() for suffix in ('.yaml', '.pgm')):
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
            'save_map_service': '/autoslam_map_saver/save_map',
            'map_directory': str(Path.home() / '.ros/malbut/maps'),
            'minimum_frontier_cells': 8, 'robot_clearance_m': 0.30,
            'minimum_goal_distance_m': 0.45,
            'exploration_period_s': 1.0, 'completion_delay_s': 12.0,
            'map_timeout_s': 10.0, 'tf_timeout_s': 3.0,
            'ready_timeout_s': DEFAULT_READY_TIMEOUT_S, 'navigation_timeout_s': 90.0,
            'auto_start': False,
            'scan_topic': '/scan_raw', 'odom_topic': '/odom',
            'normalized_scan_topic': '/scan_normalized',
            'runtime_directory': str(Path.home() / '.ros/malbut/autoslam'),
            'sensor_timeout_s': 3.0,
        }
        for name, default in defaults.items():
            self.declare_parameter(name, default)
        self.settings = {name: self.get_parameter(name).value for name in defaults}
        for name in defaults:
            if name.endswith(('_s', '_m', '_cells')):
                value = self.settings[name]
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f'{name} must be positive and finite')
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.busy = False
        self.message = None
        self.received_at = 0.0
        self.child = None
        self.runtime = None
        self.scan_received_at = 0.0
        self.odom_received_at = 0.0
        self.navigation_busy = False
        self.known_area_m2 = 0.0
        self.frontier_count = 0
        self.group = ReentrantCallbackGroup()
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
        self.wake.set()

    def _receive_scan(self, _message):
        self.scan_received_at = time.monotonic()

    def _receive_odom(self, _message):
        self.odom_received_at = time.monotonic()

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
            normalized_scan_publishers=publishers(self.settings['normalized_scan_topic']),
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
            components, self.settings['scan_topic'], self.settings['odom_topic'],
            self.settings['normalized_scan_topic'])
        if self.runtime.log_path is not None:
            self.get_logger().info(f'Mapping prerequisite log: {self.runtime.log_path}')

    def _wait_active_navigation(self, handle, deadline):
        if not self.settings['auto_start']:
            return
        for name, client in self.lifecycle_clients.items():
            future = None
            while True:
                self._check(handle)
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'mapping prerequisites not ready: {name} is not active')
                if future is None and client.service_is_ready():
                    future = client.call_async(GetState.Request())
                if future is not None and future.done():
                    response = future.result()
                    if response.current_state.id == State.PRIMARY_STATE_ACTIVE:
                        break
                    future = None
                self._feedback(handle, 'WAITING')
                self._pause()

    def _check_sensor_updates(self):
        if not self.settings['auto_start']:
            return
        now = time.monotonic()
        if (now - self.scan_received_at > self.settings['sensor_timeout_s']
                or now - self.odom_received_at > self.settings['sensor_timeout_s']):
            raise RuntimeError('waiting for fresh LiDAR and odometry')

    def _goal(self, request):
        with self.lock:
            if self.busy or self.stopping.is_set():
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

    def _snapshot(self):
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
        if age > self.settings['tf_timeout_s']:
            raise RuntimeError('robot transform is stale')
        position = transform.transform.translation
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

    def _navigate(self, handle, frontier, frame):
        request = NavigateToPose.Goal()
        request.pose.header.frame_id = frame
        request.pose.header.stamp = self.get_clock().now().to_msg()
        request.pose.pose.position.x = frontier.x
        request.pose.pose.position.y = frontier.y
        request.pose.pose.orientation.z = math.sin(frontier.yaw / 2.0)
        request.pose.pose.orientation.w = math.cos(frontier.yaw / 2.0)
        self._check(handle)
        self.child = Navigation(self.navigation, request)
        deadline = time.monotonic() + self.settings['navigation_timeout_s']
        while not self.child.done.wait(0.2):
            self._check(handle)
            self._snapshot()  # Loss of SLAM/TF cancels before relinquishing BASE.
            if self.child.error:
                raise RuntimeError(f'Nav2 transport error: {self.child.error}')
            self._feedback(handle, 'NAVIGATING')
            if time.monotonic() >= deadline:
                self._settle_child(handle)
                return False
        result = self.child.result
        self.child = None
        self._check(handle)
        return result is not None and result.status == GoalStatus.STATUS_SUCCEEDED

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
        while not done.wait(0.2):
            # Saving is a non-cancellable Service. Wait for its actual response.
            if self.stopping.is_set():
                # Parent launch also stops map_saver on SIGINT, so a response
                # may never arrive. Still run owned mapping cleanup in finally.
                raise Interrupted('server stopping; map save result is unconfirmed')
            self._feedback(handle, 'CANCELING' if handle.is_cancel_requested
                           else 'SAVING')
        response = future.result()
        if not response.result:
            raise RuntimeError('Nav2 map saver failed')
        yaml_path = Path(str(base) + '.yaml')
        if not yaml_path.is_file() or not Path(str(base) + '.pgm').is_file():
            raise RuntimeError('map saver returned without the expected map files')
        return str(yaml_path)

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
                        or not self.saver.service_is_ready()):
                    raise RuntimeError('waiting for Nav2 and map saver')
                break
            except (RuntimeError, ValueError, TransformException) as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'mapping prerequisites not ready: {error}')
                self._feedback(handle, 'WAITING')
                self._pause()

        self._wait_active_navigation(handle, deadline)

        blacklist = []
        completed = None
        repeat_visits = 0
        empty_since = None
        next_plan = 0.0
        while True:
            self._check(handle)
            message, pose = self._snapshot()
            now = time.monotonic()
            if now < next_plan:
                self._pause()
                continue
            next_plan = now + self.settings['exploration_period_s']
            grid = map_grid_from_message(message)
            result.known_area_m2 = map_statistics(grid)['known_area_m2']
            candidates = find_frontiers(
                grid, pose,
                minimum_cells=self.settings['minimum_frontier_cells'],
                minimum_clearance_m=self.settings['robot_clearance_m'],
                minimum_goal_distance_m=self.settings['minimum_goal_distance_m'],
                blacklisted=tuple(blacklist),
            )
            self._feedback(handle, 'EXPLORING', grid, len(candidates))
            if not candidates:
                if empty_since is None:
                    empty_since = now
                if now - empty_since >= self.settings['completion_delay_s']:
                    if map_statistics(grid)['free_area_m2'] <= 0:
                        raise RuntimeError('SLAM map contains no usable free space')
                    self._feedback(handle, 'SAVING', grid)
                    result.map_yaml = self._save(handle, base)
                    self._check(handle)
                    return
                self._pause()
                continue
            empty_since = None
            target = candidates[0]
            # Preserve the existing protection against unresolved frontier loops.
            if completed is not None:
                if math.hypot(target.x - completed[0], target.y - completed[1]) < 0.75:
                    repeat_visits += 1
                    if repeat_visits >= 2:
                        blacklist.append(completed)
                        blacklist = blacklist[-32:]
                        completed = None
                        repeat_visits = 0
                        continue
                else:
                    repeat_visits = 0
            completed = None
            if self._navigate(handle, target, message.header.frame_id):
                completed = (target.x, target.y)
            else:
                blacklist.append((target.x, target.y))
                blacklist = blacklist[-32:]

    def _execute(self, handle):
        result = AutoSlam.Result()
        self.known_area_m2 = 0.0
        self.frontier_count = 0
        try:
            self._explore(handle, result)
            result.success = True
            result.message = 'No more usable frontiers; navigation map saved'
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
