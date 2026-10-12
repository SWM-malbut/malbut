"""
Find the robot's pose on the selected saved map and remember its last pose.

Bringup's map provider selects the saved map and requests /relocalize after each
switch. AUTO first tries the pose saved for this map and checks it against the
LiDAR scan; when it does not fit (the robot was moved while off) or no pose is
saved, it runs AMCL's global localization while rotating in place with Nav2
Spin. AMCL alone estimates the position: the scan match only scores it. Particle
limits are raised during this Action and restored with the resulting AMCL pose
and covariance afterwards. No velocity is sent.
"""

import json
import math
from pathlib import Path
import threading
import time

from builtin_interfaces.msg import Duration
from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from malbut_interfaces.action import Relocalize
from nav2_msgs.action import Spin
from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import GetParameters, SetParametersAtomically
from rclpy.qos import (
    DurabilityPolicy, qos_profile_sensor_data, QoSProfile, ReliabilityPolicy,
)
from rclpy.time import Time
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from std_srvs.srv import Empty
from tf2_ros import Buffer, TransformException, TransformListener

from .pose_store import map_identity, read_initial_pose, valid_pose, write_pose
from .scan_match import distance_field, match_ratio


LOCALIZATION_STATE_TOPIC = '/malbut/localization/state'
# AMCL's first estimate may move within the requested spread; beyond it, the
# estimate is an older one still in flight.
MIN_ACCEPT_DISTANCE_M = 0.5
MIN_ACCEPT_ANGLE_RAD = 0.5
SCAN_MAX_AGE_S = 2.0
FULL_TURN_RAD = 2.0 * math.pi
SPIN_WAIT_S = 2.0
RESTORE_TIMEOUT_S = 5.0


class RelocalizationError(RuntimeError):
    """A relocalization request cannot be completed."""


class Canceled(RuntimeError):
    """The client canceled the goal."""


def pose_record(message):
    """Return a storable planar pose from a map-frame pose message, or None."""
    position = message.pose.pose.position
    q = message.pose.pose.orientation
    norm = math.sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w)
    if not math.isfinite(norm) or norm == 0:
        return None
    x, y, z, w = (value / norm for value in (q.x, q.y, q.z, q.w))
    pose = {'frame_id': message.header.frame_id, 'x': position.x, 'y': position.y,
            'yaw': math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
            'covariance': [float(value) for value in message.pose.covariance]}
    return pose if valid_pose(pose) else None


def pose_message(pose, stamp):
    """Build the /initialpose message for a stored planar pose."""
    message = PoseWithCovarianceStamped()
    message.header.frame_id = 'map'
    message.header.stamp = stamp
    message.pose.pose.position.x = float(pose['x'])
    message.pose.pose.position.y = float(pose['y'])
    message.pose.pose.orientation.z = math.sin(pose['yaw'] / 2)
    message.pose.pose.orientation.w = math.cos(pose['yaw'] / 2)
    message.pose.covariance = [float(value) for value in pose['covariance']]
    return message


def map_geometry(map_file):
    """Return (width, height, resolution, origin x, origin y) of a saved map."""
    import cv2
    import yaml

    path = Path(map_file)
    metadata = yaml.safe_load(path.read_text(encoding='utf-8'))
    image = Path(metadata['image'])
    pixels = cv2.imread(str(image if image.is_absolute() else path.parent / image),
                        cv2.IMREAD_UNCHANGED)
    if pixels is None:
        raise ValueError('cannot read the map image')
    if abs(float(metadata['origin'][2])) > 1e-6:
        raise ValueError('rotated map origins are not supported')
    return _rounded(pixels.shape[1], pixels.shape[0], metadata['resolution'],
                    metadata['origin'][0], metadata['origin'][1])


def grid_geometry(message):
    """Return the same geometry tuple for a received OccupancyGrid."""
    info = message.info
    return _rounded(info.width, info.height, info.resolution,
                    info.origin.position.x, info.origin.position.y)


def _rounded(width, height, resolution, x, y):
    # map_server stores float32 metadata; compare at millimetre precision.
    return (int(width), int(height), round(float(resolution), 6),
            round(float(x), 3), round(float(y), 3))


def accepts(estimate, requested):
    """Return whether AMCL's estimate started from the requested pose."""
    covariance = requested['covariance']
    distance = math.hypot(estimate['x'] - requested['x'], estimate['y'] - requested['y'])
    angle = abs(math.atan2(math.sin(estimate['yaw'] - requested['yaw']),
                           math.cos(estimate['yaw'] - requested['yaw'])))
    return (distance <= max(MIN_ACCEPT_DISTANCE_M,
                            3 * math.sqrt(max(covariance[0], covariance[7])))
            and angle <= max(MIN_ACCEPT_ANGLE_RAD, 3 * math.sqrt(covariance[35])))


class Relocalization(Node):
    """Serve /relocalize and keep the last AMCL pose for each saved map."""

    def __init__(self, **kwargs):
        super().__init__('relocalization', **kwargs)
        defaults = {
            'pose_file': str(Path.home() / '.ros/malbut/localization/last_pose.yaml'),
            'save_period_s': 5.0, 'timeout_s': 10.0,
            'scan_topic': '/scan_raw', 'map_topic': '/map', 'base_frame': 'base_footprint',
            # A scan endpoint within this distance of a mapped obstacle is a hit.
            'match_distance_m': 0.10,
            # Share of hits needed to trust a pose; a moved robot scores far lower.
            'match_ratio_min': 0.95, 'max_scan_range_m': 8.0,
            'min_particles': 2000, 'max_particles': 8000,
        }
        settings = {name: self.declare_parameter(name, value).value
                    for name, value in defaults.items()}
        for name in ('save_period_s', 'timeout_s', 'match_distance_m', 'max_scan_range_m'):
            if not math.isfinite(settings[name]) or settings[name] <= 0:
                raise ValueError(f'{name} must be positive')
        if not 0 < settings['match_ratio_min'] <= 1:
            raise ValueError('match_ratio_min must be in (0, 1]')
        if not 0 < settings['min_particles'] <= settings['max_particles']:
            raise ValueError('particle limits must satisfy 0 < min <= max')
        self.settings = settings
        self.path = settings['pose_file']
        self.period = settings['save_period_s']
        self.timeout = settings['timeout_s']
        self.lock = threading.Lock()
        self.map_file = None
        self.identity = None
        self.saving = False
        self.expected_map = None
        self.map_message = None
        self.field = None
        self.scan = None
        self.latest = None
        self.received = 0.0
        self.saved_received = 0.0
        self.initial_stamp = 0
        self.estimate = None
        self.busy = False
        self.amcl_active = False
        self.state_future = None
        self.state_requested = 0.0
        group = ReentrantCallbackGroup()
        latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.publisher = self.create_publisher(
            PoseWithCovarianceStamped, '/initialpose', 10)
        # Sensor-compatible reliability and VOLATILE: never act on a latched old estimate.
        self.create_subscription(
            PoseWithCovarianceStamped, '/amcl_pose', self._receive,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                       durability=DurabilityPolicy.VOLATILE), callback_group=group)
        self.create_subscription(
            PoseWithCovarianceStamped, '/initialpose', self._initialpose, 10,
            callback_group=group)
        self.create_subscription(
            String, LOCALIZATION_STATE_TOPIC, self._localization, latched, callback_group=group)
        self.create_subscription(
            OccupancyGrid, settings['map_topic'], self._map, latched, callback_group=group)
        self.create_subscription(
            LaserScan, settings['scan_topic'], self._scan, qos_profile_sensor_data,
            callback_group=group)
        self.tf = Buffer()
        self.tf_listener = TransformListener(self.tf, self)
        self.client = self.create_client(GetState, '/amcl/get_state', callback_group=group)
        self.global_localization = self.create_client(
            Empty, '/reinitialize_global_localization', callback_group=group)
        self.nomotion_update = self.create_client(
            Empty, '/request_nomotion_update', callback_group=group)
        self._particle_reader = self.create_client(
            GetParameters, '/amcl/get_parameters', callback_group=group)
        self._particle_writer = self.create_client(
            SetParametersAtomically, '/amcl/set_parameters_atomically', callback_group=group)
        self.spin = ActionClient(self, Spin, '/spin', callback_group=group)
        self.create_timer(0.5, self._poll_amcl, callback_group=group)
        self.create_timer(self.period, self.save_latest, callback_group=group)
        self.server = ActionServer(
            self, Relocalize, '/relocalize', execute_callback=self._execute,
            goal_callback=self._goal, cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=group)

    def _start_observing(self):
        with self.lock:
            self.scan = None

    def _stop_observing(self):
        with self.lock:
            self.scan = None

    def _localization(self, message):
        try:
            state = json.loads(message.data)
            mode = state.get('mode')
            # While switching to a saved map the manager asks for its pose.
            map_file = state.get('map') if mode in ('SWITCHING', 'LOCALIZATION') else None
        except (AttributeError, ValueError):
            return
        if (map_file or None) != self.map_file:
            self._select(map_file or None)
        self.saving = mode == 'LOCALIZATION' and self.identity is not None

    def _select(self, map_file):
        """Remember poses only for the selected saved map; mapping has none."""
        identity = expected = None
        if map_file:
            try:
                identity = map_identity(map_file)
                expected = map_geometry(map_file)
            except (OSError, KeyError, TypeError, ValueError) as error:
                self.get_logger().error(f'Cannot read saved map {map_file}: {error}')
                identity = None
        with self.lock:
            self.map_file, self.identity, self.expected_map = map_file, identity, expected
            self.field = None
            self.latest = None
            # An estimate on another map is no pose to return to.
            self.estimate = None
            self.received = self.saved_received = 0.0
            self.saving = False

    def _map(self, message):
        # Kept as received; the distance field is built only for the selected map.
        with self.lock:
            self.map_message = message

    def _selected_field(self):
        """Return the distance field of the selected map once map_server sends it."""
        with self.lock:
            message, expected, field = self.map_message, self.expected_map, self.field
        if field is not None and field[0] is message:
            return field[1]
        if message is None or expected is None or grid_geometry(message) != expected:
            return None  # Still SLAM's or the previous map.
        info = message.info
        try:
            built = distance_field(info.width, info.height, info.resolution,
                                   (info.origin.position.x, info.origin.position.y, 0.0),
                                   message.data)
        except ValueError as error:
            self.get_logger().warning(f'Ignoring malformed map: {error}')
            return None
        with self.lock:
            self.field = (message, built)
        return built

    def _scan(self, message):
        with self.lock:
            self.scan = (time.monotonic(), message)

    def _initialpose(self, message):
        if message.header.frame_id == 'map':
            # Estimates computed before any correction must not be saved.
            with self.lock:
                self.latest = None
                self.initial_stamp = self.get_clock().now().nanoseconds

    def _receive(self, message):
        if message.header.frame_id != 'map':
            return
        pose = pose_record(message)
        if pose is None:
            return
        now = self.get_clock().now().nanoseconds
        stamp = message.header.stamp.sec * 10**9 + message.header.stamp.nanosec
        with self.lock:
            self.estimate = (time.monotonic(), pose, message)
            if (self.identity is None or stamp <= 0 or stamp < self.initial_stamp
                    or not 0 <= (now - stamp) / 1e9 <= 2 * self.period):
                return
            self.latest = pose
            self.received = time.monotonic()

    def _poll_amcl(self):
        """Track AMCL's lifecycle state without blocking an executor thread."""
        if not self.client.service_is_ready():
            self.amcl_active = False
            self._forget_state_request()
            return
        future = self.state_future
        if future is not None and not future.done():
            if time.monotonic() - self.state_requested < self.period:
                return
            # Discovery can stay healthy even when one GetState reply is lost.
            self._forget_state_request()
            self.amcl_active = False
        elif future is not None:
            try:
                self.amcl_active = (future.result().current_state.id
                                    == State.PRIMARY_STATE_ACTIVE)
            except Exception:  # noqa: B902 - a lost lifecycle service is inactive
                self.amcl_active = False
        self.state_future = self.client.call_async(GetState.Request())
        self.state_requested = time.monotonic()

    def _forget_state_request(self):
        if self.state_future is not None:
            self.client.remove_pending_request(self.state_future)
            self.state_future.cancel()
            self.state_future = None

    def _amcl_listening(self):
        return any(info.node_name == 'amcl'
                   for info in self.get_subscriptions_info_by_topic('/initialpose'))

    def _goal(self, _request):
        with self.lock:
            if self.busy:
                return GoalResponse.REJECT
            self.busy = True
        return GoalResponse.ACCEPT

    def _execute(self, handle):
        result = Relocalize.Result()
        try:
            self._start_observing()
            found, message, estimate, ratio = self._relocalize(handle)
            result.success, result.message, result.match_ratio = found, message, ratio
            if estimate is not None:
                result.pose = estimate[2]
            if found:
                handle.succeed()
            else:
                self.get_logger().warning(f'Relocalization failed: {message}')
                handle.abort()
        except Canceled as error:
            result.message = str(error)
            handle.canceled()
        except RelocalizationError as error:
            result.message = str(error)
            self.get_logger().warning(f'Relocalization failed: {error}')
            handle.abort()
        finally:
            self._stop_observing()
            with self.lock:
                self.busy = False
        return result

    def _relocalize(self, handle):
        request = handle.request
        goal = Relocalize.Goal
        if request.method not in (goal.AUTO, goal.GIVEN_POSE, goal.GLOBAL_SEARCH):
            raise RelocalizationError(f'unknown relocalization method {request.method}')
        deadline = time.monotonic() + self.timeout
        self._wait_ready(handle, deadline)
        original = self._particle_limits(handle, deadline)
        boosted = (self.settings['min_particles'], self.settings['max_particles'])
        if original == boosted:
            return self._locate(handle, deadline)
        with self.lock:
            before = self.estimate
            map_file, identity = self.map_file, self.identity
        seed = before[1] if before is not None else None
        if request.method == goal.AUTO:
            seed = read_initial_pose(self.path, map_file, identity) or seed
        elif request.method == goal.GIVEN_POSE:
            seed = pose_record(request.initial_pose)
            if seed is None:
                raise RelocalizationError('initial_pose must be a finite pose in the map frame')
        restored_pose = seed
        seeded = None
        try:
            # Set both limits together. Even a lost response needs restoration:
            # the server may already have reset its particle filter.
            self._set_particle_limits(boosted, handle, deadline)
            seeded = self._apply(handle, seed, deadline) if seed is not None else None
            located = self._locate(handle, deadline, seeded)
            with self.lock:
                # Spin has stopped before _locate returns: keep its final AMCL
                # heading, not an earlier scored heading from mid-rotation.
                current = self.estimate
            if current is not None:
                restored_pose = current[1]
                located = (*located[:2], current, located[3])
            return located
        finally:
            # Cancellation during a running search also keeps the last live
            # estimate, but a reset before seeding must not replace the seed.
            if seeded is not None:
                with self.lock:
                    current = self.estimate
                if current is not None:
                    restored_pose = current[1]
            cleanup = time.monotonic() + RESTORE_TIMEOUT_S
            self._set_particle_limits(original, None, cleanup)
            if restored_pose is not None:
                # Humble resets its filter when limits change. Preserve AMCL's
                # own pose AND uncertainty, never manufacture a fitted pose.
                self._apply(None, restored_pose, cleanup)

    def _particle_limits(self, handle, deadline):
        reply = self._rpc(self._particle_reader, GetParameters.Request(
            names=['min_particles', 'max_particles']), handle, deadline, 'read particle limits')
        if (len(reply.values) != 2 or any(
                value.type != ParameterType.PARAMETER_INTEGER for value in reply.values)):
            raise RelocalizationError('AMCL particle limits are unavailable')
        limits = tuple(value.integer_value for value in reply.values)
        if not 0 < limits[0] <= limits[1]:
            raise RelocalizationError('AMCL particle limits are invalid')
        return limits

    def _set_particle_limits(self, limits, handle, deadline):
        request = SetParametersAtomically.Request(parameters=[
            Parameter(name=name, value=ParameterValue(
                type=ParameterType.PARAMETER_INTEGER, integer_value=int(value)))
            for name, value in zip(('min_particles', 'max_particles'), limits)])
        reply = self._rpc(self._particle_writer, request, handle, deadline,
                          'set particle limits')
        if not reply.result.successful:
            raise RelocalizationError(f'AMCL rejected particle limits: {reply.result.reason}')

    def _rpc(self, client, request, handle, deadline, label):
        while not client.wait_for_service(timeout_sec=0.05):
            self._check(handle)
            if time.monotonic() >= deadline:
                raise RelocalizationError(f'AMCL {label} service is unavailable')
        self._check(handle)
        if time.monotonic() >= deadline:
            raise RelocalizationError(f'AMCL {label} did not finish in time')
        return self._wait_future(handle, client.call_async(request), deadline, label)

    def _locate(self, handle, deadline, seeded=None):
        request = handle.request
        goal = Relocalize.Goal
        minimum = self.settings['match_ratio_min']
        if request.method == goal.GIVEN_POSE:
            pose = pose_record(request.initial_pose)
            if pose is None:
                raise RelocalizationError('initial_pose must be a finite pose in the map frame')
            self._feedback(handle, 'APPLYING')
            estimate = seeded or self._apply(handle, pose, deadline)
            ratio = self._score(handle, estimate, deadline)
            # An operator's pose is kept even when the scan disagrees; report it.
            return True, f'pose set; {ratio:.0%} of the scan matches the map', estimate, ratio
        reason = 'global search requested'
        if request.method == goal.AUTO:
            with self.lock:
                map_file, identity = self.map_file, self.identity
            saved = read_initial_pose(self.path, map_file, identity)
            reason = 'no saved pose for this map'
            if saved is not None:
                self._feedback(handle, 'CHECKING_SAVED_POSE')
                estimate = seeded or self._apply(handle, saved, deadline)
                ratio = self._score(handle, estimate, deadline)
                self._feedback(handle, 'CHECKING_SAVED_POSE', ratio)
                if ratio >= minimum:
                    return (True, f'saved pose confirmed; {ratio:.0%} of the scan matches the map',
                            estimate, ratio)
                reason = f'saved pose matched only {ratio:.0%} of the scan'
            self.get_logger().info(f'Relocalization: {reason}; searching the whole map')
        estimate, ratio = self._search(handle, deadline, minimum)
        if ratio >= minimum:
            return (True, f'{reason}; found by global search, {ratio:.0%} of the scan '
                    'matches the map', estimate, ratio)
        return (True, f'{reason}; search time limit reached; keeping the latest AMCL pose, '
                f'{ratio:.0%} of the scan matches the map', estimate, ratio)

    def _wait_ready(self, handle, deadline):
        self._feedback(handle, 'WAITING')
        while True:
            self._check(handle)
            missing = self._missing()
            if not missing:
                return
            if time.monotonic() >= deadline:
                raise RelocalizationError(missing)
            time.sleep(0.1)

    def _missing(self):
        with self.lock:
            identity, scan = self.identity, self.scan
        if identity is None:
            return 'no saved map is selected'
        if not self.amcl_active or not self._amcl_listening():
            return 'AMCL is not active'
        if self._selected_field() is None:
            return 'the selected map was not received on the map topic'
        if scan is None or time.monotonic() - scan[0] > SCAN_MAX_AGE_S:
            return 'no recent LiDAR scan'
        return ''

    def _apply(self, handle, pose, deadline):
        """Send one initial pose and return AMCL's first estimate from it."""
        sent = time.monotonic()
        self.publisher.publish(pose_message(pose, self.get_clock().now().to_msg()))
        while True:
            self._check(handle)
            with self.lock:
                estimate = self.estimate
            # AMCL publishes once after every initial pose, even when stationary.
            if estimate is not None and estimate[0] > sent and accepts(estimate[1], pose):
                return estimate
            if time.monotonic() >= deadline:
                raise RelocalizationError('AMCL did not report the corrected pose in time')
            time.sleep(0.05)

    def _score(self, handle, estimate, deadline):
        """Match a scan taken after the estimate against the map at that pose."""
        message, laser, field = self._observe(handle, estimate, deadline)
        pose = estimate[1]
        return self._ratio(field, message, laser, (pose['x'], pose['y'], pose['yaw']))

    def _ratio(self, field, scan, laser, pose):
        ratio, beams = match_ratio(
            field, scan, laser, pose, hit_distance_m=self.settings['match_distance_m'],
            max_range_m=self.settings['max_scan_range_m'])
        return ratio if beams else 0.0

    def _observe(self, handle, estimate, deadline):
        """Return a scan taken after the estimate, the laser mount and the map field."""
        while True:
            self._check(handle)
            with self.lock:
                scan = self.scan
            field = self._selected_field()
            if scan is not None and scan[0] >= estimate[0] and field is not None:
                break
            if time.monotonic() >= deadline:
                raise RelocalizationError('no LiDAR scan after the pose correction')
            time.sleep(0.05)
        message = scan[1]
        try:
            transform = self.tf.lookup_transform(
                self.settings['base_frame'], message.header.frame_id, Time())
        except TransformException as error:
            raise RelocalizationError(f'no LiDAR transform: {error}') from error
        translation, q = transform.transform.translation, transform.transform.rotation
        laser = (translation.x, translation.y,
                 math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)))
        return message, laser, field

    def _search(self, handle, deadline, minimum):
        """Let AMCL converge; stop at the requested score or the deadline."""
        with self.lock:
            message = self.map_message
        # Humble AMCL's uniformPoseGenerator indexes the free-space vector
        # without an empty check. The all-unknown default map has no entries.
        # Reject only this operation, not other map-independent functions or
        # an operator's explicit GIVEN_POSE request.
        if message is None or not any(value == 0 for value in message.data):
            raise RelocalizationError(
                'selected map has no known free cells for global localization')
        with self.lock:
            # A pose from the saved-position check is not a global-search result.
            self.estimate = None
            self.latest = None
        self._feedback(handle, 'SEARCHING')
        self._call(self.global_localization, 'global localization', deadline, handle)
        return self._rotate(handle, deadline, minimum)

    def _spin_ready(self, handle, deadline):
        until = min(deadline, time.monotonic() + SPIN_WAIT_S)
        while not self.spin.wait_for_server(timeout_sec=0.05):
            self._check(handle)
            if time.monotonic() >= until:
                return False
            time.sleep(0.05)
        return True

    def _rotate(self, handle, deadline, minimum):
        """Observe AMCL during Spin, cancel motion before restoring its limits."""
        started = time.monotonic()
        spin = result = None
        if self._spin_ready(handle, deadline):
            remaining = max(0.0, deadline - time.monotonic())
            goal = Spin.Goal(target_yaw=FULL_TURN_RAD, time_allowance=Duration(
                sec=int(remaining), nanosec=int((remaining % 1) * 1e9)))
            sent = self.spin.send_goal_async(goal)
            try:
                spin = self._wait_future(handle, sent, deadline, 'Spin goal')
            except (Canceled, RelocalizationError):
                # An acceptance delivered after cancellation still owns motion.
                late = self._wait_motion(sent, 'late Spin acceptance')
                if late.accepted:
                    late.cancel_goal_async()
                    self._wait_motion(late.get_result_async(), 'late Spin cancellation')
                raise
            if spin.accepted:
                result = spin.get_result_async()
            else:
                spin = None
                self.get_logger().warning('Spin was rejected; searching without rotating')
        else:
            self.get_logger().warning('Spin is unavailable; searching without rotating')
        estimate, ratio, scored_at, update_at = None, 0.0, None, 0.0
        try:
            while time.monotonic() < deadline:
                self._check(handle)
                with self.lock:
                    current, scan = self.estimate, self.scan
                # Do not start waiting for another scan at the search deadline.
                # Keep the last completed measurement when the ten seconds end.
                if (current is not None and current[0] >= started and current[0] != scored_at
                        and scan is not None and scan[0] >= current[0]):
                    ratio = self._score(handle, current, deadline)
                    estimate, scored_at = current, current[0]
                    self._feedback(handle, 'SEARCHING', ratio)
                if estimate is not None and ratio >= minimum:
                    break
                now = time.monotonic()
                if (result is None or result.done()) and now - update_at >= 0.2:
                    self._call(self.nomotion_update, 'AMCL update', deadline, handle)
                    update_at = now
                time.sleep(0.05)
        finally:
            if spin is not None and result is not None and not result.done():
                spin.cancel_goal_async()
                self._wait_motion(result, 'Spin cancellation')
        if estimate is None:
            raise RelocalizationError('AMCL reported no pose during the global search')
        with self.lock:
            current = self.estimate
        if current is not None and current[0] >= started:
            estimate = current
        return estimate, ratio

    def _wait_motion(self, future, label):
        """Keep this Action and its dependencies until owned motion really ends."""
        warn_at = time.monotonic() + RESTORE_TIMEOUT_S
        warned = False
        while not future.done():
            self._check(None)
            if not warned and time.monotonic() >= warn_at:
                self.get_logger().warning(f'{label} is unconfirmed; retaining motion ownership')
                warned = True
            time.sleep(0.05)
        return future.result()

    def _call(self, client, label, deadline, handle=None):
        return self._rpc(client, Empty.Request(), handle, deadline, label)

    def _wait_future(self, handle, future, deadline, label):
        while not future.done():
            if handle is not None:
                self._check(handle)
            if time.monotonic() >= deadline:
                raise RelocalizationError(f'{label} did not finish in time')
            time.sleep(0.05)
        try:
            return future.result()
        except Exception as error:
            raise RelocalizationError(f'{label} failed: {error}') from error

    def _check(self, handle):
        if handle is not None and handle.is_cancel_requested:
            raise Canceled('relocalization canceled')
        if not rclpy.ok(context=self.context):
            raise RelocalizationError('relocalization server is shutting down')

    def _feedback(self, handle, state, ratio=0.0):
        feedback = Relocalize.Feedback()
        feedback.state = state
        feedback.match_ratio = float(ratio)
        handle.publish_feedback(feedback)

    def save_latest(self):
        """Write only a new, recent estimate while AMCL localizes on a saved map."""
        with self.lock:
            identity, latest, received = self.identity, self.latest, self.received
        if (identity is None or self.busy or not self.saving or not self.amcl_active
                or latest is None
                or received == self.saved_received
                or time.monotonic() - received > 2 * self.period):
            return
        try:
            write_pose(self.path, identity, latest)
            self.saved_received = received
        except OSError as error:
            self.get_logger().error(f'Cannot save localization pose: {error}')


def main(args=None):
    """Serve relocalization; publish only /initialpose, never velocity."""
    rclpy.init(args=args)
    node = None
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        node = Relocalization()
        executor.add_node(node)
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown()
        # Humble's executor does not join its worker pool. Drain callbacks
        # before destroying the ROS handles those callbacks may still use.
        executor._executor.shutdown(wait=True)
        if node is not None:
            node.save_latest()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
