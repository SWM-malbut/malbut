"""Pure injected controller for the Gazebo monitor-room Nav2 boundary."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
from threading import RLock
import time
from typing import Iterator, Optional
import re
from types import MappingProxyType

from malbut_gazebo.gazebo_monitor_room_store import (
    CancelOperation,
    GazeboMonitorRoomStore,
    GazeboMonitorRoomConflictError,
    GazeboMonitorRoomDeadlineError,
    GazeboMonitorRoomLeaseError,
    GazeboMonitorRoomStoreError,
    GoalTransition,
    OperationObservation,
    PrivateOperationBinding,
    PrivateStoredSample,
)


_DIGEST_CHARS = frozenset('0123456789abcdef')
_SAFE_CODE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_PREFLIGHT_OUTCOMES = frozenset({'ready', 'retryable', 'rejected'})
_PREFLIGHT_CODES = MappingProxyType({
    'ready': 'preflight_ready',
    'retryable': 'preflight_retryable',
    'rejected': 'preflight_rejected',
})
_OBSERVE_STATUSES = frozenset(
    {'accepted', 'active', 'succeeded', 'aborted', 'rejected',
     'canceled', 'unknown'}
)
_CANCEL_STATUSES = frozenset({'active', 'canceled', 'rejected', 'unknown'})
_UNKNOWN_STATES = frozenset({'delivery_unknown', 'cancel_unknown'})
_TERMINAL_STATES = frozenset({'succeeded', 'failed', 'canceled'})
_AMBIGUOUS_SEND_DIGEST = hashlib.sha256(
    b'malbut-gazebo-monitor-room-nav2-send-ambiguous-v1'
).hexdigest()
_AMBIGUOUS_OBSERVE_DIGEST = hashlib.sha256(
    b'malbut-gazebo-monitor-room-nav2-observe-ambiguous-v1'
).hexdigest()
_AMBIGUOUS_CANCEL_DIGEST = hashlib.sha256(
    b'malbut-gazebo-monitor-room-nav2-cancel-ambiguous-v1'
).hexdigest()
_START_DEADLINE_DIGEST = hashlib.sha256(
    b'malbut-gazebo-monitor-room-nav2-start-deadline-v1'
).hexdigest()
_ERROR_CODES = frozenset(
    {
        'nav2_adapter_rejected',
        'nav2_binding_changed',
        'nav2_cancel_active',
        'nav2_cancel_origin_missing',
        'nav2_cancel_unknown',
        'nav2_clock_unavailable',
        'nav2_external_call_in_progress',
        'nav2_goal_aborted',
        'nav2_goal_canceled',
        'nav2_goal_not_observable',
        'nav2_goal_rejected',
        'nav2_invalid_binding_digest',
        'nav2_invalid_cancel_request_id',
        'nav2_invalid_cancel_status',
        'nav2_invalid_code',
        'nav2_invalid_deadline',
        'nav2_invalid_evidence_digest',
        'nav2_invalid_fence_epoch',
        'nav2_invalid_frame_id',
        'nav2_invalid_goal_uuid',
        'nav2_invalid_lease_seconds',
        'nav2_invalid_now',
        'nav2_invalid_operation_id',
        'nav2_invalid_outcome',
        'nav2_invalid_payload',
        'nav2_invalid_preflight',
        'nav2_invalid_preflight_digest',
        'nav2_invalid_request_fingerprint',
        'nav2_invalid_sample',
        'nav2_invalid_sample_count',
        'nav2_invalid_sample_index',
        'nav2_invalid_status',
        'nav2_invalid_worker_id',
        'nav2_invalid_x_m',
        'nav2_invalid_y_m',
        'nav2_lease_expired',
        'nav2_port_rejected',
        'nav2_sample_changed',
        'nav2_state_rejected',
        'nav2_store_binding_changed',
        'nav2_store_rejected',
        'nav2_store_sample_changed',
    }
)


class GazeboMonitorRoomNav2AdapterError(RuntimeError):
    """Content-free public error for the injected Nav2 controller."""

    def __init__(self, code='nav2_adapter_rejected'):
        """Create a stable content-free adapter error code."""
        if type(code) is not str or code not in _ERROR_CODES:
            code = 'nav2_adapter_rejected'
        super().__init__(code)
        self.code = code

    def __getattribute__(self, name):
        """Hide exception-chain metadata at the adapter boundary."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


def _clear_error(error):
    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
    return error


def _boundary(function):
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except GazeboMonitorRoomNav2AdapterError as error:
            raise _clear_error(
                GazeboMonitorRoomNav2AdapterError(error.code)
            )
        except GazeboMonitorRoomStoreError:
            raise _clear_error(
                GazeboMonitorRoomNav2AdapterError('nav2_store_rejected')
            )
        except Exception:
            raise _clear_error(
                GazeboMonitorRoomNav2AdapterError('nav2_port_rejected')
            )

    return wrapped


def _identifier(value, label):
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise GazeboMonitorRoomNav2AdapterError(f'nav2_invalid_{label}')
    return value


def _digest(value, label):
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise GazeboMonitorRoomNav2AdapterError(f'nav2_invalid_{label}')
    return value


def _code(value):
    if type(value) is not str or _SAFE_CODE.fullmatch(value) is None:
        raise GazeboMonitorRoomNav2AdapterError('nav2_invalid_code')
    return value


def _timestamp(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GazeboMonitorRoomNav2AdapterError(f'nav2_invalid_{label}')
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise GazeboMonitorRoomNav2AdapterError(
            f'nav2_invalid_{label}'
        ) from None
    if not math.isfinite(result) or result < 0.0:
        raise GazeboMonitorRoomNav2AdapterError(f'nav2_invalid_{label}')
    return result


def _boottime():
    """Read the suspend-inclusive Linux authority clock without fallback."""
    try:
        clock_id = time.CLOCK_BOOTTIME
        value = time.clock_gettime(clock_id)
        return _timestamp(value, 'now')
    except Exception:
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_clock_unavailable'
        ) from None


def _coordinate(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GazeboMonitorRoomNav2AdapterError(f'nav2_invalid_{label}')
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise GazeboMonitorRoomNav2AdapterError(
            f'nav2_invalid_{label}'
        ) from None
    if not math.isfinite(result):
        raise GazeboMonitorRoomNav2AdapterError(f'nav2_invalid_{label}')
    return result


def _hash_json(value):
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_payload'
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _same_exact_fields(canonical, value):
    """Compare validated dataclass storage without coercive equality."""
    try:
        expected = canonical.__dict__
        current = value.__dict__
    except AttributeError:
        return False
    if expected.keys() != current.keys():
        return False
    return all(
        type(current[name]) is type(expected_value)
        and current[name] == expected_value
        for name, expected_value in expected.items()
    )


def _sample_fingerprint(sample: PrivateStoredSample) -> str:
    if type(sample) is not PrivateStoredSample:
        raise GazeboMonitorRoomNav2AdapterError('nav2_invalid_sample')
    canonical = PrivateStoredSample(
        operation_id=sample.operation_id,
        store_namespace=sample.store_namespace,
        index=sample.index,
        polygon_ordinal=sample.polygon_ordinal,
        row_ordinal=sample.row_ordinal,
        x_mm=sample.x_mm,
        y_mm=sample.y_mm,
        frame_id=sample.frame_id,
        goal_uuid=sample.goal_uuid,
        state=sample.state,
    )
    if not _same_exact_fields(canonical, sample):
        raise GazeboMonitorRoomNav2AdapterError('nav2_sample_changed')
    return _hash_json(
        {
            'operation_id': canonical.operation_id,
            'store_namespace': canonical.store_namespace,
            'index': canonical.index,
            'polygon_ordinal': canonical.polygon_ordinal,
            'row_ordinal': canonical.row_ordinal,
            'x_mm': canonical.x_mm,
            'y_mm': canonical.y_mm,
            'frame_id': canonical.frame_id,
            'goal_uuid': canonical.goal_uuid,
            'state': canonical.state,
        }
    )


def _preflight_request_fingerprint(request: 'Nav2PreflightRequest') -> str:
    if type(request) is not Nav2PreflightRequest:
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_preflight'
        )
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
    if not _same_exact_fields(canonical, request):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_binding_changed'
        )
    return _hash_json(canonical.__dict__)


def _start_request_fingerprint(request: 'Nav2StartRequest') -> str:
    if type(request) is not Nav2StartRequest:
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_preflight'
        )
    try:
        canonical = Nav2StartRequest(
            preflight=request.preflight,
            worker_id=request.worker_id,
            fence_epoch=request.fence_epoch,
            lease_expires_at=request.lease_expires_at,
            deadline=request.deadline,
            preflight_digest=request.preflight_digest,
        )
    except (AttributeError, GazeboMonitorRoomNav2AdapterError):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_preflight'
        ) from None
    preflight_fingerprint = _preflight_request_fingerprint(
        canonical.preflight
    )
    if not _same_exact_fields(canonical, request):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_binding_changed'
        )
    return _hash_json(
        {
            'preflight': preflight_fingerprint,
            'worker_id': canonical.worker_id,
            'fence_epoch': canonical.fence_epoch,
            'lease_expires_at': canonical.lease_expires_at,
            'deadline': canonical.deadline,
            'preflight_digest': canonical.preflight_digest,
            'wire_payload_digest': _start_wire_payload_digest(canonical),
        }
    )


def _start_wire_payload_digest(request: 'Nav2StartRequest') -> str:
    if type(request) is not Nav2StartRequest:
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_preflight'
        )
    _preflight_request_fingerprint(request.preflight)
    preflight = request.preflight
    return _hash_json(
        {
            'contract': 'malbut-nav2-navigate-to-pose-wire-v1',
            'action_fqn': '/navigate_to_pose',
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


def _cancel_wire_payload_digest(request: 'Nav2CancelRequest') -> str:
    if type(request) is not Nav2CancelRequest:
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_payload'
        )
    _cancel_request_fingerprint(request)
    return _hash_json(
        {
            'contract': 'malbut-nav2-cancel-goal-wire-v1',
            'service_fqn': '/navigate_to_pose/_action/cancel_goal',
            'goal_uuid': request.goal_uuid,
            'goal_info_stamp_policy': 'zero_exact_goal',
            'runtime_mode': 'gazebo',
            'use_sim_time': True,
        }
    )


def _goal_query_request_fingerprint(request: 'Nav2GoalQuery') -> str:
    if type(request) is not Nav2GoalQuery:
        raise GazeboMonitorRoomNav2AdapterError('nav2_invalid_payload')
    try:
        canonical = Nav2GoalQuery(
            operation_id=request.operation_id,
            worker_id=request.worker_id,
            fence_epoch=request.fence_epoch,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
        )
    except (AttributeError, GazeboMonitorRoomNav2AdapterError):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_payload'
        ) from None
    if not _same_exact_fields(canonical, request):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_binding_changed'
        )
    return _hash_json(dict(canonical.__dict__))


def _cancel_request_fingerprint(request: 'Nav2CancelRequest') -> str:
    if type(request) is not Nav2CancelRequest:
        raise GazeboMonitorRoomNav2AdapterError('nav2_invalid_payload')
    try:
        canonical = Nav2CancelRequest(
            operation_id=request.operation_id,
            worker_id=request.worker_id,
            fence_epoch=request.fence_epoch,
            cancel_request_id=request.cancel_request_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
        )
    except (AttributeError, GazeboMonitorRoomNav2AdapterError):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_invalid_payload'
        ) from None
    if not _same_exact_fields(canonical, request):
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_binding_changed'
        )
    return _hash_json(dict(canonical.__dict__))


def _plain_request_fingerprint(request) -> str:
    if type(request) is Nav2GoalQuery:
        return _goal_query_request_fingerprint(request)
    if type(request) is Nav2CancelRequest:
        return _cancel_request_fingerprint(request)
    raise GazeboMonitorRoomNav2AdapterError('nav2_invalid_payload')


@dataclass(frozen=True)
class Nav2PreflightRequest:
    """Exact current sample and operation binding for preflight checks."""

    operation_id: str
    robot_id: str
    map_id: str
    map_revision: str
    semantic_revision: str
    zones_digest: str
    target_binding_digest: str
    effects_digest: str
    profile_digest: str
    plan_digest: str
    sample_count: int
    sample_index: int
    polygon_ordinal: int
    row_ordinal: int
    goal_uuid: str
    binding_digest: str
    x_m: float = field(repr=False)
    y_m: float = field(repr=False)
    frame_id: str = 'map'
    use_sim_time: bool = field(default=True, init=False)
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)

    def __post_init__(self):
        """Validate the preflight command without hiding its exact binding."""
        for name in (
            'operation_id',
            'robot_id',
            'map_id',
            'map_revision',
            'semantic_revision',
        ):
            _identifier(getattr(self, name), name)
        for name in (
            'zones_digest',
            'target_binding_digest',
            'effects_digest',
            'profile_digest',
            'plan_digest',
            'binding_digest',
        ):
            _digest(getattr(self, name), name)
        if type(self.sample_count) is not int or self.sample_count < 1:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_sample_count'
            )
        for name in ('sample_index', 'polygon_ordinal', 'row_ordinal'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise GazeboMonitorRoomNav2AdapterError(
                    f'nav2_invalid_{name}'
                )
        _identifier(self.goal_uuid, 'goal_uuid')
        if self.frame_id != 'map':
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_frame_id'
            )
        object.__setattr__(self, 'x_m', _coordinate(self.x_m, 'x_m'))
        object.__setattr__(self, 'y_m', _coordinate(self.y_m, 'y_m'))

    @property
    def request_fingerprint(self):
        """Return the exact immutable preflight request fingerprint."""
        return _preflight_request_fingerprint(self)


@dataclass(frozen=True)
class Nav2PreflightReport:
    """Content-minimized preflight report bound to one exact request."""

    operation_id: str
    goal_uuid: str
    binding_digest: str
    request_fingerprint: str
    outcome: str
    code: str
    evidence_digest: str

    def __post_init__(self):
        """Validate preflight evidence and exact target echo."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.goal_uuid, 'goal_uuid')
        _digest(self.binding_digest, 'binding_digest')
        _digest(self.request_fingerprint, 'request_fingerprint')
        if type(self.outcome) is not str or self.outcome not in (
            _PREFLIGHT_OUTCOMES
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_outcome'
            )
        if _code(self.code) != _PREFLIGHT_CODES[self.outcome]:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_code'
            )
        _digest(self.evidence_digest, 'evidence_digest')


@dataclass(frozen=True)
class Nav2StartRequest:
    """Strict idempotent start request for an already recorded intent."""

    preflight: Nav2PreflightRequest
    worker_id: str
    fence_epoch: int
    lease_expires_at: float
    deadline: float
    preflight_digest: str

    def __post_init__(self):
        """Validate start fencing, lease deadline, and preflight evidence."""
        if type(self.preflight) is not Nav2PreflightRequest:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_preflight'
            )
        _identifier(self.worker_id, 'worker_id')
        if (
            type(self.fence_epoch) is not int
            or self.fence_epoch < 1
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_fence_epoch'
            )
        object.__setattr__(
            self,
            'lease_expires_at',
            _timestamp(self.lease_expires_at, 'lease_expires_at'),
        )
        object.__setattr__(
            self, 'deadline', _timestamp(self.deadline, 'deadline')
        )
        _digest(self.preflight_digest, 'preflight_digest')

    @property
    def request_fingerprint(self):
        """Return the exact start request fingerprint."""
        return _start_request_fingerprint(self)

    @property
    def wire_payload_digest(self):
        """Bind the fixed navigation-only NavigateToPose wire payload."""
        return _start_wire_payload_digest(self)


@dataclass(frozen=True)
class Nav2GoalQuery:
    """Read-only observation request for one stable Nav2 goal UUID."""

    operation_id: str
    worker_id: str
    fence_epoch: int
    goal_uuid: str
    binding_digest: str

    def __post_init__(self):
        """Validate the narrow read-only goal observation selector."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.worker_id, 'worker_id')
        if (
            type(self.fence_epoch) is not int
            or self.fence_epoch < 1
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_fence_epoch'
            )
        _identifier(self.goal_uuid, 'goal_uuid')
        _digest(self.binding_digest, 'binding_digest')

    @property
    def request_fingerprint(self):
        """Return the exact goal-query request fingerprint."""
        return _plain_request_fingerprint(self)


@dataclass(frozen=True)
class Nav2CancelRequest:
    """Strict idempotent cancel request for a side-effecting cancel call."""

    operation_id: str
    worker_id: str
    fence_epoch: int
    cancel_request_id: str
    goal_uuid: str
    binding_digest: str

    def __post_init__(self):
        """Validate cancel fencing and stable goal binding."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.worker_id, 'worker_id')
        _identifier(self.cancel_request_id, 'cancel_request_id')
        if (
            type(self.fence_epoch) is not int
            or self.fence_epoch < 1
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_fence_epoch'
            )
        _identifier(self.goal_uuid, 'goal_uuid')
        _digest(self.binding_digest, 'binding_digest')

    @property
    def request_fingerprint(self):
        """Return the exact cancel request fingerprint."""
        return _plain_request_fingerprint(self)

    @property
    def wire_payload_digest(self):
        """Bind the fixed exact-goal CancelGoal wire payload."""
        return _cancel_wire_payload_digest(self)


@dataclass(frozen=True)
class Nav2GoalReport:
    """Content-minimized observation from an injected Nav2 port."""

    operation_id: str
    goal_uuid: str
    binding_digest: str
    fence_epoch: int
    status: str
    evidence_digest: str

    def __post_init__(self):
        """Validate a bounded Nav2 observation."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.goal_uuid, 'goal_uuid')
        _digest(self.binding_digest, 'binding_digest')
        if (
            type(self.fence_epoch) is not int
            or self.fence_epoch < 1
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_fence_epoch'
            )
        if type(self.status) is not str or self.status not in (
            _OBSERVE_STATUSES
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_status'
            )
        _digest(self.evidence_digest, 'evidence_digest')


@dataclass(frozen=True)
class Nav2CancelReport:
    """Content-minimized cancellation observation from an injected port."""

    operation_id: str
    goal_uuid: str
    binding_digest: str
    fence_epoch: int
    status: str
    evidence_digest: str

    def __post_init__(self):
        """Validate a bounded cancellation observation."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.goal_uuid, 'goal_uuid')
        _digest(self.binding_digest, 'binding_digest')
        if (
            type(self.fence_epoch) is not int
            or self.fence_epoch < 1
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_fence_epoch'
            )
        if type(self.status) is not str or self.status not in (
            _CANCEL_STATUSES
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_cancel_status'
            )
        _digest(self.evidence_digest, 'evidence_digest')


class GazeboMonitorRoomNav2Controller:
    """Drive one durable operation through an injected, side-effecting port."""

    def __init__(
        self,
        store: GazeboMonitorRoomStore,
        port,
        *,
        worker_id: str,
        lease_seconds: float = 5.0,
        clock=None,
    ):
        """Bind a store and port; construction performs no ROS work."""
        if not isinstance(store, GazeboMonitorRoomStore):
            raise TypeError('store must be GazeboMonitorRoomStore')
        self._store = store
        self._port = port
        self._worker_id = _identifier(worker_id, 'worker_id')
        self._lease_seconds = _timestamp(lease_seconds, 'lease_seconds')
        if self._lease_seconds <= 0.0:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_invalid_lease_seconds'
            )
        self._clock = _boottime if clock is None else clock
        self._reservation_lock = RLock()
        self._reservations = set()

    def _now(self):
        return _timestamp(self._clock(), 'now')

    def _ensure_lease(
        self,
        observation: OperationObservation,
        now: float,
    ) -> OperationObservation:
        if observation.state in _TERMINAL_STATES | _UNKNOWN_STATES:
            return observation
        grant = self._store.acquire_lease(
            observation.operation_id,
            worker_id=self._worker_id,
            expected_fence=observation.fence_epoch,
            lease_seconds=self._lease_seconds,
            now=now,
        )
        return grant.observation

    def _token(self, observation: OperationObservation) -> GoalTransition:
        """Build a CAS token from an already validated exact snapshot."""
        if type(observation) is not OperationObservation:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_state_rejected'
            )
        return GoalTransition(
            operation_id=observation.operation_id,
            worker_id=self._worker_id,
            fence_epoch=observation.fence_epoch,
            sample_index=observation.current_sample_index,
            goal_uuid=observation.current_goal_uuid,
            expected_operation_state=observation.state,
            expected_sample_state=observation.current_sample_state,
        )

    def _private_sample(
        self,
        observation: OperationObservation,
    ) -> PrivateStoredSample:
        sample = self._store.private_current_sample(observation.operation_id)
        if (
            sample.index != observation.current_sample_index
            or sample.goal_uuid != observation.current_goal_uuid
            or sample.state != observation.current_sample_state
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_store_sample_changed'
            )
        return sample

    def _private_binding(
        self,
        observation: OperationObservation,
    ) -> PrivateOperationBinding:
        binding = self._store.private_operation_binding(
            observation.operation_id
        )
        if (
            binding.operation_id != observation.operation_id
            or binding.sample_count != observation.navigation_samples_total
            or binding.deadline != observation.deadline
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_store_binding_changed'
            )
        binding.binding_digest
        return binding

    def _preflight_request(
        self,
        binding: PrivateOperationBinding,
        sample: PrivateStoredSample,
    ) -> Nav2PreflightRequest:
        return Nav2PreflightRequest(
            operation_id=binding.operation_id,
            robot_id=binding.robot_id,
            map_id=binding.map_id,
            map_revision=binding.map_revision,
            semantic_revision=binding.semantic_revision,
            zones_digest=binding.zones_digest,
            target_binding_digest=binding.target_binding_digest,
            effects_digest=binding.effects_digest,
            profile_digest=binding.profile_digest,
            plan_digest=binding.plan_digest,
            sample_count=binding.sample_count,
            sample_index=sample.index,
            polygon_ordinal=sample.polygon_ordinal,
            row_ordinal=sample.row_ordinal,
            goal_uuid=sample.goal_uuid,
            binding_digest=binding.binding_digest,
            x_m=sample.x_m,
            y_m=sample.y_m,
            frame_id=sample.frame_id,
        )

    def _goal_query(
        self,
        observation: OperationObservation,
        binding_digest: str,
    ) -> Nav2GoalQuery:
        return Nav2GoalQuery(
            operation_id=observation.operation_id,
            worker_id=self._worker_id,
            fence_epoch=observation.fence_epoch,
            goal_uuid=observation.current_goal_uuid,
            binding_digest=binding_digest,
        )

    @staticmethod
    def _same_external_target(
        current: OperationObservation,
        origin: OperationObservation,
        allowed_states,
        worker_id: str,
    ) -> bool:
        """Check the exact durable target of one completed port call."""
        return (
            current.operation_id == origin.operation_id
            and current.fence_epoch == origin.fence_epoch
            and current.lease_owner == worker_id
            and current.current_sample_index == origin.current_sample_index
            and current.current_goal_uuid == origin.current_goal_uuid
            and origin.state in allowed_states
            and origin.current_sample_state in allowed_states
            and current.state == origin.state
            and current.current_sample_state == origin.current_sample_state
        )

    def _post_call_target(
        self,
        origin: OperationObservation,
        *,
        binding_digest: str,
        sample_digest: str,
        allowed_states,
        now: float,
    ):
        """Renew only an unchanged target and return its exact CAS snapshot."""
        current = self._store.observe(origin.operation_id)
        if not self._same_external_target(
            current, origin, allowed_states, self._worker_id
        ):
            return current, False
        try:
            current = self._ensure_lease(current, now)
        except GazeboMonitorRoomConflictError:
            return self._store.observe(origin.operation_id), False
        if not self._same_external_target(
            current, origin, allowed_states, self._worker_id
        ):
            return current, False
        try:
            current_binding = self._private_binding(current)
            current_sample = self._private_sample(current)
        except GazeboMonitorRoomNav2AdapterError as error:
            if error.code not in {
                'nav2_store_binding_changed',
                'nav2_store_sample_changed',
            }:
                raise
            latest = self._store.observe(origin.operation_id)
            if not self._same_external_target(
                latest, origin, allowed_states, self._worker_id
            ):
                return latest, False
            raise
        if current_binding.binding_digest != binding_digest:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_binding_changed'
            )
        if _sample_fingerprint(current_sample) != sample_digest:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_sample_changed'
            )
        return current, True

    @contextmanager
    def _reserve_external(
        self,
        observation: OperationObservation,
        phase: str,
    ) -> Iterator[None]:
        """Coalesce duplicate side effects within this controller instance."""
        key = (
            observation.operation_id,
            observation.current_sample_index,
            observation.current_goal_uuid,
            observation.fence_epoch,
            phase,
        )
        with self._reservation_lock:
            if key in self._reservations:
                raise GazeboMonitorRoomNav2AdapterError(
                    'nav2_external_call_in_progress'
                )
            self._reservations.add(key)
        try:
            yield
        finally:
            with self._reservation_lock:
                self._reservations.discard(key)

    @staticmethod
    def _assert_binding_stable(
        binding: PrivateOperationBinding,
        expected_digest: str,
    ) -> None:
        if binding.binding_digest != expected_digest:
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_binding_changed'
            )

    @staticmethod
    def _assert_sample_stable(
        sample: PrivateStoredSample,
        expected_digest: str,
    ) -> None:
        if _sample_fingerprint(sample) != expected_digest:
            raise GazeboMonitorRoomNav2AdapterError('nav2_sample_changed')

    @staticmethod
    def _assert_preflight_report_target(
        report: Nav2PreflightReport,
        request: Nav2PreflightRequest,
        request_fingerprint: str,
    ) -> None:
        if (
            report.operation_id != request.operation_id
            or report.goal_uuid != request.goal_uuid
            or report.binding_digest != request.binding_digest
            or report.request_fingerprint != request_fingerprint
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_goal_not_observable'
            )

    @staticmethod
    def _assert_goal_report_target(
        report: Nav2GoalReport,
        query: Nav2GoalQuery,
    ) -> None:
        if (
            report.operation_id != query.operation_id
            or report.goal_uuid != query.goal_uuid
            or report.binding_digest != query.binding_digest
            or report.fence_epoch != query.fence_epoch
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_goal_not_observable'
            )

    @staticmethod
    def _assert_cancel_report_target(
        report: Nav2CancelReport,
        request: Nav2CancelRequest,
    ) -> None:
        if (
            report.operation_id != request.operation_id
            or report.goal_uuid != request.goal_uuid
            or report.binding_digest != request.binding_digest
            or report.fence_epoch != request.fence_epoch
        ):
            raise GazeboMonitorRoomNav2AdapterError(
                'nav2_goal_not_observable'
            )

    def _record_failed(
        self,
        observation: OperationObservation,
        *,
        code: str,
        evidence_digest: Optional[str],
        now: float,
    ) -> OperationObservation:
        try:
            return self._store.record_failed(
                self._token(observation),
                code=code,
                evidence_digest=evidence_digest,
                now=now,
            )
        except GazeboMonitorRoomConflictError:
            return self._store.observe(observation.operation_id)

    def _record_delivery_unknown(
        self,
        observation: OperationObservation,
        *,
        evidence_digest: str,
        now: float,
    ) -> OperationObservation:
        """Apply ambiguous delivery only to its exact original CAS target."""
        try:
            return self._store.record_delivery_unknown(
                self._token(observation),
                code='nav2_goal_not_observable',
                evidence_digest=evidence_digest,
                now=now,
            )
        except GazeboMonitorRoomConflictError:
            return self._store.observe(observation.operation_id)

    def _record_cancel_unknown(
        self,
        observation: OperationObservation,
        *,
        code: str,
        evidence_digest: str,
        now: float,
    ) -> OperationObservation:
        """Apply ambiguous cancellation only to its exact CAS target."""
        try:
            return self._store.record_cancel_unknown(
                self._token(observation),
                code=code,
                evidence_digest=evidence_digest,
                now=now,
            )
        except GazeboMonitorRoomConflictError:
            return self._store.observe(observation.operation_id)

    def _record_observed_goal(
        self,
        observation: OperationObservation,
        report: Nav2GoalReport,
        now: float,
    ) -> OperationObservation:
        operation_id = observation.operation_id
        if report.status in {'accepted', 'active'}:
            if observation.state == 'send_intent':
                try:
                    return self._store.record_navigating(
                        self._token(observation),
                        acceptance_digest=report.evidence_digest,
                        now=now,
                    )
                except GazeboMonitorRoomConflictError:
                    return self._store.observe(operation_id)
            return self._store.observe(operation_id)
        if report.status == 'succeeded':
            current = observation
            if current.state == 'send_intent':
                try:
                    current = self._store.record_navigating(
                        self._token(current),
                        acceptance_digest=report.evidence_digest,
                        now=now,
                    )
                except GazeboMonitorRoomConflictError:
                    return self._store.observe(operation_id)
            try:
                return self._store.record_sample_succeeded(
                    self._token(current),
                    result_evidence_digest=report.evidence_digest,
                    now=now,
                )
            except GazeboMonitorRoomConflictError:
                return self._store.observe(operation_id)
        if report.status == 'unknown':
            return self._record_delivery_unknown(
                observation,
                evidence_digest=report.evidence_digest,
                now=now,
            )
        return self._record_failed(
            observation,
            code=f'nav2_goal_{report.status}',
            evidence_digest=report.evidence_digest,
            now=now,
        )

    def _observe_claimed_start(
        self,
        observation: OperationObservation,
        binding: PrivateOperationBinding,
        binding_digest: str,
        sample: PrivateStoredSample,
        sample_digest: str,
    ) -> OperationObservation:
        """Reconcile a durable start claim without resending the goal."""
        query = self._goal_query(observation, binding_digest)
        query_digest = query.request_fingerprint
        with self._reserve_external(observation, 'observe'):
            observe_error = False
            report = None
            try:
                report = Nav2GoalReport(
                    **self._port.observe_goal(query)
                )
                if query.request_fingerprint != query_digest:
                    raise GazeboMonitorRoomNav2AdapterError(
                        'nav2_binding_changed'
                    )
                self._assert_goal_report_target(report, query)
                self._assert_binding_stable(binding, binding_digest)
                self._assert_sample_stable(sample, sample_digest)
            except Exception:
                observe_error = True
            now = self._now()
            current, target_matches = self._post_call_target(
                observation,
                binding_digest=binding_digest,
                sample_digest=sample_digest,
                allowed_states=frozenset({observation.state}),
                now=now,
            )
            if not target_matches:
                return current
            if observe_error:
                return self._record_delivery_unknown(
                    current,
                    evidence_digest=_AMBIGUOUS_OBSERVE_DIGEST,
                    now=now,
                )
            return self._record_observed_goal(current, report, now)

    def _cancel_origin_state(self, observation: OperationObservation) -> str:
        for event in reversed(self._store.events(observation.operation_id)):
            if (
                event.event_type == 'cancel_requested'
                and event.to_operation_state == 'cancel_requested'
                and event.from_operation_state is not None
            ):
                return event.from_operation_state
        raise GazeboMonitorRoomNav2AdapterError(
            'nav2_cancel_origin_missing'
        )

    def _drive_cancel_requested(
        self,
        observation: OperationObservation,
        now: float,
    ) -> OperationObservation:
        origin_state = self._cancel_origin_state(observation)
        if origin_state in {'prepared', 'preflighting'}:
            try:
                return self._store.record_canceled(
                    self._token(observation),
                    terminal_evidence_digest=None,
                    now=now,
                )
            except GazeboMonitorRoomConflictError:
                return self._store.observe(observation.operation_id)
        binding = self._private_binding(observation)
        binding_digest = binding.binding_digest
        sample = self._private_sample(observation)
        sample_digest = _sample_fingerprint(sample)
        request = Nav2CancelRequest(
            operation_id=observation.operation_id,
            worker_id=self._worker_id,
            fence_epoch=observation.fence_epoch,
            cancel_request_id=observation.cancel_request_id,
            goal_uuid=sample.goal_uuid,
            binding_digest=binding_digest,
        )
        request_digest = request.request_fingerprint
        try:
            may_dispatch = self._store.claim_cancel_dispatch(
                self._token(observation),
                cancel_request_id=request.cancel_request_id,
                request_fingerprint=request_digest,
                binding_digest=binding_digest,
                wire_payload_digest=request.wire_payload_digest,
                now=now,
            )
        except GazeboMonitorRoomConflictError:
            current = self._store.observe(observation.operation_id)
            if self._same_external_target(
                current,
                observation,
                frozenset({'cancel_requested'}),
                self._worker_id,
            ):
                raise
            return current
        if not may_dispatch:
            return self._observe_claimed_cancel(
                observation,
                binding,
                binding_digest,
                sample,
                sample_digest,
                now,
            )
        try:
            ready = self._store.assert_cancel_ready(
                self._token(observation),
                cancel_request_id=request.cancel_request_id,
                now=self._now(),
            )
        except GazeboMonitorRoomConflictError:
            return self._store.observe(observation.operation_id)
        if not self._same_external_target(
            ready,
            observation,
            frozenset({'cancel_requested'}),
            self._worker_id,
        ):
            return ready
        with self._reserve_external(observation, 'cancel'):
            cancel_error = False
            report = None
            try:
                report = Nav2CancelReport(**self._port.cancel_goal(request))
                if request.request_fingerprint != request_digest:
                    raise GazeboMonitorRoomNav2AdapterError(
                        'nav2_binding_changed'
                    )
                self._assert_cancel_report_target(report, request)
                self._assert_binding_stable(binding, binding_digest)
                self._assert_sample_stable(sample, sample_digest)
            except Exception:
                cancel_error = True
            now = self._now()
            current, target_matches = self._post_call_target(
                observation,
                binding_digest=binding_digest,
                sample_digest=sample_digest,
                allowed_states=frozenset({'cancel_requested'}),
                now=now,
            )
            if not target_matches:
                return current
            if cancel_error:
                return self._record_cancel_unknown(
                    current,
                    code='nav2_cancel_unknown',
                    evidence_digest=_AMBIGUOUS_CANCEL_DIGEST,
                    now=now,
                )
            if report.status == 'canceled':
                try:
                    return self._store.record_canceled(
                        self._token(current),
                        terminal_evidence_digest=report.evidence_digest,
                        now=now,
                    )
                except GazeboMonitorRoomConflictError:
                    return self._store.observe(current.operation_id)
            if report.status == 'active':
                return self._store.observe(current.operation_id)
            return self._record_cancel_unknown(
                current,
                code=f'nav2_cancel_{report.status}',
                evidence_digest=report.evidence_digest,
                now=now,
            )

    def _observe_claimed_cancel(
        self,
        observation: OperationObservation,
        binding: PrivateOperationBinding,
        binding_digest: str,
        sample: PrivateStoredSample,
        sample_digest: str,
        now: float,
    ) -> OperationObservation:
        """Reconcile a durable cancel claim without resending CancelGoal."""
        query = self._goal_query(observation, binding_digest)
        query_digest = query.request_fingerprint
        with self._reserve_external(observation, 'cancel_observe'):
            observe_error = False
            report = None
            try:
                report = Nav2GoalReport(
                    **self._port.observe_goal(query)
                )
                if query.request_fingerprint != query_digest:
                    raise GazeboMonitorRoomNav2AdapterError(
                        'nav2_binding_changed'
                    )
                self._assert_goal_report_target(report, query)
                self._assert_binding_stable(binding, binding_digest)
                self._assert_sample_stable(sample, sample_digest)
            except Exception:
                observe_error = True
            now = self._now()
            current, target_matches = self._post_call_target(
                observation,
                binding_digest=binding_digest,
                sample_digest=sample_digest,
                allowed_states=frozenset({'cancel_requested'}),
                now=now,
            )
            if not target_matches:
                return current
            if observe_error:
                return self._record_cancel_unknown(
                    current,
                    code='nav2_cancel_unknown',
                    evidence_digest=_AMBIGUOUS_OBSERVE_DIGEST,
                    now=now,
                )
            if report.status == 'canceled':
                try:
                    return self._store.record_canceled(
                        self._token(current),
                        terminal_evidence_digest=report.evidence_digest,
                        now=now,
                    )
                except GazeboMonitorRoomConflictError:
                    return self._store.observe(current.operation_id)
            if report.status in {'accepted', 'active'}:
                return current
            return self._record_cancel_unknown(
                current,
                code='nav2_cancel_unknown',
                evidence_digest=report.evidence_digest,
                now=now,
            )

    @_boundary
    def cancel_once(
        self,
        operation_id: str,
        cancel_request_id: str,
    ) -> OperationObservation:
        """Persist one exact cancel intent and drive its next safe step."""
        normalized_operation_id = _identifier(
            operation_id, 'operation_id'
        )
        normalized_cancel_request_id = _identifier(
            cancel_request_id, 'cancel_request_id'
        )
        now = self._now()
        observation = self._store.observe(normalized_operation_id)
        if observation.state in _TERMINAL_STATES | _UNKNOWN_STATES:
            return observation
        observation = self._ensure_lease(observation, now)
        if observation.state == 'cancel_requested':
            if observation.cancel_request_id != normalized_cancel_request_id:
                raise GazeboMonitorRoomNav2AdapterError(
                    'nav2_state_rejected'
                )
        else:
            observation = self._store.request_cancel(
                CancelOperation(
                    cancel_request_id=normalized_cancel_request_id,
                    transition=self._token(observation),
                ),
                now=now,
            )
        return self.drive_once(observation.operation_id)

    @_boundary
    def drive_once(self, operation_id: str) -> OperationObservation:
        """Perform at most one injected Nav2 side effect for an operation."""
        normalized_operation_id = _identifier(operation_id, 'operation_id')
        now = self._now()
        observation = self._store.observe(normalized_operation_id)
        if observation.state in _TERMINAL_STATES | _UNKNOWN_STATES:
            return observation
        observation = self._ensure_lease(observation, now)
        if observation.state == 'prepared':
            if now >= observation.deadline:
                return self._record_failed(
                    observation,
                    code='deadline_expired',
                    evidence_digest=None,
                    now=now,
                )
            return self._store.begin_preflight(
                self._token(observation),
                now=now,
            )
        if observation.state == 'preflighting':
            if now >= observation.deadline:
                return self._record_failed(
                    observation,
                    code='deadline_expired',
                    evidence_digest=None,
                    now=now,
                )
            binding = self._private_binding(observation)
            binding_digest = binding.binding_digest
            sample = self._private_sample(observation)
            sample_digest = _sample_fingerprint(sample)
            preflight_request = self._preflight_request(binding, sample)
            preflight_request_digest = _preflight_request_fingerprint(
                preflight_request
            )
            with self._reserve_external(observation, 'preflight'):
                preflight_error = None
                preflight_report = None
                try:
                    preflight_report = Nav2PreflightReport(
                        **self._port.preflight(preflight_request)
                    )
                except GazeboMonitorRoomNav2AdapterError as error:
                    preflight_error = error.code
                except Exception:
                    preflight_error = 'nav2_port_rejected'
                now = self._now()
                current, target_matches = self._post_call_target(
                    observation,
                    binding_digest=binding_digest,
                    sample_digest=sample_digest,
                    allowed_states=frozenset({'preflighting'}),
                    now=now,
                )
                if not target_matches:
                    return current
                if preflight_error is not None:
                    raise GazeboMonitorRoomNav2AdapterError(
                        preflight_error
                    )
                if (
                    _preflight_request_fingerprint(preflight_request)
                    != preflight_request_digest
                ):
                    raise GazeboMonitorRoomNav2AdapterError(
                        'nav2_binding_changed'
                    )
                self._assert_preflight_report_target(
                    preflight_report,
                    preflight_request,
                    preflight_request_digest,
                )
                self._assert_binding_stable(binding, binding_digest)
                self._assert_sample_stable(sample, sample_digest)
                if preflight_report.outcome == 'retryable':
                    return current
                if preflight_report.outcome == 'rejected':
                    return self._record_failed(
                        current,
                        code=preflight_report.code,
                        evidence_digest=preflight_report.evidence_digest,
                        now=now,
                    )
                if now >= current.deadline:
                    return self._record_failed(
                        current,
                        code='deadline_expired',
                        evidence_digest=None,
                        now=now,
                    )
                try:
                    send_intent = self._store.record_send_intent(
                        self._token(current),
                        preflight_digest=preflight_report.evidence_digest,
                        now=now,
                    )
                except GazeboMonitorRoomConflictError:
                    return self._store.observe(current.operation_id)
            try:
                binding = self._private_binding(send_intent)
                self._assert_binding_stable(binding, binding_digest)
                send_sample = self._private_sample(send_intent)
            except GazeboMonitorRoomNav2AdapterError as error:
                if error.code not in {
                    'nav2_store_binding_changed',
                    'nav2_store_sample_changed',
                }:
                    raise
                return self._store.observe(send_intent.operation_id)
            send_sample_digest = _sample_fingerprint(send_sample)
            now = self._now()
            if send_intent.lease_expires_at is None:
                return self._store.observe(send_intent.operation_id)
            start = Nav2StartRequest(
                preflight=self._preflight_request(binding, send_sample),
                worker_id=self._worker_id,
                fence_epoch=send_intent.fence_epoch,
                lease_expires_at=send_intent.lease_expires_at,
                deadline=send_intent.deadline,
                preflight_digest=preflight_report.evidence_digest,
            )
            start_digest = start.request_fingerprint
            try:
                may_dispatch = self._store.claim_start_dispatch(
                    self._token(send_intent),
                    start_fingerprint=start_digest,
                    binding_digest=binding_digest,
                    preflight_digest=preflight_report.evidence_digest,
                    wire_payload_digest=start.wire_payload_digest,
                    now=now,
                )
            except GazeboMonitorRoomLeaseError:
                return self._store.observe(send_intent.operation_id)
            except GazeboMonitorRoomDeadlineError:
                current = self._store.observe(send_intent.operation_id)
                if (
                    self._same_external_target(
                        current,
                        send_intent,
                        frozenset({'send_intent'}),
                        self._worker_id,
                    )
                    and current.lease_expires_at is not None
                    and now < current.lease_expires_at
                ):
                    return self._record_failed(
                        current,
                        code='deadline_expired',
                        evidence_digest=_START_DEADLINE_DIGEST,
                        now=now,
                    )
                return current
            except GazeboMonitorRoomConflictError:
                current = self._store.observe(send_intent.operation_id)
                if self._same_external_target(
                    current,
                    send_intent,
                    frozenset({'send_intent'}),
                    self._worker_id,
                ):
                    raise
                return current
            if not may_dispatch:
                return self._observe_claimed_start(
                    send_intent,
                    binding,
                    binding_digest,
                    send_sample,
                    send_sample_digest,
                )
            with self._reserve_external(send_intent, 'ensure_started'):
                start_error = False
                accepted = None
                try:
                    accepted = Nav2GoalReport(
                        **self._port.ensure_started(start)
                    )
                    if start.request_fingerprint != start_digest:
                        raise GazeboMonitorRoomNav2AdapterError(
                            'nav2_binding_changed'
                        )
                    self._assert_goal_report_target(
                        accepted,
                        self._goal_query(send_intent, binding_digest),
                    )
                    self._assert_binding_stable(binding, binding_digest)
                    self._assert_sample_stable(
                        send_sample, send_sample_digest
                    )
                except Exception:
                    start_error = True
                now = self._now()
                current, target_matches = self._post_call_target(
                    send_intent,
                    binding_digest=binding_digest,
                    sample_digest=send_sample_digest,
                    allowed_states=frozenset({'send_intent'}),
                    now=now,
                )
                if not target_matches:
                    return current
                if start_error:
                    return self._record_delivery_unknown(
                        current,
                        evidence_digest=_AMBIGUOUS_SEND_DIGEST,
                        now=now,
                    )
                return self._record_observed_goal(
                    current, accepted, now
                )
        if observation.state in {'send_intent', 'navigating'}:
            binding = self._private_binding(observation)
            binding_digest = binding.binding_digest
            sample = self._private_sample(observation)
            sample_digest = _sample_fingerprint(sample)
            return self._observe_claimed_start(
                observation,
                binding,
                binding_digest,
                sample,
                sample_digest,
            )
        if observation.state == 'cancel_requested':
            return self._drive_cancel_requested(observation, now)
        raise GazeboMonitorRoomNav2AdapterError('nav2_state_rejected')
