"""
ROS 2 Humble transport for the Gazebo monitor-room Nav2 contract.

The controller owns durable intent.  This module owns only the ROS action
wire boundary: exact stable goal UUIDs, bounded response waits, read-only
status reconciliation, and exact-goal cancellation.  Constructing the port
creates ROS entities but never sends or cancels a goal.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import json
import math
import re
from threading import Event, RLock
import time
from types import MappingProxyType
from typing import Any, Dict, Optional

from action_msgs.msg import GoalInfo, GoalStatus, GoalStatusArray
from action_msgs.srv import CancelGoal
from builtin_interfaces.msg import Time as TimeMessage
from geometry_msgs.msg import Point, Pose, PoseStamped, Quaternion
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from unique_identifier_msgs.msg import UUID

from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    Nav2CancelRequest,
    Nav2GoalQuery,
    Nav2PreflightRequest,
    Nav2StartRequest,
)


NAVIGATE_ACTION_FQN = '/navigate_to_pose'
NAVIGATE_STATUS_TOPIC_FQN = '/navigate_to_pose/_action/status'
NAVIGATE_CANCEL_SERVICE_FQN = (
    '/navigate_to_pose/_action/cancel_goal'
)
_FIXED_TOPIC_FQNS = (
    NAVIGATE_ACTION_FQN,
    '/navigate_to_pose/_action/feedback',
    NAVIGATE_STATUS_TOPIC_FQN,
)
_FIXED_SERVICE_FQNS = (
    NAVIGATE_ACTION_FQN,
    '/navigate_to_pose/_action/send_goal',
    '/navigate_to_pose/_action/get_result',
    NAVIGATE_CANCEL_SERVICE_FQN,
)

_MAX_STATUS_GOALS = 256
_MAX_TRACKED_OPERATIONS = 4096
_MAX_WAIT_SECONDS = 30.0
_SAFE_CODE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_OBSERVE_STATUS = MappingProxyType({
    GoalStatus.STATUS_ACCEPTED: 'accepted',
    GoalStatus.STATUS_EXECUTING: 'active',
    GoalStatus.STATUS_CANCELING: 'active',
    GoalStatus.STATUS_SUCCEEDED: 'succeeded',
    GoalStatus.STATUS_ABORTED: 'aborted',
    GoalStatus.STATUS_CANCELED: 'canceled',
})
_TERMINAL_ROS_STATUSES = frozenset(
    {
        GoalStatus.STATUS_SUCCEEDED,
        GoalStatus.STATUS_ABORTED,
        GoalStatus.STATUS_CANCELED,
    }
)
_ALLOWED_STATUS_TRANSITIONS = MappingProxyType({
    GoalStatus.STATUS_ACCEPTED: frozenset(_OBSERVE_STATUS),
    GoalStatus.STATUS_EXECUTING: frozenset(
        {
            GoalStatus.STATUS_EXECUTING,
            GoalStatus.STATUS_CANCELING,
        }
    ) | _TERMINAL_ROS_STATUSES,
    GoalStatus.STATUS_CANCELING: frozenset(
        {GoalStatus.STATUS_CANCELING}
    ) | _TERMINAL_ROS_STATUSES,
    GoalStatus.STATUS_SUCCEEDED: frozenset(
        {GoalStatus.STATUS_SUCCEEDED}
    ),
    GoalStatus.STATUS_ABORTED: frozenset(
        {GoalStatus.STATUS_ABORTED}
    ),
    GoalStatus.STATUS_CANCELED: frozenset(
        {GoalStatus.STATUS_CANCELED}
    ),
})
_PREFLIGHT_PUBLIC_CODE = MappingProxyType({
    'ready': 'preflight_ready',
    'retryable': 'preflight_retryable',
    'rejected': 'preflight_rejected',
})


def _status_qos() -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )


class GazeboMonitorRoomNav2RosPortError(RuntimeError):
    """Content-free local port failure."""

    _CODES = frozenset(
        {
            'nav2_ros_port_closed',
            'nav2_ros_port_invalid_authority',
            'nav2_ros_port_invalid_configuration',
            'nav2_ros_port_invalid_request',
        }
    )

    def __init__(self, code: str) -> None:
        """Create a bounded public failure without retaining raw context."""
        normalized = (
            code if type(code) is str and code in self._CODES
            else 'nav2_ros_port_invalid_request'
        )
        super().__init__(normalized)
        self.code = normalized

    def __getattribute__(self, name):
        """Do not expose collaborator exception chains at this boundary."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


def _digest_json(value: Dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _valid_digest(value: Any) -> bool:
    return type(value) is str and _HEX_DIGEST.fullmatch(value) is not None


def _valid_proof_digest(value: Any) -> bool:
    return _valid_digest(value) and value != '0' * 64


def _valid_code(value: Any) -> bool:
    return type(value) is str and _SAFE_CODE.fullmatch(value) is not None


def _finite_time(value: Any) -> float:
    if type(value) not in (int, float):
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_configuration'
        )
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_configuration'
        ) from None
    if not math.isfinite(normalized) or normalized < 0.0:
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_configuration'
        )
    return normalized


def _host_boottime() -> float:
    """Return the Linux suspend-inclusive host authorization clock."""
    try:
        clock_id = time.CLOCK_BOOTTIME
        value = time.clock_gettime(clock_id)
    except Exception:
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_configuration'
        ) from None
    return _finite_time(value)


def _require_fixed_gazebo_runtime(node) -> None:
    """Require exact sim time and unremapped digest-declared endpoints."""
    try:
        snapshots = []
        for _pass in range(2):
            use_sim_time = node.get_parameter('use_sim_time').value
            resolved_topics = tuple(
                node.resolve_topic_name(name)
                for name in _FIXED_TOPIC_FQNS
            )
            resolved_services = tuple(
                node.resolve_service_name(name)
                for name in _FIXED_SERVICE_FQNS
            )
            use_sim_time_after = node.get_parameter(
                'use_sim_time'
            ).value
            snapshots.append(
                (
                    use_sim_time,
                    use_sim_time_after,
                    resolved_topics,
                    resolved_services,
                )
            )
    except Exception:
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_configuration'
        ) from None
    for (
        use_sim_time,
        use_sim_time_after,
        resolved_topics,
        resolved_services,
    ) in snapshots:
        if (
            type(use_sim_time) is not bool
            or use_sim_time is not True
            or type(use_sim_time_after) is not bool
            or use_sim_time_after is not True
            or any(
                type(actual) is not str or actual != expected
                for actual, expected in zip(
                    resolved_topics, _FIXED_TOPIC_FQNS
                )
            )
            or any(
                type(actual) is not str or actual != expected
                for actual, expected in zip(
                    resolved_services, _FIXED_SERVICE_FQNS
                )
            )
        ):
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            )


def _goal_uuid_message(goal_uuid: str) -> UUID:
    """Convert the controller's stable UUID hex into the ROS wire type."""
    if not _valid_goal_uuid(goal_uuid):
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_request'
        )
    return UUID(uuid=list(bytes.fromhex(goal_uuid)))


def _valid_goal_uuid(goal_uuid: Any) -> bool:
    return (
        type(goal_uuid) is str
        and len(goal_uuid) == 32
        and goal_uuid != '0' * 32
        and all(
            character in '0123456789abcdef'
            for character in goal_uuid
        )
    )


def _uuid_hex(value: Any) -> Optional[str]:
    """Return one exact nonzero UUID hex value or ``None``."""
    if type(value) is not UUID:
        return None
    try:
        raw = bytes(value.uuid)
    except Exception:
        return None
    if len(raw) != 16 or raw == bytes(16):
        return None
    return raw.hex()


def _time_fields(value: Any) -> Optional[tuple[int, int]]:
    """Return strict ROS Time fields, including the semantic nanosecond bound."""
    if type(value) is not TimeMessage:
        return None
    try:
        sec = value.sec
        nanosec = value.nanosec
    except Exception:
        return None
    if (
        type(sec) is not int
        or not -(2 ** 31) <= sec < 2 ** 31
        or type(nanosec) is not int
        or not 0 <= nanosec < 1_000_000_000
    ):
        return None
    return sec, nanosec


def _same_finite_float(value: Any, expected: float) -> bool:
    """Compare exact finite binary floats, distinguishing signed zero."""
    return (
        type(value) is float
        and math.isfinite(value)
        and value.hex() == expected.hex()
    )


def _exact_goal_wire(
    goal: Any,
    request: Nav2StartRequest,
    expected_stamp: tuple[int, int],
) -> bool:
    """Validate every semantic NavigateToPose goal field before enqueue."""
    try:
        pose_stamped = goal.pose
        header = pose_stamped.header
        pose = pose_stamped.pose
        position = pose.position
        orientation = pose.orientation
        stamp = _time_fields(header.stamp)
        expected_header_type = type(PoseStamped().header)
        return (
            type(goal) is NavigateToPose.Goal
            and type(pose_stamped) is PoseStamped
            and type(header) is expected_header_type
            and type(pose) is Pose
            and type(position) is Point
            and type(orientation) is Quaternion
            and stamp == expected_stamp
            and type(header.frame_id) is str
            and header.frame_id == 'map'
            and _same_finite_float(
                position.x, request.preflight.x_m
            )
            and _same_finite_float(
                position.y, request.preflight.y_m
            )
            and _same_finite_float(position.z, 0.0)
            and _same_finite_float(orientation.x, 0.0)
            and _same_finite_float(orientation.y, 0.0)
            and _same_finite_float(orientation.z, 0.0)
            and _same_finite_float(orientation.w, 1.0)
            and type(goal.behavior_tree) is str
            and goal.behavior_tree == ''
        )
    except Exception:
        return False


def _exact_cancel_goal_info(
    value: Any,
    goal_uuid: str,
    *,
    require_zero_stamp: bool,
) -> bool:
    """Validate an exact UUID GoalInfo and a well-formed ROS timestamp."""
    if type(value) is not GoalInfo or _uuid_hex(value.goal_id) != goal_uuid:
        return False
    stamp = _time_fields(value.stamp)
    if stamp is None:
        return False
    return not require_zero_stamp or stamp == (0, 0)


def _start_wire_digest(request: Nav2StartRequest) -> str:
    """Rebuild the fixed NavigateToPose wire-policy digest."""
    preflight = request.preflight
    return _digest_json(
        {
            'contract': 'malbut-nav2-navigate-to-pose-wire-v1',
            'action_fqn': NAVIGATE_ACTION_FQN,
            'goal_uuid': preflight.goal_uuid,
            'frame_id': 'map',
            'position': {
                'x': preflight.x_m,
                'y': preflight.y_m,
                'z': 0.0,
            },
            'orientation': {
                'x': 0.0,
                'y': 0.0,
                'z': 0.0,
                'w': 1.0,
            },
            'behavior_tree': '',
            'pose_stamp_policy': 'ros_now_at_enqueue',
            'runtime_mode': 'gazebo',
            'use_sim_time': True,
        }
    )


def _cancel_wire_digest(request: Nav2CancelRequest) -> str:
    """Rebuild the fixed exact-goal cancellation wire-policy digest."""
    return _digest_json(
        {
            'contract': 'malbut-nav2-cancel-goal-wire-v1',
            'service_fqn': NAVIGATE_CANCEL_SERVICE_FQN,
            'goal_uuid': request.goal_uuid,
            'goal_info_stamp_policy': 'zero_exact_goal',
            'runtime_mode': 'gazebo',
            'use_sim_time': True,
        }
    )


@dataclass(frozen=True)
class Nav2LivePreflightValidation:
    """Exact independent live-binding and path evidence from a validator."""

    request_fingerprint: str
    binding_digest: str
    goal_uuid: str
    outcome: str
    code: str
    live_binding_digest: str
    path_evidence_digest: str

    def __post_init__(self) -> None:
        """Reject weak or unbound live validation results."""
        if (
            not _valid_digest(self.request_fingerprint)
            or not _valid_digest(self.binding_digest)
            or not _valid_goal_uuid(self.goal_uuid)
            or type(self.outcome) is not str
            or self.outcome not in {'ready', 'retryable', 'rejected'}
            or not _valid_code(self.code)
            or not _valid_proof_digest(self.live_binding_digest)
            or not _valid_proof_digest(self.path_evidence_digest)
        ):
            raise ValueError('live preflight validation is invalid')


@dataclass(frozen=True)
class Nav2StartAuthorization:
    """Exact side-effect-boundary authorization for one start request."""

    operation_id: str
    worker_id: str
    goal_uuid: str
    binding_digest: str
    fence_epoch: int
    request_fingerprint: str
    wire_payload_digest: str
    checked_at: float
    authority_evidence_digest: str

    def __post_init__(self) -> None:
        """Validate the closed start-authorization result shape."""
        if (
            type(self.operation_id) is not str
            or not self.operation_id
            or type(self.worker_id) is not str
            or not self.worker_id
            or not _valid_goal_uuid(self.goal_uuid)
            or type(self.fence_epoch) is not int
            or self.fence_epoch < 1
            or not _valid_digest(self.binding_digest)
            or not _valid_digest(self.request_fingerprint)
            or not _valid_digest(self.wire_payload_digest)
            or not _valid_proof_digest(self.authority_evidence_digest)
            or type(self.checked_at) is not float
        ):
            raise ValueError('start authorization is invalid')
        _finite_time(self.checked_at)


@dataclass(frozen=True)
class Nav2CancelAuthorization:
    """Exact side-effect-boundary authorization for one cancel request."""

    operation_id: str
    worker_id: str
    cancel_request_id: str
    goal_uuid: str
    binding_digest: str
    fence_epoch: int
    request_fingerprint: str
    wire_payload_digest: str
    checked_at: float
    authority_evidence_digest: str

    def __post_init__(self) -> None:
        """Validate the closed cancel-authorization result shape."""
        if (
            type(self.operation_id) is not str
            or not self.operation_id
            or type(self.worker_id) is not str
            or not self.worker_id
            or type(self.cancel_request_id) is not str
            or not self.cancel_request_id
            or not _valid_goal_uuid(self.goal_uuid)
            or type(self.fence_epoch) is not int
            or self.fence_epoch < 1
            or not _valid_digest(self.binding_digest)
            or not _valid_digest(self.request_fingerprint)
            or not _valid_digest(self.wire_payload_digest)
            or not _valid_proof_digest(self.authority_evidence_digest)
            or type(self.checked_at) is not float
        ):
            raise ValueError('cancel authorization is invalid')
        _finite_time(self.checked_at)


class TrustedGazeboMonitorRoomNav2Validator(ABC):
    """
    Composition-root trust seam for live path and durable authority.

    The port accepts only this explicit validator family and exact immutable
    result types.  Implementations are expected to bind a live finalized map,
    semantic/zones evidence, path/costmap validation, and the durable store.
    """

    @abstractmethod
    def validate_preflight(
        self,
        request: Nav2PreflightRequest,
        *,
        checked_at: float,
    ) -> Nav2LivePreflightValidation:
        """Validate current bindings and a safe exact path without motion."""

    @abstractmethod
    def authorize_start(
        self,
        request: Nav2StartRequest,
        *,
        checked_at: float,
    ) -> Nav2StartAuthorization:
        """Recheck the exact lease, fence, deadline, binding, and target."""

    @abstractmethod
    def authorize_cancel(
        self,
        request: Nav2CancelRequest,
        *,
        checked_at: float,
    ) -> Nav2CancelAuthorization:
        """Recheck the exact cancel intent, lease, fence, and target."""


class _FailClosedValidator(TrustedGazeboMonitorRoomNav2Validator):
    """Default validator that can never mint successful authority."""

    def validate_preflight(self, request, *, checked_at):
        marker = _digest_json(
            {
                'contract': 'malbut-nav2-live-validator-missing-v1',
                'kind': 'live_binding',
            }
        )
        path = _digest_json(
            {
                'contract': 'malbut-nav2-live-validator-missing-v1',
                'kind': 'path',
            }
        )
        return Nav2LivePreflightValidation(
            request_fingerprint=request.request_fingerprint,
            binding_digest=request.binding_digest,
            goal_uuid=request.goal_uuid,
            outcome='rejected',
            code='live_validator_required',
            live_binding_digest=marker,
            path_evidence_digest=path,
        )

    def authorize_start(self, request, *, checked_at):
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_authority'
        )

    def authorize_cancel(self, request, *, checked_at):
        raise GazeboMonitorRoomNav2RosPortError(
            'nav2_ros_port_invalid_authority'
        )


_FAIL_CLOSED_VALIDATOR = _FailClosedValidator()


@dataclass
class _DispatchRecord:
    """Bounded process-local no-resend record for one stable goal."""

    request_fingerprint: str
    wire_payload_digest: str
    operation_id: str
    binding_digest: str
    fence_epoch: int
    status: str
    evidence_digest: str
    authority_evidence_digest: Optional[str]
    goal_handle: Any = None


@dataclass
class _CancelRecord:
    """Bounded process-local no-resend record for one cancel identity."""

    request_fingerprint: str
    wire_payload_digest: str
    goal_uuid: str
    status: str
    evidence_digest: str
    authority_evidence_digest: str


class GazeboMonitorRoomNav2RosPort:
    """
    Implement the injected monitor-room port with ROS 2 Humble APIs.

    ``ensure_started`` and ``cancel_goal`` are bounded blocking boundaries.
    They must run on a dedicated controller worker, never on a callback thread
    of the executor spinning ``node``.  The executor must keep spinning so ROS
    future callbacks can complete while that worker waits.

    ``close`` only tears down this transport.  It neither cancels active goals
    nor proves that robot motion stopped; a supervisor must reconcile exact
    UUIDs and establish quiescence before transport shutdown.

    ROS 2 exposes authority validation and action enqueue as separate calls.
    The final CLOCK_BOOTTIME/runtime/request checks minimize that unavoidable
    TOCTOU gap; deployment still requires exact-UUID reconciliation (or a
    future atomic local gateway) across suspension, crash, and fence races.
    """

    BLOCKING_CALL_CONTEXT = 'dedicated_non_executor_worker'

    def __init__(
        self,
        node,
        *,
        validator: Optional[TrustedGazeboMonitorRoomNav2Validator] = None,
        clock=None,
        response_timeout_seconds: float = 2.0,
        cancel_timeout_seconds: float = 2.0,
        action_client_factory=ActionClient,
    ) -> None:
        """Create ROS entities without sending or canceling any goal."""
        selected_validator = (
            _FAIL_CLOSED_VALIDATOR if validator is None else validator
        )
        if not isinstance(
            selected_validator, TrustedGazeboMonitorRoomNav2Validator
        ):
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            )
        response_timeout = _finite_time(response_timeout_seconds)
        cancel_timeout = _finite_time(cancel_timeout_seconds)
        if (
            response_timeout <= 0.0
            or response_timeout > _MAX_WAIT_SECONDS
            or cancel_timeout <= 0.0
            or cancel_timeout > _MAX_WAIT_SECONDS
        ):
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            )
        _require_fixed_gazebo_runtime(node)
        selected_clock = _host_boottime if clock is None else clock
        try:
            validate_preflight = selected_validator.validate_preflight
            authorize_start = selected_validator.authorize_start
            authorize_cancel = selected_validator.authorize_cancel
        except Exception:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            ) from None
        if (
            not callable(selected_clock)
            or not callable(validate_preflight)
            or not callable(authorize_start)
            or not callable(authorize_cancel)
        ):
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            )
        self._node = node
        self._clock = selected_clock
        self._validate_preflight = validate_preflight
        self._authorize_start = authorize_start
        self._authorize_cancel = authorize_cancel
        self._response_timeout = response_timeout
        self._cancel_timeout = cancel_timeout
        self._lock = RLock()
        self._dispatch_lock = RLock()
        self._last_now: Optional[float] = None
        self._closed = False
        self._statuses: Dict[str, int] = {}
        self._status_history: Dict[str, int] = {}
        self._status_snapshot_seen = False
        self._status_snapshot_valid = False
        self._dispatches: Dict[str, _DispatchRecord] = {}
        self._cancels: Dict[str, _CancelRecord] = {}
        self._cancel_owner_by_goal: Dict[str, str] = {}
        self._dispatch_tracking_exhausted = False
        self._cancel_tracking_exhausted = False
        navigate = None
        cancel_client = None
        status_subscription = None
        try:
            navigate = action_client_factory(
                node,
                NavigateToPose,
                NAVIGATE_ACTION_FQN,
            )
            if (
                navigate is None
                or not callable(getattr(navigate, 'send_goal_async', None))
                or not callable(getattr(navigate, 'server_is_ready', None))
                or not callable(getattr(navigate, 'destroy', None))
            ):
                raise ValueError
            cancel_client = node.create_client(
                CancelGoal,
                NAVIGATE_CANCEL_SERVICE_FQN,
            )
            if (
                cancel_client is None
                or not callable(getattr(cancel_client, 'call_async', None))
                or not callable(
                    getattr(cancel_client, 'service_is_ready', None)
                )
            ):
                raise ValueError
            status_subscription = node.create_subscription(
                GoalStatusArray,
                NAVIGATE_STATUS_TOPIC_FQN,
                self._on_status,
                _status_qos(),
            )
            if status_subscription is None:
                raise ValueError
        except Exception:
            if status_subscription is not None:
                try:
                    node.destroy_subscription(status_subscription)
                except Exception:
                    pass
            if cancel_client is not None:
                try:
                    node.destroy_client(cancel_client)
                except Exception:
                    pass
            if navigate is not None:
                try:
                    navigate.destroy()
                except Exception:
                    pass
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            ) from None
        self._navigate = navigate
        self._cancel_client = cancel_client
        self._status_subscription = status_subscription

    def _now(self) -> float:
        with self._lock:
            try:
                current = _finite_time(self._clock())
            except GazeboMonitorRoomNav2RosPortError:
                raise
            except Exception:
                raise GazeboMonitorRoomNav2RosPortError(
                    'nav2_ros_port_invalid_configuration'
                ) from None
            if self._last_now is not None and current < self._last_now:
                raise GazeboMonitorRoomNav2RosPortError(
                    'nav2_ros_port_invalid_configuration'
                )
            self._last_now = current
        return current

    def _require_open(self) -> None:
        with self._lock:
            if self._closed:
                raise GazeboMonitorRoomNav2RosPortError(
                    'nav2_ros_port_closed'
                )

    @staticmethod
    def _preflight_evidence(
        validation: Nav2LivePreflightValidation,
        public_code: str,
    ) -> str:
        return _digest_json(
            {
                'contract': 'malbut-nav2-live-preflight-evidence-v1',
                'request_fingerprint': validation.request_fingerprint,
                'binding_digest': validation.binding_digest,
                'goal_uuid': validation.goal_uuid,
                'outcome': validation.outcome,
                'code': public_code,
                'live_binding_digest': validation.live_binding_digest,
                'path_evidence_digest': validation.path_evidence_digest,
            }
        )

    @staticmethod
    def _goal_evidence(
        request_fingerprint: str,
        goal_uuid: str,
        status: str,
        source: str,
        authority_evidence_digest: Optional[str] = None,
    ) -> str:
        return _digest_json(
            {
                'contract': 'malbut-nav2-goal-observation-evidence-v1',
                'request_fingerprint': request_fingerprint,
                'goal_uuid': goal_uuid,
                'status': status,
                'source': source,
                'authority_evidence_digest': authority_evidence_digest,
            }
        )

    @staticmethod
    def _cancel_evidence(
        request_fingerprint: str,
        goal_uuid: str,
        status: str,
        source: str,
        authority_evidence_digest: Optional[str] = None,
    ) -> str:
        return _digest_json(
            {
                'contract': 'malbut-nav2-cancel-observation-evidence-v1',
                'request_fingerprint': request_fingerprint,
                'goal_uuid': goal_uuid,
                'status': status,
                'source': source,
                'authority_evidence_digest': authority_evidence_digest,
            }
        )

    @staticmethod
    def _goal_report(request, status: str, evidence: str) -> Dict[str, Any]:
        return {
            'operation_id': (
                request.preflight.operation_id
                if type(request) is Nav2StartRequest
                else request.operation_id
            ),
            'goal_uuid': (
                request.preflight.goal_uuid
                if type(request) is Nav2StartRequest
                else request.goal_uuid
            ),
            'binding_digest': (
                request.preflight.binding_digest
                if type(request) is Nav2StartRequest
                else request.binding_digest
            ),
            'fence_epoch': request.fence_epoch,
            'status': status,
            'evidence_digest': evidence,
        }

    @staticmethod
    def _cancel_report(
        request: Nav2CancelRequest,
        status: str,
        evidence: str,
    ) -> Dict[str, Any]:
        return {
            'operation_id': request.operation_id,
            'goal_uuid': request.goal_uuid,
            'binding_digest': request.binding_digest,
            'fence_epoch': request.fence_epoch,
            'status': status,
            'evidence_digest': evidence,
        }

    def _on_status(self, message: GoalStatusArray) -> None:
        """Replace the authoritative bounded action-server status snapshot."""
        snapshot: Dict[str, int] = {}
        try:
            valid = (
                type(message) is GoalStatusArray
                and type(message.status_list) is list
                and len(message.status_list) <= _MAX_STATUS_GOALS
            )
            if valid:
                for item in message.status_list:
                    if (
                        type(item) is not GoalStatus
                        or type(item.goal_info) is not GoalInfo
                    ):
                        valid = False
                        break
                    goal_uuid = _uuid_hex(item.goal_info.goal_id)
                    status = item.status
                    if (
                        goal_uuid is None
                        or type(status) is not int
                        or status not in _OBSERVE_STATUS
                        or _time_fields(item.goal_info.stamp) is None
                        or goal_uuid in snapshot
                    ):
                        valid = False
                        break
                    snapshot[goal_uuid] = status
        except Exception:
            valid = False
        with self._lock:
            if self._closed:
                return
            if valid:
                new_goal_count = sum(
                    goal_uuid not in self._status_history
                    for goal_uuid in snapshot
                )
                if (
                    len(self._status_history) + new_goal_count
                    > _MAX_TRACKED_OPERATIONS
                ):
                    valid = False
            if valid:
                for goal_uuid, status in snapshot.items():
                    previous = self._status_history.get(goal_uuid)
                    if (
                        previous is not None
                        and status not in (
                            _ALLOWED_STATUS_TRANSITIONS[previous]
                        )
                    ):
                        valid = False
                        break
            if valid:
                self._status_history.update(snapshot)
            self._statuses = snapshot if valid else {}
            self._status_snapshot_seen = True
            self._status_snapshot_valid = valid

    def _status_for(self, goal_uuid: str) -> Optional[str]:
        status, _seen, _valid = self._status_state(goal_uuid)
        return status

    def _status_state(self, goal_uuid: str):
        with self._lock:
            value = self._statuses.get(goal_uuid)
            seen = self._status_snapshot_seen
            valid = self._status_snapshot_valid
        return _OBSERVE_STATUS.get(value), seen, valid

    @staticmethod
    def _canonical_preflight(
        request: Nav2PreflightRequest,
    ) -> Nav2PreflightRequest:
        if type(request) is not Nav2PreflightRequest:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            )
        try:
            canonical = Nav2PreflightRequest(
                operation_id=request.operation_id,
                robot_id=request.robot_id,
                map_id=request.map_id,
                map_revision=request.map_revision,
                semantic_revision=request.semantic_revision,
                zones_digest=request.zones_digest,
                target_binding_digest=request.target_binding_digest,
                effects_digest=request.effects_digest,
                profile_digest=request.profile_digest,
                plan_digest=request.plan_digest,
                sample_count=request.sample_count,
                sample_index=request.sample_index,
                polygon_ordinal=request.polygon_ordinal,
                row_ordinal=request.row_ordinal,
                goal_uuid=request.goal_uuid,
                binding_digest=request.binding_digest,
                x_m=request.x_m,
                y_m=request.y_m,
                frame_id=request.frame_id,
            )
            if (
                canonical != request
                or canonical.request_fingerprint
                != request.request_fingerprint
                or not _valid_goal_uuid(canonical.goal_uuid)
            ):
                raise ValueError
            return canonical
        except Exception:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            ) from None

    @classmethod
    def _canonical_start(cls, request: Nav2StartRequest) -> Nav2StartRequest:
        if type(request) is not Nav2StartRequest:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            )
        try:
            canonical = Nav2StartRequest(
                preflight=cls._canonical_preflight(request.preflight),
                worker_id=request.worker_id,
                fence_epoch=request.fence_epoch,
                lease_expires_at=request.lease_expires_at,
                deadline=request.deadline,
                preflight_digest=request.preflight_digest,
            )
            if (
                canonical != request
                or canonical.request_fingerprint
                != request.request_fingerprint
                or canonical.wire_payload_digest
                != request.wire_payload_digest
                or _start_wire_digest(canonical)
                != request.wire_payload_digest
            ):
                raise ValueError
            return canonical
        except GazeboMonitorRoomNav2RosPortError:
            raise
        except Exception:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            ) from None

    @staticmethod
    def _canonical_query(request: Nav2GoalQuery) -> Nav2GoalQuery:
        if type(request) is not Nav2GoalQuery:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            )
        try:
            canonical = Nav2GoalQuery(
                operation_id=request.operation_id,
                worker_id=request.worker_id,
                fence_epoch=request.fence_epoch,
                goal_uuid=request.goal_uuid,
                binding_digest=request.binding_digest,
            )
            if (
                canonical != request
                or canonical.request_fingerprint
                != request.request_fingerprint
                or not _valid_goal_uuid(canonical.goal_uuid)
            ):
                raise ValueError
            return canonical
        except Exception:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            ) from None

    @staticmethod
    def _canonical_cancel(
        request: Nav2CancelRequest,
    ) -> Nav2CancelRequest:
        if type(request) is not Nav2CancelRequest:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            )
        try:
            canonical = Nav2CancelRequest(
                operation_id=request.operation_id,
                worker_id=request.worker_id,
                fence_epoch=request.fence_epoch,
                cancel_request_id=request.cancel_request_id,
                goal_uuid=request.goal_uuid,
                binding_digest=request.binding_digest,
            )
            if (
                canonical != request
                or canonical.request_fingerprint
                != request.request_fingerprint
                or canonical.wire_payload_digest
                != request.wire_payload_digest
                or _cancel_wire_digest(canonical)
                != request.wire_payload_digest
                or not _valid_goal_uuid(canonical.goal_uuid)
            ):
                raise ValueError
            return canonical
        except Exception:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_request'
            ) from None

    def preflight(self, request: Nav2PreflightRequest) -> Dict[str, Any]:
        """Run the required trusted live-binding and exact-path validator."""
        self._require_open()
        canonical = self._canonical_preflight(request)
        snapshot = self._canonical_preflight(canonical)
        validator_request = self._canonical_preflight(snapshot)
        expected_fingerprint = snapshot.request_fingerprint
        checked_at = self._now()
        try:
            result = self._validate_preflight(
                validator_request,
                checked_at=checked_at,
            )
            post_validation = self._canonical_preflight(
                validator_request
            )
            if type(result) is not Nav2LivePreflightValidation:
                raise ValueError
            validation = Nav2LivePreflightValidation(**result.__dict__)
            prohibited = {
                expected_fingerprint,
                snapshot.binding_digest,
                snapshot.zones_digest,
                snapshot.target_binding_digest,
                snapshot.effects_digest,
                snapshot.profile_digest,
                snapshot.plan_digest,
            }
            if (
                post_validation != snapshot
                or post_validation.request_fingerprint
                != expected_fingerprint
                or validation != result
                or validation.request_fingerprint
                != expected_fingerprint
                or validation.binding_digest != snapshot.binding_digest
                or validation.goal_uuid != snapshot.goal_uuid
                or validation.live_binding_digest in prohibited
                or validation.path_evidence_digest in prohibited
                or validation.live_binding_digest
                == validation.path_evidence_digest
            ):
                raise ValueError
        except Exception:
            marker = _digest_json(
                {
                    'contract': 'malbut-nav2-validator-rejected-v1',
                    'kind': 'live_binding',
                }
            )
            validation = Nav2LivePreflightValidation(
                request_fingerprint=expected_fingerprint,
                binding_digest=snapshot.binding_digest,
                goal_uuid=snapshot.goal_uuid,
                outcome='rejected',
                code='live_validator_rejected',
                live_binding_digest=marker,
                path_evidence_digest=_digest_json(
                    {
                        'contract': 'malbut-nav2-validator-rejected-v1',
                        'kind': 'path',
                    }
                ),
            )
        public_code = _PREFLIGHT_PUBLIC_CODE[validation.outcome]
        return {
            'operation_id': snapshot.operation_id,
            'goal_uuid': snapshot.goal_uuid,
            'binding_digest': snapshot.binding_digest,
            'request_fingerprint': expected_fingerprint,
            'evidence_digest': self._preflight_evidence(
                validation,
                public_code,
            ),
            'outcome': validation.outcome,
            'code': public_code,
        }

    @staticmethod
    def _assert_start_authorization(
        authorization: Any,
        request: Nav2StartRequest,
        checked_at: float,
    ) -> Nav2StartAuthorization:
        if type(authorization) is not Nav2StartAuthorization:
            raise ValueError
        canonical = Nav2StartAuthorization(**authorization.__dict__)
        preflight = request.preflight
        if (
            canonical != authorization
            or canonical.operation_id != preflight.operation_id
            or canonical.worker_id != request.worker_id
            or canonical.goal_uuid != preflight.goal_uuid
            or canonical.binding_digest != preflight.binding_digest
            or canonical.fence_epoch != request.fence_epoch
            or canonical.request_fingerprint
            != request.request_fingerprint
            or canonical.wire_payload_digest
            != request.wire_payload_digest
            or canonical.checked_at != checked_at
            or canonical.authority_evidence_digest in {
                request.request_fingerprint,
                request.wire_payload_digest,
                preflight.binding_digest,
            }
        ):
            raise ValueError
        return canonical

    @staticmethod
    def _assert_cancel_authorization(
        authorization: Any,
        request: Nav2CancelRequest,
        checked_at: float,
    ) -> Nav2CancelAuthorization:
        if type(authorization) is not Nav2CancelAuthorization:
            raise ValueError
        canonical = Nav2CancelAuthorization(**authorization.__dict__)
        if (
            canonical != authorization
            or canonical.operation_id != request.operation_id
            or canonical.worker_id != request.worker_id
            or canonical.cancel_request_id != request.cancel_request_id
            or canonical.goal_uuid != request.goal_uuid
            or canonical.binding_digest != request.binding_digest
            or canonical.fence_epoch != request.fence_epoch
            or canonical.request_fingerprint
            != request.request_fingerprint
            or canonical.wire_payload_digest
            != request.wire_payload_digest
            or canonical.checked_at != checked_at
            or canonical.authority_evidence_digest in {
                request.request_fingerprint,
                request.wire_payload_digest,
                request.binding_digest,
            }
        ):
            raise ValueError
        return canonical

    def _wait_future(self, future, timeout: float):
        event = Event()
        try:
            future.add_done_callback(lambda _completed: event.set())
        except Exception:
            return None, False
        if not event.wait(timeout):
            return None, False
        try:
            return future.result(), True
        except Exception:
            return None, False

    def _ros_stamp(self) -> tuple[TimeMessage, tuple[int, int]]:
        """Read, validate, and detach one simulation-clock pose stamp."""
        try:
            raw = self._node.get_clock().now().to_msg()
            fields = _time_fields(raw)
            if fields is None:
                raise ValueError
            stamp = TimeMessage(sec=fields[0], nanosec=fields[1])
        except Exception:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            ) from None
        return stamp, fields

    def _build_goal(
        self,
        request: Nav2StartRequest,
        stamp: TimeMessage,
    ) -> NavigateToPose.Goal:
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = 'map'
        stamp_fields = _time_fields(stamp)
        if stamp_fields is None:
            raise GazeboMonitorRoomNav2RosPortError(
                'nav2_ros_port_invalid_configuration'
            )
        goal.pose.header.stamp = TimeMessage(
            sec=stamp_fields[0], nanosec=stamp_fields[1]
        )
        goal.pose.pose.position.x = request.preflight.x_m
        goal.pose.pose.position.y = request.preflight.y_m
        goal.pose.pose.position.z = 0.0
        goal.pose.pose.orientation.x = 0.0
        goal.pose.pose.orientation.y = 0.0
        goal.pose.pose.orientation.z = 0.0
        goal.pose.pose.orientation.w = 1.0
        goal.behavior_tree = ''
        return goal

    def _result_callback(self, goal_uuid: str, future) -> None:
        try:
            wrapped = future.result()
            if type(wrapped) is not (
                NavigateToPose.Impl.GetResultService.Response
            ):
                return
            ros_status = wrapped.status
            result = wrapped.result
            expected_payload_type = type(
                NavigateToPose.Result().result
            )
            if (
                type(ros_status) is not int
                or type(result) is not NavigateToPose.Result
                or type(result.result) is not expected_payload_type
            ):
                return
            status = _OBSERVE_STATUS.get(ros_status)
            if status not in {'succeeded', 'aborted', 'canceled'}:
                return
        except Exception:
            return
        with self._lock:
            if self._closed:
                return
            record = self._dispatches.get(goal_uuid)
            if record is None:
                return
            if (
                self._status_snapshot_seen
                and not self._status_snapshot_valid
            ):
                record.status = 'unknown'
                record.evidence_digest = self._goal_evidence(
                    record.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'result_after_invalid_status',
                    record.authority_evidence_digest,
                )
                return
            previous = self._status_history.get(goal_uuid)
            if (
                previous in _TERMINAL_ROS_STATUSES
                and previous != ros_status
            ):
                record.status = 'unknown'
                record.evidence_digest = self._goal_evidence(
                    record.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'result_conflict',
                    record.authority_evidence_digest,
                )
                self._statuses = {}
                self._status_snapshot_seen = True
                self._status_snapshot_valid = False
                return
            if (
                previous is None
                and len(self._status_history)
                >= _MAX_TRACKED_OPERATIONS
            ):
                record.status = 'unknown'
                record.evidence_digest = self._goal_evidence(
                    record.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'status_history_exhausted',
                    record.authority_evidence_digest,
                )
                self._statuses = {}
                self._status_snapshot_seen = True
                self._status_snapshot_valid = False
                return
            record.status = status
            record.evidence_digest = self._goal_evidence(
                record.request_fingerprint,
                goal_uuid,
                status,
                'result',
                record.authority_evidence_digest,
            )
            if (
                goal_uuid not in self._statuses
                and len(self._statuses) >= _MAX_STATUS_GOALS
            ):
                self._statuses = {}
                self._status_snapshot_seen = True
                self._status_snapshot_valid = False
                return
            self._statuses[goal_uuid] = ros_status
            self._status_history[goal_uuid] = ros_status

    def ensure_started(self, request: Nav2StartRequest) -> Dict[str, Any]:
        """
        Enqueue one exact stable-UUID goal at most once per port life.

        This blocking method obeys :attr:`BLOCKING_CALL_CONTEXT`.
        """
        self._require_open()
        canonical = self._canonical_start(request)
        canonical = self._canonical_start(canonical)
        expected_fingerprint = canonical.request_fingerprint
        expected_wire_digest = canonical.wire_payload_digest
        goal_uuid = canonical.preflight.goal_uuid
        with self._dispatch_lock:
            self._require_open()
            with self._lock:
                prior = self._dispatches.get(goal_uuid)
                snapshot_seen = self._status_snapshot_seen
                snapshot_valid = self._status_snapshot_valid
                live_status = _OBSERVE_STATUS.get(
                    self._statuses.get(goal_uuid)
                )
            if prior is not None:
                if (
                    prior.request_fingerprint
                    != canonical.request_fingerprint
                    or prior.wire_payload_digest
                    != canonical.wire_payload_digest
                    or prior.fence_epoch != canonical.fence_epoch
                ):
                    evidence = self._goal_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        'unknown',
                        'dispatch_conflict',
                    )
                    return self._goal_report(
                        canonical, 'unknown', evidence
                    )
                if live_status is not None:
                    status = live_status
                    evidence = self._goal_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        status,
                        'status',
                        prior.authority_evidence_digest,
                    )
                elif snapshot_seen:
                    status = 'unknown'
                    evidence = self._goal_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        status,
                        'status_missing',
                        prior.authority_evidence_digest,
                    )
                else:
                    status = prior.status
                    evidence = prior.evidence_digest
                return self._goal_report(canonical, status, evidence)

            existing_status = live_status
            if existing_status is not None:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    existing_status,
                    'status',
                )
                with self._lock:
                    if (
                        self._dispatch_tracking_exhausted
                        or len(self._dispatches)
                        >= _MAX_TRACKED_OPERATIONS
                    ):
                        self._dispatch_tracking_exhausted = True
                        return self._goal_report(
                            canonical,
                            'unknown',
                            self._goal_evidence(
                                canonical.request_fingerprint,
                                goal_uuid,
                                'unknown',
                                'tracking_exhausted',
                            ),
                        )
                    self._dispatches[goal_uuid] = _DispatchRecord(
                        request_fingerprint=(
                            canonical.request_fingerprint
                        ),
                        wire_payload_digest=(
                            canonical.wire_payload_digest
                        ),
                        operation_id=canonical.preflight.operation_id,
                        binding_digest=canonical.preflight.binding_digest,
                        fence_epoch=canonical.fence_epoch,
                        status=existing_status,
                        evidence_digest=evidence,
                        authority_evidence_digest=None,
                    )
                return self._goal_report(
                    canonical, existing_status, evidence
                )

            status_invalid = snapshot_seen and not snapshot_valid
            if status_invalid:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'status_invalid',
                )
                return self._goal_report(
                    canonical, 'unknown', evidence
                )

            with self._lock:
                if (
                    self._dispatch_tracking_exhausted
                    or len(self._dispatches) >= _MAX_TRACKED_OPERATIONS
                ):
                    self._dispatch_tracking_exhausted = True
                    evidence = self._goal_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        'unknown',
                        'tracking_exhausted',
                    )
                    return self._goal_report(
                        canonical, 'unknown', evidence
                    )

            initial_at = self._now()
            if (
                initial_at >= canonical.lease_expires_at
                or initial_at >= canonical.deadline
            ):
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'rejected',
                    'authority_time',
                )
                return self._goal_report(canonical, 'rejected', evidence)
            try:
                if self._navigate.server_is_ready() is not True:
                    raise ValueError
            except Exception:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'rejected',
                    'transport_unavailable',
                )
                return self._goal_report(canonical, 'rejected', evidence)

            checked_at = self._now()
            if (
                checked_at >= canonical.lease_expires_at
                or checked_at >= canonical.deadline
            ):
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'rejected',
                    'authority_time',
                )
                return self._goal_report(canonical, 'rejected', evidence)
            try:
                authorization_request = self._canonical_start(canonical)
                authorization = self._authorize_start(
                    authorization_request,
                    checked_at=checked_at,
                )
                post_authorization = self._canonical_start(
                    authorization_request
                )
                if (
                    post_authorization != canonical
                    or post_authorization.request_fingerprint
                    != expected_fingerprint
                    or post_authorization.wire_payload_digest
                    != expected_wire_digest
                ):
                    raise ValueError
                authorization = self._assert_start_authorization(
                    authorization, canonical, checked_at
                )
            except Exception:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'rejected',
                    'authority_rejected',
                )
                return self._goal_report(canonical, 'rejected', evidence)

            try:
                build_request = self._canonical_start(canonical)
                stamp, expected_stamp = self._ros_stamp()
                goal = self._build_goal(build_request, stamp)
                post_build = self._canonical_start(build_request)
                final_request = self._canonical_start(canonical)
                if (
                    post_build != canonical
                    or post_build.request_fingerprint
                    != expected_fingerprint
                    or post_build.wire_payload_digest
                    != expected_wire_digest
                    or final_request != canonical
                    or final_request.request_fingerprint
                    != expected_fingerprint
                    or final_request.wire_payload_digest
                    != expected_wire_digest
                    or not _exact_goal_wire(
                        goal, canonical, expected_stamp
                    )
                ):
                    raise ValueError
                _require_fixed_gazebo_runtime(self._node)
            except Exception:
                evidence = self._goal_evidence(
                    expected_fingerprint,
                    goal_uuid,
                    'rejected',
                    'request_mutated',
                    authorization.authority_evidence_digest,
                )
                return self._goal_report(canonical, 'rejected', evidence)
            (
                latest_status,
                latest_snapshot_seen,
                latest_snapshot_valid,
            ) = self._status_state(goal_uuid)
            status_invalid = (
                latest_snapshot_seen and not latest_snapshot_valid
            )
            if latest_status is not None:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    latest_status,
                    'status_before_send',
                    authorization.authority_evidence_digest,
                )
                with self._lock:
                    self._dispatches[goal_uuid] = _DispatchRecord(
                        request_fingerprint=(
                            canonical.request_fingerprint
                        ),
                        wire_payload_digest=(
                            canonical.wire_payload_digest
                        ),
                        operation_id=canonical.preflight.operation_id,
                        binding_digest=canonical.preflight.binding_digest,
                        fence_epoch=canonical.fence_epoch,
                        status=latest_status,
                        evidence_digest=evidence,
                        authority_evidence_digest=(
                            authorization.authority_evidence_digest
                        ),
                    )
                return self._goal_report(
                    canonical, latest_status, evidence
                )
            if status_invalid:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'status_invalid',
                    authorization.authority_evidence_digest,
                )
                return self._goal_report(
                    canonical, 'unknown', evidence
                )
            try:
                boundary_request = self._canonical_start(canonical)
                _require_fixed_gazebo_runtime(self._node)
                if (
                    boundary_request != canonical
                    or boundary_request.request_fingerprint
                    != expected_fingerprint
                    or boundary_request.wire_payload_digest
                    != expected_wire_digest
                    or not _exact_goal_wire(
                        goal, boundary_request, expected_stamp
                    )
                ):
                    raise ValueError
            except Exception:
                evidence = self._goal_evidence(
                    expected_fingerprint,
                    goal_uuid,
                    'rejected',
                    'request_mutated',
                    authorization.authority_evidence_digest,
                )
                return self._goal_report(canonical, 'rejected', evidence)
            send_boundary_at = self._now()
            if (
                send_boundary_at >= canonical.lease_expires_at
                or send_boundary_at >= canonical.deadline
            ):
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'rejected',
                    'authority_time',
                    authorization.authority_evidence_digest,
                )
                return self._goal_report(canonical, 'rejected', evidence)
            try:
                final_boundary_request = self._canonical_start(canonical)
                if (
                    final_boundary_request != canonical
                    or final_boundary_request.request_fingerprint
                    != expected_fingerprint
                    or final_boundary_request.wire_payload_digest
                    != expected_wire_digest
                    or not _exact_goal_wire(
                        goal, final_boundary_request, expected_stamp
                    )
                ):
                    raise ValueError
            except Exception:
                evidence = self._goal_evidence(
                    expected_fingerprint,
                    goal_uuid,
                    'rejected',
                    'request_mutated',
                    authorization.authority_evidence_digest,
                )
                return self._goal_report(canonical, 'rejected', evidence)
            try:
                boundary_authorization_request = self._canonical_start(
                    canonical
                )
                boundary_authorization = self._authorize_start(
                    boundary_authorization_request,
                    checked_at=send_boundary_at,
                )
                post_boundary_authorization = self._canonical_start(
                    boundary_authorization_request
                )
                if (
                    post_boundary_authorization != canonical
                    or post_boundary_authorization.request_fingerprint
                    != expected_fingerprint
                    or post_boundary_authorization.wire_payload_digest
                    != expected_wire_digest
                    or not _exact_goal_wire(
                        goal,
                        post_boundary_authorization,
                        expected_stamp,
                    )
                ):
                    raise ValueError
                authorization = self._assert_start_authorization(
                    boundary_authorization,
                    canonical,
                    send_boundary_at,
                )
            except Exception:
                evidence = self._goal_evidence(
                    expected_fingerprint,
                    goal_uuid,
                    'rejected',
                    'authority_rejected',
                )
                return self._goal_report(canonical, 'rejected', evidence)

            dispatching_evidence = self._goal_evidence(
                canonical.request_fingerprint,
                goal_uuid,
                'unknown',
                'dispatching',
                authorization.authority_evidence_digest,
            )
            record = _DispatchRecord(
                request_fingerprint=canonical.request_fingerprint,
                wire_payload_digest=canonical.wire_payload_digest,
                operation_id=canonical.preflight.operation_id,
                binding_digest=canonical.preflight.binding_digest,
                fence_epoch=canonical.fence_epoch,
                status='unknown',
                evidence_digest=dispatching_evidence,
                authority_evidence_digest=(
                    authorization.authority_evidence_digest
                ),
            )
            with self._lock:
                self._dispatches[goal_uuid] = record
            try:
                future = self._navigate.send_goal_async(
                    goal,
                    goal_uuid=_goal_uuid_message(goal_uuid),
                )
            except Exception:
                return self._goal_report(
                    canonical, 'unknown', dispatching_evidence
                )
            remaining = min(
                self._response_timeout,
                max(
                    0.0,
                    canonical.lease_expires_at - send_boundary_at,
                ),
                max(0.0, canonical.deadline - send_boundary_at),
            )
            goal_handle, completed = self._wait_future(future, remaining)
            if not completed or goal_handle is None:
                return self._goal_report(
                    canonical, 'unknown', dispatching_evidence
                )
            try:
                response_goal_uuid = _uuid_hex(goal_handle.goal_id)
                response_stamp = _time_fields(goal_handle.stamp)
                accepted = goal_handle.accepted
            except Exception:
                return self._goal_report(
                    canonical, 'unknown', dispatching_evidence
                )
            if (
                response_goal_uuid != goal_uuid
                or response_stamp is None
                or type(accepted) is not bool
            ):
                return self._goal_report(
                    canonical, 'unknown', dispatching_evidence
                )
            observed, seen, valid = self._status_state(goal_uuid)
            status_invalid = seen and not valid
            if status_invalid:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'status_invalid',
                    authorization.authority_evidence_digest,
                )
                with self._lock:
                    record.status = 'unknown'
                    record.evidence_digest = evidence
                return self._goal_report(
                    canonical, 'unknown', evidence
                )
            if accepted is False:
                status = (
                    observed
                    if observed is not None
                    else 'unknown'
                )
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    status,
                    (
                        'status_after_rejection'
                        if observed is not None
                        else 'send_response_unproven_rejection'
                    ),
                    authorization.authority_evidence_digest,
                )
                with self._lock:
                    record.status = status
                    record.evidence_digest = evidence
                return self._goal_report(canonical, status, evidence)
            accepted_status = observed or 'accepted'
            evidence = self._goal_evidence(
                canonical.request_fingerprint,
                goal_uuid,
                accepted_status,
                (
                    'status_after_acceptance'
                    if observed is not None
                    else 'send_response'
                ),
                authorization.authority_evidence_digest,
            )
            with self._lock:
                record.status = accepted_status
                record.evidence_digest = evidence
                record.goal_handle = goal_handle
            try:
                result_future = goal_handle.get_result_async()
                result_future.add_done_callback(
                    lambda completed: self._result_callback(
                        goal_uuid, completed
                    )
                )
            except Exception:
                with self._lock:
                    record.status = 'unknown'
                    record.evidence_digest = dispatching_evidence
                return self._goal_report(
                    canonical, 'unknown', dispatching_evidence
                )
            final_status, final_seen, final_valid = self._status_state(
                goal_uuid
            )
            if final_seen and (
                not final_valid or final_status is None
            ):
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    (
                        'status_invalid'
                        if not final_valid
                        else 'status_missing_after_result_registration'
                    ),
                    authorization.authority_evidence_digest,
                )
                with self._lock:
                    record.status = 'unknown'
                    record.evidence_digest = evidence
                return self._goal_report(
                    canonical, 'unknown', evidence
                )
            if final_status is not None:
                evidence = self._goal_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    final_status,
                    'status_after_result_registration',
                    authorization.authority_evidence_digest,
                )
                with self._lock:
                    record.status = final_status
                    record.evidence_digest = evidence
            with self._lock:
                current_status = record.status
                current_evidence = record.evidence_digest
            return self._goal_report(
                canonical,
                current_status,
                current_evidence,
            )

    def observe_goal(self, request: Nav2GoalQuery) -> Dict[str, Any]:
        """Observe one exact UUID without sending or reconstructing a goal."""
        self._require_open()
        canonical = self._canonical_query(request)
        source = 'status'
        with self._lock:
            status = _OBSERVE_STATUS.get(
                self._statuses.get(canonical.goal_uuid)
            )
            snapshot_seen = self._status_snapshot_seen
            dispatch = self._dispatches.get(canonical.goal_uuid)
        authority_evidence = None
        if (
            dispatch is not None
            and dispatch.operation_id == canonical.operation_id
            and dispatch.binding_digest == canonical.binding_digest
            and dispatch.fence_epoch == canonical.fence_epoch
        ):
            authority_evidence = dispatch.authority_evidence_digest
        if status is None and not snapshot_seen and dispatch is not None:
            if (
                dispatch.operation_id == canonical.operation_id
                and dispatch.binding_digest == canonical.binding_digest
                and dispatch.fence_epoch == canonical.fence_epoch
            ):
                status = dispatch.status
                source = 'local_dispatch'
        if status is None:
            status = 'unknown'
            source = 'status_missing'
        evidence = self._goal_evidence(
            canonical.request_fingerprint,
            canonical.goal_uuid,
            status,
            source,
            authority_evidence,
        )
        return self._goal_report(canonical, status, evidence)

    def cancel_goal(self, request: Nav2CancelRequest) -> Dict[str, Any]:
        """
        Request exact-goal cancellation once; wait for bounded ACK.

        Only an exact ``STATUS_CANCELED`` observation produces ``canceled``.
        This blocking method obeys :attr:`BLOCKING_CALL_CONTEXT`.
        """
        self._require_open()
        canonical = self._canonical_cancel(request)
        canonical = self._canonical_cancel(canonical)
        expected_fingerprint = canonical.request_fingerprint
        expected_wire_digest = canonical.wire_payload_digest
        goal_uuid = canonical.goal_uuid
        with self._dispatch_lock:
            self._require_open()
            with self._lock:
                observed = _OBSERVE_STATUS.get(
                    self._statuses.get(goal_uuid)
                )
                snapshot_seen = self._status_snapshot_seen
                snapshot_valid = self._status_snapshot_valid
                owner = self._cancel_owner_by_goal.get(goal_uuid)
                prior = self._cancels.get(canonical.cancel_request_id)
            if observed == 'canceled':
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'canceled',
                    'status',
                )
                return self._cancel_report(
                    canonical, 'canceled', evidence
                )
            if observed in {'succeeded', 'aborted'}:
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'terminal_status',
                )
                return self._cancel_report(
                    canonical, 'unknown', evidence
                )
            status_invalid = snapshot_seen and not snapshot_valid
            if status_invalid:
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'status_invalid',
                )
                return self._cancel_report(
                    canonical, 'unknown', evidence
                )
            if owner is not None and owner != canonical.cancel_request_id:
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    'cancel_identity_conflict',
                )
                return self._cancel_report(canonical, 'unknown', evidence)
            if prior is not None:
                if (
                    prior.request_fingerprint
                    != canonical.request_fingerprint
                    or prior.wire_payload_digest
                    != canonical.wire_payload_digest
                    or prior.goal_uuid != goal_uuid
                ):
                    evidence = self._cancel_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        'unknown',
                        'cancel_request_conflict',
                    )
                    return self._cancel_report(
                        canonical, 'unknown', evidence
                    )
                if observed == 'active':
                    status = 'active'
                    evidence = self._cancel_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        status,
                        'status',
                        prior.authority_evidence_digest,
                    )
                elif snapshot_seen:
                    status = 'unknown'
                    evidence = self._cancel_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        status,
                        'status_missing',
                        prior.authority_evidence_digest,
                    )
                else:
                    status = prior.status
                    evidence = prior.evidence_digest
                return self._cancel_report(canonical, status, evidence)

            with self._lock:
                if (
                    self._cancel_tracking_exhausted
                    or len(self._cancels) >= _MAX_TRACKED_OPERATIONS
                ):
                    self._cancel_tracking_exhausted = True
                    evidence = self._cancel_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        'unknown',
                        'tracking_exhausted',
                    )
                    return self._cancel_report(
                        canonical, 'unknown', evidence
                    )

            try:
                if self._cancel_client.service_is_ready() is not True:
                    raise ValueError
                ros_request = CancelGoal.Request()
                ros_request.goal_info.goal_id = _goal_uuid_message(
                    goal_uuid
                )
                if not _exact_cancel_goal_info(
                    ros_request.goal_info,
                    goal_uuid,
                    require_zero_stamp=True,
                ):
                    raise ValueError
                _require_fixed_gazebo_runtime(self._node)
            except Exception:
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'rejected',
                    'transport_unavailable',
                )
                return self._cancel_report(canonical, 'rejected', evidence)

            checked_at = self._now()
            try:
                authorization_request = self._canonical_cancel(canonical)
                authorization = self._authorize_cancel(
                    authorization_request,
                    checked_at=checked_at,
                )
                post_authorization = self._canonical_cancel(
                    authorization_request
                )
                if (
                    post_authorization != canonical
                    or post_authorization.request_fingerprint
                    != expected_fingerprint
                    or post_authorization.wire_payload_digest
                    != expected_wire_digest
                ):
                    raise ValueError
                authorization = self._assert_cancel_authorization(
                    authorization, canonical, checked_at
                )
            except Exception:
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'rejected',
                    'authority_rejected',
                )
                return self._cancel_report(canonical, 'rejected', evidence)

            try:
                final_request = self._canonical_cancel(canonical)
                if (
                    final_request != canonical
                    or final_request.request_fingerprint
                    != expected_fingerprint
                    or final_request.wire_payload_digest
                    != expected_wire_digest
                    or type(ros_request) is not CancelGoal.Request
                    or not _exact_cancel_goal_info(
                        ros_request.goal_info,
                        goal_uuid,
                        require_zero_stamp=True,
                    )
                ):
                    raise ValueError
                _require_fixed_gazebo_runtime(self._node)
            except Exception:
                evidence = self._cancel_evidence(
                    expected_fingerprint,
                    goal_uuid,
                    'rejected',
                    'request_mutated',
                    authorization.authority_evidence_digest,
                )
                return self._cancel_report(canonical, 'rejected', evidence)
            (
                latest_status,
                latest_snapshot_seen,
                latest_snapshot_valid,
            ) = self._status_state(goal_uuid)
            status_invalid = (
                latest_snapshot_seen and not latest_snapshot_valid
            )
            if latest_status == 'canceled':
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'canceled',
                    'status_before_cancel',
                    authorization.authority_evidence_digest,
                )
                return self._cancel_report(
                    canonical, 'canceled', evidence
                )
            if (
                latest_status in {'succeeded', 'aborted'}
                or status_invalid
            ):
                evidence = self._cancel_evidence(
                    canonical.request_fingerprint,
                    goal_uuid,
                    'unknown',
                    (
                        'terminal_status'
                        if latest_status in {'succeeded', 'aborted'}
                        else 'status_invalid'
                    ),
                    authorization.authority_evidence_digest,
                )
                return self._cancel_report(
                    canonical, 'unknown', evidence
                )
            boundary_at = self._now()
            try:
                boundary_request = self._canonical_cancel(canonical)
                boundary_authorization = self._authorize_cancel(
                    boundary_request,
                    checked_at=boundary_at,
                )
                post_boundary = self._canonical_cancel(
                    boundary_request
                )
                if (
                    post_boundary != canonical
                    or post_boundary.request_fingerprint
                    != expected_fingerprint
                    or post_boundary.wire_payload_digest
                    != expected_wire_digest
                    or type(ros_request) is not CancelGoal.Request
                    or not _exact_cancel_goal_info(
                        ros_request.goal_info,
                        goal_uuid,
                        require_zero_stamp=True,
                    )
                ):
                    raise ValueError
                boundary_authorization = self._assert_cancel_authorization(
                    boundary_authorization, canonical, boundary_at
                )
                authorization = boundary_authorization
            except Exception:
                evidence = self._cancel_evidence(
                    expected_fingerprint,
                    goal_uuid,
                    'rejected',
                    'authority_rejected',
                )
                return self._cancel_report(
                    canonical, 'rejected', evidence
                )

            unknown_evidence = self._cancel_evidence(
                canonical.request_fingerprint,
                goal_uuid,
                'unknown',
                'cancel_dispatching',
                authorization.authority_evidence_digest,
            )
            record = _CancelRecord(
                request_fingerprint=canonical.request_fingerprint,
                wire_payload_digest=canonical.wire_payload_digest,
                goal_uuid=goal_uuid,
                status='unknown',
                evidence_digest=unknown_evidence,
                authority_evidence_digest=(
                    authorization.authority_evidence_digest
                ),
            )
            with self._lock:
                self._cancels[canonical.cancel_request_id] = record
                self._cancel_owner_by_goal[goal_uuid] = (
                    canonical.cancel_request_id
                )
            # The default builtin_interfaces/Time is exactly zero.  Assigning
            # no acceptance timestamp selects only this nonzero goal UUID.
            try:
                future = self._cancel_client.call_async(ros_request)
            except Exception:
                return self._cancel_report(
                    canonical, 'unknown', unknown_evidence
                )
            response, completed = self._wait_future(
                future, self._cancel_timeout
            )
            if not completed or response is None:
                if self._status_for(goal_uuid) == 'canceled':
                    evidence = self._cancel_evidence(
                        canonical.request_fingerprint,
                        goal_uuid,
                        'canceled',
                        'status',
                        authorization.authority_evidence_digest,
                    )
                    with self._lock:
                        record.status = 'canceled'
                        record.evidence_digest = evidence
                    return self._cancel_report(
                        canonical, 'canceled', evidence
                    )
                return self._cancel_report(
                    canonical, 'unknown', unknown_evidence
                )

            observed, response_seen, response_valid = self._status_state(
                goal_uuid
            )
            response_status_invalid = response_seen and not response_valid
            if response_status_invalid:
                status = 'unknown'
                source = 'status_invalid'
            elif observed == 'canceled':
                status = 'canceled'
                source = 'status'
            elif observed in {'succeeded', 'aborted'}:
                status = 'unknown'
                source = 'terminal_status'
            else:
                try:
                    if (
                        type(response) is not CancelGoal.Response
                        or type(response.return_code) is not int
                        or type(response.goals_canceling) is not list
                        or len(response.goals_canceling)
                        > _MAX_STATUS_GOALS
                    ):
                        raise ValueError
                    exact = []
                    for item in response.goals_canceling:
                        item_uuid = _uuid_hex(item.goal_id)
                        if (
                            type(item) is not GoalInfo
                            or item_uuid is None
                            or _time_fields(item.stamp) is None
                        ):
                            raise ValueError
                        if item_uuid in exact:
                            raise ValueError
                        exact.append(item_uuid)
                    return_code = response.return_code
                except Exception:
                    return_code = None
                    exact = []
                if (
                    return_code == CancelGoal.Response.ERROR_NONE
                    and exact == [goal_uuid]
                ):
                    status = 'active'
                    source = 'cancel_accepted'
                elif (
                    return_code == CancelGoal.Response.ERROR_REJECTED
                    and not exact
                ):
                    status = 'rejected'
                    source = 'cancel_rejected'
                else:
                    status = 'unknown'
                    source = (
                        'goal_already_terminal'
                        if return_code
                        == CancelGoal.Response.ERROR_GOAL_TERMINATED
                        else 'cancel_response_mismatch'
                    )
            evidence = self._cancel_evidence(
                canonical.request_fingerprint,
                goal_uuid,
                status,
                source,
                authorization.authority_evidence_digest,
            )
            with self._lock:
                record.status = status
                record.evidence_digest = evidence
            return self._cancel_report(canonical, status, evidence)

    def close(self) -> None:
        """Tear down transport only, without sending goal cancellation."""
        with self._dispatch_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                subscription = self._status_subscription
                cancel_client = self._cancel_client
                navigate = self._navigate
                self._status_subscription = None
                self._cancel_client = None
                self._navigate = None
                self._statuses = {}
                self._status_history = {}
                self._status_snapshot_seen = False
                self._status_snapshot_valid = False
                self._dispatches = {}
                self._cancels = {}
                self._cancel_owner_by_goal = {}
            try:
                self._node.destroy_subscription(subscription)
            except Exception:
                pass
            try:
                self._node.destroy_client(cancel_client)
            except Exception:
                pass
            try:
                navigate.destroy()
            except Exception:
                pass

    def __enter__(self):
        """Return this open port."""
        self._require_open()
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        """Close the port on context exit."""
        self.close()


__all__ = [
    'GazeboMonitorRoomNav2RosPort',
    'GazeboMonitorRoomNav2RosPortError',
    'NAVIGATE_ACTION_FQN',
    'NAVIGATE_CANCEL_SERVICE_FQN',
    'NAVIGATE_STATUS_TOPIC_FQN',
    'Nav2CancelAuthorization',
    'Nav2LivePreflightValidation',
    'Nav2StartAuthorization',
    'TrustedGazeboMonitorRoomNav2Validator',
]
