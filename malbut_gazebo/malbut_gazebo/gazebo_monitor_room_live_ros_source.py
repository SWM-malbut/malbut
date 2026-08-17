"""
Read-only ROS 2 Humble evidence source for monitor-room preflight.

This boundary may inspect Nav2 lifecycle state, localization, the global
costmap, and a ``ComputePathToPose`` result.  It deliberately has no
``NavigateToPose``, cancel, velocity, or drive surface.
"""

from abc import ABC, abstractmethod
from array import array
from dataclasses import dataclass
import hashlib
import json
import math
import struct
from threading import Event, RLock
import time
from types import MappingProxyType
from typing import Any, Callable
from weakref import WeakKeyDictionary

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Duration as DurationMessage
from builtin_interfaces.msg import Time as TimeMessage
from geometry_msgs.msg import (
    Point,
    Pose,
    PoseStamped,
    Quaternion,
    Transform,
    TransformStamped,
    Vector3,
)
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import ComputePathToPose
from nav2_msgs.msg import Costmap, CostmapMetaData
from nav2_msgs.srv import GetCostmap
from nav_msgs.msg import Path
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.time import Time
from std_msgs.msg import Header
import tf2_ros

from malbut_gazebo.gazebo_monitor_room_live_validator import (
    GazeboMonitorRoomLiveEvidence,
    GazeboMonitorRoomLiveEvidenceUnavailableError,
    TrustedGazeboMonitorRoomLiveEvidenceSource,
)
from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    Nav2PreflightRequest,
)
from malbut_gazebo.gazebo_monitor_room_navigation_safety import (
    MAX_ABS_COORDINATE_M,
    MAX_ENDPOINT_GAP_M,
    MAX_GRID_CELLS,
    MAX_GRID_DIMENSION,
    MAX_PATH_POINTS,
    MAX_RESOLUTION_M,
    MIN_RESOLUTION_M,
    MapCostGrid,
    PathPoint,
    SamplePath,
)


MAP_FRAME = 'map'
BASE_FRAME = 'base_footprint'
COMPUTE_PATH_ACTION_FQN = '/compute_path_to_pose'
GLOBAL_COSTMAP_SERVICE_FQN = '/global_costmap/get_costmap'
LIFECYCLE_SERVICE_FQNS = (
    '/amcl/get_state',
    '/bt_navigator/get_state',
    '/planner_server/get_state',
    '/controller_server/get_state',
    '/global_costmap/global_costmap/get_state',
)
ROS_CALL_TIMEOUT_SECONDS = 1.0
CAPTURE_TIMEOUT_SECONDS = 2.0
LIVE_EVIDENCE_TTL_SECONDS = 1.0
MAX_ROS_EVIDENCE_AGE_NS = 2_000_000_000
MAX_ROS_FUTURE_SKEW_NS = 100_000_000

_HEX_DIGITS = frozenset('0123456789abcdef')
_FIXED_TOPIC_FQNS = (
    '/tf',
    '/tf_static',
    COMPUTE_PATH_ACTION_FQN,
    f'{COMPUTE_PATH_ACTION_FQN}/_action/feedback',
    f'{COMPUTE_PATH_ACTION_FQN}/_action/status',
)
_FIXED_SERVICE_FQNS = LIFECYCLE_SERVICE_FQNS + (
    GLOBAL_COSTMAP_SERVICE_FQN,
    f'{COMPUTE_PATH_ACTION_FQN}/_action/send_goal',
    f'{COMPUTE_PATH_ACTION_FQN}/_action/get_result',
    f'{COMPUTE_PATH_ACTION_FQN}/_action/cancel_goal',
)

_FACADE_SEAL_LOCK = RLock()
_FACADE_SEALS: WeakKeyDictionary[Any, tuple[Any, ...]] = (
    WeakKeyDictionary()
)
_SOURCE_SEAL_LOCK = RLock()
_SOURCE_SEALS: WeakKeyDictionary[Any, tuple[Any, ...]] = (
    WeakKeyDictionary()
)


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


_FIXED_CONFIGURATION_DIGEST = _digest_json({
    'contract': 'gazebo-monitor-room-live-ros-configuration-v1',
    'use_sim_time': True,
    'map_frame': MAP_FRAME,
    'base_frame': BASE_FRAME,
    'lifecycle_services': LIFECYCLE_SERVICE_FQNS,
    'global_costmap_service': GLOBAL_COSTMAP_SERVICE_FQN,
    'compute_path_action': COMPUTE_PATH_ACTION_FQN,
    'topics': _FIXED_TOPIC_FQNS,
    'services': _FIXED_SERVICE_FQNS,
    'call_timeout_seconds': ROS_CALL_TIMEOUT_SECONDS,
    'capture_timeout_seconds': CAPTURE_TIMEOUT_SECONDS,
})


class GazeboMonitorRoomLiveRosSourceError(RuntimeError):
    """Expose one of a small set of content-free source failures."""

    _CODES = frozenset({
        'live_ros_source_invalid_configuration',
        'live_ros_source_invalid_request',
        'live_ros_source_evidence_rejected',
    })

    def __init__(self, code: str) -> None:
        """Create a bounded error without rejected ROS content."""
        normalized = (
            code if type(code) is str and code in self._CODES
            else 'live_ros_source_evidence_rejected'
        )
        super().__init__(normalized)
        self.code = normalized

    def __getattribute__(self, name):
        """Hide collaborator exception chains at the public boundary."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class _RosFacadeUnavailableError(RuntimeError):
    """Mark a temporary bounded ROS transport failure."""


class _RosFacadeRejectedError(RuntimeError):
    """Mark an accepted planner request that produced no safe result."""


class TrustedGazeboMonitorRoomLiveRosFacade(ABC):
    """Define the narrow trusted ROS observation and planning surface."""

    @abstractmethod
    def assert_fixed_configuration(self) -> str:
        """Revalidate fixed frames, endpoints, and simulated ROS time."""

    @abstractmethod
    def lifecycle_state(
        self, service_fqn: str, timeout_seconds: float
    ) -> GetState.Response:
        """Fetch one exact lifecycle service response."""

    @abstractmethod
    def lookup_transform(
        self,
        target_frame: str,
        source_frame: str,
        timeout_seconds: float,
    ) -> TransformStamped:
        """Fetch the latest exact transform without changing robot state."""

    @abstractmethod
    def global_costmap(
        self, service_fqn: str, timeout_seconds: float
    ) -> GetCostmap.Response:
        """Fetch one exact global-costmap service response."""

    @abstractmethod
    def compute_path(
        self,
        action_fqn: str,
        goal: ComputePathToPose.Goal,
        timeout_seconds: float,
    ) -> ComputePathToPose.Result:
        """Run only the read-only ComputePathToPose action."""

    @abstractmethod
    def ros_now_nanoseconds(self) -> int:
        """Read the node's simulated ROS clock in nanoseconds."""


def _bounded_timeout(value: Any) -> float:
    if (
        type(value) is not float
        or not math.isfinite(value)
        or value <= 0.0
        or value > ROS_CALL_TIMEOUT_SECONDS
    ):
        raise _RosFacadeRejectedError
    return value


def _deadline_clock() -> float:
    try:
        value = time.clock_gettime(time.CLOCK_BOOTTIME)
    except Exception:
        raise _RosFacadeUnavailableError from None
    if type(value) is not float or not math.isfinite(value) or value < 0.0:
        raise _RosFacadeUnavailableError
    return value


def _facade_deadline(timeout_seconds: float) -> float:
    return _deadline_clock() + _bounded_timeout(timeout_seconds)


def _facade_remaining(deadline: float) -> float:
    remaining = deadline - _deadline_clock()
    if not math.isfinite(remaining) or remaining <= 0.0:
        raise _RosFacadeUnavailableError
    return min(ROS_CALL_TIMEOUT_SECONDS, remaining)


def _wait_for_future(future: Any, timeout_seconds: float) -> Any:
    timeout = _bounded_timeout(timeout_seconds)
    completed = Event()
    try:
        future.add_done_callback(lambda _future: completed.set())
        already_done = future.done()
    except Exception:
        raise _RosFacadeUnavailableError from None
    if not already_done and not completed.wait(timeout):
        raise _RosFacadeUnavailableError
    failed = False
    result = None
    try:
        result = future.result()
    except Exception:
        failed = True
    if failed or result is None:
        raise _RosFacadeUnavailableError
    return result


def _node_configuration_digest(node: Node) -> str:
    snapshots = []
    failed = False
    try:
        for _pass in range(2):
            use_sim_time = node.get_parameter('use_sim_time').value
            topics = tuple(
                node.resolve_topic_name(name)
                for name in _FIXED_TOPIC_FQNS
            )
            services = tuple(
                node.resolve_service_name(name)
                for name in _FIXED_SERVICE_FQNS
            )
            use_sim_time_after = (
                node.get_parameter('use_sim_time').value
            )
            snapshots.append((
                use_sim_time,
                use_sim_time_after,
                topics,
                services,
            ))
    except Exception:
        failed = True
    if failed or len(snapshots) != 2 or snapshots[0] != snapshots[1]:
        raise GazeboMonitorRoomLiveRosSourceError(
            'live_ros_source_invalid_configuration'
        )
    for use_time, use_time_after, topics, services in snapshots:
        if (
            type(use_time) is not bool
            or use_time is not True
            or type(use_time_after) is not bool
            or use_time_after is not True
            or topics != _FIXED_TOPIC_FQNS
            or services != _FIXED_SERVICE_FQNS
        ):
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
    return _FIXED_CONFIGURATION_DIGEST


class GazeboMonitorRoomRclpyLiveRosFacade(
    TrustedGazeboMonitorRoomLiveRosFacade
):
    """Own fixed ROS Humble clients without issuing work at construction."""

    def __init__(
        self,
        node: Node,
        *,
        future_waiter: Callable[[Any, float], Any] | None = None,
    ) -> None:
        """Create fixed read-only entities; make no service/action call."""
        if not isinstance(node, Node):
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        waiter = _wait_for_future if future_waiter is None else future_waiter
        if not callable(waiter):
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        _node_configuration_digest(node)
        failed = False
        lifecycle_clients = None
        costmap_client = None
        compute_path_client = None
        tf_buffer = None
        tf_listener = None
        try:
            callback_group = ReentrantCallbackGroup()
            lifecycle_clients = {
                name: node.create_client(
                    GetState,
                    name,
                    callback_group=callback_group,
                )
                for name in LIFECYCLE_SERVICE_FQNS
            }
            costmap_client = node.create_client(
                GetCostmap,
                GLOBAL_COSTMAP_SERVICE_FQN,
                callback_group=callback_group,
            )
            compute_path_client = ActionClient(
                node,
                ComputePathToPose,
                COMPUTE_PATH_ACTION_FQN,
                callback_group=callback_group,
            )
            tf_buffer = tf2_ros.Buffer(node=node)
            tf_listener = tf2_ros.TransformListener(
                tf_buffer,
                node,
                spin_thread=False,
            )
        except Exception:
            failed = True
        if failed:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        object.__setattr__(self, '_node', node)
        object.__setattr__(
            self,
            '_lifecycle_clients',
            MappingProxyType(lifecycle_clients),
        )
        object.__setattr__(self, '_costmap_client', costmap_client)
        object.__setattr__(self, '_compute_path_client', compute_path_client)
        object.__setattr__(self, '_tf_buffer', tf_buffer)
        object.__setattr__(self, '_tf_listener', tf_listener)
        object.__setattr__(self, '_waiter', waiter)
        object.__setattr__(self, '_configuration', _node_configuration_digest)
        object.__setattr__(self, '_sealed', True)
        seal = (
            node,
            object.__getattribute__(self, '_lifecycle_clients'),
            tuple(lifecycle_clients.items()),
            costmap_client,
            compute_path_client,
            tf_buffer,
            tf_listener,
            waiter,
            _node_configuration_digest,
        )
        with _FACADE_SEAL_LOCK:
            _FACADE_SEALS[self] = seal

    def __setattr__(self, name, value):
        """Prevent post-construction endpoint or executor replacement."""
        if getattr(self, '_sealed', False):
            raise AttributeError('live ROS facade is immutable')
        object.__setattr__(self, name, value)

    def assert_fixed_configuration(self) -> str:
        """Revalidate sim time and every fixed ROS endpoint twice."""
        seal = GazeboMonitorRoomRclpyLiveRosFacade._require_sealed(self)
        return seal[8](seal[0])

    def _require_sealed(self) -> tuple[Any, ...]:
        """Return original collaborators after external seal attestation."""
        expected = None
        try:
            with _FACADE_SEAL_LOCK:
                expected = _FACADE_SEALS.get(self)
            storage = object.__getattribute__(self, '__dict__')
            lifecycle = object.__getattribute__(
                self, '_lifecycle_clients'
            )
            current_items = tuple(lifecycle.items())
            invalid = (
                type(self) is not GazeboMonitorRoomRclpyLiveRosFacade
                or type(expected) is not tuple
                or len(expected) != 9
                or type(storage) is not dict
                or set(storage) != {
                    '_node',
                    '_lifecycle_clients',
                    '_costmap_client',
                    '_compute_path_client',
                    '_tf_buffer',
                    '_tf_listener',
                    '_waiter',
                    '_configuration',
                    '_sealed',
                }
                or object.__getattribute__(self, '_sealed') is not True
                or object.__getattribute__(self, '_node') is not expected[0]
                or lifecycle is not expected[1]
                or len(current_items) != len(expected[2])
                or any(
                    current_name != expected_name
                    or current_client is not expected_client
                    for (current_name, current_client),
                    (expected_name, expected_client)
                    in zip(current_items, expected[2])
                )
                or object.__getattribute__(
                    self, '_costmap_client'
                ) is not expected[3]
                or object.__getattribute__(
                    self, '_compute_path_client'
                ) is not expected[4]
                or object.__getattribute__(
                    self, '_tf_buffer'
                ) is not expected[5]
                or object.__getattribute__(
                    self, '_tf_listener'
                ) is not expected[6]
                or object.__getattribute__(
                    self, '_waiter'
                ) is not expected[7]
                or object.__getattribute__(
                    self, '_configuration'
                ) is not expected[8]
            )
        except Exception:
            invalid = True
        if invalid or expected is None:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        return expected

    def lifecycle_state(
        self, service_fqn: str, timeout_seconds: float
    ) -> GetState.Response:
        """Call one allow-listed lifecycle service with a bounded wait."""
        seal = GazeboMonitorRoomRclpyLiveRosFacade._require_sealed(self)
        deadline = _facade_deadline(timeout_seconds)
        if (
            type(service_fqn) is not str
            or service_fqn not in LIFECYCLE_SERVICE_FQNS
        ):
            raise _RosFacadeRejectedError
        clients = dict(seal[2])
        client = clients[service_fqn]
        try:
            ready = client.wait_for_service(
                timeout_sec=_facade_remaining(deadline)
            )
        except Exception:
            raise _RosFacadeUnavailableError from None
        if ready is not True:
            raise _RosFacadeUnavailableError
        failed = False
        future = None
        try:
            future = client.call_async(GetState.Request())
        except Exception:
            failed = True
        if failed or future is None:
            raise _RosFacadeUnavailableError
        return seal[7](future, _facade_remaining(deadline))

    def lookup_transform(
        self,
        target_frame: str,
        source_frame: str,
        timeout_seconds: float,
    ) -> TransformStamped:
        """Look up only the fixed map-to-base transform."""
        seal = GazeboMonitorRoomRclpyLiveRosFacade._require_sealed(self)
        deadline = _facade_deadline(timeout_seconds)
        if target_frame != MAP_FRAME or source_frame != BASE_FRAME:
            raise _RosFacadeRejectedError
        failed = False
        result = None
        try:
            result = seal[5].lookup_transform(
                MAP_FRAME,
                BASE_FRAME,
                Time(),
                timeout=Duration(
                    seconds=_facade_remaining(deadline)
                ),
            )
        except Exception:
            failed = True
        if failed or result is None:
            raise _RosFacadeUnavailableError
        return result

    def global_costmap(
        self, service_fqn: str, timeout_seconds: float
    ) -> GetCostmap.Response:
        """Call only the fixed global-costmap read service."""
        seal = GazeboMonitorRoomRclpyLiveRosFacade._require_sealed(self)
        deadline = _facade_deadline(timeout_seconds)
        if service_fqn != GLOBAL_COSTMAP_SERVICE_FQN:
            raise _RosFacadeRejectedError
        try:
            ready = seal[3].wait_for_service(
                timeout_sec=_facade_remaining(deadline)
            )
        except Exception:
            raise _RosFacadeUnavailableError from None
        if ready is not True:
            raise _RosFacadeUnavailableError
        failed = False
        future = None
        try:
            future = seal[3].call_async(
                GetCostmap.Request()
            )
        except Exception:
            failed = True
        if failed or future is None:
            raise _RosFacadeUnavailableError
        return seal[7](future, _facade_remaining(deadline))

    def compute_path(
        self,
        action_fqn: str,
        goal: ComputePathToPose.Goal,
        timeout_seconds: float,
    ) -> ComputePathToPose.Result:
        """Run the fixed planner action and never send a motion goal."""
        seal = GazeboMonitorRoomRclpyLiveRosFacade._require_sealed(self)
        deadline = _facade_deadline(timeout_seconds)
        if (
            action_fqn != COMPUTE_PATH_ACTION_FQN
            or type(goal) is not ComputePathToPose.Goal
        ):
            raise _RosFacadeRejectedError
        try:
            ready = seal[4].wait_for_server(
                timeout_sec=_facade_remaining(deadline)
            )
        except Exception:
            raise _RosFacadeUnavailableError from None
        if ready is not True:
            raise _RosFacadeUnavailableError
        failed = False
        send_future = None
        try:
            send_future = seal[4].send_goal_async(goal)
        except Exception:
            failed = True
        if failed or send_future is None:
            raise _RosFacadeUnavailableError
        goal_handle = seal[7](
            send_future, _facade_remaining(deadline)
        )
        if type(getattr(goal_handle, 'accepted', None)) is not bool:
            raise _RosFacadeRejectedError
        if goal_handle.accepted is not True:
            raise _RosFacadeRejectedError
        failed = False
        result_future = None
        try:
            result_future = goal_handle.get_result_async()
        except Exception:
            failed = True
        if failed or result_future is None:
            raise _RosFacadeUnavailableError
        wrapped = seal[7](
            result_future, _facade_remaining(deadline)
        )
        if (
            type(wrapped)
            is not ComputePathToPose.Impl.GetResultService.Response
            or type(wrapped.status) is not int
            or wrapped.status != GoalStatus.STATUS_SUCCEEDED
            or type(wrapped.result) is not ComputePathToPose.Result
        ):
            raise _RosFacadeRejectedError
        return wrapped.result

    def ros_now_nanoseconds(self) -> int:
        """Read the node's configured simulated ROS clock."""
        seal = GazeboMonitorRoomRclpyLiveRosFacade._require_sealed(self)
        failed = False
        value = None
        try:
            value = seal[0].get_clock().now().nanoseconds
        except Exception:
            failed = True
        if failed or type(value) is not int or value < 0:
            raise _RosFacadeUnavailableError
        return value


@dataclass(frozen=True, slots=True)
class _LifecycleSnapshot:
    service_fqn: str
    state_id: int
    state_label: str


@dataclass(frozen=True, slots=True)
class _TransformSnapshot:
    stamp_ns: int
    x_m: float
    y_m: float
    z_m: float
    qx: float
    qy: float
    qz: float
    qw: float

    @property
    def state_digest(self) -> str:
        return _digest_json({
            'contract': 'gazebo-monitor-room-live-transform-state-v1',
            'frame_id': MAP_FRAME,
            'child_frame_id': BASE_FRAME,
            'translation': tuple(
                value.hex() for value in (self.x_m, self.y_m, self.z_m)
            ),
            'rotation': tuple(
                value.hex()
                for value in (self.qx, self.qy, self.qz, self.qw)
            ),
        })


@dataclass(frozen=True, slots=True)
class _CostmapSnapshot:
    header_stamp_ns: int
    map_load_time_ns: int
    update_time_ns: int
    layer: str
    width: int
    height: int
    resolution_m: float
    origin_x_m: float
    origin_y_m: float
    costs: tuple[int, ...]

    @property
    def state_digest(self) -> str:
        return _digest_json({
            'contract': 'gazebo-monitor-room-live-costmap-state-v1',
            'frame_id': MAP_FRAME,
            'layer': self.layer,
            'width': self.width,
            'height': self.height,
            'resolution': self.resolution_m.hex(),
            'origin_x': self.origin_x_m.hex(),
            'origin_y': self.origin_y_m.hex(),
            'origin_yaw': 0.0.hex(),
            'costs_sha256': hashlib.sha256(bytes(self.costs)).hexdigest(),
        })


@dataclass(frozen=True, slots=True)
class _PathSnapshot:
    header_stamp_ns: int
    planning_time_ns: int
    points: tuple[tuple[float, float], ...]
    content_digest: str


def _finite_float(value: Any, *, coordinate: bool = False) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise _RosFacadeRejectedError
    if coordinate and abs(value) > MAX_ABS_COORDINATE_M:
        raise _RosFacadeRejectedError
    return 0.0 if value == 0.0 else value


def _time_ns(value: Any) -> int:
    if type(value) is not TimeMessage:
        raise _RosFacadeRejectedError
    first = (value.sec, value.nanosec)
    second = (value.sec, value.nanosec)
    if first != second:
        raise _RosFacadeRejectedError
    sec, nanosec = first
    if (
        type(sec) is not int
        or type(nanosec) is not int
        or sec < 0
        or not 0 <= nanosec < 1_000_000_000
    ):
        raise _RosFacadeRejectedError
    return sec * 1_000_000_000 + nanosec


def _duration_ns(value: Any) -> int:
    if type(value) is not DurationMessage:
        raise _RosFacadeRejectedError
    first = (value.sec, value.nanosec)
    second = (value.sec, value.nanosec)
    if first != second:
        raise _RosFacadeRejectedError
    sec, nanosec = first
    if (
        type(sec) is not int
        or type(nanosec) is not int
        or sec < 0
        or not 0 <= nanosec < 1_000_000_000
    ):
        raise _RosFacadeRejectedError
    return sec * 1_000_000_000 + nanosec


def _fresh_stamp(stamp_ns: int, ros_now_ns: int) -> None:
    if (
        type(ros_now_ns) is not int
        or ros_now_ns <= 0
        or stamp_ns <= 0
        or stamp_ns > ros_now_ns + MAX_ROS_FUTURE_SKEW_NS
        or ros_now_ns - stamp_ns > MAX_ROS_EVIDENCE_AGE_NS
    ):
        raise _RosFacadeRejectedError


def _quaternion(value: Any, *, require_zero_yaw: bool) -> tuple:
    if type(value) is not Quaternion:
        raise _RosFacadeRejectedError
    first = (value.x, value.y, value.z, value.w)
    second = (value.x, value.y, value.z, value.w)
    if first != second:
        raise _RosFacadeRejectedError
    values = tuple(_finite_float(item) for item in first)
    norm = math.sqrt(sum(item * item for item in values))
    if abs(norm - 1.0) > 0.000001:
        raise _RosFacadeRejectedError
    qx, qy, qz, qw = values
    if abs(qx) > 0.000001 or abs(qy) > 0.000001:
        raise _RosFacadeRejectedError
    yaw = math.atan2(
        2.0 * (qw * qz + qx * qy),
        1.0 - 2.0 * (qy * qy + qz * qz),
    )
    if require_zero_yaw and abs(yaw) > 0.000001:
        raise _RosFacadeRejectedError
    return qx, qy, qz, qw


def _point(value: Any, *, require_planar: bool) -> tuple:
    if type(value) is not Point:
        raise _RosFacadeRejectedError
    first = (value.x, value.y, value.z)
    second = (value.x, value.y, value.z)
    if first != second:
        raise _RosFacadeRejectedError
    x_m, y_m, z_m = tuple(
        _finite_float(item, coordinate=True) for item in first
    )
    if require_planar and abs(z_m) > 0.000001:
        raise _RosFacadeRejectedError
    return x_m, y_m, z_m


def _vector(value: Any) -> tuple:
    if type(value) is not Vector3:
        raise _RosFacadeRejectedError
    first = (value.x, value.y, value.z)
    second = (value.x, value.y, value.z)
    if first != second:
        raise _RosFacadeRejectedError
    return tuple(
        _finite_float(item, coordinate=True) for item in first
    )


def _header(value: Any, *, frame_id: str) -> int:
    if type(value) is not Header:
        raise _RosFacadeRejectedError
    first_frame = value.frame_id
    stamp = _time_ns(value.stamp)
    second_frame = value.frame_id
    if (
        type(first_frame) is not str
        or first_frame != frame_id
        or second_frame != first_frame
    ):
        raise _RosFacadeRejectedError
    return stamp


def _lifecycle_snapshot(
    service_fqn: str, response: Any
) -> _LifecycleSnapshot:
    if type(response) is not GetState.Response:
        raise _RosFacadeRejectedError
    state = response.current_state
    if type(state) is not State:
        raise _RosFacadeRejectedError
    first = (state.id, state.label)
    second = (state.id, state.label)
    if (
        first != second
        or type(first[0]) is not int
        or first[0] != State.PRIMARY_STATE_ACTIVE
        or type(first[1]) is not str
        or first[1] != 'active'
    ):
        raise _RosFacadeRejectedError
    return _LifecycleSnapshot(service_fqn, first[0], first[1])


def _transform_snapshot(
    response: Any, *, ros_now_ns: int
) -> _TransformSnapshot:
    if type(response) is not TransformStamped:
        raise _RosFacadeRejectedError
    stamp_ns = _header(response.header, frame_id=MAP_FRAME)
    child_before = response.child_frame_id
    transform = response.transform
    child_after = response.child_frame_id
    if (
        child_before != BASE_FRAME
        or child_after != child_before
        or type(transform) is not Transform
    ):
        raise _RosFacadeRejectedError
    translation = _vector(transform.translation)
    rotation = _quaternion(transform.rotation, require_zero_yaw=False)
    _fresh_stamp(stamp_ns, ros_now_ns)
    return _TransformSnapshot(
        stamp_ns,
        translation[0],
        translation[1],
        translation[2],
        rotation[0],
        rotation[1],
        rotation[2],
        rotation[3],
    )


def _canonical_float32_decimal(value: float) -> float:
    raw = struct.pack('!f', value)
    for significant_digits in range(1, 10):
        candidate = float(format(value, f'.{significant_digits}g'))
        if struct.pack('!f', candidate) == raw:
            return candidate
    raise _RosFacadeRejectedError


def _costmap_snapshot(
    response: Any, *, ros_now_ns: int
) -> _CostmapSnapshot:
    if type(response) is not GetCostmap.Response:
        raise _RosFacadeRejectedError
    costmap = response.map
    if type(costmap) is not Costmap:
        raise _RosFacadeRejectedError
    header_stamp_ns = _header(costmap.header, frame_id=MAP_FRAME)
    metadata = costmap.metadata
    if type(metadata) is not CostmapMetaData:
        raise _RosFacadeRejectedError
    map_load_time_ns = _time_ns(metadata.map_load_time)
    update_time_ns = _time_ns(metadata.update_time)
    layer_before = metadata.layer
    width_before = metadata.size_x
    height_before = metadata.size_y
    resolution_before = metadata.resolution
    origin = metadata.origin
    layer_after = metadata.layer
    width_after = metadata.size_x
    height_after = metadata.size_y
    resolution_after = metadata.resolution
    if (
        type(layer_before) is not str
        or not layer_before
        or len(layer_before.encode('utf-8')) > 128
        or layer_after != layer_before
        or type(width_before) is not int
        or type(height_before) is not int
        or width_after != width_before
        or height_after != height_before
        or not 1 <= width_before <= MAX_GRID_DIMENSION
        or not 1 <= height_before <= MAX_GRID_DIMENSION
        or width_before * height_before > MAX_GRID_CELLS
        or type(resolution_before) is not float
        or resolution_after.hex() != resolution_before.hex()
        or type(origin) is not Pose
    ):
        raise _RosFacadeRejectedError
    resolution = _finite_float(resolution_before)
    if not MIN_RESOLUTION_M <= resolution <= MAX_RESOLUTION_M:
        raise _RosFacadeRejectedError
    resolution = _canonical_float32_decimal(resolution)
    origin_x, origin_y, _origin_z = _point(
        origin.position, require_planar=True
    )
    _quaternion(origin.orientation, require_zero_yaw=True)
    costs_value = costmap.data
    if type(costs_value) is not array or costs_value.typecode != 'B':
        raise _RosFacadeRejectedError
    costs_first = tuple(costs_value)
    costs_second = tuple(costmap.data)
    if (
        costs_first != costs_second
        or len(costs_first) != width_before * height_before
        or any(
            type(value) is not int or not 0 <= value <= 255
            for value in costs_first
        )
    ):
        raise _RosFacadeRejectedError
    _fresh_stamp(header_stamp_ns, ros_now_ns)
    _fresh_stamp(update_time_ns, ros_now_ns)
    if map_load_time_ns > ros_now_ns + MAX_ROS_FUTURE_SKEW_NS:
        raise _RosFacadeRejectedError
    return _CostmapSnapshot(
        header_stamp_ns,
        map_load_time_ns,
        update_time_ns,
        layer_before,
        width_before,
        height_before,
        resolution,
        origin_x,
        origin_y,
        costs_first,
    )


def _pose_stamped_snapshot(
    value: Any,
    *,
    ros_now_ns: int,
    allow_zero_stamp: bool,
) -> tuple[int, float, float, float, float, float, float, float]:
    if type(value) is not PoseStamped:
        raise _RosFacadeRejectedError
    stamp_ns = _header(value.header, frame_id=MAP_FRAME)
    pose = value.pose
    if type(pose) is not Pose:
        raise _RosFacadeRejectedError
    x_m, y_m, z_m = _point(pose.position, require_planar=True)
    rotation = _quaternion(pose.orientation, require_zero_yaw=False)
    if not (allow_zero_stamp and stamp_ns == 0):
        _fresh_stamp(stamp_ns, ros_now_ns)
    return (
        stamp_ns,
        x_m,
        y_m,
        z_m,
        rotation[0],
        rotation[1],
        rotation[2],
        rotation[3],
    )


def _path_snapshot(
    result: Any,
    *,
    ros_now_ns: int,
    start_point: PathPoint,
    target_point: PathPoint,
) -> _PathSnapshot:
    if type(result) is not ComputePathToPose.Result:
        raise _RosFacadeRejectedError
    path = result.path
    if type(path) is not Path:
        raise _RosFacadeRejectedError
    header_stamp_ns = _header(path.header, frame_id=MAP_FRAME)
    _fresh_stamp(header_stamp_ns, ros_now_ns)
    planning_time_ns = _duration_ns(result.planning_time)
    if planning_time_ns > int(ROS_CALL_TIMEOUT_SECONDS * 1_000_000_000):
        raise _RosFacadeRejectedError
    poses = path.poses
    if type(poses) is not list or not 1 <= len(poses) <= MAX_PATH_POINTS:
        raise _RosFacadeRejectedError
    first = tuple(
        _pose_stamped_snapshot(
            pose,
            ros_now_ns=ros_now_ns,
            allow_zero_stamp=True,
        )
        for pose in poses
    )
    second = tuple(
        _pose_stamped_snapshot(
            pose,
            ros_now_ns=ros_now_ns,
            allow_zero_stamp=True,
        )
        for pose in path.poses
    )
    if first != second:
        raise _RosFacadeRejectedError
    points = tuple((item[1], item[2]) for item in first)
    expected_start = (
        object.__getattribute__(start_point, '_x_m'),
        object.__getattribute__(start_point, '_y_m'),
    )
    expected_target = (
        object.__getattribute__(target_point, '_x_m'),
        object.__getattribute__(target_point, '_y_m'),
    )
    if (
        math.hypot(
            points[0][0] - expected_start[0],
            points[0][1] - expected_start[1],
        ) > MAX_ENDPOINT_GAP_M
        or math.hypot(
            points[-1][0] - expected_target[0],
            points[-1][1] - expected_target[1],
        ) > MAX_ENDPOINT_GAP_M
    ):
        raise _RosFacadeRejectedError
    content_digest = _digest_json({
        'contract': 'gazebo-monitor-room-live-compute-path-result-v1',
        'frame_id': MAP_FRAME,
        'header_stamp_ns': header_stamp_ns,
        'planning_time_ns': planning_time_ns,
        'poses': tuple(
            tuple(
                item.hex() if type(item) is float else item
                for item in pose
            )
            for pose in first
        ),
    })
    return _PathSnapshot(
        header_stamp_ns,
        planning_time_ns,
        points,
        content_digest,
    )


def _goal_snapshot(goal: Any) -> tuple:
    if type(goal) is not ComputePathToPose.Goal:
        raise _RosFacadeRejectedError
    if type(goal.use_start) is not bool or goal.use_start is not True:
        raise _RosFacadeRejectedError
    if type(goal.planner_id) is not str or goal.planner_id != '':
        raise _RosFacadeRejectedError
    return (
        _pose_stamped_snapshot(
            goal.start,
            ros_now_ns=_time_ns(goal.start.header.stamp),
            allow_zero_stamp=False,
        ),
        _pose_stamped_snapshot(
            goal.goal,
            ros_now_ns=_time_ns(goal.goal.header.stamp),
            allow_zero_stamp=False,
        ),
        goal.planner_id,
        goal.use_start,
    )


def _time_message(nanoseconds: int) -> TimeMessage:
    if type(nanoseconds) is not int or nanoseconds <= 0:
        raise _RosFacadeRejectedError
    value = TimeMessage()
    value.sec = nanoseconds // 1_000_000_000
    value.nanosec = nanoseconds % 1_000_000_000
    return value


def _pose_stamped(
    *,
    stamp: TimeMessage,
    x_m: float,
    y_m: float,
    qz: float,
    qw: float,
) -> PoseStamped:
    result = PoseStamped()
    result.header.frame_id = MAP_FRAME
    result.header.stamp = stamp
    result.pose.position.x = x_m
    result.pose.position.y = y_m
    result.pose.position.z = 0.0
    result.pose.orientation.x = 0.0
    result.pose.orientation.y = 0.0
    result.pose.orientation.z = qz
    result.pose.orientation.w = qw
    return result


def _host_boottime() -> float:
    failed = False
    value = None
    try:
        value = time.clock_gettime(time.CLOCK_BOOTTIME)
    except Exception:
        failed = True
    if failed or type(value) is not float or not math.isfinite(value):
        raise GazeboMonitorRoomLiveRosSourceError(
            'live_ros_source_invalid_configuration'
        )
    return value


class GazeboMonitorRoomLiveRosSource(
    TrustedGazeboMonitorRoomLiveEvidenceSource
):
    """Capture one fail-closed, read-only ROS snapshot for preflight."""

    def __init__(
        self,
        facade: TrustedGazeboMonitorRoomLiveRosFacade,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Bind a trusted facade without making ROS service/action calls."""
        if not isinstance(facade, TrustedGazeboMonitorRoomLiveRosFacade):
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        source_clock = _host_boottime if clock is None else clock
        if not callable(source_clock):
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        failed = False
        methods = None
        try:
            methods = (
                facade.assert_fixed_configuration,
                facade.lifecycle_state,
                facade.lookup_transform,
                facade.global_costmap,
                facade.compute_path,
                facade.ros_now_nanoseconds,
            )
            if not all(callable(method) for method in methods):
                raise TypeError
            first = methods[0]()
            second = methods[0]()
            if (
                type(first) is not str
                or first != _FIXED_CONFIGURATION_DIGEST
                or second != first
            ):
                raise ValueError
        except Exception:
            failed = True
        if failed or methods is None:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        object.__setattr__(self, '_configuration', methods[0])
        object.__setattr__(self, '_lifecycle', methods[1])
        object.__setattr__(self, '_transform', methods[2])
        object.__setattr__(self, '_costmap', methods[3])
        object.__setattr__(self, '_compute_path', methods[4])
        object.__setattr__(self, '_ros_now', methods[5])
        object.__setattr__(self, '_clock', source_clock)
        object.__setattr__(self, '_sealed', True)
        seal = methods + (source_clock,)
        with _SOURCE_SEAL_LOCK:
            _SOURCE_SEALS[self] = seal

    def __setattr__(self, name, value):
        """Prevent post-construction collaborator replacement."""
        if getattr(self, '_sealed', False):
            raise AttributeError('live ROS source is immutable')
        object.__setattr__(self, name, value)

    def _require_sealed(self) -> tuple[Any, ...]:
        """Return original callbacks after external seal attestation."""
        expected = None
        try:
            with _SOURCE_SEAL_LOCK:
                expected = _SOURCE_SEALS.get(self)
            storage = object.__getattribute__(self, '__dict__')
            current = (
                object.__getattribute__(self, '_configuration'),
                object.__getattribute__(self, '_lifecycle'),
                object.__getattribute__(self, '_transform'),
                object.__getattribute__(self, '_costmap'),
                object.__getattribute__(self, '_compute_path'),
                object.__getattribute__(self, '_ros_now'),
                object.__getattribute__(self, '_clock'),
            )
            invalid = (
                type(self) is not GazeboMonitorRoomLiveRosSource
                or type(expected) is not tuple
                or len(expected) != 7
                or type(storage) is not dict
                or set(storage) != {
                    '_configuration',
                    '_lifecycle',
                    '_transform',
                    '_costmap',
                    '_compute_path',
                    '_ros_now',
                    '_clock',
                    '_sealed',
                }
                or object.__getattribute__(self, '_sealed') is not True
                or any(
                    current_item is not expected_item
                    for current_item, expected_item
                    in zip(current, expected)
                )
            )
        except Exception:
            invalid = True
        if invalid or expected is None:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        return expected

    def capture(
        self,
        request: Nav2PreflightRequest,
        *,
        checked_at: float,
        active_map_evidence_digest: str,
        semantic_content_digest: str,
    ) -> GazeboMonitorRoomLiveEvidence:
        """Capture lifecycle, TF, costmap, and a planner-only path."""
        unavailable = False
        rejected = False
        result = None
        try:
            result = GazeboMonitorRoomLiveRosSource._capture(
                self,
                request,
                checked_at=checked_at,
                active_map_evidence_digest=active_map_evidence_digest,
                semantic_content_digest=semantic_content_digest,
            )
        except (
            _RosFacadeUnavailableError,
            GazeboMonitorRoomLiveEvidenceUnavailableError,
        ):
            unavailable = True
        except GazeboMonitorRoomLiveRosSourceError:
            raise
        except Exception:
            rejected = True
        if unavailable:
            raise GazeboMonitorRoomLiveEvidenceUnavailableError(
                'live_evidence_unavailable'
            )
        if rejected or result is None:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_evidence_rejected'
            )
        return result

    def _capture(
        self,
        request: Nav2PreflightRequest,
        *,
        checked_at: float,
        active_map_evidence_digest: str,
        semantic_content_digest: str,
    ) -> GazeboMonitorRoomLiveEvidence:
        seal = GazeboMonitorRoomLiveRosSource._require_sealed(self)
        (
            configuration,
            lifecycle,
            transform,
            costmap_source,
            compute_path,
            ros_now,
            source_clock,
        ) = seal

        def clock_value() -> float:
            value = source_clock()
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value < 0.0
            ):
                raise GazeboMonitorRoomLiveRosSourceError(
                    'live_ros_source_invalid_configuration'
                )
            return value

        def remaining(deadline_value: float) -> float:
            value = deadline_value - clock_value()
            if value <= 0.0:
                raise _RosFacadeUnavailableError
            return min(ROS_CALL_TIMEOUT_SECONDS, value)

        canonical_request = _canonical_request(request)
        if (
            type(checked_at) is not float
            or not math.isfinite(checked_at)
            or checked_at < 0.0
            or not _valid_digest(active_map_evidence_digest)
            or not _valid_digest(semantic_content_digest)
        ):
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_request'
            )
        request_fingerprint = canonical_request.request_fingerprint
        operation_id = canonical_request.operation_id
        goal_uuid = canonical_request.goal_uuid
        target_point = PathPoint(
            canonical_request.x_m, canonical_request.y_m
        )
        capture_started_at = clock_value()
        if capture_started_at < checked_at:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        deadline = capture_started_at + CAPTURE_TIMEOUT_SECONDS
        if configuration() != _FIXED_CONFIGURATION_DIGEST:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        ros_start = ros_now()
        if type(ros_start) is not int or ros_start <= 0:
            raise _RosFacadeUnavailableError
        lifecycle_before = tuple(
            _lifecycle_snapshot(
                service,
                lifecycle(service, remaining(deadline)),
            )
            for service in LIFECYCLE_SERVICE_FQNS
        )
        tf_response = transform(
            MAP_FRAME,
            BASE_FRAME,
            remaining(deadline),
        )
        tf_before_now = ros_now()
        tf_before = _stable_snapshot(
            _transform_snapshot,
            tf_response,
            ros_now_ns=tf_before_now,
        )
        start_point = PathPoint(tf_before.x_m, tf_before.y_m)
        map_response = costmap_source(
            GLOBAL_COSTMAP_SERVICE_FQN,
            remaining(deadline),
        )
        map_before_now = ros_now()
        map_before = _stable_snapshot(
            _costmap_snapshot,
            map_response,
            ros_now_ns=map_before_now,
        )
        goal_stamp = _time_message(ros_now())
        goal = ComputePathToPose.Goal()
        goal.start = _pose_stamped(
            stamp=goal_stamp,
            x_m=tf_before.x_m,
            y_m=tf_before.y_m,
            qz=tf_before.qz,
            qw=tf_before.qw,
        )
        goal.goal = _pose_stamped(
            stamp=goal_stamp,
            x_m=canonical_request.x_m,
            y_m=canonical_request.y_m,
            qz=0.0,
            qw=1.0,
        )
        goal.planner_id = ''
        goal.use_start = True
        goal_before = _goal_snapshot(goal)
        path_result = compute_path(
            COMPUTE_PATH_ACTION_FQN,
            goal,
            remaining(deadline),
        )
        if _goal_snapshot(goal) != goal_before:
            raise _RosFacadeRejectedError
        path_now = ros_now()
        path_snapshot = _stable_snapshot(
            _path_snapshot,
            path_result,
            ros_now_ns=path_now,
            start_point=start_point,
            target_point=target_point,
        )
        lifecycle_after = tuple(
            _lifecycle_snapshot(
                service,
                lifecycle(service, remaining(deadline)),
            )
            for service in LIFECYCLE_SERVICE_FQNS
        )
        tf_after_response = transform(
            MAP_FRAME,
            BASE_FRAME,
            remaining(deadline),
        )
        tf_after_now = ros_now()
        tf_after = _stable_snapshot(
            _transform_snapshot,
            tf_after_response,
            ros_now_ns=tf_after_now,
        )
        map_after_response = costmap_source(
            GLOBAL_COSTMAP_SERVICE_FQN,
            remaining(deadline),
        )
        map_after_now = ros_now()
        map_after = _stable_snapshot(
            _costmap_snapshot,
            map_after_response,
            ros_now_ns=map_after_now,
        )
        ros_end = ros_now()
        if (
            lifecycle_after != lifecycle_before
            or tf_after.state_digest != tf_before.state_digest
            or map_after.state_digest != map_before.state_digest
            or type(ros_end) is not int
            or ros_end < ros_start
            or configuration() != _FIXED_CONFIGURATION_DIGEST
        ):
            raise _RosFacadeRejectedError
        costmap = MapCostGrid(
            MAP_FRAME,
            map_after.width,
            map_after.height,
            map_after.resolution_m,
            map_after.origin_x_m,
            map_after.origin_y_m,
            0.0,
            map_after.costs,
        )
        path = SamplePath(
            MAP_FRAME,
            [PathPoint(x_m, y_m) for x_m, y_m in path_snapshot.points],
        )
        lifecycle_digest = _digest_json({
            'contract': 'gazebo-monitor-room-live-lifecycle-v1',
            'states': tuple(
                (item.service_fqn, item.state_id, item.state_label)
                for item in lifecycle_after
            ),
        })
        transform_digest = _digest_json({
            'contract': 'gazebo-monitor-room-live-transform-v1',
            'state_digest': tf_after.state_digest,
            'stamp_ns': tf_after.stamp_ns,
        })
        compute_digest = _digest_json({
            'contract': 'gazebo-monitor-room-live-compute-path-v1',
            'request_fingerprint': request_fingerprint,
            'start_point_digest': start_point.digest,
            'target_point_digest': target_point.digest,
            'costmap_state_digest': map_after.state_digest,
            'path_content_digest': path_snapshot.content_digest,
            'path_digest': path.digest,
        })
        captured_at = clock_value()
        if captured_at < checked_at:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        if captured_at > deadline:
            raise _RosFacadeUnavailableError
        return GazeboMonitorRoomLiveEvidence(
            request_fingerprint=request_fingerprint,
            operation_id=operation_id,
            goal_uuid=goal_uuid,
            active_map_evidence_digest=active_map_evidence_digest,
            semantic_content_digest=semantic_content_digest,
            captured_at=captured_at,
            valid_until=captured_at + LIVE_EVIDENCE_TTL_SECONDS,
            lifecycle_ready=True,
            tf_ready=True,
            planner_succeeded=True,
            lifecycle_evidence_digest=lifecycle_digest,
            transform_evidence_digest=transform_digest,
            compute_path_evidence_digest=compute_digest,
            start_point=start_point,
            target_point=target_point,
            costmap=costmap,
            path=path,
        )

    def _clock_value(self) -> float:
        seal = GazeboMonitorRoomLiveRosSource._require_sealed(self)
        value = seal[6]()
        if type(value) is not float or not math.isfinite(value) or value < 0.0:
            raise GazeboMonitorRoomLiveRosSourceError(
                'live_ros_source_invalid_configuration'
            )
        return value

    def _remaining(self, deadline: float) -> float:
        remaining = (
            deadline
            - GazeboMonitorRoomLiveRosSource._clock_value(self)
        )
        if remaining <= 0.0:
            raise _RosFacadeUnavailableError
        return min(ROS_CALL_TIMEOUT_SECONDS, remaining)


def _stable_snapshot(function, value, **keywords):
    first = function(value, **keywords)
    second = function(value, **keywords)
    if type(first) is not type(second) or first != second:
        raise _RosFacadeRejectedError
    return first


def _canonical_request(value: Any) -> Nav2PreflightRequest:
    if type(value) is not Nav2PreflightRequest:
        raise GazeboMonitorRoomLiveRosSourceError(
            'live_ros_source_invalid_request'
        )
    failed = False
    canonical = None
    first_storage = None
    second_storage = None
    try:
        storage = object.__getattribute__(value, '__dict__')
        if type(storage) is not dict:
            raise TypeError
        first_storage = dict(storage)
        canonical = Nav2PreflightRequest(
            operation_id=first_storage['operation_id'],
            robot_id=first_storage['robot_id'],
            map_id=first_storage['map_id'],
            map_revision=first_storage['map_revision'],
            semantic_revision=first_storage['semantic_revision'],
            zones_digest=first_storage['zones_digest'],
            target_binding_digest=first_storage['target_binding_digest'],
            effects_digest=first_storage['effects_digest'],
            profile_digest=first_storage['profile_digest'],
            plan_digest=first_storage['plan_digest'],
            sample_count=first_storage['sample_count'],
            sample_index=first_storage['sample_index'],
            polygon_ordinal=first_storage['polygon_ordinal'],
            row_ordinal=first_storage['row_ordinal'],
            goal_uuid=first_storage['goal_uuid'],
            binding_digest=first_storage['binding_digest'],
            x_m=first_storage['x_m'],
            y_m=first_storage['y_m'],
            frame_id=first_storage['frame_id'],
        )
        second_storage = dict(object.__getattribute__(value, '__dict__'))
        canonical_storage = object.__getattribute__(canonical, '__dict__')
        if (
            first_storage.keys() != canonical_storage.keys()
            or second_storage.keys() != canonical_storage.keys()
            or any(
                type(first_storage[name]) is not type(expected)
                or first_storage[name] != expected
                or type(second_storage[name]) is not type(expected)
                or second_storage[name] != expected
                for name, expected in canonical_storage.items()
            )
        ):
            raise ValueError
    except Exception:
        failed = True
    if failed or canonical is None:
        raise GazeboMonitorRoomLiveRosSourceError(
            'live_ros_source_invalid_request'
        )
    return canonical


def _valid_digest(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value != '0' * 64
        and all(character in _HEX_DIGITS for character in value)
    )
