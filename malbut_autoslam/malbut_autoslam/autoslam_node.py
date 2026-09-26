"""Expose existing frontier exploration as one safely cancellable Action."""

import math
from pathlib import Path
import signal
import threading
import time

from action_msgs.msg import GoalStatus
from malbut_interfaces.action import AutoSlam
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from nav2_msgs.srv import SaveMap
from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.signals import SignalHandlerOptions
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener

from malbut_autoslam.frontier import (
    map_grid_from_message, map_statistics, path_is_known_free, point_has_clearance,
    search_frontiers,
)
from malbut_autoslam.saved_pose import write_mapping_pose


class Interrupted(RuntimeError):
    """Execution was canceled or the server is shutting down."""


def map_base(directory, name):
    """Resolve a filename without allowing traversal or replacing saved maps."""
    if (not name or name in ('.', '..') or '/' in name or '\\' in name
            or '\x00' in name or name.endswith(('.yaml', '.pgm'))):
        raise ValueError('map_name must be a filename without a path or extension')
    base = Path(directory).expanduser().resolve() / name
    if any(Path(str(base) + suffix).exists()
           for suffix in ('.yaml', '.pgm', '.pose.yaml', '.zones.geojson')):
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
            'ready_timeout_s': 30.0, 'navigation_timeout_s': 90.0,
            # Nav2 retries a blocked goal internally for minutes; move on to
            # another frontier once the robot has stood still this long.
            'stall_timeout_s': 30.0, 'stall_distance_m': 0.10,
            'max_exploration_time_s': 1200.0,
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
        self.save_uncertain = False
        self.message = None
        self.received_at = 0.0
        self.map_revision = 0
        self.child = None
        self.planned_path = []
        self.known_area_m2 = 0.0
        self.frontier_count = 0
        self.group = ReentrantCallbackGroup()
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

    def _pause(self):
        self.wake.wait(0.2)
        self.wake.clear()

    def _snapshot(self):
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
        while not child.done.wait(0.2):
            self._feedback(handle, 'CANCELING')
        self.child = None

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
            if not path_is_known_free(grid, points):
                return False
            self.planned_path = points
            return True
        finally:
            planning.cancel()  # Also cancels a goal that is accepted after timeout.

    def _navigate(self, handle, frontier, frame, run_deadline):
        # Obstacles and a blocked base are Nav2's job: the Collision Monitor
        # slows motion toward LiDAR points and the controller's progress checker
        # aborts a goal that stops moving. A failed goal is skipped below.
        request = NavigateToPose.Goal()
        request.pose = self._target_pose(frontier, frame)
        self._check(handle)
        self.child = Navigation(self.navigation, request)
        deadline = min(run_deadline,
                       time.monotonic() + self.settings['navigation_timeout_s'])
        moved_from, moved_at = None, time.monotonic()
        while not self.child.done.wait(0.2):
            self._check(handle)
            message, pose = self._snapshot()
            if message.header.frame_id != frame:
                raise RuntimeError('SLAM frame changed during navigation')
            if self.child.error:
                raise RuntimeError(f'Nav2 transport error: {self.child.error}')
            self._feedback(handle, 'NAVIGATING')
            now = time.monotonic()
            if (moved_from is None
                    or math.dist(pose, moved_from) >= self.settings['stall_distance_m']):
                moved_from, moved_at = pose, now
            elif now - moved_at >= self.settings['stall_timeout_s']:
                self.get_logger().warning(
                    f'Robot did not move for {now - moved_at:.0f} s; '
                    f'skipping frontier ({frontier.x:.2f}, {frontier.y:.2f})')
                self._settle_child(handle)
                return False
            if now >= deadline:
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
        # Read after SaveMap completes, while SLAM still publishes map->odom.
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
                blacklisted=tuple(blacklist),
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
                    # explored, so retry the failed frontiers once.
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
