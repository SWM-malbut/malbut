"""Run one map-driven camera patrol through cancellable Nav2 actions."""

import json
from collections import deque
import math
import os
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
from sensor_msgs.msg import CameraInfo, Image, LaserScan
from std_msgs.msg import String
from std_srvs.srv import SetBool
from tf2_ros import Buffer, TransformException, TransformListener

from malbut_patrol.coverage import (
    CoverageGrid, CoveragePlanner, CoverageProfile, read_room_map,
)
from malbut_patrol.photo_recorder import DEFAULT_DIRECTORY, PhotoRecorder
from malbut_patrol.inspection import InspectionCalibration, InspectionEvidence
from malbut_patrol.reobserve import plan_reobservations, read_results, unresolved_targets
from malbut_patrol.route import ReturnRoutePlanner
from malbut_patrol.maneuver import ManeuverSpace
from malbut_patrol.spatial_capture import stamp_seconds


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
            'start_enabled': True,
            'map_topic': '/map',
            'costmap_topic': '/global_costmap/costmap',
            'camera_image_topic': '/camera/color/image_raw',
            'camera_info_topic': '/camera/color/camera_info',
            'camera_optical_frame': '',
            'camera_image_rectified': False,
            'depth_image_topic': '/camera/depth/image_raw',
            'depth_aligned_to_rgb': False,
            'depth_registered_frame': '',
            'depth_scale_m': 0.0,
            'scan_topic': '/scan',
            'reobserve_enabled': True,
            'reobserve_rounds': 2,
            'reobserve_targets': 2,
            'reobserve_analysis_timeout_s': 120.0,
            'reobserve_max_travel_m': 3.0,
            'return_tolerance_m': 0.25,
            'inspection_calibration_file': '',
            'inspection_analysis_timeout_s': 120.0,
            'room_map_file': '',
            'base_frame': 'base_footprint',
            'nav2_action_name': 'navigate_to_pose',
            'spin_action_name': 'spin',
            'robot_clearance_m': 0.26,
            'robot_length_m': 0.277,
            'robot_width_m': 0.212,
            'footprint_padding_m': 0.01,
            # Match Nav2 general_goal_checker.xy_goal_tolerance.
            'goal_position_tolerance_m': 0.12,
            'observation_ranges_m': [4.0, 3.0, 2.0],
            'coverage_targets': [0.80, 0.90, 0.95],
            'candidate_spacing_m': [1.5, 1.0, 0.65],
            'observation_hz': 5.0,
            'sensor_timeout_s': 3.0,
            'costmap_timeout_s': 5.0,
            'goal_response_timeout_s': 5.0,
            'cancel_completion_timeout_s': 5.0,
            'navigation_timeout_s': 120.0,
            # Nav2 retries a blocked goal internally for minutes; move on to the
            # next viewpoint once the robot has stood still this long.
            'stall_timeout_s': 30.0,
            'stall_distance_m': 0.10,
            'spin_time_allowance_s': 60.0,
            'maximum_goal_cost': 80,
            'record_observations': os.environ.get('MALBUT_PATROL_OBSERVATIONS', '1') == '1',
            'observation_directory': os.environ.get(
                'MALBUT_PATROL_OBSERVATION_DIR', DEFAULT_DIRECTORY),
        }
        for name, value in defaults.items():
            self.declare_parameter(name, value)
        self.settings = {name: self.get_parameter(name).value for name in defaults}
        for name in ('reobserve_rounds', 'reobserve_targets'):
            if type(self.settings[name]) is not int or not 1 <= self.settings[name] <= 2:
                raise ValueError(f'{name} must be 1 or 2')
        if (not math.isfinite(self.settings['depth_scale_m'])
                or self.settings['depth_scale_m'] < 0):
            raise ValueError('depth_scale_m must be zero or positive and finite')
        for name in ('observation_hz', 'sensor_timeout_s', 'costmap_timeout_s',
                     'goal_response_timeout_s', 'cancel_completion_timeout_s',
                     'navigation_timeout_s', 'spin_time_allowance_s',
                     'robot_clearance_m', 'stall_timeout_s', 'stall_distance_m',
                     'robot_length_m', 'robot_width_m',
                     'reobserve_analysis_timeout_s', 'reobserve_max_travel_m',
                     'return_tolerance_m', 'inspection_analysis_timeout_s'):
            if not math.isfinite(self.settings[name]) or self.settings[name] <= 0:
                raise ValueError(f'{name} must be positive and finite')
        for name in ('footprint_padding_m', 'goal_position_tolerance_m'):
            if not math.isfinite(self.settings[name]) or self.settings[name] < 0:
                raise ValueError(f'{name} must be nonnegative and finite')
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
        self.return_pose = None
        self.return_status = {'state': 'not_started', 'distance_m': None}
        self.route_status = {'revision': 0, 'remaining': []}
        self.route_planner = None
        self.busy = False
        self.child = None
        self.map_message = None
        self.map_changed = False
        self.costmap = None
        self.costmap_received = 0.0
        self.maneuver_space = None
        self.observation_cells = None
        self.image = None
        # At most 20 Hz, retaining the default three-second sensor window even
        # when TF delivery falls behind a high-rate camera on the robot.
        self.rgb_images = deque(maxlen=64)
        self.image_received = 0.0
        self.camera_info = None
        self.depth_images = deque(maxlen=64)
        self.lidar_scans = deque(maxlen=64)
        self.planner = None
        self.phase = 'IDLE'
        self.visited = 0
        self.reobserve_target = None
        self.reobserve_status = {}
        self.last_image_stamp = None
        self.last_observation = 0.0
        self.last_pump = 0.0
        self.last_feedback = 0.0
        self.calibration = None
        self.inspection = None
        self.last_inspection_poll = 0.0
        calibration_file = self.settings['inspection_calibration_file']
        if calibration_file:
            if not self.settings['record_observations']:
                raise ValueError('Inspection requires photo recording')
            self.calibration = InspectionCalibration(
                calibration_file, bool(self.get_parameter('use_sim_time').value))
        self.group = ReentrantCallbackGroup()
        self.photo_recorder = (PhotoRecorder(self.settings['observation_directory'],
                                             inspection=self.calibration is not None)
                               if self.settings['record_observations'] else None)
        self.tf = Buffer(cache_time=Duration(seconds=10.0))
        self.tf_listener = TransformListener(self.tf, self)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(OccupancyGrid, self.settings['map_topic'],
                                 self._receive_map, latched,
                                 callback_group=self.group)
        self._costmap_subscription = self.create_subscription(
            OccupancyGrid, self.settings['costmap_topic'], self._receive_costmap, latched,
            callback_group=self.group)
        self._updates_subscription = self.create_subscription(
            OccupancyGridUpdate, self.settings['costmap_topic'] + '_updates',
            self._receive_costmap_update, 10, callback_group=self.group)
        self._image_subscription = self.create_subscription(
            Image, self.settings['camera_image_topic'], self._receive_image,
            qos_profile_sensor_data, callback_group=self.group)
        self.create_subscription(CameraInfo, self.settings['camera_info_topic'],
                                 self._receive_info, qos_profile_sensor_data,
                                 callback_group=self.group)
        self._depth_subscription = self._subscribe_depth()
        self._scan_subscription = self._subscribe_scan()
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
        self._processing_enabled = True
        self.create_service(SetBool, '~/set_processing', self._set_processing)
        self._change_processing(bool(self.settings['start_enabled']))

    def _change_processing(self, enabled):
        with self.lock:
            if self._processing_enabled == enabled:
                return
            self._processing_enabled = enabled
            # Keep ROS entities alive for the owning executor's lifetime. A
            # worker may already have selected a subscription when this RPC
            # runs; destroying it here raises InvalidHandle in executor.spin().
            # Callbacks discard idle inputs, and every enable starts empty.
            self.image = self.costmap = None
            self.rgb_images.clear()
            self.depth_images.clear()
            self.lidar_scans.clear()
            self.image_received = self.costmap_received = 0.0
            self.last_image_stamp = None

    def _subscribe_scan(self):
        if self.photo_recorder is None or not self.settings['depth_aligned_to_rgb']:
            return None
        return self.create_subscription(
            LaserScan, self.settings['scan_topic'], self._receive_scan,
            qos_profile_sensor_data, callback_group=self.group)

    def _subscribe_depth(self):
        if self.photo_recorder is None or not self.settings['depth_aligned_to_rgb']:
            return None
        return self.create_subscription(
            Image, self.settings['depth_image_topic'], self._receive_depth,
            qos_profile_sensor_data, callback_group=self.group)

    def _set_processing(self, request, response):
        # Goal acceptance reserves ownership under the same lock. Do not
        # disable inputs between checking busy and accepting a new patrol.
        with self.lock:
            if not request.data and self.busy:
                response.success, response.message = False, 'patrol is still running'
                return response
            self._change_processing(request.data)
        if request.data:
            # Image/costmap callbacks remain independent of this preparation RPC.
            deadline = time.monotonic() + self.settings['sensor_timeout_s']
            while self._ready():
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    response.success, response.message = False, self._ready()
                    self._change_processing(False)
                    return response
                time.sleep(0.02)
        response.success = True
        return response

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
            if not self._processing_enabled:
                return
            self.costmap = message
            self.costmap_received = time.monotonic()

    def _receive_costmap_update(self, update):
        # Nav2 can publish patches instead of a new full grid every cycle.
        with self.lock:
            if not self._processing_enabled:
                return
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
        # TF can arrive after RGB; retain a bounded window of original frames.
        if message.width and message.height and len(message.data):
            with self.lock:
                if not self._processing_enabled:
                    return
                self.image = message
                self._retain_frame(self.rgb_images, message)
                self.image_received = time.monotonic()

    def _receive_info(self, message):
        with self.lock:
            self.camera_info = message

    def _receive_depth(self, message):
        with self.lock:
            if self._processing_enabled and self.photo_recorder is not None:
                self._retain_frame(self.depth_images, message)

    def _receive_scan(self, message):
        with self.lock:
            if self._processing_enabled and self.photo_recorder is not None:
                self._retain_frame(self.lidar_scans, message)

    def _retain_frame(self, frames, message):
        """Keep original, time-spaced samples within a bounded sensor window."""
        stamp = stamp_seconds(message.header.stamp)
        if frames and stamp - stamp_seconds(frames[-1].header.stamp) < .05:
            return
        frames.append(message)
        cutoff = stamp - self.settings['sensor_timeout_s']
        while frames and stamp_seconds(frames[0].header.stamp) < cutoff:
            frames.popleft()

    def _lidar_bundle(self, scan):
        if scan is None:
            return None
        bundle = {'scan': scan}
        duration = scan.time_increment * max(0, len(scan.ranges)-1)
        if not math.isfinite(duration) or not 0 <= duration <= .25:
            return bundle
        start = Time.from_msg(scan.header.stamp)
        try:
            bundle['transform'] = self.tf.lookup_transform(
                self.map_message.header.frame_id, scan.header.frame_id, start).transform
            bundle['end_transform'] = self.tf.lookup_transform(
                self.map_message.header.frame_id, scan.header.frame_id,
                start + Duration(seconds=duration)).transform
        except TransformException:
            pass
        return bundle

    def _robot_xy(self):
        transform = self.tf.lookup_transform(
            self.map_message.header.frame_id, self.settings['base_frame'], Time())
        p = transform.transform.translation
        return p.x, p.y

    def _robot_pose(self):
        transform = self.tf.lookup_transform(
            self.map_message.header.frame_id, self.settings['base_frame'], Time())
        p = transform.transform.translation
        return p.x, p.y, _yaw(transform.transform.rotation)

    def _make_maneuver_space(self, grid):
        return ManeuverSpace(
            grid, self.settings['robot_length_m'], self.settings['robot_width_m'],
            self.settings['footprint_padding_m'])

    def _refresh_maneuvers(self):
        """Use one immutable live-map snapshot per planning/arrival decision."""
        pose = self._robot_pose()
        with self.lock:
            grid = _grid(self.costmap)
        self.maneuver_space = self._make_maneuver_space(grid)
        self.observation_cells = self.maneuver_space.observation_mask(
            pose, self.return_pose, self.settings['goal_position_tolerance_m'])

    def _observation_allowed(self, x, y):
        space = self.maneuver_space
        if space is None or self.observation_cells is None:
            return False
        row, col = space.grid.world_to_cell(x, y)
        return bool(space.grid.contains(row, col) and self.observation_cells[row, col]
                    and 0 <= space.grid.cells[row, col] <= self.settings['maximum_goal_cost'])

    def _can_observe_here(self):
        pose = self._robot_pose()
        return (self._allowed(*pose[:2])
                and self.maneuver_space.connected(pose, self.return_pose))

    def _ready(self):
        now = time.monotonic()
        if self.map_message is None or not self.map_message.header.frame_id:
            return 'Saved /map is not available'
        if not self.map_message.data or self.map_message.info.resolution <= 0:
            return 'Saved /map is empty or invalid'
        if (self.image is None or self.camera_info is None
                or not self.camera_info.width
                or (self.camera_info.p[0] if self.settings['camera_image_rectified']
                    else self.camera_info.k[0]) <= 0):
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
            images = tuple(self.rgb_images)
            info = self.camera_info
            depths, scans = tuple(self.depth_images), tuple(self.lidar_scans)
        optical_frame = self.settings['camera_optical_frame']
        registered = self.settings['depth_aligned_to_rgb']
        # Use the newest uncredited frame with its exact capture-time TF. Never
        # substitute the current robot pose or repeatedly credit an older frame.
        for image in sorted(images, key=lambda m: stamp_seconds(m.header.stamp), reverse=True):
            stamp = (image.header.stamp.sec, image.header.stamp.nanosec)
            if self.last_image_stamp is not None and stamp <= self.last_image_stamp:
                continue
            image_time = Time.from_msg(image.header.stamp)
            age = (self.get_clock().now() - image_time).nanoseconds / 1e9
            if age < -0.1 or age > self.settings['sensor_timeout_s']:
                continue
            if registered and self.phase in ('OBSERVING', 'REOBSERVING') and age < .12:
                continue
            # RGB can precede its matching depth callback. Prefer a complete
            # pair in the bounded buffer instead of consuming the newest RGB
            # immediately with an older depth frame.
            depth = min(
                depths, key=lambda d: abs(
                    stamp_seconds(d.header.stamp) - stamp_seconds(image.header.stamp)),
                default=None)
            if (registered and age < .35 and (depth is None or abs(
                    stamp_seconds(depth.header.stamp)-stamp_seconds(image.header.stamp)) > .05)):
                continue
            if not optical_frame and image.header.frame_id != info.header.frame_id:
                continue
            try:
                transform = self.tf.lookup_transform(
                    self.map_message.header.frame_id,
                    optical_frame or image.header.frame_id, image_time)
                break
            except TransformException:
                continue
        else:
            return
        depth = min(depths, key=lambda value: abs(
            stamp_seconds(value.header.stamp) - stamp_seconds(image.header.stamp)),
            default=None)
        scan = min(scans, key=lambda value: abs(
            stamp_seconds(value.header.stamp)
            + value.time_increment*max(0, len(value.ranges)-1)/2
            - stamp_seconds(image.header.stamp)), default=None)
        q = transform.transform.rotation
        # CameraInfo uses an optical frame: +Z is the viewing direction.
        forward_x = 2.0 * (q.x * q.z + q.w * q.y)
        forward_y = 2.0 * (q.y * q.z - q.w * q.x)
        if math.hypot(forward_x, forward_y) < 1e-6:
            return
        focal_length = info.p[0] if self.settings['camera_image_rectified'] else info.k[0]
        fov = 2.0 * math.atan2(info.width, 2.0 * focal_length)
        p = transform.transform.translation
        yaw = math.atan2(forward_y, forward_x)
        calibration = getattr(self, 'calibration', None)
        camera = None
        if calibration is None:
            self.planner.mark_observed(p.x, p.y, yaw, fov)
        else:
            camera = calibration.camera_record(image.width, image.height, fov, p.z)
        self.last_image_stamp = stamp
        self.last_observation = time.monotonic()

        recorder = getattr(self, 'photo_recorder', None)
        if recorder is not None:
            if registered and (self.phase not in ('OBSERVING', 'REOBSERVING')
                               or stamp_seconds(image.header.stamp)
                               < getattr(self, 'capture_not_before', 0.)):
                return
            if (calibration is not None and self.phase != 'REOBSERVING'
                    and self.planner.observation_gain(p.x, p.y, yaw, fov) == 0):
                return
            try:
                recorder.offer(
                    image, x=float(p.x), y=float(p.y), yaw=yaw,
                    room_names=self.planner.rooms_at(p.x, p.y), phase=self.phase,
                    simulated=bool(self.get_parameter('use_sim_time').value),
                    lidar=self._lidar_bundle(scan),
                    reobserve_target=self.reobserve_target,
                    inspection=camera,
                    spatial={'aligned': self.settings['depth_aligned_to_rgb'],
                             'depth': depth, 'info': info, 'transform': transform.transform,
                             'image_rectified': self.settings['camera_image_rectified'],
                             'depth_registered_frame': self.settings['depth_registered_frame'],
                             'depth_scale_m': self.settings['depth_scale_m'],
                             'optical_frame': optical_frame or image.header.frame_id})
            except Exception:
                if calibration is not None:
                    raise PatrolInterrupted('Inspection photo capture unavailable')
                self.get_logger().warning('Patrol photo capture unavailable')

    def _poll_inspection(self, *, verify_all=False):
        if self.inspection is None:
            return
        now = time.monotonic()
        if not verify_all and now - self.last_inspection_poll < .25:
            return
        self.last_inspection_poll = now
        run, captures = self.photo_recorder.evidence()
        # Retry batches carry their own evidence and never reset the coverage audit.
        current = run['patrol_id'] in self.inspection.runs
        stats = self.photo_recorder.status()
        if current and stats['failed']:
            raise PatrolInterrupted('Inspection photo save failed')
        for view in self.inspection.poll(captures if current else [], verify_all=verify_all):
            self.planner.mark_observed(*view)
        if current:
            self.inspection.pending += max(0, stats['queued'] - stats['saved'])

    def _await_inspection(self, handle):
        """Seal one viewpoint batch before analysis, keeping all earlier credit auditable."""
        self.photo_recorder.finish()
        self.phase = 'ANALYZING'
        deadline = time.monotonic() + self.settings['inspection_analysis_timeout_s']
        while True:
            self._pump(handle)
            stats = self.photo_recorder.status()
            if (stats['batch_ready'] and stats['queued'] == stats['saved']
                    and not self.inspection.pending):
                self._poll_inspection(verify_all=True)
                if not self.inspection.pending:
                    return
            if time.monotonic() >= deadline:
                raise PatrolInterrupted('Inspection photo analysis timed out; coverage incomplete')
            self.stop_event.wait(.05)

    def _spin(self, handle):
        self._refresh_maneuvers()
        if not self._can_observe_here():
            self.get_logger().warning(
                'Observation skipped: turning space or return connection blocked')
            return False
        # NavigateToPose has settled, but velocity_smoother may still be
        # decelerating its last translation command. Let that tail finish before
        # the Spin server takes over /cmd_vel with zero linear velocity.
        settle_until = time.monotonic() + .3
        while time.monotonic() < settle_until:
            self._pump(handle)
            self.stop_event.wait(.05)
        self._refresh_maneuvers()
        if not self._can_observe_here():
            return False
        stationary = (getattr(self, 'photo_recorder', None) is not None
                      and self.settings['depth_aligned_to_rgb'])
        steps = 6 if stationary else 1
        for _ in range(steps):
            self._refresh_maneuvers()
            if not self._can_observe_here():
                return False
            self.phase = 'ROTATING' if stationary else 'OBSERVING'
            request = Spin.Goal()
            request.target_yaw = 2.0 * math.pi / steps
            allowance = self.settings['spin_time_allowance_s']
            request.time_allowance = Duration(seconds=allowance).to_msg()
            if not self._run_child(self.spin_client, request, handle, allowance + 5.0):
                return False
            if stationary:
                self._stationary_observation(handle, 'OBSERVING')
        return True

    def _stationary_observation(self, handle, phase):
        """Wait out motion/scan exposure, then offer fresh synchronized frames."""
        self.phase = 'SETTLING'
        settled = time.monotonic()+.4
        while time.monotonic() < settled:
            self._pump(handle)
            self.stop_event.wait(.05)
        self.capture_not_before = self.get_clock().now().nanoseconds/1e9
        self.phase = phase
        before = self.photo_recorder.status()['queued']
        deadline = time.monotonic()+1.8
        while time.monotonic() < deadline:
            self._pump(handle)
            if self.photo_recorder.status()['queued'] > before:
                return True
            self.stop_event.wait(.05)
        return False

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
        if getattr(self, 'inspection', None) is not None:
            self._poll_inspection()
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

    def _run_child(self, client, request, handle, timeout, stall=False):
        self.child = ChildAction(client, request)
        operation = self.child
        moved_from, moved_at = None, time.monotonic()
        while not operation.settled:
            self._pump(handle)
            elapsed = time.monotonic() - operation.sent_at
            if operation.error is not None:
                raise PatrolInterrupted(f'Nav2 communication failed: {operation.error}')
            if (operation.handle is None and elapsed
                    > self.settings['goal_response_timeout_s']):
                raise PatrolInterrupted('Nav2 goal response timeout')
            stalled = False
            if stall:
                try:
                    position = self._robot_xy()
                except TransformException:
                    position = None  # _pump reports missing localization.
                now = time.monotonic()
                if position is not None and (
                        moved_from is None or math.dist(position, moved_from)
                        >= self.settings['stall_distance_m']):
                    moved_from, moved_at = position, now
                stalled = now - moved_at >= self.settings['stall_timeout_s']
                if stalled:
                    self.get_logger().warning(
                        f'Robot did not move for {now - moved_at:.0f} s; '
                        'trying the next viewpoint')
            if stalled or elapsed > timeout:
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
        self.reobserve_target = None
        self.reobserve_status = {'rounds': 0, 'views': 0, 'state': 'not_started'}
        self.return_pose = None
        self.return_status = {'state': 'pending', 'distance_m': None}
        self.route_status = {'revision': 0, 'remaining': []}
        self.route_planner = None
        self.inspection = None
        self.last_inspection_poll = 0.0
        self.last_observation = time.monotonic()
        success = False
        detail = ''
        try:
            self._publish_status('planning', 'Computing reachable viewpoints')
            rooms = None
            grid = _grid(self.map_message)
            room_file = self.settings['room_map_file']
            if room_file:
                # Read at every start: rooms edited since the last patrol are used.
                rooms, reason = read_room_map(Path(room_file).expanduser(), grid)
                if reason:
                    self.get_logger().info(reason)
            info = self.camera_info
            start = self.tf.lookup_transform(
                self.map_message.header.frame_id, self.settings['base_frame'], Time()).transform
            self.return_pose = (start.translation.x, start.translation.y, _yaw(start.rotation))
            calibration = getattr(self, 'calibration', None)
            profile = self.profiles[handle.request.thoroughness]
            fov = 2.0 * math.atan2(info.width, 2.0 * info.k[0])
            if calibration is not None:
                profile = CoverageProfile(calibration.range_m, .95,
                                          min(profile.candidate_spacing_m,
                                              calibration.range_m/2))
                fov = calibration.camera['hfov_rad']
            self.planner = CoveragePlanner(
                grid, profile,
                self.return_pose[:2], self.settings['robot_clearance_m'], rooms,
                fov, require_room_entry=calibration is None)
            self.route_planner = ReturnRoutePlanner(self.planner, self.return_pose[:2])
            recorder = getattr(self, 'photo_recorder', None)
            if recorder is not None:
                recorder.begin(grid, self.map_message.header.frame_id, room_file, rooms)
            self.last_observation = time.monotonic()
            if calibration is not None:
                run, _ = recorder.evidence()
                self.inspection = InspectionEvidence(recorder.directory, calibration, run)
                self._pump(handle)
                if not self._spin(handle):
                    raise PatrolInterrupted('Initial inspection rotation failed')
                self._await_inspection(handle)
            while True:
                self._pump(handle)
                if self.planner.complete:
                    if self.inspection is not None:
                        self._await_inspection(handle)
                    success = True
                    detail = ('Analyzed photo coverage completed' if self.inspection is not None
                              else 'Requested coverage and reachable room visits completed')
                    break
                if (self.inspection is not None
                        and len(self.inspection.references) >= recorder.max_photos):
                    detail = 'Inspection photo limit reached; partial coverage returned'
                    break
                route = self._plan_remaining(handle, 'coverage_update')
                if not route.viewpoints:
                    detail = 'No usable untried viewpoint remains; partial coverage returned'
                    break
                viewpoint = route.viewpoints[0]
                self.planner.mark_attempted(viewpoint.index)
                if self.inspection is not None:
                    recorder.begin(
                        grid, self.map_message.header.frame_id, room_file, rooms,
                        inspection_of=self.inspection.run['patrol_id'],
                        capture_limit=recorder.max_photos-len(self.inspection.references))
                    run, _ = recorder.evidence()
                    self.inspection.register_batch(run)
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
                                       self.settings['navigation_timeout_s'], stall=True):
                    if self.inspection is not None:
                        self._await_inspection(handle)
                    continue
                spun = self._spin(handle)
                if spun:
                    self.visited += 1
                else:
                    self.get_logger().warning(
                        'Observation turn failed; replanning remaining viewpoints')
                if self.inspection is not None:
                    if not spun:
                        raise PatrolInterrupted('Inspection rotation failed')
                    self._await_inspection(handle)
            if recorder is not None and self.settings['reobserve_enabled']:
                self._reobserve(handle, grid, room_file, rooms)
                detail += '; reobservation: ' + self.reobserve_status['state']
            returned = self._return_to_start(handle)
            success = success and returned
            if self.inspection is not None:
                self._poll_inspection(verify_all=True)
                success = success and not self.inspection.pending
            detail += '; return: ' + self.return_status['state']
        except (PatrolInterrupted, ValueError, OSError, TransformException) as error:
            success = False
            if self.return_status['state'] != 'arrived':
                self.return_status['state'] = ('canceled' if handle.is_cancel_requested
                                               else 'interrupted')
            detail = str(error)
        except Exception as error:
            success = False
            self.get_logger().error(f'Patrol failed: {error}')
            detail = f'Patrol failed: {error}'
        finally:
            settled = self._settle_child()
            try:
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
                recorder = getattr(self, 'photo_recorder', None)
                if recorder is not None:
                    recorder.finish('canceled' if state == 'idle' else state)
                self._publish_status(state, result.message)
                return result
            finally:
                self.reobserve_target = None
                # Diagnostic/result publication must not strand busy after
                # Nav2 has stopped. An unconfirmed stop still owns the inputs.
                with self.lock:
                    if settled:
                        self.busy = False

    def _record_route(self, views, distance, reason):
        """Publish a replaceable remaining route, always ending at the original start."""
        self.route_status = {
            'revision': self.route_status['revision']+1, 'reason': reason,
            'estimated_distance_m': distance,
            'distance_model': 'static_clearance_grid_4_connected',
            'remaining': [dict(kind=kind, x=view.x, y=view.y) for kind, view in views]
            + [dict(kind='return', x=self.return_pose[0], y=self.return_pose[1])],
        }
        self._publish_status(self.phase.lower(), '')

    def _plan_remaining(self, handle, reason):
        self.phase = 'PLANNING'
        self._refresh_viewpoints(handle)
        route = self.route_planner.plan(
            self._robot_xy(), self._observation_allowed, lambda: self._pump(handle))
        self._record_route([('observe', v) for v in route.viewpoints],
                           route.distance_m, reason)
        return route

    def _refresh_viewpoints(self, handle):
        self._refresh_maneuvers()
        if self.planner.refresh_candidates(self._observation_allowed,
                                           lambda: self._pump(handle)):
            # Cached distance vectors include every candidate and the home
            # index. Rebuild them when live-map alternatives are appended.
            self.route_planner = ReturnRoutePlanner(self.planner, self.return_pose[:2])

    def _return_to_start(self, handle):
        """Verify measured return distance in addition to the completed Nav2 action."""
        self._pump(handle)
        self.phase = 'RETURNING'
        xy = self._robot_xy()
        plan = self.route_planner.plan(xy, self._allowed, lambda: self._pump(handle),
                                       maximum_stops=0)
        self._record_route([], plan.distance_m, 'return_after_observations')
        distance = math.dist(xy, self.return_pose[:2])
        self.return_status = {'state': 'returning', 'distance_m': distance}
        if distance > self.settings['return_tolerance_m']:
            if not self._allowed(*self.return_pose[:2]):
                self.return_status['state'] = 'blocked'
                return False
            request = NavigateToPose.Goal()
            request.pose.header.frame_id = self.map_message.header.frame_id
            request.pose.header.stamp = self.get_clock().now().to_msg()
            request.pose.pose.position.x, request.pose.pose.position.y = self.return_pose[:2]
            request.pose.pose.orientation.z = math.sin(self.return_pose[2]/2)
            request.pose.pose.orientation.w = math.cos(self.return_pose[2]/2)
            if not self._run_child(self.navigation, request, handle,
                                   self.settings['navigation_timeout_s'], stall=True):
                self.return_status['state'] = 'navigation_failed'
                return False
            distance = math.dist(self._robot_xy(), self.return_pose[:2])
        arrived = distance <= self.settings['return_tolerance_m']
        self.return_status = {'state': 'arrived' if arrived else 'outside_tolerance',
                              'distance_m': distance}
        if arrived:
            self.route_status['remaining'] = []
        return arrived

    def _await_analysis(self, handle):
        """Keep cancellation and live sensor checks active while the Agent analyzes a batch."""
        recorder = self.photo_recorder
        identity = recorder.identity()
        recorder.finish()
        self.phase = 'ANALYZING'
        deadline = time.monotonic() + self.settings['reobserve_analysis_timeout_s']
        while time.monotonic() < deadline:
            self._pump(handle)
            stats = recorder.status()
            if stats['queued'] == stats['saved'] + stats['failed']:
                if not stats['saved']:
                    self.reobserve_status['state'] = 'no_saved_photos'
                    return []
                rows = read_results(recorder.directory, **identity,
                                    simulated=bool(self.get_parameter('use_sim_time').value))
                if rows is not None:
                    self.reobserve_status['state'] = 'analyzed' if rows else 'analysis_unavailable'
                    return rows
            self.stop_event.wait(.1)
        self.reobserve_status['state'] = 'analysis_timeout'
        return []

    def _reobserve_heading_error(self, target, not_before=0.):
        """Aim from the measured optical pose, not Nav2's permissive goal yaw."""
        with self.lock:
            images = tuple(self.rgb_images)
        for image in sorted(images, key=lambda m: stamp_seconds(m.header.stamp), reverse=True):
            captured = stamp_seconds(image.header.stamp)
            age = self.get_clock().now().nanoseconds/1e9-captured
            if captured < not_before or not 0 <= age <= self.settings['sensor_timeout_s']:
                continue
            try:
                transform = self.tf.lookup_transform(
                    self.map_message.header.frame_id,
                    self.settings['camera_optical_frame'] or image.header.frame_id,
                    Time.from_msg(image.header.stamp)).transform
                break
            except TransformException:
                continue
        else:
            raise TransformException('Post-arrival camera transform is not available yet')
        p, q = transform.translation, transform.rotation
        forward_x = 2.0 * (q.x * q.z + q.w * q.y)
        forward_y = 2.0 * (q.y * q.z - q.w * q.x)
        if math.hypot(forward_x, forward_y) < 1e-6:
            raise ValueError('Reobservation camera has no horizontal viewing direction')
        desired = math.atan2(target['xy'][1]-p.y, target['xy'][0]-p.x)
        error = desired - math.atan2(forward_y, forward_x)
        return math.atan2(math.sin(error), math.cos(error))

    def _orient_reobservation(self, handle, target):
        """Confirm the camera faces the target before consuming a retry photo."""
        self.phase = 'REORIENTING'
        not_before = self.get_clock().now().nanoseconds/1e9
        # A second bounded correction handles residual Nav2 Spin tolerance.
        for attempt in range(3):
            deadline = time.monotonic()+self.settings['sensor_timeout_s']
            while True:
                self._pump(handle)
                try:
                    error = self._reobserve_heading_error(target, not_before)
                    break
                except TransformException:
                    if time.monotonic() >= deadline:
                        return False
                    self.stop_event.wait(.05)
            if abs(error) <= .15:
                return True
            if attempt == 2:
                return False
            request = Spin.Goal()
            request.target_yaw = error
            allowance = self.settings['spin_time_allowance_s']
            request.time_allowance = Duration(seconds=allowance).to_msg()
            if not self._run_child(self.spin_client, request, handle, allowance+5.):
                return False
            # The next check must use a post-rotation frame AND its exact TF.
            not_before = self.get_clock().now().nanoseconds/1e9
        return False

    def _reobserve(self, handle, grid, room_file, rooms):
        """Use bounded alternate views and linked batches with separate photo evidence."""
        inspection = getattr(self, 'inspection', None)
        root = (inspection.run['patrol_id'] if inspection is not None
                else self.photo_recorder.identity()['patrol_id'])
        previous = []
        if inspection is None:
            rows = self._await_analysis(handle)
        else:
            rows = []
            for run in inspection.runs.values():
                batch = read_results(self.photo_recorder.directory, run['patrol_id'],
                                     run['map_id'], inspection.calibration.simulated)
                if batch is None:
                    raise ValueError('Inspection batch analysis disappeared')
                rows.extend(batch)
        for round_index in range(self.settings['reobserve_rounds']):
            if not rows:
                return
            targets = unresolved_targets(rows, self.settings['reobserve_targets'])
            if not targets:
                self.reobserve_status['state'] = 'no_actionable_unknowns'
                return
            self.photo_recorder.begin(
                grid, self.map_message.header.frame_id, room_file, rooms,
                reobserve_of=root, reobserve_round=round_index+1, capture_limit=len(targets))
            self.reobserve_status['rounds'] = round_index+1
            pending = list(targets)
            while pending:
                self.phase = 'REPOSITIONING'
                self._pump(handle)
                self._refresh_viewpoints(handle)
                steps, distance = plan_reobservations(
                    self.planner, pending, self._robot_xy(), self.return_pose[:2], previous,
                    self._observation_allowed, self.settings['reobserve_max_travel_m'],
                    lambda: self._pump(handle))
                self._record_route([('reobserve', view) for _, view in steps], distance,
                                   'reobserve_remaining')
                if not steps:
                    break
                target, view = steps[0]
                pending.remove(target)
                previous.append([view.x, view.y])
                request = NavigateToPose.Goal()
                request.pose.header.frame_id = self.map_message.header.frame_id
                request.pose.header.stamp = self.get_clock().now().to_msg()
                request.pose.pose.position.x, request.pose.pose.position.y = view.x, view.y
                request.pose.pose.orientation.z = math.sin(view.yaw/2)
                request.pose.pose.orientation.w = math.cos(view.yaw/2)
                if not self._run_child(self.navigation, request, handle,
                                       self.settings['navigation_timeout_s'], stall=True):
                    continue
                # Wait for a post-arrival image; moving images must not consume the quota.
                settle_until = time.monotonic()+.5
                while time.monotonic() < settle_until:
                    self._pump(handle)
                    self.stop_event.wait(.05)
                self._refresh_maneuvers()
                if not self._can_observe_here():
                    continue
                if not self._orient_reobservation(handle, target):
                    continue
                self.reobserve_target = {k: target[k] for k in ('observation_id', 'object_index')}
                if self._stationary_observation(handle, 'REOBSERVING'):
                    self.reobserve_status['views'] += 1
                self.reobserve_target = None
                self.phase = 'REPOSITIONING'
            rows = self._await_analysis(handle)
        if rows:
            self.reobserve_status['state'] = ('attempt_limit' if unresolved_targets(rows)
                                              else 'no_actionable_unknowns')

    def _publish_status(self, state, detail):
        # This diagnostic topic is not the Action result or motion ownership.
        # A serialization/publisher failure must not interrupt Nav2 cancellation.
        try:
            message = String()
            message.data = json.dumps({
                'state': state, 'detail': detail,
                'coverage_ratio': float(self.planner.coverage_ratio) if self.planner else 0.0,
                'viewpoints_visited': self.visited,
                'unvisited_rooms': list(self.planner.unvisited_room_names)
                if self.planner else [],
                'inaccessible_rooms': list(self.planner.inaccessible_room_names)
                if self.planner else [],
                # 0: no rooms for this map, so the web says it patrolled without rooms.
                'room_count': len(self.planner.room_names) if self.planner else 0,
                'reobservation': self.reobserve_status,
                'return_to_start': dict(self.return_status, start_pose=self.return_pose),
                'route': self.route_status,
                'coverage': self.planner.summary() if self.planner else None,
                'evidence': self.inspection.status()
                if getattr(self, 'inspection', None) is not None else None,
                'photos': (self.photo_recorder.status()
                           if getattr(self, 'photo_recorder', None) is not None else None),
            })
            self.status.publish(message)
        except Exception as error:
            self.get_logger().warning(f'Could not publish patrol status: {error}')

    def destroy_node(self):
        """Release the photo writer after the owning executor has stopped."""
        if self.photo_recorder is not None:
            self.photo_recorder.close()
        return super().destroy_node()


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
