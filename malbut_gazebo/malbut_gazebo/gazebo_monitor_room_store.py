"""
Durable Gazebo-only state for ordered semantic navigation samples.

This module is deliberately a storage core.  It does not import ROS, send a
Nav2 goal, authorize physical execution, or claim room or camera coverage.
The stored millimetre coordinates are private adapter inputs; the public
observation exposes only progress and explicit non-claims.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple
import uuid


GAZEBO_MONITOR_ROOM_SCHEMA_VERSION = 3
GAZEBO_MONITOR_ROOM_MAX_SAMPLES = 4096
GAZEBO_MONITOR_ROOM_MAX_EVENTS = 65536
GAZEBO_MONITOR_ROOM_MAX_LEASE_SECONDS = 300.0

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_GOAL_UUID = re.compile(r'^[0-9a-f]{32}$')
_STORE_NAMESPACE = re.compile(r'^[0-9a-f]{32}$')
_HOST_BOOT_ID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$'
)
_ZERO_DIGEST = '0' * 64
_GOAL_NAMESPACE = b'malbut-gazebo-monitor-room-nav2-goal-v2\0'
_MAX_COORDINATE_MM = 10_000_000
_MAX_ORDINAL = 1_000_000
_MAX_FENCE = 9_223_372_036_854_775_806

NONTERMINAL_STATES = frozenset(
    {
        'prepared',
        'preflighting',
        'send_intent',
        'navigating',
        'cancel_requested',
    }
)
UNKNOWN_STATES = frozenset({'delivery_unknown', 'cancel_unknown'})
RESOLVED_TERMINAL_STATES = frozenset({'succeeded', 'failed', 'canceled'})
TERMINAL_STATES = UNKNOWN_STATES | RESOLVED_TERMINAL_STATES
OPERATION_STATES = NONTERMINAL_STATES | TERMINAL_STATES

SAMPLE_STATES = frozenset(
    {
        'pending',
        'preflighting',
        'send_intent',
        'navigating',
        'cancel_requested',
        'succeeded',
        'failed',
        'canceled',
        'delivery_unknown',
        'cancel_unknown',
    }
)


class GazeboMonitorRoomStoreError(RuntimeError):
    """Base error for the durable Gazebo navigation store."""


class GazeboMonitorRoomValidationError(
    GazeboMonitorRoomStoreError, ValueError
):
    """Raised when an adapter value is not strict and bounded."""


class GazeboMonitorRoomSchemaError(GazeboMonitorRoomStoreError):
    """Raised when the exact schema or durable rows are incompatible."""


class GazeboMonitorRoomDurabilityError(GazeboMonitorRoomStoreError):
    """Raised when the open database loses its durable path binding."""


class GazeboMonitorRoomConflictError(GazeboMonitorRoomStoreError):
    """Raised when an idempotency identity or CAS precondition conflicts."""


class GazeboMonitorRoomNotFoundError(GazeboMonitorRoomStoreError):
    """Raised when an operation does not exist."""


class GazeboMonitorRoomLeaseError(GazeboMonitorRoomConflictError):
    """Raised when a lease is busy, expired, or held by another worker."""


class GazeboMonitorRoomFenceError(GazeboMonitorRoomConflictError):
    """Raised when a worker presents a stale fence epoch."""


class GazeboMonitorRoomClockRollbackError(GazeboMonitorRoomStoreError):
    """Raised when a write clock precedes durable history."""


class GazeboMonitorRoomDeadlineError(GazeboMonitorRoomStoreError):
    """Raised before a new side effect after the operation deadline."""


class GazeboMonitorRoomBootIdentityError(
    GazeboMonitorRoomDurabilityError
):
    """Raised without host content when boot identity cannot be trusted."""

    def __getattribute__(self, name: str) -> Any:
        """Hide exception-chain metadata at the boot trust boundary."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


def _store_namespace(value: Any) -> str:
    if type(value) is not str or _STORE_NAMESPACE.fullmatch(value) is None:
        raise GazeboMonitorRoomValidationError(
            'store_namespace is invalid'
        )
    return value


def _host_boot_id(value: Any) -> str:
    if type(value) is not str or _HOST_BOOT_ID.fullmatch(value) is None:
        raise GazeboMonitorRoomBootIdentityError(
            'host boot identity is unavailable'
        )
    return value


def _read_host_boot_id() -> str:
    """Read the exact Linux boot UUID without exposing read failures."""
    try:
        with open(
            '/proc/sys/kernel/random/boot_id',
            'r',
            encoding='ascii',
        ) as stream:
            value = stream.read(38)
    except (OSError, UnicodeError):
        raise GazeboMonitorRoomBootIdentityError(
            'host boot identity is unavailable'
        ) from None
    if value.endswith('\n'):
        value = value[:-1]
    return _host_boot_id(value)


def _identifier(value: Any, field_name: str) -> str:
    if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise GazeboMonitorRoomValidationError(f'{field_name} is invalid')
    return value


def _digest(value: Any, field_name: str) -> str:
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        raise GazeboMonitorRoomValidationError(f'{field_name} is invalid')
    return value


def _goal_uuid(value: Any, field_name: str = 'goal_uuid') -> str:
    if type(value) is not str or _GOAL_UUID.fullmatch(value) is None:
        raise GazeboMonitorRoomValidationError(f'{field_name} is invalid')
    return value


def _bounded_integer(
    value: Any,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise GazeboMonitorRoomValidationError(f'{field_name} is invalid')
    return value


def _timestamp(value: Any, field_name: str) -> float:
    if type(value) not in (int, float):
        raise GazeboMonitorRoomValidationError(f'{field_name} is invalid')
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise GazeboMonitorRoomValidationError(f'{field_name} is invalid')
    return 0.0 if normalized == 0 else normalized


def _hash_json(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError):
        raise GazeboMonitorRoomValidationError(
            'canonical value is invalid'
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def stable_goal_uuid(
    store_namespace: str,
    operation_id: str,
    sample_index: int,
) -> str:
    """Return a store-scoped stable UUID independent of lease fences."""
    normalized_namespace = _store_namespace(store_namespace)
    normalized_operation = _identifier(operation_id, 'operation_id')
    normalized_index = _bounded_integer(
        sample_index,
        'sample_index',
        0,
        GAZEBO_MONITOR_ROOM_MAX_SAMPLES - 1,
    )
    raw = bytearray(
        hashlib.sha256(
            _GOAL_NAMESPACE
            + normalized_namespace.encode('ascii')
            + b'\0'
            + normalized_operation.encode('utf-8')
            + b'\0'
            + str(normalized_index).encode('ascii')
        ).digest()[:16]
    )
    raw[6] = (raw[6] & 0x0f) | 0x50
    raw[8] = (raw[8] & 0x3f) | 0x80
    return uuid.UUID(bytes=bytes(raw)).hex


@dataclass(frozen=True)
class OrderedSemanticSample:
    """One private semantic candidate, explicitly not a route or coverage."""

    index: int
    polygon_ordinal: int
    row_ordinal: int
    x_mm: int = field(repr=False)
    y_mm: int = field(repr=False)
    frame_id: str = 'map'

    def __post_init__(self) -> None:
        """Require bounded integer millimetres and the Nav2 map frame."""
        object.__setattr__(
            self,
            'index',
            _bounded_integer(
                self.index,
                'sample index',
                0,
                GAZEBO_MONITOR_ROOM_MAX_SAMPLES - 1,
            ),
        )
        for name in ('polygon_ordinal', 'row_ordinal'):
            object.__setattr__(
                self,
                name,
                _bounded_integer(
                    getattr(self, name), name, 0, _MAX_ORDINAL
                ),
            )
        for name in ('x_mm', 'y_mm'):
            object.__setattr__(
                self,
                name,
                _bounded_integer(
                    getattr(self, name),
                    name,
                    -_MAX_COORDINATE_MM,
                    _MAX_COORDINATE_MM,
                ),
            )
        if self.frame_id != 'map':
            raise GazeboMonitorRoomValidationError(
                'sample frame_id must be map'
            )

    def _private_payload(self) -> Dict[str, Any]:
        """Return coordinate-bearing data only for durable binding."""
        return {
            'index': self.index,
            'polygon_ordinal': self.polygon_ordinal,
            'row_ordinal': self.row_ordinal,
            'x_mm': self.x_mm,
            'y_mm': self.y_mm,
            'frame_id': self.frame_id,
        }


@dataclass(frozen=True)
class PrepareOperation:
    """Exact immutable input for one Gazebo-only operation."""

    prepare_request_id: str
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
    ordered_semantic_samples: Tuple[OrderedSemanticSample, ...] = field(
        repr=False
    )
    deadline: float
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _payload_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Freeze identifiers, bindings, samples, deadline, and non-claims."""
        for name in (
            'prepare_request_id',
            'operation_id',
            'robot_id',
            'map_id',
            'map_revision',
            'semantic_revision',
        ):
            object.__setattr__(
                self, name, _identifier(getattr(self, name), name)
            )
        for name in (
            'zones_digest',
            'target_binding_digest',
            'effects_digest',
            'profile_digest',
            'plan_digest',
        ):
            object.__setattr__(
                self, name, _digest(getattr(self, name), name)
            )
        samples = self.ordered_semantic_samples
        if (
            type(samples) is not tuple
            or not 1 <= len(samples) <= GAZEBO_MONITOR_ROOM_MAX_SAMPLES
            or any(
                type(sample) is not OrderedSemanticSample
                for sample in samples
            )
            or any(
                sample.index != index
                for index, sample in enumerate(samples)
            )
        ):
            raise GazeboMonitorRoomValidationError(
                'ordered semantic samples are invalid'
            )
        object.__setattr__(self, 'deadline', _timestamp(
            self.deadline, 'deadline'
        ))
        object.__setattr__(
            self,
            '_payload_fingerprint',
            _hash_json(
                {
                    'contract': 'gazebo-monitor-room-prepare-v1',
                    'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
                    'prepare_request_id': self.prepare_request_id,
                    'operation_id': self.operation_id,
                    'robot_id': self.robot_id,
                    'runtime_mode': 'gazebo',
                    'map_id': self.map_id,
                    'map_revision': self.map_revision,
                    'semantic_revision': self.semantic_revision,
                    'zones_digest': self.zones_digest,
                    'target_binding_digest': self.target_binding_digest,
                    'effects_digest': self.effects_digest,
                    'profile_digest': self.profile_digest,
                    'plan_digest': self.plan_digest,
                    'ordered_semantic_samples': [
                        sample._private_payload() for sample in samples
                    ],
                    'deadline': self.deadline,
                    'simulation': True,
                    'physical_authorized': False,
                    'physical_effects': False,
                    'viewer_live': False,
                    'camera_coverage_validated': False,
                    'coverage_achieved': False,
                }
            ),
        )

    @property
    def payload_fingerprint(self) -> str:
        """Return the exact coordinate-bearing prepare fingerprint."""
        return self._payload_fingerprint


@dataclass(frozen=True)
class GoalTransition:
    """Compare-and-swap token for one exact operation and Nav2 goal."""

    operation_id: str
    worker_id: str
    fence_epoch: int
    sample_index: int
    goal_uuid: str
    expected_operation_state: str
    expected_sample_state: str

    def __post_init__(self) -> None:
        """Reject incomplete or weak transition selectors."""
        for name in ('operation_id', 'worker_id'):
            object.__setattr__(
                self, name, _identifier(getattr(self, name), name)
            )
        object.__setattr__(
            self,
            'fence_epoch',
            _bounded_integer(
                self.fence_epoch, 'fence_epoch', 1, _MAX_FENCE
            ),
        )
        object.__setattr__(
            self,
            'sample_index',
            _bounded_integer(
                self.sample_index,
                'sample_index',
                0,
                GAZEBO_MONITOR_ROOM_MAX_SAMPLES - 1,
            ),
        )
        object.__setattr__(self, 'goal_uuid', _goal_uuid(self.goal_uuid))
        if self.expected_operation_state not in OPERATION_STATES:
            raise GazeboMonitorRoomValidationError(
                'expected operation state is invalid'
            )
        if self.expected_sample_state not in SAMPLE_STATES:
            raise GazeboMonitorRoomValidationError(
                'expected sample state is invalid'
            )


@dataclass(frozen=True)
class CancelOperation:
    """Idempotent cancellation intent bound to one exact CAS token."""

    cancel_request_id: str
    transition: GoalTransition
    reason_code: str = 'operator_requested'
    _request_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind cancellation identity, reason, fence, sample, and goal."""
        object.__setattr__(
            self,
            'cancel_request_id',
            _identifier(self.cancel_request_id, 'cancel_request_id'),
        )
        if type(self.transition) is not GoalTransition:
            raise GazeboMonitorRoomValidationError(
                'cancel transition is invalid'
            )
        object.__setattr__(
            self,
            'reason_code',
            _identifier(self.reason_code, 'reason_code'),
        )
        token = self.transition
        object.__setattr__(
            self,
            '_request_fingerprint',
            _hash_json(
                {
                    'contract': 'gazebo-monitor-room-cancel-v1',
                    'cancel_request_id': self.cancel_request_id,
                    'operation_id': token.operation_id,
                    'worker_id': token.worker_id,
                    'fence_epoch': token.fence_epoch,
                    'sample_index': token.sample_index,
                    'goal_uuid': token.goal_uuid,
                    'expected_operation_state': (
                        token.expected_operation_state
                    ),
                    'expected_sample_state': token.expected_sample_state,
                    'reason_code': self.reason_code,
                }
            ),
        )

    @property
    def request_fingerprint(self) -> str:
        """Return the exact cancel request fingerprint."""
        return self._request_fingerprint


def _canonical_goal_transition(value: Any) -> GoalTransition:
    """Rebuild a transition so frozen-object mutation cannot bypass checks."""
    if type(value) is not GoalTransition:
        raise GazeboMonitorRoomValidationError(
            'goal transition is invalid'
        )
    try:
        canonical = GoalTransition(
            operation_id=value.operation_id,
            worker_id=value.worker_id,
            fence_epoch=value.fence_epoch,
            sample_index=value.sample_index,
            goal_uuid=value.goal_uuid,
            expected_operation_state=value.expected_operation_state,
            expected_sample_state=value.expected_sample_state,
        )
    except (AttributeError, GazeboMonitorRoomValidationError):
        raise GazeboMonitorRoomValidationError(
            'goal transition is invalid'
        ) from None
    if canonical != value:
        raise GazeboMonitorRoomValidationError(
            'goal transition is not canonical'
        )
    return canonical


def _canonical_cancel_operation(value: Any) -> CancelOperation:
    """Rebuild cancellation and its nested CAS before trusting its digest."""
    if type(value) is not CancelOperation:
        raise GazeboMonitorRoomValidationError(
            'cancel request is invalid'
        )
    try:
        canonical = CancelOperation(
            cancel_request_id=value.cancel_request_id,
            transition=_canonical_goal_transition(value.transition),
            reason_code=value.reason_code,
        )
    except (AttributeError, GazeboMonitorRoomValidationError):
        raise GazeboMonitorRoomValidationError(
            'cancel request is invalid'
        ) from None
    if (
        canonical != value
        or canonical.request_fingerprint != value.request_fingerprint
    ):
        raise GazeboMonitorRoomValidationError(
            'cancel request is not canonical'
        )
    return canonical


def _canonical_prepare_operation(value: Any) -> PrepareOperation:
    """Rebuild all private samples before trusting a prepare fingerprint."""
    if type(value) is not PrepareOperation:
        raise GazeboMonitorRoomValidationError(
            'prepare request is invalid'
        )
    try:
        if type(value.ordered_semantic_samples) is not tuple:
            raise GazeboMonitorRoomValidationError(
                'ordered semantic samples are invalid'
            )
        samples = tuple(
            OrderedSemanticSample(
                index=sample.index,
                polygon_ordinal=sample.polygon_ordinal,
                row_ordinal=sample.row_ordinal,
                x_mm=sample.x_mm,
                y_mm=sample.y_mm,
                frame_id=sample.frame_id,
            )
            for sample in value.ordered_semantic_samples
            if type(sample) is OrderedSemanticSample
        )
        if len(samples) != len(value.ordered_semantic_samples):
            raise GazeboMonitorRoomValidationError(
                'ordered semantic samples are invalid'
            )
        canonical = PrepareOperation(
            prepare_request_id=value.prepare_request_id,
            operation_id=value.operation_id,
            robot_id=value.robot_id,
            map_id=value.map_id,
            map_revision=value.map_revision,
            semantic_revision=value.semantic_revision,
            zones_digest=value.zones_digest,
            target_binding_digest=value.target_binding_digest,
            effects_digest=value.effects_digest,
            profile_digest=value.profile_digest,
            plan_digest=value.plan_digest,
            ordered_semantic_samples=samples,
            deadline=value.deadline,
        )
    except (AttributeError, GazeboMonitorRoomValidationError):
        raise GazeboMonitorRoomValidationError(
            'prepare request is invalid'
        ) from None
    if (
        canonical != value
        or canonical.payload_fingerprint != value.payload_fingerprint
    ):
        raise GazeboMonitorRoomValidationError(
            'prepare request is not canonical'
        )
    return canonical


@dataclass(frozen=True)
class OperationObservation:
    """Coordinate-free durable operation observation."""

    operation_id: str
    robot_id: str
    state: str
    current_sample_index: int
    current_sample_state: str
    current_goal_uuid: str
    navigation_samples_total: int
    navigation_samples_reached: int
    fence_epoch: int
    lease_owner: Optional[str]
    lease_expires_at: Optional[float]
    deadline: float
    terminal_code: Optional[str]
    cancel_request_id: Optional[str]
    created_at: float
    updated_at: float
    replayed: bool = False
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Reject malformed observations before they leave the store."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.robot_id, 'robot_id')
        if self.state not in OPERATION_STATES:
            raise GazeboMonitorRoomValidationError('state is invalid')
        if self.current_sample_state not in SAMPLE_STATES:
            raise GazeboMonitorRoomValidationError(
                'current sample state is invalid'
            )
        _goal_uuid(self.current_goal_uuid)
        _bounded_integer(
            self.navigation_samples_total,
            'navigation_samples_total',
            1,
            GAZEBO_MONITOR_ROOM_MAX_SAMPLES,
        )
        _bounded_integer(
            self.current_sample_index,
            'current_sample_index',
            0,
            self.navigation_samples_total - 1,
        )
        _bounded_integer(
            self.navigation_samples_reached,
            'navigation_samples_reached',
            0,
            self.navigation_samples_total,
        )
        _bounded_integer(self.fence_epoch, 'fence_epoch', 0, _MAX_FENCE)
        if self.lease_owner is not None:
            _identifier(self.lease_owner, 'lease_owner')
        if self.lease_expires_at is not None:
            _timestamp(self.lease_expires_at, 'lease_expires_at')
        _timestamp(self.deadline, 'deadline')
        created_at = _timestamp(self.created_at, 'created_at')
        updated_at = _timestamp(self.updated_at, 'updated_at')
        if self.deadline <= created_at or updated_at < created_at:
            raise GazeboMonitorRoomValidationError(
                'observation time is invalid'
            )
        if (self.fence_epoch == 0) != (self.lease_owner is None):
            raise GazeboMonitorRoomValidationError(
                'observation lease is invalid'
            )
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise GazeboMonitorRoomValidationError(
                'observation lease is invalid'
            )
        if self.terminal_code is not None:
            _identifier(self.terminal_code, 'terminal_code')
        if self.cancel_request_id is not None:
            _identifier(self.cancel_request_id, 'cancel_request_id')
        if (self.state in TERMINAL_STATES) != (
            self.terminal_code is not None
        ):
            raise GazeboMonitorRoomValidationError(
                'observation terminal binding is invalid'
            )
        if self.state in {'cancel_requested', 'canceled', 'cancel_unknown'}:
            if self.cancel_request_id is None:
                raise GazeboMonitorRoomValidationError(
                    'observation cancel binding is invalid'
                )
        elif self.cancel_request_id is not None:
            raise GazeboMonitorRoomValidationError(
                'observation cancel binding is invalid'
            )
        if self.state == 'succeeded':
            if (
                self.navigation_samples_reached
                != self.navigation_samples_total
            ):
                raise GazeboMonitorRoomValidationError(
                    'observation progress is invalid'
                )
        elif self.navigation_samples_reached != self.current_sample_index:
            raise GazeboMonitorRoomValidationError(
                'observation progress is invalid'
            )
        if type(self.replayed) is not bool:
            raise GazeboMonitorRoomValidationError('replayed is invalid')

    @property
    def terminal(self) -> bool:
        """Return whether no further automatic state transition is allowed."""
        return self.state in TERMINAL_STATES

    @property
    def robot_blocked(self) -> bool:
        """Keep unknown external delivery states fenced from new work."""
        return self.state in NONTERMINAL_STATES | UNKNOWN_STATES

    @property
    def all_navigation_samples_reached(self) -> bool:
        """Report only navigation progress, never room coverage."""
        return (
            self.state == 'succeeded'
            and self.navigation_samples_reached
            == self.navigation_samples_total
        )

    def to_public_dict(self) -> Dict[str, Any]:
        """Return progress without private coordinates or coverage claims."""
        return {
            'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
            'operation_id': self.operation_id,
            'robot_id': self.robot_id,
            'state': self.state,
            'current_sample_index': self.current_sample_index,
            'current_sample_state': self.current_sample_state,
            'current_goal_uuid': self.current_goal_uuid,
            'navigation_samples_total': self.navigation_samples_total,
            'navigation_samples_reached': self.navigation_samples_reached,
            'all_navigation_samples_reached': (
                self.all_navigation_samples_reached
            ),
            'fence_epoch': self.fence_epoch,
            'lease_expires_at': self.lease_expires_at,
            'deadline': self.deadline,
            'terminal': self.terminal,
            'robot_blocked': self.robot_blocked,
            'terminal_code': self.terminal_code,
            'replayed': self.replayed,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }


@dataclass(frozen=True)
class LeaseGrant:
    """One acquired or renewed worker lease and fence."""

    observation: OperationObservation
    worker_id: str
    fence_epoch: int
    lease_expires_at: float
    taken_over: bool

    def __post_init__(self) -> None:
        """Require an exact lease-to-observation binding."""
        if type(self.observation) is not OperationObservation:
            raise GazeboMonitorRoomValidationError(
                'lease observation is invalid'
            )
        _identifier(self.worker_id, 'worker_id')
        _bounded_integer(self.fence_epoch, 'fence_epoch', 1, _MAX_FENCE)
        _timestamp(self.lease_expires_at, 'lease_expires_at')
        if (
            self.observation.lease_owner != self.worker_id
            or self.observation.fence_epoch != self.fence_epoch
            or self.observation.lease_expires_at != self.lease_expires_at
            or type(self.taken_over) is not bool
        ):
            raise GazeboMonitorRoomValidationError(
                'lease grant binding is invalid'
            )


@dataclass(frozen=True)
class DispatchClaimEvidence:
    """Coordinate-free proof that one durable dispatch claim is current."""

    phase: str = field(repr=False)
    store_namespace: str = field(repr=False)
    operation_id: str = field(repr=False)
    sample_index: int = field(repr=False)
    goal_uuid: str = field(repr=False)
    operation_state: str = field(repr=False)
    sample_state: str = field(repr=False)
    start_fingerprint: Optional[str] = field(repr=False)
    cancel_request_id: Optional[str] = field(repr=False)
    cancel_request_fingerprint: Optional[str] = field(repr=False)
    worker_id: str = field(repr=False)
    fence_epoch: int = field(repr=False)
    binding_digest: str = field(repr=False)
    preflight_digest: Optional[str] = field(repr=False)
    wire_payload_digest: str = field(repr=False)
    claim_lease_expires_at: float = field(repr=False)
    current_lease_expires_at: float = field(repr=False)
    claimed_at: float = field(repr=False)
    operation_deadline: float = field(repr=False)
    checked_at: float = field(repr=False)
    claim_record_digest: str = field(repr=False)
    operation_record_digest: str = field(repr=False)
    sample_record_digest: str = field(repr=False)
    schema_version: int = field(
        default=GAZEBO_MONITOR_ROOM_SCHEMA_VERSION, init=False
    )
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _evidence_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind an exact claim snapshot without exposing private samples."""
        if type(self.phase) is not str or self.phase not in {
            'start',
            'cancel',
        }:
            raise GazeboMonitorRoomValidationError(
                'dispatch evidence phase is invalid'
            )
        _store_namespace(self.store_namespace)
        for name in ('operation_id', 'worker_id'):
            _identifier(getattr(self, name), name)
        _bounded_integer(
            self.sample_index,
            'sample_index',
            0,
            GAZEBO_MONITOR_ROOM_MAX_SAMPLES - 1,
        )
        _goal_uuid(self.goal_uuid)
        expected_state = (
            'send_intent' if self.phase == 'start' else 'cancel_requested'
        )
        if (
            type(self.operation_state) is not str
            or type(self.sample_state) is not str
            or self.operation_state != expected_state
            or self.sample_state != expected_state
        ):
            raise GazeboMonitorRoomValidationError(
                'dispatch evidence state is invalid'
            )
        _bounded_integer(
            self.fence_epoch, 'fence_epoch', 1, _MAX_FENCE
        )
        for name in (
            'binding_digest',
            'wire_payload_digest',
            'claim_record_digest',
            'operation_record_digest',
            'sample_record_digest',
        ):
            _digest(getattr(self, name), name)
        if self.phase == 'start':
            _digest(self.start_fingerprint, 'start_fingerprint')
            _digest(self.preflight_digest, 'preflight_digest')
            if (
                self.cancel_request_id is not None
                or self.cancel_request_fingerprint is not None
            ):
                raise GazeboMonitorRoomValidationError(
                    'start dispatch evidence is invalid'
                )
        else:
            _identifier(self.cancel_request_id, 'cancel_request_id')
            _digest(
                self.cancel_request_fingerprint,
                'cancel_request_fingerprint',
            )
            if (
                self.start_fingerprint is not None
                or self.preflight_digest is not None
            ):
                raise GazeboMonitorRoomValidationError(
                    'cancel dispatch evidence is invalid'
                )
        for name in (
            'claim_lease_expires_at',
            'current_lease_expires_at',
            'claimed_at',
            'operation_deadline',
            'checked_at',
        ):
            object.__setattr__(
                self, name, _timestamp(getattr(self, name), name)
            )
        if (
            self.claimed_at >= self.claim_lease_expires_at
            or self.current_lease_expires_at
            < self.claim_lease_expires_at
            or self.checked_at < self.claimed_at
            or self.checked_at >= self.current_lease_expires_at
            or (
                self.phase == 'start'
                and (
                    self.checked_at >= self.claim_lease_expires_at
                    or self.checked_at >= self.operation_deadline
                )
            )
        ):
            raise GazeboMonitorRoomValidationError(
                'dispatch evidence chronology is invalid'
            )
        if self.claim_record_digest != _dispatch_claim_digest(
            {
                'schema_version': self.schema_version,
                'store_namespace': self.store_namespace,
                'phase': self.phase,
                'operation_id': self.operation_id,
                'sample_index': self.sample_index,
                'goal_uuid': self.goal_uuid,
                'start_fingerprint': self.start_fingerprint,
                'cancel_request_id': self.cancel_request_id,
                'cancel_request_fingerprint': (
                    self.cancel_request_fingerprint
                ),
                'worker_id': self.worker_id,
                'fence_epoch': self.fence_epoch,
                'binding_digest': self.binding_digest,
                'preflight_digest': self.preflight_digest,
                'wire_payload_digest': self.wire_payload_digest,
                'lease_expires_at': self.claim_lease_expires_at,
                'claimed_at': self.claimed_at,
            }
        ):
            raise GazeboMonitorRoomValidationError(
                'dispatch evidence claim digest is invalid'
            )
        object.__setattr__(
            self, '_evidence_digest', _hash_json(self._digest_payload())
        )

    def _digest_payload(self) -> Dict[str, Any]:
        """Return every public proof field in its canonical hash shape."""
        return {
            'contract': 'gazebo-monitor-room-dispatch-evidence-v1',
            'schema_version': self.schema_version,
            'phase': self.phase,
            'store_namespace': self.store_namespace,
            'operation_id': self.operation_id,
            'sample_index': self.sample_index,
            'goal_uuid': self.goal_uuid,
            'operation_state': self.operation_state,
            'sample_state': self.sample_state,
            'start_fingerprint': self.start_fingerprint,
            'cancel_request_id': self.cancel_request_id,
            'cancel_request_fingerprint': (
                self.cancel_request_fingerprint
            ),
            'worker_id': self.worker_id,
            'fence_epoch': self.fence_epoch,
            'binding_digest': self.binding_digest,
            'preflight_digest': self.preflight_digest,
            'wire_payload_digest': self.wire_payload_digest,
            'claim_lease_expires_at': self.claim_lease_expires_at,
            'current_lease_expires_at': self.current_lease_expires_at,
            'claimed_at': self.claimed_at,
            'operation_deadline': self.operation_deadline,
            'checked_at': self.checked_at,
            'claim_record_digest': self.claim_record_digest,
            'operation_record_digest': self.operation_record_digest,
            'sample_record_digest': self.sample_record_digest,
            'runtime_mode': self.runtime_mode,
            'simulation': self.simulation,
            'physical_authorized': self.physical_authorized,
            'physical_effects': self.physical_effects,
            'viewer_live': self.viewer_live,
            'camera_coverage_validated': (
                self.camera_coverage_validated
            ),
            'coverage_achieved': self.coverage_achieved,
        }

    @property
    def evidence_digest(self) -> str:
        """Return the canonical proof digest and detect frozen bypasses."""
        current = _hash_json(self._digest_payload())
        if current != self._evidence_digest:
            raise GazeboMonitorRoomValidationError(
                'dispatch claim evidence changed after validation'
            )
        return current


@dataclass(frozen=True)
class PrivateStoredSample:
    """Private coordinate-bearing input for a future Gazebo Nav2 adapter."""

    operation_id: str
    store_namespace: str = field(repr=False)
    index: int
    polygon_ordinal: int
    row_ordinal: int
    x_mm: int = field(repr=False)
    y_mm: int = field(repr=False)
    frame_id: str
    goal_uuid: str
    state: str

    def __post_init__(self) -> None:
        """Validate the coordinate-bearing internal adapter value."""
        _identifier(self.operation_id, 'operation_id')
        _store_namespace(self.store_namespace)
        OrderedSemanticSample(
            index=self.index,
            polygon_ordinal=self.polygon_ordinal,
            row_ordinal=self.row_ordinal,
            x_mm=self.x_mm,
            y_mm=self.y_mm,
            frame_id=self.frame_id,
        )
        if self.goal_uuid != stable_goal_uuid(
            self.store_namespace, self.operation_id, self.index
        ):
            raise GazeboMonitorRoomValidationError(
                'stored sample goal UUID is invalid'
            )
        if self.state not in SAMPLE_STATES:
            raise GazeboMonitorRoomValidationError(
                'stored sample state is invalid'
            )

    @property
    def x_m(self) -> float:
        """Convert the exact private X coordinate to metres."""
        return self.x_mm / 1000.0

    @property
    def y_m(self) -> float:
        """Convert the exact private Y coordinate to metres."""
        return self.y_mm / 1000.0


@dataclass(frozen=True)
class PrivateOperationBinding:
    """Private persisted evidence, never adapter execution authority."""

    operation_id: str = field(repr=False)
    prepare_fingerprint: str = field(repr=False)
    robot_id: str = field(repr=False)
    map_id: str = field(repr=False)
    map_revision: str = field(repr=False)
    semantic_revision: str = field(repr=False)
    zones_digest: str = field(repr=False)
    target_binding_digest: str = field(repr=False)
    effects_digest: str = field(repr=False)
    profile_digest: str = field(repr=False)
    plan_digest: str = field(repr=False)
    sample_count: int = field(repr=False)
    deadline: float = field(repr=False)
    schema_version: int = field(
        default=GAZEBO_MONITOR_ROOM_SCHEMA_VERSION, init=False
    )
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Validate the durable binding before an adapter sees it."""
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
            'prepare_fingerprint',
        ):
            object.__setattr__(
                self, name, _digest(getattr(self, name), name)
            )
        object.__setattr__(
            self,
            'sample_count',
            _bounded_integer(
                self.sample_count,
                'sample_count',
                1,
                GAZEBO_MONITOR_ROOM_MAX_SAMPLES,
            ),
        )
        object.__setattr__(
            self, 'deadline', _timestamp(self.deadline, 'deadline')
        )

    @property
    def binding_digest(self) -> str:
        """Return the exact non-coordinate operation binding digest."""
        canonical = _canonical_private_operation_binding(self)
        return _hash_json(
            {
                'contract': 'gazebo-monitor-room-operation-binding-v1',
                'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
                'operation_id': canonical.operation_id,
                'prepare_fingerprint': canonical.prepare_fingerprint,
                'robot_id': canonical.robot_id,
                'runtime_mode': 'gazebo',
                'map_id': canonical.map_id,
                'map_revision': canonical.map_revision,
                'semantic_revision': canonical.semantic_revision,
                'zones_digest': canonical.zones_digest,
                'target_binding_digest': canonical.target_binding_digest,
                'effects_digest': canonical.effects_digest,
                'profile_digest': canonical.profile_digest,
                'plan_digest': canonical.plan_digest,
                'sample_count': canonical.sample_count,
                'deadline': canonical.deadline,
                'simulation': True,
                'physical_authorized': False,
                'physical_effects': False,
                'viewer_live': False,
                'camera_coverage_validated': False,
                'coverage_achieved': False,
            }
        )


def _canonical_private_operation_binding(
    value: PrivateOperationBinding,
) -> PrivateOperationBinding:
    """Revalidate all current binding fields after frozen-object bypasses."""
    if type(value) is not PrivateOperationBinding:
        raise GazeboMonitorRoomValidationError(
            'private operation binding is invalid'
        )
    canonical = PrivateOperationBinding(
        operation_id=value.operation_id,
        prepare_fingerprint=value.prepare_fingerprint,
        robot_id=value.robot_id,
        map_id=value.map_id,
        map_revision=value.map_revision,
        semantic_revision=value.semantic_revision,
        zones_digest=value.zones_digest,
        target_binding_digest=value.target_binding_digest,
        effects_digest=value.effects_digest,
        profile_digest=value.profile_digest,
        plan_digest=value.plan_digest,
        sample_count=value.sample_count,
        deadline=value.deadline,
    )
    for name in (
        'operation_id',
        'prepare_fingerprint',
        'robot_id',
        'map_id',
        'map_revision',
        'semantic_revision',
        'zones_digest',
        'target_binding_digest',
        'effects_digest',
        'profile_digest',
        'plan_digest',
        'sample_count',
        'deadline',
        'schema_version',
        'runtime_mode',
        'simulation',
        'physical_authorized',
        'physical_effects',
        'viewer_live',
        'camera_coverage_validated',
        'coverage_achieved',
    ):
        current = getattr(value, name)
        expected = getattr(canonical, name)
        if type(current) is not type(expected) or current != expected:
            raise GazeboMonitorRoomValidationError(
                'private operation binding changed after validation'
            )
    return canonical


@dataclass(frozen=True)
class OperationEvent:
    """Coordinate-free append-only audit event."""

    operation_id: str
    event_seq: int
    event_type: str
    recorded_at: float
    fence_epoch: int
    lease_expires_at: Optional[float]
    worker_id: str
    sample_index: Optional[int]
    goal_uuid: Optional[str]
    from_operation_state: Optional[str]
    to_operation_state: str
    from_sample_state: Optional[str]
    to_sample_state: Optional[str]
    code: Optional[str]
    evidence_digest: Optional[str]
    event_digest: str

    def __post_init__(self) -> None:
        """Validate the bounded coordinate-free event value."""
        _identifier(self.operation_id, 'operation_id')
        _bounded_integer(
            self.event_seq,
            'event_seq',
            1,
            GAZEBO_MONITOR_ROOM_MAX_EVENTS,
        )
        _identifier(self.event_type, 'event_type')
        _timestamp(self.recorded_at, 'recorded_at')
        _bounded_integer(self.fence_epoch, 'fence_epoch', 0, _MAX_FENCE)
        if self.lease_expires_at is not None:
            lease_expires_at = _timestamp(
                self.lease_expires_at, 'lease_expires_at'
            )
            if lease_expires_at <= self.recorded_at:
                raise GazeboMonitorRoomValidationError(
                    'event lease is invalid'
                )
        if (self.fence_epoch == 0) != (self.lease_expires_at is None):
            raise GazeboMonitorRoomValidationError(
                'event lease is invalid'
            )
        _identifier(self.worker_id, 'worker_id')
        if self.sample_index is None:
            if any(
                value is not None
                for value in (
                    self.goal_uuid,
                    self.from_sample_state,
                    self.to_sample_state,
                )
            ):
                raise GazeboMonitorRoomValidationError(
                    'event sample binding is invalid'
                )
        else:
            _bounded_integer(
                self.sample_index,
                'sample_index',
                0,
                GAZEBO_MONITOR_ROOM_MAX_SAMPLES - 1,
            )
            _goal_uuid(self.goal_uuid)
            if self.from_sample_state is not None and (
                self.from_sample_state not in SAMPLE_STATES
            ):
                raise GazeboMonitorRoomValidationError(
                    'event sample state is invalid'
                )
            if self.to_sample_state not in SAMPLE_STATES:
                raise GazeboMonitorRoomValidationError(
                    'event sample state is invalid'
                )
        if self.from_operation_state is not None and (
            self.from_operation_state not in OPERATION_STATES
        ):
            raise GazeboMonitorRoomValidationError(
                'event operation state is invalid'
            )
        if self.to_operation_state not in OPERATION_STATES:
            raise GazeboMonitorRoomValidationError(
                'event operation state is invalid'
            )
        if self.code is not None:
            _identifier(self.code, 'code')
        if self.evidence_digest is not None:
            _digest(self.evidence_digest, 'evidence_digest')
        _digest(self.event_digest, 'event_digest')


_OPERATION_STATES_SQL = """
        'prepared', 'preflighting', 'send_intent', 'navigating',
        'cancel_requested', 'succeeded', 'failed', 'canceled',
        'delivery_unknown', 'cancel_unknown'
""".strip()

_SAMPLE_STATES_SQL = """
        'pending', 'preflighting', 'send_intent', 'navigating',
        'cancel_requested', 'succeeded', 'failed', 'canceled',
        'delivery_unknown', 'cancel_unknown'
""".strip()

METADATA_TABLE_SQL = '''
CREATE TABLE gazebo_monitor_room_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    store_namespace TEXT NOT NULL UNIQUE CHECK (
        length(store_namespace) = 32
        AND store_namespace NOT GLOB '*[^0-9a-f]*'
    ),
    host_boot_id TEXT NOT NULL CHECK (
        length(host_boot_id) = 36
        AND host_boot_id GLOB
            '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    ),
    contract_digest TEXT NOT NULL CHECK (
        length(contract_digest) = 64
        AND contract_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''

OPERATIONS_TABLE_SQL = f'''
CREATE TABLE gazebo_monitor_room_operations (
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    operation_id TEXT NOT NULL PRIMARY KEY,
    prepare_request_id TEXT NOT NULL UNIQUE,
    prepare_fingerprint TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    runtime_mode TEXT NOT NULL DEFAULT 'gazebo'
        CHECK (runtime_mode = 'gazebo'),
    map_id TEXT NOT NULL,
    map_revision TEXT NOT NULL,
    semantic_revision TEXT NOT NULL,
    zones_digest TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    effects_digest TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    current_sample_index INTEGER NOT NULL,
    samples_reached INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({_OPERATION_STATES_SQL})),
    terminal_code TEXT,
    cancel_request_id TEXT,
    cancel_fingerprint TEXT,
    cancel_origin_state TEXT,
    lease_owner TEXT,
    lease_expires_at REAL,
    fence_epoch INTEGER NOT NULL,
    deadline REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL DEFAULT 0
        CHECK (physical_effects = 0),
    viewer_live INTEGER NOT NULL DEFAULT 0 CHECK (viewer_live = 0),
    camera_coverage_validated INTEGER NOT NULL DEFAULT 0
        CHECK (camera_coverage_validated = 0),
    coverage_achieved INTEGER NOT NULL DEFAULT 0
        CHECK (coverage_achieved = 0),
    record_digest TEXT NOT NULL,
    CHECK (
        length(prepare_fingerprint) = 64
        AND prepare_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(zones_digest) = 64
        AND zones_digest NOT GLOB '*[^0-9a-f]*'
        AND length(target_binding_digest) = 64
        AND target_binding_digest NOT GLOB '*[^0-9a-f]*'
        AND length(effects_digest) = 64
        AND effects_digest NOT GLOB '*[^0-9a-f]*'
        AND length(profile_digest) = 64
        AND profile_digest NOT GLOB '*[^0-9a-f]*'
        AND length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
        AND length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(sample_count) = 'integer'
        AND sample_count BETWEEN 1 AND {GAZEBO_MONITOR_ROOM_MAX_SAMPLES}
        AND typeof(current_sample_index) = 'integer'
        AND current_sample_index BETWEEN 0 AND sample_count - 1
        AND typeof(samples_reached) = 'integer'
        AND samples_reached BETWEEN 0 AND sample_count
        AND typeof(fence_epoch) = 'integer'
        AND fence_epoch BETWEEN 0 AND {_MAX_FENCE}
    ),
    CHECK (
        typeof(deadline) IN ('integer', 'real')
        AND deadline > created_at
        AND deadline <= 1.7976931348623157e308
        AND typeof(created_at) IN ('integer', 'real')
        AND created_at >= 0
        AND typeof(updated_at) IN ('integer', 'real')
        AND updated_at >= created_at
        AND updated_at <= 1.7976931348623157e308
    ),
    CHECK (
        (fence_epoch = 0 AND lease_owner IS NULL
         AND lease_expires_at IS NULL)
        OR
        (fence_epoch >= 1 AND lease_owner IS NOT NULL
         AND typeof(lease_expires_at) IN ('integer', 'real')
         AND lease_expires_at > created_at)
    ),
    CHECK (
        (state IN ('prepared', 'preflighting', 'send_intent',
                   'navigating', 'cancel_requested')
         AND terminal_code IS NULL)
        OR
        (state IN ('succeeded', 'failed', 'canceled',
                   'delivery_unknown', 'cancel_unknown')
         AND terminal_code IS NOT NULL)
    ),
    CHECK (
        (cancel_request_id IS NULL AND cancel_fingerprint IS NULL
         AND cancel_origin_state IS NULL
         AND state NOT IN ('cancel_requested', 'canceled',
                           'cancel_unknown'))
        OR
        (cancel_request_id IS NOT NULL
         AND cancel_fingerprint IS NOT NULL
         AND cancel_origin_state IN ('prepared', 'preflighting',
             'send_intent', 'navigating')
         AND state IN ('cancel_requested', 'canceled', 'cancel_unknown'))
    ),
    CHECK (
        (state = 'succeeded' AND samples_reached = sample_count
         AND terminal_code = 'all_navigation_samples_reached')
        OR state != 'succeeded'
    )
)
'''

SAMPLES_TABLE_SQL = f'''
CREATE TABLE gazebo_monitor_room_samples (
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    operation_id TEXT NOT NULL,
    sample_index INTEGER NOT NULL,
    polygon_ordinal INTEGER NOT NULL,
    row_ordinal INTEGER NOT NULL,
    x_mm INTEGER NOT NULL,
    y_mm INTEGER NOT NULL,
    frame_id TEXT NOT NULL CHECK (frame_id = 'map'),
    goal_uuid TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ({_SAMPLE_STATES_SQL})),
    preflight_digest TEXT,
    acceptance_digest TEXT,
    terminal_evidence_digest TEXT,
    send_intent_at REAL,
    accepted_at REAL,
    terminal_at REAL,
    result_code TEXT,
    updated_at REAL NOT NULL,
    record_digest TEXT NOT NULL,
    PRIMARY KEY (operation_id, sample_index),
    FOREIGN KEY (operation_id)
        REFERENCES gazebo_monitor_room_operations (operation_id)
        ON DELETE RESTRICT,
    CHECK (
        typeof(sample_index) = 'integer'
        AND sample_index BETWEEN 0
            AND {GAZEBO_MONITOR_ROOM_MAX_SAMPLES - 1}
        AND typeof(polygon_ordinal) = 'integer'
        AND polygon_ordinal BETWEEN 0 AND {_MAX_ORDINAL}
        AND typeof(row_ordinal) = 'integer'
        AND row_ordinal BETWEEN 0 AND {_MAX_ORDINAL}
        AND typeof(x_mm) = 'integer'
        AND x_mm BETWEEN {-_MAX_COORDINATE_MM}
            AND {_MAX_COORDINATE_MM}
        AND typeof(y_mm) = 'integer'
        AND y_mm BETWEEN {-_MAX_COORDINATE_MM}
            AND {_MAX_COORDINATE_MM}
    ),
    CHECK (
        length(goal_uuid) = 32
        AND goal_uuid NOT GLOB '*[^0-9a-f]*'
        AND length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        preflight_digest IS NULL
        OR (length(preflight_digest) = 64
            AND preflight_digest NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (
        acceptance_digest IS NULL
        OR (length(acceptance_digest) = 64
            AND acceptance_digest NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (
        terminal_evidence_digest IS NULL
        OR (length(terminal_evidence_digest) = 64
            AND terminal_evidence_digest NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (
        typeof(updated_at) IN ('integer', 'real')
        AND updated_at >= 0
        AND updated_at <= 1.7976931348623157e308
    ),
    CHECK (
        (state IN ('pending', 'preflighting')
         AND send_intent_at IS NULL AND accepted_at IS NULL
         AND terminal_at IS NULL AND result_code IS NULL)
        OR
        (state = 'send_intent' AND preflight_digest IS NOT NULL
         AND send_intent_at IS NOT NULL AND accepted_at IS NULL
         AND terminal_at IS NULL AND result_code IS NULL)
        OR
        (state = 'navigating' AND preflight_digest IS NOT NULL
         AND send_intent_at IS NOT NULL AND acceptance_digest IS NOT NULL
         AND accepted_at IS NOT NULL AND terminal_at IS NULL
         AND result_code IS NULL)
        OR
        (state = 'cancel_requested' AND terminal_at IS NULL
         AND result_code IS NULL)
        OR
        (state IN ('succeeded', 'failed', 'canceled',
                   'delivery_unknown', 'cancel_unknown')
         AND terminal_at IS NOT NULL AND result_code IS NOT NULL)
    )
)
'''

EVENTS_TABLE_SQL = '''
CREATE TABLE gazebo_monitor_room_events (
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    operation_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    fence_epoch INTEGER NOT NULL,
    lease_expires_at REAL,
    worker_id TEXT NOT NULL,
    sample_index INTEGER,
    goal_uuid TEXT,
    from_operation_state TEXT,
    to_operation_state TEXT NOT NULL,
    from_sample_state TEXT,
    to_sample_state TEXT,
    code TEXT,
    evidence_digest TEXT,
    operation_record_digest TEXT NOT NULL,
    sample_record_digest TEXT,
    previous_event_digest TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    PRIMARY KEY (operation_id, event_seq),
    FOREIGN KEY (operation_id)
        REFERENCES gazebo_monitor_room_operations (operation_id)
        ON DELETE RESTRICT,
    CHECK (typeof(event_seq) = 'integer' AND event_seq >= 1),
    CHECK (typeof(fence_epoch) = 'integer' AND fence_epoch >= 0),
    CHECK (
        (fence_epoch = 0 AND lease_expires_at IS NULL)
        OR
        (fence_epoch >= 1
         AND typeof(lease_expires_at) IN ('integer', 'real')
         AND lease_expires_at > recorded_at
         AND lease_expires_at <= 1.7976931348623157e308)
    ),
    CHECK (
        typeof(recorded_at) IN ('integer', 'real')
        AND recorded_at >= 0
        AND recorded_at <= 1.7976931348623157e308
    ),
    CHECK (
        length(operation_record_digest) = 64
        AND operation_record_digest NOT GLOB '*[^0-9a-f]*'
        AND length(previous_event_digest) = 64
        AND previous_event_digest NOT GLOB '*[^0-9a-f]*'
        AND length(event_digest) = 64
        AND event_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        evidence_digest IS NULL
        OR (length(evidence_digest) = 64
            AND evidence_digest NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (
        (sample_index IS NULL AND goal_uuid IS NULL
         AND from_sample_state IS NULL AND to_sample_state IS NULL
         AND sample_record_digest IS NULL)
        OR
        (typeof(sample_index) = 'integer' AND sample_index >= 0
         AND goal_uuid IS NOT NULL
         AND to_sample_state IS NOT NULL
         AND sample_record_digest IS NOT NULL
         AND length(goal_uuid) = 32
         AND goal_uuid NOT GLOB '*[^0-9a-f]*'
         AND length(sample_record_digest) = 64
         AND sample_record_digest NOT GLOB '*[^0-9a-f]*')
    )
)
'''

DISPATCH_CLAIMS_TABLE_SQL = f'''
CREATE TABLE gazebo_monitor_room_dispatch_claims (
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    store_namespace TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('start', 'cancel')),
    operation_id TEXT NOT NULL,
    sample_index INTEGER NOT NULL,
    goal_uuid TEXT NOT NULL,
    start_fingerprint TEXT,
    cancel_request_id TEXT,
    cancel_request_fingerprint TEXT,
    worker_id TEXT NOT NULL,
    fence_epoch INTEGER NOT NULL,
    binding_digest TEXT NOT NULL,
    preflight_digest TEXT,
    wire_payload_digest TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    claimed_at REAL NOT NULL,
    record_digest TEXT NOT NULL,
    PRIMARY KEY (
        store_namespace, phase, operation_id, sample_index, goal_uuid
    ),
    FOREIGN KEY (store_namespace)
        REFERENCES gazebo_monitor_room_schema_metadata (store_namespace)
        ON DELETE RESTRICT,
    FOREIGN KEY (operation_id, sample_index)
        REFERENCES gazebo_monitor_room_samples (operation_id, sample_index)
        ON DELETE RESTRICT,
    FOREIGN KEY (goal_uuid)
        REFERENCES gazebo_monitor_room_samples (goal_uuid)
        ON DELETE RESTRICT,
    CHECK (
        length(store_namespace) = 32
        AND store_namespace NOT GLOB '*[^0-9a-f]*'
        AND typeof(sample_index) = 'integer'
        AND sample_index BETWEEN 0
            AND {GAZEBO_MONITOR_ROOM_MAX_SAMPLES - 1}
        AND length(goal_uuid) = 32
        AND goal_uuid NOT GLOB '*[^0-9a-f]*'
        AND typeof(fence_epoch) = 'integer'
        AND fence_epoch BETWEEN 1 AND {_MAX_FENCE}
    ),
    CHECK (
        length(binding_digest) = 64
        AND binding_digest NOT GLOB '*[^0-9a-f]*'
        AND length(wire_payload_digest) = 64
        AND wire_payload_digest NOT GLOB '*[^0-9a-f]*'
        AND length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(claimed_at) IN ('integer', 'real')
        AND claimed_at >= 0
        AND claimed_at <= 1.7976931348623157e308
        AND typeof(lease_expires_at) IN ('integer', 'real')
        AND lease_expires_at > claimed_at
        AND lease_expires_at <= 1.7976931348623157e308
    ),
    CHECK (
        (phase = 'start'
         AND start_fingerprint IS NOT NULL
         AND length(start_fingerprint) = 64
         AND start_fingerprint NOT GLOB '*[^0-9a-f]*'
         AND preflight_digest IS NOT NULL
         AND length(preflight_digest) = 64
         AND preflight_digest NOT GLOB '*[^0-9a-f]*'
         AND cancel_request_id IS NULL
         AND cancel_request_fingerprint IS NULL)
        OR
        (phase = 'cancel'
         AND start_fingerprint IS NULL
         AND preflight_digest IS NULL
         AND cancel_request_id IS NOT NULL
         AND cancel_request_fingerprint IS NOT NULL
         AND length(cancel_request_fingerprint) = 64
         AND cancel_request_fingerprint NOT GLOB '*[^0-9a-f]*')
    )
)
'''

ONE_BLOCKING_OPERATION_INDEX_SQL = '''
CREATE UNIQUE INDEX gazebo_monitor_room_one_blocking_robot_idx
ON gazebo_monitor_room_operations (robot_id)
WHERE state IN (
    'prepared', 'preflighting', 'send_intent', 'navigating',
    'cancel_requested', 'delivery_unknown', 'cancel_unknown'
)
'''

CANCEL_REQUEST_INDEX_SQL = '''
CREATE UNIQUE INDEX gazebo_monitor_room_cancel_request_idx
ON gazebo_monitor_room_operations (cancel_request_id)
WHERE cancel_request_id IS NOT NULL
'''

METADATA_IMMUTABLE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_metadata_immutable
BEFORE UPDATE ON gazebo_monitor_room_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room metadata is immutable');
END
'''

METADATA_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_metadata_no_delete
BEFORE DELETE ON gazebo_monitor_room_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room metadata is immutable');
END
'''

METADATA_NO_REPLACE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_metadata_no_replace
BEFORE INSERT ON gazebo_monitor_room_schema_metadata
WHEN EXISTS (
    SELECT 1 FROM gazebo_monitor_room_schema_metadata
    WHERE singleton = NEW.singleton
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room metadata is immutable');
END
'''

OPERATION_IDENTITY_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_operation_identity
BEFORE UPDATE OF
    schema_version, operation_id, prepare_request_id,
    prepare_fingerprint, robot_id, runtime_mode, map_id, map_revision,
    semantic_revision, zones_digest, target_binding_digest,
    effects_digest, profile_digest, plan_digest, sample_count,
    deadline, created_at, simulation, physical_authorized,
    physical_effects, viewer_live, camera_coverage_validated,
    coverage_achieved
ON gazebo_monitor_room_operations
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room identity is immutable');
END
'''

OPERATION_TRANSITION_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_operation_transition
BEFORE UPDATE ON gazebo_monitor_room_operations
WHEN NOT (
    NEW.updated_at >= OLD.updated_at
    AND NEW.fence_epoch BETWEEN OLD.fence_epoch AND OLD.fence_epoch + 1
    AND (
        (NEW.state = OLD.state
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached
         AND NEW.terminal_code IS OLD.terminal_code
         AND NEW.cancel_request_id IS OLD.cancel_request_id
         AND NEW.cancel_fingerprint IS OLD.cancel_fingerprint
         AND NEW.cancel_origin_state IS OLD.cancel_origin_state)
        OR
        (OLD.state = 'prepared' AND NEW.state = 'preflighting'
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached)
        OR
        (OLD.state = 'preflighting' AND NEW.state = 'send_intent'
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached)
        OR
        (OLD.state = 'send_intent' AND NEW.state = 'navigating'
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached)
        OR
        (OLD.state = 'navigating' AND NEW.state = 'preflighting'
         AND NEW.current_sample_index = OLD.current_sample_index + 1
         AND NEW.samples_reached = OLD.samples_reached + 1)
        OR
        (OLD.state = 'navigating' AND NEW.state = 'succeeded'
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached + 1)
        OR
        (OLD.state IN ('prepared', 'preflighting', 'send_intent',
                       'navigating')
         AND NEW.state = 'cancel_requested'
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached)
        OR
        (OLD.state = 'cancel_requested'
         AND NEW.state IN ('canceled', 'cancel_unknown')
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached)
        OR
        (OLD.state IN ('send_intent', 'navigating')
         AND NEW.state = 'delivery_unknown'
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached)
        OR
        (OLD.state IN ('prepared', 'preflighting', 'send_intent',
                       'navigating')
         AND NEW.state = 'failed'
         AND NEW.current_sample_index = OLD.current_sample_index
         AND NEW.samples_reached = OLD.samples_reached)
        OR
        (OLD.state = 'navigating' AND NEW.state = 'failed'
         AND NEW.current_sample_index = OLD.current_sample_index + 1
         AND NEW.samples_reached = OLD.samples_reached + 1)
    )
    AND (
        (NEW.state = OLD.state
         AND (
             (NEW.fence_epoch = OLD.fence_epoch
              AND NEW.lease_owner IS OLD.lease_owner
              AND NEW.lease_expires_at >= OLD.lease_expires_at)
             OR
             (NEW.fence_epoch = OLD.fence_epoch + 1
              AND NEW.lease_owner IS NOT NULL
              AND NEW.lease_expires_at > NEW.updated_at)
         ))
        OR
        (NEW.state != OLD.state
         AND NEW.fence_epoch = OLD.fence_epoch
         AND NEW.lease_owner = OLD.lease_owner
         AND NEW.lease_expires_at = OLD.lease_expires_at)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room transition is invalid');
END
'''

OPERATION_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_operation_no_delete
BEFORE DELETE ON gazebo_monitor_room_operations
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room operation is durable');
END
'''

OPERATION_NO_REPLACE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_operation_no_replace
BEFORE INSERT ON gazebo_monitor_room_operations
WHEN EXISTS (
    SELECT 1 FROM gazebo_monitor_room_operations
    WHERE operation_id = NEW.operation_id
       OR prepare_request_id = NEW.prepare_request_id
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room identity is immutable');
END
'''

SAMPLE_IDENTITY_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_sample_identity
BEFORE UPDATE OF
    schema_version, operation_id, sample_index, polygon_ordinal,
    row_ordinal, x_mm, y_mm, frame_id, goal_uuid
ON gazebo_monitor_room_samples
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room sample is immutable');
END
'''

SAMPLE_TRANSITION_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_sample_transition
BEFORE UPDATE ON gazebo_monitor_room_samples
WHEN NOT (
    NEW.updated_at >= OLD.updated_at
    AND (
        (OLD.state = 'pending' AND NEW.state IN (
            'preflighting', 'cancel_requested', 'failed'))
        OR
        (OLD.state = 'preflighting' AND NEW.state IN (
            'send_intent', 'cancel_requested', 'failed'))
        OR
        (OLD.state = 'send_intent' AND NEW.state IN (
            'navigating', 'cancel_requested', 'delivery_unknown',
            'failed'))
        OR
        (OLD.state = 'navigating' AND NEW.state IN (
            'succeeded', 'cancel_requested', 'delivery_unknown',
            'failed'))
        OR
        (OLD.state = 'cancel_requested' AND NEW.state IN (
            'canceled', 'cancel_unknown'))
    )
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room sample transition is invalid');
END
'''

SAMPLE_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_sample_no_delete
BEFORE DELETE ON gazebo_monitor_room_samples
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room sample is durable');
END
'''

SAMPLE_NO_REPLACE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_sample_no_replace
BEFORE INSERT ON gazebo_monitor_room_samples
WHEN EXISTS (
    SELECT 1 FROM gazebo_monitor_room_samples
    WHERE operation_id = NEW.operation_id
      AND sample_index = NEW.sample_index
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room sample is immutable');
END
'''

EVENT_APPEND_TRIGGER_SQL = f'''
CREATE TRIGGER gazebo_monitor_room_event_append
BEFORE INSERT ON gazebo_monitor_room_events
WHEN NOT (
    NEW.event_seq BETWEEN 1 AND {GAZEBO_MONITOR_ROOM_MAX_EVENTS}
    AND (
        (NEW.event_seq = 1
         AND NEW.previous_event_digest = '{_ZERO_DIGEST}'
         AND NOT EXISTS (
             SELECT 1 FROM gazebo_monitor_room_events
             WHERE operation_id = NEW.operation_id
         ))
        OR
        (NEW.event_seq > 1
         AND EXISTS (
             SELECT 1 FROM gazebo_monitor_room_events AS prior
             WHERE prior.operation_id = NEW.operation_id
               AND prior.event_seq = NEW.event_seq - 1
               AND prior.event_digest = NEW.previous_event_digest
               AND prior.recorded_at <= NEW.recorded_at
         )
         AND NOT EXISTS (
             SELECT 1 FROM gazebo_monitor_room_events AS later
             WHERE later.operation_id = NEW.operation_id
               AND later.event_seq >= NEW.event_seq
         ))
    )
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room event is not append-only');
END
'''

EVENT_NO_UPDATE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_event_no_update
BEFORE UPDATE ON gazebo_monitor_room_events
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room event is append-only');
END
'''

EVENT_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_event_no_delete
BEFORE DELETE ON gazebo_monitor_room_events
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room event is append-only');
END
'''

EVENT_NO_REPLACE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_event_no_replace
BEFORE INSERT ON gazebo_monitor_room_events
WHEN EXISTS (
    SELECT 1 FROM gazebo_monitor_room_events
    WHERE operation_id = NEW.operation_id
      AND event_seq = NEW.event_seq
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room event is append-only');
END
'''

DISPATCH_CLAIM_NO_UPDATE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_dispatch_claim_no_update
BEFORE UPDATE ON gazebo_monitor_room_dispatch_claims
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room dispatch claim is immutable');
END
'''

DISPATCH_CLAIM_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_dispatch_claim_no_delete
BEFORE DELETE ON gazebo_monitor_room_dispatch_claims
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room dispatch claim is durable');
END
'''

DISPATCH_CLAIM_NO_REPLACE_TRIGGER_SQL = '''
CREATE TRIGGER gazebo_monitor_room_dispatch_claim_no_replace
BEFORE INSERT ON gazebo_monitor_room_dispatch_claims
WHEN EXISTS (
    SELECT 1 FROM gazebo_monitor_room_dispatch_claims
    WHERE store_namespace = NEW.store_namespace
      AND phase = NEW.phase
      AND operation_id = NEW.operation_id
      AND sample_index = NEW.sample_index
      AND goal_uuid = NEW.goal_uuid
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo monitor-room dispatch claim is immutable');
END
'''


def _expected_schema() -> Dict[str, Tuple[str, str]]:
    """Return the exact owned SQLite object contract."""
    return {
        'gazebo_monitor_room_schema_metadata': (
            'table', METADATA_TABLE_SQL
        ),
        'gazebo_monitor_room_operations': ('table', OPERATIONS_TABLE_SQL),
        'gazebo_monitor_room_samples': ('table', SAMPLES_TABLE_SQL),
        'gazebo_monitor_room_events': ('table', EVENTS_TABLE_SQL),
        'gazebo_monitor_room_dispatch_claims': (
            'table', DISPATCH_CLAIMS_TABLE_SQL
        ),
        'gazebo_monitor_room_one_blocking_robot_idx': (
            'index', ONE_BLOCKING_OPERATION_INDEX_SQL
        ),
        'gazebo_monitor_room_cancel_request_idx': (
            'index', CANCEL_REQUEST_INDEX_SQL
        ),
        'gazebo_monitor_room_metadata_immutable': (
            'trigger', METADATA_IMMUTABLE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_metadata_no_delete': (
            'trigger', METADATA_NO_DELETE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_metadata_no_replace': (
            'trigger', METADATA_NO_REPLACE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_operation_identity': (
            'trigger', OPERATION_IDENTITY_TRIGGER_SQL
        ),
        'gazebo_monitor_room_operation_transition': (
            'trigger', OPERATION_TRANSITION_TRIGGER_SQL
        ),
        'gazebo_monitor_room_operation_no_delete': (
            'trigger', OPERATION_NO_DELETE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_operation_no_replace': (
            'trigger', OPERATION_NO_REPLACE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_sample_identity': (
            'trigger', SAMPLE_IDENTITY_TRIGGER_SQL
        ),
        'gazebo_monitor_room_sample_transition': (
            'trigger', SAMPLE_TRANSITION_TRIGGER_SQL
        ),
        'gazebo_monitor_room_sample_no_delete': (
            'trigger', SAMPLE_NO_DELETE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_sample_no_replace': (
            'trigger', SAMPLE_NO_REPLACE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_event_append': (
            'trigger', EVENT_APPEND_TRIGGER_SQL
        ),
        'gazebo_monitor_room_event_no_update': (
            'trigger', EVENT_NO_UPDATE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_event_no_delete': (
            'trigger', EVENT_NO_DELETE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_event_no_replace': (
            'trigger', EVENT_NO_REPLACE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_dispatch_claim_no_update': (
            'trigger', DISPATCH_CLAIM_NO_UPDATE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_dispatch_claim_no_delete': (
            'trigger', DISPATCH_CLAIM_NO_DELETE_TRIGGER_SQL
        ),
        'gazebo_monitor_room_dispatch_claim_no_replace': (
            'trigger', DISPATCH_CLAIM_NO_REPLACE_TRIGGER_SQL
        ),
    }


def _schema_contract_digest() -> str:
    return _hash_json(
        {
            name: {'type': object_type, 'sql': sql.strip()}
            for name, (object_type, sql) in sorted(
                _expected_schema().items()
            )
        }
    )


_OPERATION_DIGEST_FIELDS = (
    'schema_version',
    'operation_id',
    'prepare_request_id',
    'prepare_fingerprint',
    'robot_id',
    'runtime_mode',
    'map_id',
    'map_revision',
    'semantic_revision',
    'zones_digest',
    'target_binding_digest',
    'effects_digest',
    'profile_digest',
    'plan_digest',
    'sample_count',
    'current_sample_index',
    'samples_reached',
    'state',
    'terminal_code',
    'cancel_request_id',
    'cancel_fingerprint',
    'cancel_origin_state',
    'lease_owner',
    'lease_expires_at',
    'fence_epoch',
    'deadline',
    'created_at',
    'updated_at',
    'simulation',
    'physical_authorized',
    'physical_effects',
    'viewer_live',
    'camera_coverage_validated',
    'coverage_achieved',
)

_SAMPLE_DIGEST_FIELDS = (
    'schema_version',
    'operation_id',
    'sample_index',
    'polygon_ordinal',
    'row_ordinal',
    'x_mm',
    'y_mm',
    'frame_id',
    'goal_uuid',
    'state',
    'preflight_digest',
    'acceptance_digest',
    'terminal_evidence_digest',
    'send_intent_at',
    'accepted_at',
    'terminal_at',
    'result_code',
    'updated_at',
)

_EVENT_DIGEST_FIELDS = (
    'schema_version',
    'operation_id',
    'event_seq',
    'event_type',
    'recorded_at',
    'fence_epoch',
    'lease_expires_at',
    'worker_id',
    'sample_index',
    'goal_uuid',
    'from_operation_state',
    'to_operation_state',
    'from_sample_state',
    'to_sample_state',
    'code',
    'evidence_digest',
    'operation_record_digest',
    'sample_record_digest',
    'previous_event_digest',
)

_DISPATCH_CLAIM_DIGEST_FIELDS = (
    'schema_version',
    'store_namespace',
    'phase',
    'operation_id',
    'sample_index',
    'goal_uuid',
    'start_fingerprint',
    'cancel_request_id',
    'cancel_request_fingerprint',
    'worker_id',
    'fence_epoch',
    'binding_digest',
    'preflight_digest',
    'wire_payload_digest',
    'lease_expires_at',
    'claimed_at',
)


def _row_digest(
    row: Mapping[str, Any], fields: Tuple[str, ...]
) -> str:
    return _hash_json({name: row[name] for name in fields})


def _operation_digest(row: Mapping[str, Any]) -> str:
    return _row_digest(row, _OPERATION_DIGEST_FIELDS)


def _sample_digest(row: Mapping[str, Any]) -> str:
    return _row_digest(row, _SAMPLE_DIGEST_FIELDS)


def _event_digest(row: Mapping[str, Any]) -> str:
    return _row_digest(row, _EVENT_DIGEST_FIELDS)


def _dispatch_claim_digest(row: Mapping[str, Any]) -> str:
    return _row_digest(row, _DISPATCH_CLAIM_DIGEST_FIELDS)


def _validate_exact_schema_locked(
    connection: sqlite3.Connection,
    *,
    expected_boot_id: Optional[str] = None,
    expected_store_namespace: Optional[str] = None,
) -> str:
    expected = _expected_schema()
    actual_owned = {
        (row['type'], row['name'])
        for row in connection.execute(
            '''
            SELECT type, name FROM sqlite_master
            WHERE name LIKE 'gazebo_monitor_room_%'
              AND type IN ('table', 'index', 'trigger')
              AND sql IS NOT NULL
            '''
        ).fetchall()
    }
    expected_owned = {
        (object_type, name)
        for name, (object_type, _sql) in expected.items()
    }
    if actual_owned != expected_owned:
        raise GazeboMonitorRoomSchemaError(
            'Gazebo monitor-room schema has unexpected objects'
        )
    for name, (object_type, expected_sql) in expected.items():
        row = connection.execute(
            'SELECT type, sql FROM sqlite_master WHERE name = ?',
            (name,),
        ).fetchone()
        if (
            row is None
            or row['type'] != object_type
            or str(row['sql']).strip() != expected_sql.strip()
        ):
            raise GazeboMonitorRoomSchemaError(
                'Gazebo monitor-room schema is incompatible'
            )
    owned_tables = {
        'gazebo_monitor_room_schema_metadata',
        'gazebo_monitor_room_operations',
        'gazebo_monitor_room_samples',
        'gazebo_monitor_room_events',
        'gazebo_monitor_room_dispatch_claims',
    }
    placeholders = ', '.join('?' for _name in owned_tables)
    custom = {
        (row['type'], row['name'])
        for row in connection.execute(
            'SELECT type, name FROM sqlite_master '
            "WHERE type IN ('index', 'trigger') "
            f'AND tbl_name IN ({placeholders}) AND sql IS NOT NULL',
            tuple(sorted(owned_tables)),
        ).fetchall()
    }
    expected_custom = {
        (object_type, name)
        for name, (object_type, _sql) in expected.items()
        if object_type in ('index', 'trigger')
    }
    if custom != expected_custom:
        raise GazeboMonitorRoomSchemaError(
            'Gazebo monitor-room schema has unexpected objects'
        )
    metadata = connection.execute(
        'SELECT * FROM gazebo_monitor_room_schema_metadata'
    ).fetchall()
    if (
        len(metadata) != 1
        or metadata[0]['singleton'] != 1
        or metadata[0]['schema_version']
        != GAZEBO_MONITOR_ROOM_SCHEMA_VERSION
        or metadata[0]['contract_digest'] != _schema_contract_digest()
    ):
        raise GazeboMonitorRoomSchemaError(
            'Gazebo monitor-room metadata is incompatible'
        )
    try:
        namespace = _store_namespace(metadata[0]['store_namespace'])
        boot_id = _host_boot_id(metadata[0]['host_boot_id'])
    except (
        KeyError,
        TypeError,
        GazeboMonitorRoomValidationError,
        GazeboMonitorRoomBootIdentityError,
    ):
        raise GazeboMonitorRoomSchemaError(
            'Gazebo monitor-room metadata is incompatible'
        ) from None
    if expected_boot_id is not None and boot_id != expected_boot_id:
        raise GazeboMonitorRoomBootIdentityError(
            'host boot identity does not match durable state'
        )
    if (
        expected_store_namespace is not None
        and namespace != expected_store_namespace
    ):
        raise GazeboMonitorRoomDurabilityError(
            'store namespace changed during open'
        )
    foreign_key_errors = connection.execute(
        'PRAGMA foreign_key_check'
    ).fetchall()
    if foreign_key_errors:
        raise GazeboMonitorRoomSchemaError(
            'Gazebo monitor-room foreign keys are incompatible'
        )
    return namespace


def _create_schema_locked(
    connection: sqlite3.Connection,
    *,
    store_namespace: str,
    host_boot_id: str,
) -> None:
    namespace = _store_namespace(store_namespace)
    boot_id = _host_boot_id(host_boot_id)
    for name, (object_type, sql) in _expected_schema().items():
        if object_type == 'table':
            connection.execute(sql)
    connection.execute(
        '''
        INSERT INTO gazebo_monitor_room_schema_metadata (
            singleton, schema_version, store_namespace, host_boot_id,
            contract_digest
        ) VALUES (1, ?, ?, ?, ?)
        ''',
        (
            GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
            namespace,
            boot_id,
            _schema_contract_digest(),
        ),
    )
    for name, (object_type, sql) in _expected_schema().items():
        if object_type in ('index', 'trigger'):
            connection.execute(sql)


def _operation_values(request: PrepareOperation, now: float) -> Dict[str, Any]:
    values: Dict[str, Any] = {
        'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
        'operation_id': request.operation_id,
        'prepare_request_id': request.prepare_request_id,
        'prepare_fingerprint': request.payload_fingerprint,
        'robot_id': request.robot_id,
        'runtime_mode': 'gazebo',
        'map_id': request.map_id,
        'map_revision': request.map_revision,
        'semantic_revision': request.semantic_revision,
        'zones_digest': request.zones_digest,
        'target_binding_digest': request.target_binding_digest,
        'effects_digest': request.effects_digest,
        'profile_digest': request.profile_digest,
        'plan_digest': request.plan_digest,
        'sample_count': len(request.ordered_semantic_samples),
        'current_sample_index': 0,
        'samples_reached': 0,
        'state': 'prepared',
        'terminal_code': None,
        'cancel_request_id': None,
        'cancel_fingerprint': None,
        'cancel_origin_state': None,
        'lease_owner': None,
        'lease_expires_at': None,
        'fence_epoch': 0,
        'deadline': request.deadline,
        'created_at': now,
        'updated_at': now,
        'simulation': 1,
        'physical_authorized': 0,
        'physical_effects': 0,
        'viewer_live': 0,
        'camera_coverage_validated': 0,
        'coverage_achieved': 0,
    }
    values['record_digest'] = _operation_digest(values)
    return values


def _sample_values(
    store_namespace: str,
    operation_id: str,
    sample: OrderedSemanticSample,
    now: float,
) -> Dict[str, Any]:
    values: Dict[str, Any] = {
        'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
        'operation_id': operation_id,
        'sample_index': sample.index,
        'polygon_ordinal': sample.polygon_ordinal,
        'row_ordinal': sample.row_ordinal,
        'x_mm': sample.x_mm,
        'y_mm': sample.y_mm,
        'frame_id': sample.frame_id,
        'goal_uuid': stable_goal_uuid(
            store_namespace, operation_id, sample.index
        ),
        'state': 'pending',
        'preflight_digest': None,
        'acceptance_digest': None,
        'terminal_evidence_digest': None,
        'send_intent_at': None,
        'accepted_at': None,
        'terminal_at': None,
        'result_code': None,
        'updated_at': now,
    }
    values['record_digest'] = _sample_digest(values)
    return values


def _validate_operation_row(row: sqlite3.Row) -> None:
    try:
        if (
            type(row['schema_version']) is not int
            or row['schema_version'] != GAZEBO_MONITOR_ROOM_SCHEMA_VERSION
            or row['runtime_mode'] != 'gazebo'
            or row['state'] not in OPERATION_STATES
            or any(
                row[name] != expected
                for name, expected in (
                    ('simulation', 1),
                    ('physical_authorized', 0),
                    ('physical_effects', 0),
                    ('viewer_live', 0),
                    ('camera_coverage_validated', 0),
                    ('coverage_achieved', 0),
                )
            )
        ):
            raise GazeboMonitorRoomValidationError('operation row invalid')
        for name in (
            'operation_id',
            'prepare_request_id',
            'robot_id',
            'map_id',
            'map_revision',
            'semantic_revision',
        ):
            _identifier(row[name], name)
        for name in (
            'prepare_fingerprint',
            'zones_digest',
            'target_binding_digest',
            'effects_digest',
            'profile_digest',
            'plan_digest',
            'record_digest',
        ):
            _digest(row[name], name)
        sample_count = _bounded_integer(
            row['sample_count'],
            'sample_count',
            1,
            GAZEBO_MONITOR_ROOM_MAX_SAMPLES,
        )
        _bounded_integer(
            row['current_sample_index'],
            'current_sample_index',
            0,
            sample_count - 1,
        )
        _bounded_integer(
            row['samples_reached'],
            'samples_reached',
            0,
            sample_count,
        )
        _bounded_integer(row['fence_epoch'], 'fence_epoch', 0, _MAX_FENCE)
        deadline = _timestamp(row['deadline'], 'deadline')
        created_at = _timestamp(row['created_at'], 'created_at')
        updated_at = _timestamp(row['updated_at'], 'updated_at')
        if deadline <= created_at or updated_at < created_at:
            raise GazeboMonitorRoomValidationError('operation time invalid')
        if row['fence_epoch'] == 0:
            if (
                row['lease_owner'] is not None
                or row['lease_expires_at'] is not None
            ):
                raise GazeboMonitorRoomValidationError('lease invalid')
        else:
            _identifier(row['lease_owner'], 'lease_owner')
            _timestamp(row['lease_expires_at'], 'lease_expires_at')
        if row['terminal_code'] is not None:
            _identifier(row['terminal_code'], 'terminal_code')
        if row['cancel_request_id'] is not None:
            _identifier(row['cancel_request_id'], 'cancel_request_id')
            _digest(row['cancel_fingerprint'], 'cancel_fingerprint')
        if row['record_digest'] != _operation_digest(row):
            raise GazeboMonitorRoomValidationError(
                'operation digest invalid'
            )
    except (KeyError, TypeError, GazeboMonitorRoomValidationError):
        raise GazeboMonitorRoomSchemaError(
            'stored Gazebo monitor-room operation is invalid'
        ) from None


def _validate_sample_row(
    row: sqlite3.Row,
    operation: sqlite3.Row,
    store_namespace: str,
) -> None:
    try:
        if (
            type(row['schema_version']) is not int
            or row['schema_version'] != GAZEBO_MONITOR_ROOM_SCHEMA_VERSION
            or row['operation_id'] != operation['operation_id']
            or row['state'] not in SAMPLE_STATES
            or row['frame_id'] != 'map'
        ):
            raise GazeboMonitorRoomValidationError('sample row invalid')
        index = _bounded_integer(
            row['sample_index'],
            'sample_index',
            0,
            operation['sample_count'] - 1,
        )
        for name in ('polygon_ordinal', 'row_ordinal'):
            _bounded_integer(row[name], name, 0, _MAX_ORDINAL)
        for name in ('x_mm', 'y_mm'):
            _bounded_integer(
                row[name], name, -_MAX_COORDINATE_MM, _MAX_COORDINATE_MM
            )
        if row['goal_uuid'] != stable_goal_uuid(
            store_namespace, operation['operation_id'], index
        ):
            raise GazeboMonitorRoomValidationError('goal UUID invalid')
        for name in (
            'preflight_digest',
            'acceptance_digest',
            'terminal_evidence_digest',
        ):
            if row[name] is not None:
                _digest(row[name], name)
        for name in (
            'send_intent_at', 'accepted_at', 'terminal_at', 'updated_at'
        ):
            if row[name] is not None:
                _timestamp(row[name], name)
        if row['result_code'] is not None:
            _identifier(row['result_code'], 'result_code')
        _digest(row['record_digest'], 'record_digest')
        if row['record_digest'] != _sample_digest(row):
            raise GazeboMonitorRoomValidationError('sample digest invalid')
    except (KeyError, TypeError, GazeboMonitorRoomValidationError):
        raise GazeboMonitorRoomSchemaError(
            'stored Gazebo monitor-room sample is invalid'
        ) from None


def _validate_prepare_binding(
    operation: sqlite3.Row,
    samples: Tuple[sqlite3.Row, ...] | list[sqlite3.Row],
) -> None:
    """Recompute the coordinate-bearing prepare fingerprint from storage."""
    try:
        reconstructed_samples = tuple(
            OrderedSemanticSample(
                index=sample['sample_index'],
                polygon_ordinal=sample['polygon_ordinal'],
                row_ordinal=sample['row_ordinal'],
                x_mm=sample['x_mm'],
                y_mm=sample['y_mm'],
                frame_id=sample['frame_id'],
            )
            for sample in samples
        )
        reconstructed = PrepareOperation(
            prepare_request_id=operation['prepare_request_id'],
            operation_id=operation['operation_id'],
            robot_id=operation['robot_id'],
            map_id=operation['map_id'],
            map_revision=operation['map_revision'],
            semantic_revision=operation['semantic_revision'],
            zones_digest=operation['zones_digest'],
            target_binding_digest=operation['target_binding_digest'],
            effects_digest=operation['effects_digest'],
            profile_digest=operation['profile_digest'],
            plan_digest=operation['plan_digest'],
            ordered_semantic_samples=reconstructed_samples,
            deadline=operation['deadline'],
        )
    except (
        KeyError,
        TypeError,
        GazeboMonitorRoomValidationError,
    ):
        raise GazeboMonitorRoomSchemaError(
            'stored Gazebo prepare binding is invalid'
        ) from None
    if reconstructed.payload_fingerprint != operation['prepare_fingerprint']:
        raise GazeboMonitorRoomSchemaError(
            'stored Gazebo prepare binding is invalid'
        )


def _expected_current_sample_state(operation_state: str) -> str:
    return {
        'prepared': 'pending',
        'preflighting': 'preflighting',
        'send_intent': 'send_intent',
        'navigating': 'navigating',
        'cancel_requested': 'cancel_requested',
        'succeeded': 'succeeded',
        'failed': 'failed',
        'canceled': 'canceled',
        'delivery_unknown': 'delivery_unknown',
        'cancel_unknown': 'cancel_unknown',
    }[operation_state]


def _validate_event_row(
    row: sqlite3.Row,
    operation: sqlite3.Row,
    previous_digest: str,
    previous_time: float,
) -> None:
    try:
        if (
            type(row['schema_version']) is not int
            or row['schema_version'] != GAZEBO_MONITOR_ROOM_SCHEMA_VERSION
            or row['operation_id'] != operation['operation_id']
            or type(row['event_seq']) is not int
            or not 1 <= row['event_seq'] <= GAZEBO_MONITOR_ROOM_MAX_EVENTS
            or row['previous_event_digest'] != previous_digest
            or _timestamp(row['recorded_at'], 'recorded_at') < previous_time
            or type(row['fence_epoch']) is not int
            or not 0 <= row['fence_epoch'] <= operation['fence_epoch']
        ):
            raise GazeboMonitorRoomValidationError('event row invalid')
        recorded_at = _timestamp(row['recorded_at'], 'recorded_at')
        if row['fence_epoch'] == 0:
            if row['lease_expires_at'] is not None:
                raise GazeboMonitorRoomValidationError(
                    'event lease is invalid'
                )
        elif (
            row['lease_expires_at'] is None
            or _timestamp(
                row['lease_expires_at'], 'lease_expires_at'
            ) <= recorded_at
        ):
            raise GazeboMonitorRoomValidationError(
                'event lease is invalid'
            )
        _identifier(row['event_type'], 'event_type')
        _identifier(row['worker_id'], 'worker_id')
        if row['from_operation_state'] is not None and (
            row['from_operation_state'] not in OPERATION_STATES
        ):
            raise GazeboMonitorRoomValidationError('event state invalid')
        if row['to_operation_state'] not in OPERATION_STATES:
            raise GazeboMonitorRoomValidationError('event state invalid')
        if row['sample_index'] is None:
            if any(
                row[name] is not None
                for name in (
                    'goal_uuid',
                    'from_sample_state',
                    'to_sample_state',
                    'sample_record_digest',
                )
            ):
                raise GazeboMonitorRoomValidationError('event sample invalid')
        else:
            _bounded_integer(
                row['sample_index'],
                'sample_index',
                0,
                operation['sample_count'] - 1,
            )
            _goal_uuid(row['goal_uuid'])
            if row['from_sample_state'] is not None and (
                row['from_sample_state'] not in SAMPLE_STATES
            ):
                raise GazeboMonitorRoomValidationError(
                    'event sample state invalid'
                )
            if row['to_sample_state'] not in SAMPLE_STATES:
                raise GazeboMonitorRoomValidationError(
                    'event sample state invalid'
                )
            _digest(row['sample_record_digest'], 'sample_record_digest')
        for name in ('code',):
            if row[name] is not None:
                _identifier(row[name], name)
        for name in (
            'evidence_digest',
            'operation_record_digest',
            'previous_event_digest',
            'event_digest',
        ):
            if row[name] is not None:
                _digest(row[name], name)
        if row['event_digest'] != _event_digest(row):
            raise GazeboMonitorRoomValidationError('event digest invalid')
    except (KeyError, TypeError, GazeboMonitorRoomValidationError):
        raise GazeboMonitorRoomSchemaError(
            'stored Gazebo monitor-room event is invalid'
        ) from None


def _binding_from_operation(
    operation: Mapping[str, Any],
) -> PrivateOperationBinding:
    return PrivateOperationBinding(
        operation_id=operation['operation_id'],
        prepare_fingerprint=operation['prepare_fingerprint'],
        robot_id=operation['robot_id'],
        map_id=operation['map_id'],
        map_revision=operation['map_revision'],
        semantic_revision=operation['semantic_revision'],
        zones_digest=operation['zones_digest'],
        target_binding_digest=operation['target_binding_digest'],
        effects_digest=operation['effects_digest'],
        profile_digest=operation['profile_digest'],
        plan_digest=operation['plan_digest'],
        sample_count=operation['sample_count'],
        deadline=operation['deadline'],
    )


def _validate_dispatch_claim_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    operation: sqlite3.Row,
    sample: sqlite3.Row,
    store_namespace: str,
) -> None:
    """Validate one immutable first-dispatch claim and its target."""
    try:
        if (
            row['schema_version'] != GAZEBO_MONITOR_ROOM_SCHEMA_VERSION
            or row['store_namespace'] != store_namespace
            or row['operation_id'] != operation['operation_id']
            or row['sample_index'] != sample['sample_index']
            or row['goal_uuid'] != sample['goal_uuid']
            or row['phase'] not in {'start', 'cancel'}
        ):
            raise GazeboMonitorRoomValidationError(
                'dispatch claim target is invalid'
            )
        _identifier(row['worker_id'], 'worker_id')
        _bounded_integer(
            row['fence_epoch'], 'fence_epoch', 1, _MAX_FENCE
        )
        for name in (
            'binding_digest',
            'wire_payload_digest',
            'record_digest',
        ):
            _digest(row[name], name)
        claimed_at = _timestamp(row['claimed_at'], 'claimed_at')
        claimed_lease_expires_at = _timestamp(
            row['lease_expires_at'], 'lease_expires_at'
        )
        if (
            claimed_at < float(operation['created_at'])
            or claimed_at >= claimed_lease_expires_at
        ):
            raise GazeboMonitorRoomValidationError(
                'dispatch claim time is invalid'
            )
        lease_event = connection.execute(
            '''
            SELECT 1 FROM gazebo_monitor_room_events
            WHERE operation_id = ? AND fence_epoch = ?
              AND worker_id = ? AND lease_expires_at = ?
              AND recorded_at <= ?
              AND event_type IN (
                  'lease_acquired', 'lease_renewed', 'lease_taken_over'
              )
            LIMIT 1
            ''',
            (
                row['operation_id'],
                row['fence_epoch'],
                row['worker_id'],
                claimed_lease_expires_at,
                claimed_at,
            ),
        ).fetchone()
        if lease_event is None:
            raise GazeboMonitorRoomValidationError(
                'dispatch claim lease chronology is invalid'
            )
        if row['binding_digest'] != _binding_from_operation(
            operation
        ).binding_digest:
            raise GazeboMonitorRoomValidationError(
                'dispatch claim binding is invalid'
            )
        if row['phase'] == 'start':
            _digest(row['start_fingerprint'], 'start_fingerprint')
            _digest(row['preflight_digest'], 'preflight_digest')
            if (
                claimed_at >= float(operation['deadline'])
                or connection.execute(
                    '''
                    SELECT 1 FROM gazebo_monitor_room_events
                    WHERE operation_id = ? AND sample_index = ?
                      AND goal_uuid = ? AND event_type = ?
                      AND recorded_at <= ?
                    LIMIT 1
                    ''',
                    (
                        row['operation_id'],
                        row['sample_index'],
                        row['goal_uuid'],
                        'send_intent_recorded',
                        claimed_at,
                    ),
                ).fetchone() is None
                or
                row['preflight_digest'] != sample['preflight_digest']
                or row['cancel_request_id'] is not None
                or row['cancel_request_fingerprint'] is not None
            ):
                raise GazeboMonitorRoomValidationError(
                    'start dispatch claim is invalid'
                )
        else:
            _identifier(row['cancel_request_id'], 'cancel_request_id')
            _digest(
                row['cancel_request_fingerprint'],
                'cancel_request_fingerprint',
            )
            if (
                connection.execute(
                    '''
                    SELECT 1 FROM gazebo_monitor_room_events
                    WHERE operation_id = ? AND sample_index = ?
                      AND goal_uuid = ? AND event_type = ?
                      AND recorded_at <= ?
                    LIMIT 1
                    ''',
                    (
                        row['operation_id'],
                        row['sample_index'],
                        row['goal_uuid'],
                        'cancel_requested',
                        claimed_at,
                    ),
                ).fetchone() is None
                or
                row['cancel_request_id'] != operation['cancel_request_id']
                or row['start_fingerprint'] is not None
                or row['preflight_digest'] is not None
            ):
                raise GazeboMonitorRoomValidationError(
                    'cancel dispatch claim is invalid'
                )
        if row['record_digest'] != _dispatch_claim_digest(row):
            raise GazeboMonitorRoomValidationError(
                'dispatch claim digest is invalid'
            )
    except (KeyError, TypeError, GazeboMonitorRoomValidationError):
        raise GazeboMonitorRoomSchemaError(
            'stored Gazebo monitor-room dispatch claim is invalid'
        ) from None


def _validate_contents_locked(
    connection: sqlite3.Connection,
    store_namespace: str,
) -> None:
    operations = connection.execute(
        'SELECT * FROM gazebo_monitor_room_operations ORDER BY operation_id'
    ).fetchall()
    for operation in operations:
        _validate_operation_row(operation)
        samples = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_samples
            WHERE operation_id = ? ORDER BY sample_index
            ''',
            (operation['operation_id'],),
        ).fetchall()
        if (
            len(samples) != operation['sample_count']
            or any(
                row['sample_index'] != index
                for index, row in enumerate(samples)
            )
        ):
            raise GazeboMonitorRoomSchemaError(
                'stored Gazebo monitor-room sample set is invalid'
            )
        for sample in samples:
            _validate_sample_row(sample, operation, store_namespace)
        _validate_prepare_binding(operation, samples)
        current_index = operation['current_sample_index']
        reached = operation['samples_reached']
        if reached != current_index and not (
            operation['state'] == 'succeeded'
            and reached == operation['sample_count']
            and current_index == operation['sample_count'] - 1
        ):
            raise GazeboMonitorRoomSchemaError(
                'stored Gazebo monitor-room progress is invalid'
            )
        for index, sample in enumerate(samples):
            expected_state = (
                'succeeded'
                if index < current_index
                else _expected_current_sample_state(operation['state'])
                if index == current_index
                else 'pending'
            )
            if sample['state'] != expected_state:
                raise GazeboMonitorRoomSchemaError(
                    'stored Gazebo monitor-room sample progress is invalid'
                )
        events = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_events
            WHERE operation_id = ? ORDER BY event_seq
            ''',
            (operation['operation_id'],),
        ).fetchall()
        if (
            len(events) < operation['sample_count'] + 1
            or len(events) > GAZEBO_MONITOR_ROOM_MAX_EVENTS
        ):
            raise GazeboMonitorRoomSchemaError(
                'stored Gazebo monitor-room event count is invalid'
            )
        previous_digest = _ZERO_DIGEST
        previous_time = 0.0
        previous_fence = 0
        lease_owner = None
        lease_expires_at = None
        latest_sample_digests: Dict[int, str] = {}
        for expected_seq, event in enumerate(events, start=1):
            if event['event_seq'] != expected_seq:
                raise GazeboMonitorRoomSchemaError(
                    'stored Gazebo monitor-room event sequence is invalid'
                )
            _validate_event_row(
                event, operation, previous_digest, previous_time
            )
            event_fence = int(event['fence_epoch'])
            event_expiry = event['lease_expires_at']
            if event_fence == 0:
                if (
                    previous_fence != 0
                    or event_expiry is not None
                    or event['worker_id'] != 'store'
                ):
                    raise GazeboMonitorRoomSchemaError(
                        'stored Gazebo lease event history is invalid'
                    )
            elif event_fence == previous_fence + 1:
                expected_type = (
                    'lease_acquired'
                    if previous_fence == 0
                    else 'lease_taken_over'
                )
                if event['event_type'] != expected_type:
                    raise GazeboMonitorRoomSchemaError(
                        'stored Gazebo lease event history is invalid'
                    )
                lease_owner = event['worker_id']
                lease_expires_at = float(event_expiry)
            elif event_fence == previous_fence and event_fence >= 1:
                if event['event_type'] == 'lease_renewed':
                    if (
                        event['worker_id'] != lease_owner
                        or float(event_expiry) < lease_expires_at
                    ):
                        raise GazeboMonitorRoomSchemaError(
                            'stored Gazebo lease event history is invalid'
                        )
                    lease_expires_at = float(event_expiry)
                elif (
                    event['worker_id'] != lease_owner
                    or float(event_expiry) != lease_expires_at
                ):
                    raise GazeboMonitorRoomSchemaError(
                        'stored Gazebo lease event history is invalid'
                    )
            else:
                raise GazeboMonitorRoomSchemaError(
                    'stored Gazebo lease event history is invalid'
                )
            previous_fence = event_fence
            previous_digest = event['event_digest']
            previous_time = float(event['recorded_at'])
            if event['sample_index'] is not None:
                sample_index = int(event['sample_index'])
                if event['goal_uuid'] != samples[sample_index]['goal_uuid']:
                    raise GazeboMonitorRoomSchemaError(
                        'stored Gazebo event goal binding is invalid'
                    )
                latest_sample_digests[sample_index] = (
                    event['sample_record_digest']
                )
        if events[-1]['operation_record_digest'] != operation['record_digest']:
            raise GazeboMonitorRoomSchemaError(
                'stored Gazebo operation snapshot is invalid'
            )
        if (
            previous_fence != operation['fence_epoch']
            or lease_owner != operation['lease_owner']
            or lease_expires_at != operation['lease_expires_at']
        ):
            raise GazeboMonitorRoomSchemaError(
                'stored Gazebo lease snapshot is invalid'
            )
        if any(
            latest_sample_digests.get(index) != sample['record_digest']
            for index, sample in enumerate(samples)
        ):
            raise GazeboMonitorRoomSchemaError(
                'stored Gazebo sample snapshot is invalid'
            )
        claims = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_dispatch_claims
            WHERE operation_id = ? ORDER BY phase, sample_index
            ''',
            (operation['operation_id'],),
        ).fetchall()
        for claim in claims:
            _validate_dispatch_claim_row(
                connection,
                claim,
                operation,
                samples[int(claim['sample_index'])],
                store_namespace,
            )


def _validate_database_locked(
    connection: sqlite3.Connection,
    *,
    expected_boot_id: Optional[str] = None,
    expected_store_namespace: Optional[str] = None,
) -> str:
    namespace = _validate_exact_schema_locked(
        connection,
        expected_boot_id=expected_boot_id,
        expected_store_namespace=expected_store_namespace,
    )
    _validate_contents_locked(connection, namespace)
    return namespace


def _observation_from_rows(
    operation: sqlite3.Row,
    sample: sqlite3.Row,
    *,
    replayed: bool = False,
) -> OperationObservation:
    return OperationObservation(
        operation_id=str(operation['operation_id']),
        robot_id=str(operation['robot_id']),
        state=str(operation['state']),
        current_sample_index=int(operation['current_sample_index']),
        current_sample_state=str(sample['state']),
        current_goal_uuid=str(sample['goal_uuid']),
        navigation_samples_total=int(operation['sample_count']),
        navigation_samples_reached=int(operation['samples_reached']),
        fence_epoch=int(operation['fence_epoch']),
        lease_owner=operation['lease_owner'],
        lease_expires_at=(
            None
            if operation['lease_expires_at'] is None
            else float(operation['lease_expires_at'])
        ),
        deadline=float(operation['deadline']),
        terminal_code=operation['terminal_code'],
        cancel_request_id=operation['cancel_request_id'],
        created_at=float(operation['created_at']),
        updated_at=float(operation['updated_at']),
        replayed=replayed,
    )


def _validate_private_file_stat(
    value: os.stat_result,
    *,
    field_name: str,
) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_nlink != 1
    ):
        raise GazeboMonitorRoomValidationError(
            f'{field_name} is not a private service-owned file'
        )


def _validate_database_directory_component(
    value: os.stat_result,
    *,
    field_name: str,
) -> None:
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise GazeboMonitorRoomValidationError(
            f'{field_name} must be a real directory'
        )
    writable = stat.S_IMODE(value.st_mode) & 0o022
    sticky_root = bool(value.st_mode & stat.S_ISVTX) and value.st_uid == 0
    if value.st_uid not in {0, os.geteuid()} or (writable and not sticky_root):
        raise GazeboMonitorRoomValidationError(
            f'{field_name} is not service-protected'
        )


def _verify_database_directory_chain(path: Path) -> None:
    """Reject relative, non-normal, or symlinked directory components."""
    if not path.is_absolute():
        raise GazeboMonitorRoomValidationError(
            'database_path must be an absolute durable path'
        )
    if any(part in ('.', '..') for part in path.parts[1:]):
        raise GazeboMonitorRoomValidationError(
            'database_path must be lexically normalized'
        )
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current = current / part
        try:
            component = os.lstat(current)
        except OSError as error:
            raise GazeboMonitorRoomValidationError(
                'database parent is unavailable'
            ) from error
        _validate_database_directory_component(
            component,
            field_name='database parent path',
        )


def _prepare_private_database_path(
    path: Path,
) -> Tuple[bool, Tuple[int, int]]:
    """Atomically create or authenticate the coordinate-bearing DB file."""
    _verify_database_directory_chain(path)
    parent = path.parent
    try:
        parent_stat = os.lstat(parent)
    except OSError as error:
        raise GazeboMonitorRoomValidationError(
            'database parent is unavailable'
        ) from error
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o022
    ):
        raise GazeboMonitorRoomValidationError(
            'database parent is not service-protected'
        )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            existing = os.lstat(path)
        except OSError as error:
            raise GazeboMonitorRoomValidationError(
                'database path changed during validation'
            ) from error
        if stat.S_ISLNK(existing.st_mode):
            raise GazeboMonitorRoomValidationError(
                'database path must not be a symlink'
            )
        _validate_private_file_stat(existing, field_name='database path')
        return False, (existing.st_dev, existing.st_ino)
    except OSError as error:
        raise GazeboMonitorRoomValidationError(
            'database path could not be created privately'
        ) from error
    try:
        created = os.fstat(descriptor)
        _validate_private_file_stat(created, field_name='database path')
        return True, (created.st_dev, created.st_ino)
    finally:
        os.close(descriptor)


def _verify_database_path_identity(
    path: Path,
    identity: Tuple[int, int],
) -> None:
    try:
        current = os.lstat(path)
    except OSError as error:
        raise GazeboMonitorRoomValidationError(
            'database path changed during open'
        ) from error
    _validate_private_file_stat(current, field_name='database path')
    if (current.st_dev, current.st_ino) != identity:
        raise GazeboMonitorRoomValidationError(
            'database path changed during open'
        )


class GazeboMonitorRoomStore:
    """Own exact durable state for one or more Gazebo-only operations."""

    def __init__(
        self,
        database_path: os.PathLike[str] | str,
        *,
        boot_id_reader=None,
    ) -> None:
        """Open, create, and fully validate a dedicated SQLite database."""
        if isinstance(database_path, bool) or not isinstance(
            database_path, (str, os.PathLike)
        ):
            raise GazeboMonitorRoomValidationError(
                'database_path is invalid'
            )
        self._path = Path(database_path)
        if str(self._path) in ('', ':memory:'):
            raise GazeboMonitorRoomValidationError(
                'database_path must be a regular durable path'
            )
        reader = _read_host_boot_id if boot_id_reader is None else (
            boot_id_reader
        )
        if not callable(reader):
            raise GazeboMonitorRoomBootIdentityError(
                'host boot identity is unavailable'
            )
        try:
            self._host_boot_id = _host_boot_id(reader())
        except GazeboMonitorRoomBootIdentityError:
            raise GazeboMonitorRoomBootIdentityError(
                'host boot identity is unavailable'
            ) from None
        except Exception:
            raise GazeboMonitorRoomBootIdentityError(
                'host boot identity is unavailable'
            ) from None
        may_initialize, path_identity = _prepare_private_database_path(
            self._path
        )
        self._lock = RLock()
        self._connection: Optional[sqlite3.Connection] = None
        self._store_namespace: Optional[str] = None
        self._path_identity = path_identity
        connection = sqlite3.connect(
            str(self._path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('PRAGMA recursive_triggers = ON')
            connection.execute('PRAGMA trusted_schema = OFF')
            connection.execute('PRAGMA synchronous = FULL')
            journal = connection.execute(
                'PRAGMA journal_mode = DELETE'
            ).fetchone()
            if journal is None or str(journal[0]).lower() != 'delete':
                raise GazeboMonitorRoomDurabilityError(
                    'SQLite rollback-journal mode is unavailable'
                )
            connection.execute('PRAGMA busy_timeout = 5000')
            self._attest_database_locked(connection)
            connection.execute('BEGIN IMMEDIATE')
            self._attest_database_locked(connection)
            owned = connection.execute(
                '''
                SELECT name FROM sqlite_master
                WHERE name LIKE 'gazebo_monitor_room_%'
                  AND type IN ('table', 'index', 'trigger')
                '''
            ).fetchall()
            if not owned:
                if not may_initialize:
                    raise GazeboMonitorRoomSchemaError(
                        'existing database lost its durable schema'
                    )
                _create_schema_locked(
                    connection,
                    store_namespace=uuid.uuid4().hex,
                    host_boot_id=self._host_boot_id,
                )
            integrity = connection.execute(
                'PRAGMA integrity_check'
            ).fetchall()
            if (
                len(integrity) != 1
                or tuple(integrity[0]) != ('ok',)
            ):
                raise GazeboMonitorRoomSchemaError(
                    'Gazebo monitor-room database integrity failed'
                )
            store_namespace = _validate_database_locked(
                connection,
                expected_boot_id=self._host_boot_id,
            )
            self._attest_database_locked(connection)
            connection.execute('COMMIT')
            self._attest_database_locked(connection)
        except BaseException:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            connection.close()
            raise
        self._connection = connection
        self._store_namespace = store_namespace

    @property
    def store_namespace(self) -> str:
        """Return this open database's immutable random namespace."""
        with self._lock:
            self._require_connection()
            if self._store_namespace is None:
                raise GazeboMonitorRoomStoreError(
                    'Gazebo monitor-room store namespace is unavailable'
                )
            return self._store_namespace

    def __enter__(self) -> 'GazeboMonitorRoomStore':
        """Return this open store as a context manager."""
        with self._lock:
            self._require_connection()
        return self

    def __exit__(self, *_arguments: Any) -> None:
        """Close the database context."""
        self.close()

    def close(self) -> None:
        """Close the durable database handle idempotently."""
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise GazeboMonitorRoomStoreError(
                'Gazebo monitor-room store is closed'
            )
        return connection

    def _poison_connection_locked(
        self, connection: sqlite3.Connection
    ) -> None:
        if self._connection is connection:
            self._connection = None
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def _attest_database_locked(
        self, connection: sqlite3.Connection
    ) -> None:
        """Bind the live SQLite connection to one protected disk inode."""
        try:
            _verify_database_path_identity(
                self._path, self._path_identity
            )
            databases = connection.execute(
                'PRAGMA database_list'
            ).fetchall()
            main = [row for row in databases if row['name'] == 'main']
            unexpected = [
                row for row in databases
                if row['name'] != 'main'
                and not (row['name'] == 'temp' and row['file'] == '')
            ]
            if (
                len(main) != 1
                or main[0]['file'] != str(self._path)
                or unexpected
            ):
                raise GazeboMonitorRoomDurabilityError(
                    'SQLite main path binding changed'
                )
            for pragma_name, expected in (
                ('foreign_keys', 1),
                ('recursive_triggers', 1),
                ('trusted_schema', 0),
                ('query_only', 0),
                ('synchronous', 2),
            ):
                value = connection.execute(
                    f'PRAGMA {pragma_name}'
                ).fetchone()
                if value is None or value[0] != expected:
                    raise GazeboMonitorRoomDurabilityError(
                        'SQLite safety configuration changed'
                    )
            journal = connection.execute(
                'PRAGMA journal_mode'
            ).fetchone()
            if journal is None or str(journal[0]).lower() != 'delete':
                raise GazeboMonitorRoomDurabilityError(
                    'SQLite journal mode changed'
                )
        except GazeboMonitorRoomDurabilityError:
            raise
        except (
            OSError,
            sqlite3.Error,
            GazeboMonitorRoomValidationError,
        ) as error:
            raise GazeboMonitorRoomDurabilityError(
                'Gazebo monitor-room durable path binding was lost'
            ) from error

    @staticmethod
    def _rollback_quietly(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
        except sqlite3.Error:
            pass

    def _attest_or_poison_locked(
        self, connection: sqlite3.Connection
    ) -> None:
        try:
            self._attest_database_locked(connection)
        except BaseException:
            self._poison_connection_locked(connection)
            raise

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            try:
                self._attest_database_locked(connection)
                connection.execute('BEGIN')
                self._attest_database_locked(connection)
            except sqlite3.Error as error:
                self._poison_connection_locked(connection)
                raise GazeboMonitorRoomStoreError(
                    'Gazebo monitor-room read transaction failed'
                ) from error
            except BaseException:
                self._poison_connection_locked(connection)
                raise
            try:
                _validate_database_locked(
                    connection,
                    expected_boot_id=self._host_boot_id,
                    expected_store_namespace=self._store_namespace,
                )
                yield connection
                self._attest_database_locked(connection)
                connection.execute('COMMIT')
                self._attest_or_poison_locked(connection)
            except sqlite3.Error as error:
                self._rollback_quietly(connection)
                self._poison_connection_locked(connection)
                raise GazeboMonitorRoomDurabilityError(
                    'SQLite read durability is uncertain'
                ) from error
            except BaseException as error:
                self._rollback_quietly(connection)
                if isinstance(
                    error, GazeboMonitorRoomDurabilityError
                ):
                    self._poison_connection_locked(connection)
                raise

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            try:
                self._attest_database_locked(connection)
                connection.execute('BEGIN IMMEDIATE')
                self._attest_database_locked(connection)
            except sqlite3.Error as error:
                self._poison_connection_locked(connection)
                raise GazeboMonitorRoomStoreError(
                    'Gazebo monitor-room write transaction failed'
                ) from error
            except BaseException:
                self._poison_connection_locked(connection)
                raise
            try:
                _validate_database_locked(
                    connection,
                    expected_boot_id=self._host_boot_id,
                    expected_store_namespace=self._store_namespace,
                )
                yield connection
                _validate_database_locked(
                    connection,
                    expected_boot_id=self._host_boot_id,
                    expected_store_namespace=self._store_namespace,
                )
                self._attest_database_locked(connection)
                connection.execute('COMMIT')
                self._attest_or_poison_locked(connection)
            except sqlite3.Error as error:
                self._rollback_quietly(connection)
                self._poison_connection_locked(connection)
                raise GazeboMonitorRoomDurabilityError(
                    'SQLite write durability is uncertain'
                ) from error
            except BaseException as error:
                self._rollback_quietly(connection)
                if isinstance(error, GazeboMonitorRoomDurabilityError):
                    self._poison_connection_locked(connection)
                raise

    @staticmethod
    def _load_operation_locked(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> sqlite3.Row:
        normalized = _identifier(operation_id, 'operation_id')
        row = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_operations
            WHERE operation_id = ?
            ''',
            (normalized,),
        ).fetchone()
        if row is None:
            raise GazeboMonitorRoomNotFoundError(
                'Gazebo monitor-room operation was not found'
            )
        return row

    @staticmethod
    def _load_sample_locked(
        connection: sqlite3.Connection,
        operation_id: str,
        sample_index: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_samples
            WHERE operation_id = ? AND sample_index = ?
            ''',
            (operation_id, sample_index),
        ).fetchone()
        if row is None:
            raise GazeboMonitorRoomSchemaError(
                'current Gazebo monitor-room sample is missing'
            )
        return row

    @staticmethod
    def _assert_write_clock_locked(
        connection: sqlite3.Connection,
        now: float,
    ) -> float:
        normalized = _timestamp(now, 'recorded_at')
        high_watermark = connection.execute(
            '''
            SELECT MAX(value) FROM (
                SELECT MAX(recorded_at) AS value
                FROM gazebo_monitor_room_events
                UNION ALL
                SELECT MAX(claimed_at) AS value
                FROM gazebo_monitor_room_dispatch_claims
            )
            '''
        ).fetchone()[0]
        if (
            high_watermark is not None
            and normalized < float(high_watermark)
        ):
            raise GazeboMonitorRoomClockRollbackError(
                'server clock moved backwards'
            )
        return normalized

    @staticmethod
    def _assert_read_clock_locked(
        connection: sqlite3.Connection,
        now: float,
    ) -> float:
        """Check the durable clock high-watermark without writing it."""
        return GazeboMonitorRoomStore._assert_write_clock_locked(
            connection, now
        )

    @staticmethod
    def _append_event_locked(
        connection: sqlite3.Connection,
        *,
        operation: Mapping[str, Any],
        event_type: str,
        recorded_at: float,
        worker_id: str,
        sample: Optional[Mapping[str, Any]] = None,
        from_operation_state: Optional[str] = None,
        from_sample_state: Optional[str] = None,
        code: Optional[str] = None,
        evidence_digest: Optional[str] = None,
    ) -> None:
        _identifier(event_type, 'event_type')
        _identifier(worker_id, 'worker_id')
        if code is not None:
            _identifier(code, 'code')
        if evidence_digest is not None:
            _digest(evidence_digest, 'evidence_digest')
        previous = connection.execute(
            '''
            SELECT event_seq, event_digest
            FROM gazebo_monitor_room_events
            WHERE operation_id = ?
            ORDER BY event_seq DESC LIMIT 1
            ''',
            (operation['operation_id'],),
        ).fetchone()
        event_seq = 1 if previous is None else int(previous['event_seq']) + 1
        if event_seq > GAZEBO_MONITOR_ROOM_MAX_EVENTS:
            raise GazeboMonitorRoomConflictError(
                'Gazebo monitor-room event bound was reached'
            )
        values: Dict[str, Any] = {
            'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
            'operation_id': operation['operation_id'],
            'event_seq': event_seq,
            'event_type': event_type,
            'recorded_at': recorded_at,
            'fence_epoch': operation['fence_epoch'],
            'lease_expires_at': operation['lease_expires_at'],
            'worker_id': worker_id,
            'sample_index': (
                None if sample is None else sample['sample_index']
            ),
            'goal_uuid': None if sample is None else sample['goal_uuid'],
            'from_operation_state': from_operation_state,
            'to_operation_state': operation['state'],
            'from_sample_state': from_sample_state,
            'to_sample_state': None if sample is None else sample['state'],
            'code': code,
            'evidence_digest': evidence_digest,
            'operation_record_digest': operation['record_digest'],
            'sample_record_digest': (
                None if sample is None else sample['record_digest']
            ),
            'previous_event_digest': (
                _ZERO_DIGEST
                if previous is None
                else previous['event_digest']
            ),
        }
        values['event_digest'] = _event_digest(values)
        connection.execute(
            '''
            INSERT INTO gazebo_monitor_room_events (
                schema_version, operation_id, event_seq, event_type,
                recorded_at, fence_epoch, lease_expires_at, worker_id,
                sample_index,
                goal_uuid, from_operation_state, to_operation_state,
                from_sample_state, to_sample_state, code,
                evidence_digest, operation_record_digest,
                sample_record_digest, previous_event_digest,
                event_digest
            ) VALUES (
                :schema_version, :operation_id, :event_seq, :event_type,
                :recorded_at, :fence_epoch, :lease_expires_at,
                :worker_id, :sample_index,
                :goal_uuid, :from_operation_state, :to_operation_state,
                :from_sample_state, :to_sample_state, :code,
                :evidence_digest, :operation_record_digest,
                :sample_record_digest, :previous_event_digest,
                :event_digest
            )
            ''',
            values,
        )

    @staticmethod
    def _observation_locked(
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
        *,
        replayed: bool = False,
    ) -> OperationObservation:
        sample = GazeboMonitorRoomStore._load_sample_locked(
            connection,
            operation['operation_id'],
            operation['current_sample_index'],
        )
        return _observation_from_rows(
            operation, sample, replayed=replayed
        )

    def prepare(
        self,
        request: PrepareOperation,
        *,
        now: float,
    ) -> OperationObservation:
        """Create one operation or replay its exact durable preparation."""
        request = _canonical_prepare_operation(request)
        with self._write_transaction() as connection:
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            existing = connection.execute(
                '''
                SELECT * FROM gazebo_monitor_room_operations
                WHERE operation_id = ? OR prepare_request_id = ?
                ''',
                (request.operation_id, request.prepare_request_id),
            ).fetchall()
            if existing:
                if (
                    len(existing) == 1
                    and existing[0]['operation_id'] == request.operation_id
                    and existing[0]['prepare_request_id']
                    == request.prepare_request_id
                    and existing[0]['prepare_fingerprint']
                    == request.payload_fingerprint
                ):
                    return self._observation_locked(
                        connection, existing[0], replayed=True
                    )
                raise GazeboMonitorRoomConflictError(
                    'prepare request conflicts with durable identity'
                )
            if request.deadline <= normalized_now:
                raise GazeboMonitorRoomDeadlineError(
                    'operation deadline must be in the future'
                )
            values = _operation_values(request, normalized_now)
            try:
                connection.execute(
                    '''
                    INSERT INTO gazebo_monitor_room_operations (
                        schema_version, operation_id, prepare_request_id,
                        prepare_fingerprint, robot_id, runtime_mode,
                        map_id, map_revision, semantic_revision,
                        zones_digest, target_binding_digest,
                        effects_digest, profile_digest, plan_digest,
                        sample_count, current_sample_index,
                        samples_reached, state, terminal_code,
                        cancel_request_id, cancel_fingerprint,
                        cancel_origin_state, lease_owner,
                        lease_expires_at, fence_epoch, deadline,
                        created_at, updated_at, simulation,
                        physical_authorized, physical_effects,
                        viewer_live, camera_coverage_validated,
                        coverage_achieved, record_digest
                    ) VALUES (
                        :schema_version, :operation_id,
                        :prepare_request_id, :prepare_fingerprint,
                        :robot_id, 'gazebo', :map_id, :map_revision,
                        :semantic_revision, :zones_digest,
                        :target_binding_digest, :effects_digest,
                        :profile_digest, :plan_digest, :sample_count,
                        0, 0, 'prepared', NULL, NULL, NULL, NULL,
                        NULL, NULL, 0, :deadline, :created_at,
                        :updated_at, 1, 0, 0, 0, 0, 0, :record_digest
                    )
                    ''',
                    values,
                )
            except sqlite3.IntegrityError as error:
                raise GazeboMonitorRoomConflictError(
                    'robot already has active or unresolved navigation'
                ) from error
            sample_rows = []
            for sample in request.ordered_semantic_samples:
                sample_values = _sample_values(
                    self.store_namespace,
                    request.operation_id,
                    sample,
                    normalized_now,
                )
                connection.execute(
                    '''
                    INSERT INTO gazebo_monitor_room_samples (
                        schema_version, operation_id, sample_index,
                        polygon_ordinal, row_ordinal, x_mm, y_mm,
                        frame_id, goal_uuid, state, preflight_digest,
                        acceptance_digest, terminal_evidence_digest,
                        send_intent_at, accepted_at, terminal_at,
                        result_code, updated_at, record_digest
                    ) VALUES (
                        :schema_version, :operation_id, :sample_index,
                        :polygon_ordinal, :row_ordinal, :x_mm, :y_mm,
                        'map', :goal_uuid, 'pending', NULL, NULL, NULL,
                        NULL, NULL, NULL, NULL, :updated_at,
                        :record_digest
                    )
                    ''',
                    sample_values,
                )
                sample_rows.append(sample_values)
            operation = connection.execute(
                '''
                SELECT * FROM gazebo_monitor_room_operations
                WHERE operation_id = ?
                ''',
                (request.operation_id,),
            ).fetchone()
            for sample_values in sample_rows:
                self._append_event_locked(
                    connection,
                    operation=operation,
                    event_type='sample_registered',
                    recorded_at=normalized_now,
                    worker_id='store',
                    sample=sample_values,
                )
            self._append_event_locked(
                connection,
                operation=operation,
                event_type='operation_prepared',
                recorded_at=normalized_now,
                worker_id='store',
            )
            return self._observation_locked(connection, operation)

    def observe(self, operation_id: str) -> OperationObservation:
        """Return one validated coordinate-free durable observation."""
        with self._read_transaction() as connection:
            operation = self._load_operation_locked(
                connection, operation_id
            )
            return self._observation_locked(connection, operation)

    def private_current_sample(
        self, operation_id: str
    ) -> PrivateStoredSample:
        """Return the current private candidate for the in-process adapter."""
        with self._read_transaction() as connection:
            operation = self._load_operation_locked(
                connection, operation_id
            )
            row = self._load_sample_locked(
                connection,
                operation['operation_id'],
                operation['current_sample_index'],
            )
            return PrivateStoredSample(
                operation_id=str(row['operation_id']),
                store_namespace=self.store_namespace,
                index=int(row['sample_index']),
                polygon_ordinal=int(row['polygon_ordinal']),
                row_ordinal=int(row['row_ordinal']),
                x_mm=int(row['x_mm']),
                y_mm=int(row['y_mm']),
                frame_id=str(row['frame_id']),
                goal_uuid=str(row['goal_uuid']),
                state=str(row['state']),
            )

    def private_operation_binding(
        self, operation_id: str
    ) -> PrivateOperationBinding:
        """Return persisted evidence, never authorization to execute."""
        with self._read_transaction() as connection:
            operation = self._load_operation_locked(
                connection, operation_id
            )
            return _binding_from_operation(operation)

    def events(self, operation_id: str) -> Tuple[OperationEvent, ...]:
        """Return coordinate-free append-only events in durable order."""
        with self._read_transaction() as connection:
            operation = self._load_operation_locked(
                connection, operation_id
            )
            rows = connection.execute(
                '''
                SELECT * FROM gazebo_monitor_room_events
                WHERE operation_id = ? ORDER BY event_seq
                ''',
                (operation['operation_id'],),
            ).fetchall()
            return tuple(
                OperationEvent(
                    operation_id=str(row['operation_id']),
                    event_seq=int(row['event_seq']),
                    event_type=str(row['event_type']),
                    recorded_at=float(row['recorded_at']),
                    fence_epoch=int(row['fence_epoch']),
                    lease_expires_at=(
                        None
                        if row['lease_expires_at'] is None
                        else float(row['lease_expires_at'])
                    ),
                    worker_id=str(row['worker_id']),
                    sample_index=row['sample_index'],
                    goal_uuid=row['goal_uuid'],
                    from_operation_state=row['from_operation_state'],
                    to_operation_state=str(row['to_operation_state']),
                    from_sample_state=row['from_sample_state'],
                    to_sample_state=row['to_sample_state'],
                    code=row['code'],
                    evidence_digest=row['evidence_digest'],
                    event_digest=str(row['event_digest']),
                )
                for row in rows
            )

    def acquire_lease(
        self,
        operation_id: str,
        *,
        worker_id: str,
        expected_fence: int,
        lease_seconds: float,
        now: float,
    ) -> LeaseGrant:
        """Acquire, renew, or take over a lease with a monotonic fence."""
        normalized_operation = _identifier(operation_id, 'operation_id')
        normalized_worker = _identifier(worker_id, 'worker_id')
        normalized_fence = _bounded_integer(
            expected_fence, 'expected_fence', 0, _MAX_FENCE
        )
        normalized_lease = _timestamp(lease_seconds, 'lease_seconds')
        if not 0 < normalized_lease <= GAZEBO_MONITOR_ROOM_MAX_LEASE_SECONDS:
            raise GazeboMonitorRoomValidationError(
                'lease_seconds is invalid'
            )
        with self._write_transaction() as connection:
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            old = self._load_operation_locked(
                connection, normalized_operation
            )
            if old['state'] in TERMINAL_STATES:
                raise GazeboMonitorRoomLeaseError(
                    'terminal operation cannot be leased'
                )
            if old['fence_epoch'] != normalized_fence:
                raise GazeboMonitorRoomFenceError('lease fence is stale')
            unexpired = (
                old['lease_expires_at'] is not None
                and normalized_now < float(old['lease_expires_at'])
            )
            if unexpired and old['lease_owner'] != normalized_worker:
                raise GazeboMonitorRoomLeaseError(
                    'operation lease is held by another worker'
                )
            takeover = old['fence_epoch'] == 0 or not unexpired
            next_fence = (
                int(old['fence_epoch']) + 1
                if takeover
                else int(old['fence_epoch'])
            )
            if next_fence > _MAX_FENCE:
                raise GazeboMonitorRoomFenceError(
                    'operation fence bound was reached'
                )
            requested_expiry = normalized_now + normalized_lease
            expires_at = (
                requested_expiry
                if takeover
                else max(
                    requested_expiry,
                    float(old['lease_expires_at']),
                )
            )
            if not math.isfinite(expires_at):
                raise GazeboMonitorRoomValidationError(
                    'lease expiry is invalid'
                )
            values = dict(old)
            values.update(
                {
                    'lease_owner': normalized_worker,
                    'lease_expires_at': expires_at,
                    'fence_epoch': next_fence,
                    'updated_at': normalized_now,
                }
            )
            values['record_digest'] = _operation_digest(values)
            cursor = connection.execute(
                '''
                UPDATE gazebo_monitor_room_operations
                SET lease_owner = :lease_owner,
                    lease_expires_at = :lease_expires_at,
                    fence_epoch = :fence_epoch,
                    updated_at = :updated_at,
                    record_digest = :record_digest
                WHERE operation_id = :operation_id
                  AND state = :state
                  AND current_sample_index = :current_sample_index
                  AND fence_epoch = :old_fence
                  AND record_digest = :old_digest
                ''',
                {
                    **values,
                    'old_fence': old['fence_epoch'],
                    'old_digest': old['record_digest'],
                },
            )
            if cursor.rowcount != 1:
                raise GazeboMonitorRoomConflictError(
                    'operation changed during lease acquisition'
                )
            stored = self._load_operation_locked(
                connection, normalized_operation
            )
            event_type = (
                'lease_acquired'
                if old['fence_epoch'] == 0
                else 'lease_taken_over'
                if takeover
                else 'lease_renewed'
            )
            self._append_event_locked(
                connection,
                operation=stored,
                event_type=event_type,
                recorded_at=normalized_now,
                worker_id=normalized_worker,
                from_operation_state=old['state'],
            )
            observation = self._observation_locked(connection, stored)
            return LeaseGrant(
                observation=observation,
                worker_id=normalized_worker,
                fence_epoch=next_fence,
                lease_expires_at=expires_at,
                taken_over=takeover,
            )

    @staticmethod
    def _assert_goal_transition_locked(
        connection: sqlite3.Connection,
        transition: GoalTransition,
        now: float,
    ) -> Tuple[sqlite3.Row, sqlite3.Row]:
        transition = _canonical_goal_transition(transition)
        operation = GazeboMonitorRoomStore._load_operation_locked(
            connection, transition.operation_id
        )
        if operation['fence_epoch'] != transition.fence_epoch:
            raise GazeboMonitorRoomFenceError(
                'goal transition fence is stale'
            )
        if (
            operation['lease_owner'] != transition.worker_id
            or operation['lease_expires_at'] is None
            or now >= float(operation['lease_expires_at'])
        ):
            raise GazeboMonitorRoomLeaseError(
                'goal transition lease is not current'
            )
        sample = GazeboMonitorRoomStore._load_sample_locked(
            connection,
            transition.operation_id,
            transition.sample_index,
        )
        if (
            operation['state'] != transition.expected_operation_state
            or operation['current_sample_index']
            != transition.sample_index
            or sample['state'] != transition.expected_sample_state
            or sample['goal_uuid'] != transition.goal_uuid
        ):
            raise GazeboMonitorRoomConflictError(
                'goal transition compare-and-swap conflict'
            )
        return operation, sample

    @staticmethod
    def _assert_cancel_ready_locked(
        connection: sqlite3.Connection,
        transition: GoalTransition,
        cancel_request_id: str,
        now: float,
    ) -> Tuple[sqlite3.Row, sqlite3.Row]:
        operation, sample = (
            GazeboMonitorRoomStore._assert_goal_transition_locked(
                connection, transition, now
            )
        )
        if operation['cancel_request_id'] != cancel_request_id:
            raise GazeboMonitorRoomConflictError(
                'cancel request does not match durable intent'
            )
        return operation, sample

    @staticmethod
    def _claim_dispatch_locked(
        connection: sqlite3.Connection,
        values: Dict[str, Any],
    ) -> bool:
        existing = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_dispatch_claims
            WHERE store_namespace = ? AND phase = ?
              AND operation_id = ? AND sample_index = ?
              AND goal_uuid = ?
            ''',
            (
                values['store_namespace'],
                values['phase'],
                values['operation_id'],
                values['sample_index'],
                values['goal_uuid'],
            ),
        ).fetchone()
        if existing is not None:
            replay_fields = tuple(
                name for name in _DISPATCH_CLAIM_DIGEST_FIELDS
                if name != 'claimed_at'
            )
            if all(existing[name] == values[name] for name in replay_fields):
                return False
            raise GazeboMonitorRoomConflictError(
                'dispatch claim conflicts with durable first claim'
            )
        values['record_digest'] = _dispatch_claim_digest(values)
        connection.execute(
            '''
            INSERT INTO gazebo_monitor_room_dispatch_claims (
                schema_version, store_namespace, phase, operation_id,
                sample_index, goal_uuid, start_fingerprint,
                cancel_request_id, cancel_request_fingerprint,
                worker_id, fence_epoch, binding_digest,
                preflight_digest, wire_payload_digest,
                lease_expires_at, claimed_at, record_digest
            ) VALUES (
                :schema_version, :store_namespace, :phase,
                :operation_id, :sample_index, :goal_uuid,
                :start_fingerprint, :cancel_request_id,
                :cancel_request_fingerprint, :worker_id,
                :fence_epoch, :binding_digest, :preflight_digest,
                :wire_payload_digest, :lease_expires_at, :claimed_at,
                :record_digest
            )
            ''',
            values,
        )
        return True

    @staticmethod
    def _existing_dispatch_claim_locked(
        connection: sqlite3.Connection,
        *,
        store_namespace: str,
        phase: str,
        transition: GoalTransition,
    ) -> bool:
        """Return true for an already claimed exact logical wire target."""
        existing = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_dispatch_claims
            WHERE store_namespace = ? AND phase = ?
              AND operation_id = ? AND sample_index = ?
              AND goal_uuid = ?
            ''',
            (
                store_namespace,
                phase,
                transition.operation_id,
                transition.sample_index,
                transition.goal_uuid,
            ),
        ).fetchone()
        return existing is not None

    @staticmethod
    def _load_dispatch_claim_locked(
        connection: sqlite3.Connection,
        *,
        store_namespace: str,
        phase: str,
        transition: GoalTransition,
    ) -> sqlite3.Row:
        """Load the immutable claim for one exact logical wire target."""
        claim = connection.execute(
            '''
            SELECT * FROM gazebo_monitor_room_dispatch_claims
            WHERE store_namespace = ? AND phase = ?
              AND operation_id = ? AND sample_index = ?
              AND goal_uuid = ?
            ''',
            (
                store_namespace,
                phase,
                transition.operation_id,
                transition.sample_index,
                transition.goal_uuid,
            ),
        ).fetchone()
        if claim is None:
            raise GazeboMonitorRoomConflictError(
                'durable dispatch claim is missing'
            )
        return claim

    def _dispatch_claim_evidence_locked(
        self,
        connection: sqlite3.Connection,
        *,
        phase: str,
        transition: GoalTransition,
        operation: sqlite3.Row,
        sample: sqlite3.Row,
        start_fingerprint: Optional[str],
        cancel_request_id: Optional[str],
        cancel_request_fingerprint: Optional[str],
        binding_digest: str,
        preflight_digest: Optional[str],
        wire_payload_digest: str,
        checked_at: float,
    ) -> DispatchClaimEvidence:
        """Validate and snapshot one claim inside the caller transaction."""
        claim = self._load_dispatch_claim_locked(
            connection,
            store_namespace=self.store_namespace,
            phase=phase,
            transition=transition,
        )
        _validate_dispatch_claim_row(
            connection,
            claim,
            operation,
            sample,
            self.store_namespace,
        )
        expected = {
            'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
            'store_namespace': self.store_namespace,
            'phase': phase,
            'operation_id': transition.operation_id,
            'sample_index': transition.sample_index,
            'goal_uuid': transition.goal_uuid,
            'start_fingerprint': start_fingerprint,
            'cancel_request_id': cancel_request_id,
            'cancel_request_fingerprint': cancel_request_fingerprint,
            'worker_id': transition.worker_id,
            'fence_epoch': transition.fence_epoch,
            'binding_digest': binding_digest,
            'preflight_digest': preflight_digest,
            'wire_payload_digest': wire_payload_digest,
        }
        if any(claim[name] != value for name, value in expected.items()):
            raise GazeboMonitorRoomConflictError(
                'dispatch claim conflicts with current request'
            )
        if (
            phase == 'start'
            and checked_at >= float(claim['lease_expires_at'])
        ):
            raise GazeboMonitorRoomLeaseError(
                'start dispatch claim lease is expired'
            )
        if (
            _binding_from_operation(operation).binding_digest
            != binding_digest
            or operation['record_digest'] != _operation_digest(operation)
            or sample['record_digest'] != _sample_digest(sample)
        ):
            raise GazeboMonitorRoomConflictError(
                'dispatch claim conflicts with current binding'
            )
        if phase == 'start':
            if sample['preflight_digest'] != preflight_digest:
                raise GazeboMonitorRoomConflictError(
                    'dispatch claim conflicts with current preflight'
                )
        elif operation['cancel_request_id'] != cancel_request_id:
            raise GazeboMonitorRoomConflictError(
                'dispatch claim conflicts with current cancellation'
            )
        return DispatchClaimEvidence(
            phase=phase,
            store_namespace=str(claim['store_namespace']),
            operation_id=str(operation['operation_id']),
            sample_index=int(sample['sample_index']),
            goal_uuid=str(sample['goal_uuid']),
            operation_state=str(operation['state']),
            sample_state=str(sample['state']),
            start_fingerprint=claim['start_fingerprint'],
            cancel_request_id=claim['cancel_request_id'],
            cancel_request_fingerprint=(
                claim['cancel_request_fingerprint']
            ),
            worker_id=str(claim['worker_id']),
            fence_epoch=int(claim['fence_epoch']),
            binding_digest=str(claim['binding_digest']),
            preflight_digest=claim['preflight_digest'],
            wire_payload_digest=str(claim['wire_payload_digest']),
            claim_lease_expires_at=float(claim['lease_expires_at']),
            current_lease_expires_at=float(
                operation['lease_expires_at']
            ),
            claimed_at=float(claim['claimed_at']),
            operation_deadline=float(operation['deadline']),
            checked_at=checked_at,
            claim_record_digest=str(claim['record_digest']),
            operation_record_digest=str(operation['record_digest']),
            sample_record_digest=str(sample['record_digest']),
        )

    @staticmethod
    def _update_sample_locked(
        connection: sqlite3.Connection,
        *,
        old_operation: sqlite3.Row,
        old_sample: sqlite3.Row,
        new_sample: Mapping[str, Any],
        worker_id: str,
        fence_epoch: int,
        now: float,
    ) -> None:
        cursor = connection.execute(
            '''
            UPDATE gazebo_monitor_room_samples
            SET state = :state,
                preflight_digest = :preflight_digest,
                acceptance_digest = :acceptance_digest,
                terminal_evidence_digest = :terminal_evidence_digest,
                send_intent_at = :send_intent_at,
                accepted_at = :accepted_at,
                terminal_at = :terminal_at,
                result_code = :result_code,
                updated_at = :updated_at,
                record_digest = :record_digest
            WHERE operation_id = :operation_id
              AND sample_index = :sample_index
              AND goal_uuid = :goal_uuid
              AND state = :old_state
              AND record_digest = :old_digest
              AND EXISTS (
                  SELECT 1 FROM gazebo_monitor_room_operations AS operation
                  WHERE operation.operation_id = :operation_id
                    AND operation.state = :old_operation_state
                    AND operation.current_sample_index = :old_current_index
                    AND operation.fence_epoch = :expected_fence
                    AND operation.lease_owner = :worker_id
                    AND operation.lease_expires_at > :now
                    AND operation.record_digest = :old_operation_digest
              )
            ''',
            {
                **new_sample,
                'old_state': old_sample['state'],
                'old_digest': old_sample['record_digest'],
                'old_operation_state': old_operation['state'],
                'old_current_index': old_operation['current_sample_index'],
                'expected_fence': fence_epoch,
                'worker_id': worker_id,
                'now': now,
                'old_operation_digest': old_operation['record_digest'],
            },
        )
        if cursor.rowcount != 1:
            raise GazeboMonitorRoomConflictError(
                'sample changed during goal transition'
            )

    @staticmethod
    def _update_operation_locked(
        connection: sqlite3.Connection,
        *,
        old_operation: sqlite3.Row,
        new_operation: Mapping[str, Any],
        worker_id: str,
        fence_epoch: int,
        now: float,
    ) -> None:
        cursor = connection.execute(
            '''
            UPDATE gazebo_monitor_room_operations
            SET current_sample_index = :current_sample_index,
                samples_reached = :samples_reached,
                state = :state,
                terminal_code = :terminal_code,
                cancel_request_id = :cancel_request_id,
                cancel_fingerprint = :cancel_fingerprint,
                cancel_origin_state = :cancel_origin_state,
                updated_at = :updated_at,
                record_digest = :record_digest
            WHERE operation_id = :operation_id
              AND state = :old_state
              AND current_sample_index = :old_current_index
              AND samples_reached = :old_samples_reached
              AND fence_epoch = :expected_fence
              AND lease_owner = :worker_id
              AND lease_expires_at > :now
              AND record_digest = :old_digest
            ''',
            {
                **new_operation,
                'old_state': old_operation['state'],
                'old_current_index': old_operation['current_sample_index'],
                'old_samples_reached': old_operation['samples_reached'],
                'expected_fence': fence_epoch,
                'worker_id': worker_id,
                'now': now,
                'old_digest': old_operation['record_digest'],
            },
        )
        if cursor.rowcount != 1:
            raise GazeboMonitorRoomConflictError(
                'operation changed during goal transition'
            )

    @staticmethod
    def _changed_sample(
        old_sample: sqlite3.Row,
        *,
        state: str,
        now: float,
        preflight_digest: Optional[str] = None,
        acceptance_digest: Optional[str] = None,
        terminal_evidence_digest: Optional[str] = None,
        send_intent_at: Optional[float] = None,
        accepted_at: Optional[float] = None,
        terminal_at: Optional[float] = None,
        result_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        values = dict(old_sample)
        values.update(
            {
                'state': state,
                'preflight_digest': (
                    old_sample['preflight_digest']
                    if preflight_digest is None
                    else preflight_digest
                ),
                'acceptance_digest': (
                    old_sample['acceptance_digest']
                    if acceptance_digest is None
                    else acceptance_digest
                ),
                'terminal_evidence_digest': (
                    old_sample['terminal_evidence_digest']
                    if terminal_evidence_digest is None
                    else terminal_evidence_digest
                ),
                'send_intent_at': (
                    old_sample['send_intent_at']
                    if send_intent_at is None
                    else send_intent_at
                ),
                'accepted_at': (
                    old_sample['accepted_at']
                    if accepted_at is None
                    else accepted_at
                ),
                'terminal_at': terminal_at,
                'result_code': result_code,
                'updated_at': now,
            }
        )
        values['record_digest'] = _sample_digest(values)
        return values

    @staticmethod
    def _changed_operation(
        old_operation: sqlite3.Row,
        *,
        state: str,
        now: float,
        terminal_code: Optional[str] = None,
        current_sample_index: Optional[int] = None,
        samples_reached: Optional[int] = None,
        cancel_request_id: Optional[str] = None,
        cancel_fingerprint: Optional[str] = None,
        cancel_origin_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        values = dict(old_operation)
        values.update(
            {
                'state': state,
                'terminal_code': terminal_code,
                'current_sample_index': (
                    old_operation['current_sample_index']
                    if current_sample_index is None
                    else current_sample_index
                ),
                'samples_reached': (
                    old_operation['samples_reached']
                    if samples_reached is None
                    else samples_reached
                ),
                'cancel_request_id': (
                    old_operation['cancel_request_id']
                    if cancel_request_id is None
                    else cancel_request_id
                ),
                'cancel_fingerprint': (
                    old_operation['cancel_fingerprint']
                    if cancel_fingerprint is None
                    else cancel_fingerprint
                ),
                'cancel_origin_state': (
                    old_operation['cancel_origin_state']
                    if cancel_origin_state is None
                    else cancel_origin_state
                ),
                'updated_at': now,
            }
        )
        values['record_digest'] = _operation_digest(values)
        return values

    def _single_transition(
        self,
        transition: GoalTransition,
        *,
        now: float,
        target_operation_state: str,
        target_sample_state: str,
        event_type: str,
        terminal_code: Optional[str] = None,
        sample_result_code: Optional[str] = None,
        preflight_digest: Optional[str] = None,
        acceptance_digest: Optional[str] = None,
        terminal_evidence_digest: Optional[str] = None,
        terminal_at: Optional[float] = None,
        evidence_digest: Optional[str] = None,
    ) -> OperationObservation:
        transition = _canonical_goal_transition(transition)
        with self._write_transaction() as connection:
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            old_operation, old_sample = self._assert_goal_transition_locked(
                connection, transition, normalized_now
            )
            new_sample = self._changed_sample(
                old_sample,
                state=target_sample_state,
                now=normalized_now,
                preflight_digest=preflight_digest,
                acceptance_digest=acceptance_digest,
                terminal_evidence_digest=terminal_evidence_digest,
                send_intent_at=(
                    normalized_now
                    if target_sample_state == 'send_intent'
                    else None
                ),
                accepted_at=(
                    normalized_now
                    if target_sample_state == 'navigating'
                    else None
                ),
                terminal_at=terminal_at,
                result_code=sample_result_code,
            )
            new_operation = self._changed_operation(
                old_operation,
                state=target_operation_state,
                now=normalized_now,
                terminal_code=terminal_code,
            )
            self._update_sample_locked(
                connection,
                old_operation=old_operation,
                old_sample=old_sample,
                new_sample=new_sample,
                worker_id=transition.worker_id,
                fence_epoch=transition.fence_epoch,
                now=normalized_now,
            )
            self._update_operation_locked(
                connection,
                old_operation=old_operation,
                new_operation=new_operation,
                worker_id=transition.worker_id,
                fence_epoch=transition.fence_epoch,
                now=normalized_now,
            )
            stored_operation = self._load_operation_locked(
                connection, transition.operation_id
            )
            stored_sample = self._load_sample_locked(
                connection,
                transition.operation_id,
                transition.sample_index,
            )
            self._append_event_locked(
                connection,
                operation=stored_operation,
                event_type=event_type,
                recorded_at=normalized_now,
                worker_id=transition.worker_id,
                sample=stored_sample,
                from_operation_state=old_operation['state'],
                from_sample_state=old_sample['state'],
                code=terminal_code or sample_result_code,
                evidence_digest=evidence_digest,
            )
            return self._observation_locked(
                connection, stored_operation
            )

    def begin_preflight(
        self,
        transition: GoalTransition,
        *,
        now: float,
    ) -> OperationObservation:
        """Move a prepared first sample into read-only preflight."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'prepared'
            or transition.expected_sample_state != 'pending'
        ):
            raise GazeboMonitorRoomConflictError(
                'begin-preflight state is invalid'
            )
        normalized_now = _timestamp(now, 'recorded_at')
        observation = self.observe(transition.operation_id)
        if normalized_now >= observation.deadline:
            raise GazeboMonitorRoomDeadlineError(
                'operation deadline prevents preflight'
            )
        return self._single_transition(
            transition,
            now=normalized_now,
            target_operation_state='preflighting',
            target_sample_state='preflighting',
            event_type='preflight_started',
        )

    def record_send_intent(
        self,
        transition: GoalTransition,
        *,
        preflight_digest: str,
        now: float,
    ) -> OperationObservation:
        """Durably record intent before any future Nav2 send call."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'preflighting'
            or transition.expected_sample_state != 'preflighting'
        ):
            raise GazeboMonitorRoomConflictError(
                'send-intent state is invalid'
            )
        normalized_preflight = _digest(
            preflight_digest, 'preflight_digest'
        )
        normalized_now = _timestamp(now, 'recorded_at')
        observation = self.observe(transition.operation_id)
        if normalized_now >= observation.deadline:
            raise GazeboMonitorRoomDeadlineError(
                'operation deadline prevents a new send intent'
            )
        return self._single_transition(
            transition,
            now=normalized_now,
            target_operation_state='send_intent',
            target_sample_state='send_intent',
            event_type='send_intent_recorded',
            preflight_digest=normalized_preflight,
            evidence_digest=normalized_preflight,
        )

    def assert_start_ready(
        self,
        transition: GoalTransition,
        *,
        now: float,
    ) -> OperationObservation:
        """Assert exact send-intent readiness without renewing authority."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'send_intent'
            or transition.expected_sample_state != 'send_intent'
        ):
            raise GazeboMonitorRoomConflictError(
                'start-ready state is invalid'
            )
        with self._write_transaction() as connection:
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            operation, _sample = self._assert_goal_transition_locked(
                connection, transition, normalized_now
            )
            if normalized_now >= float(operation['deadline']):
                raise GazeboMonitorRoomDeadlineError(
                    'operation deadline prevents start'
                )
            return self._observation_locked(connection, operation)

    def claim_start_dispatch(
        self,
        transition: GoalTransition,
        *,
        start_fingerprint: str,
        binding_digest: str,
        preflight_digest: str,
        wire_payload_digest: str,
        now: float,
    ) -> bool:
        """Claim the one permitted start dispatch for an exact goal."""
        transition = _canonical_goal_transition(transition)
        with self._write_transaction() as connection:
            if self._existing_dispatch_claim_locked(
                connection,
                store_namespace=self.store_namespace,
                phase='start',
                transition=transition,
            ):
                return False
            if (
                transition.expected_operation_state != 'send_intent'
                or transition.expected_sample_state != 'send_intent'
            ):
                raise GazeboMonitorRoomConflictError(
                    'start dispatch state is invalid'
                )
            normalized_start = _digest(
                start_fingerprint, 'start_fingerprint'
            )
            normalized_binding = _digest(
                binding_digest, 'binding_digest'
            )
            normalized_preflight = _digest(
                preflight_digest, 'preflight_digest'
            )
            normalized_wire = _digest(
                wire_payload_digest, 'wire_payload_digest'
            )
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            operation, sample = self._assert_goal_transition_locked(
                connection, transition, normalized_now
            )
            if normalized_now >= float(operation['deadline']):
                raise GazeboMonitorRoomDeadlineError(
                    'operation deadline prevents start dispatch'
                )
            if (
                _binding_from_operation(operation).binding_digest
                != normalized_binding
                or sample['preflight_digest'] != normalized_preflight
            ):
                raise GazeboMonitorRoomConflictError(
                    'start dispatch binding conflicts with durable state'
                )
            return self._claim_dispatch_locked(
                connection,
                {
                    'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
                    'store_namespace': self.store_namespace,
                    'phase': 'start',
                    'operation_id': transition.operation_id,
                    'sample_index': transition.sample_index,
                    'goal_uuid': transition.goal_uuid,
                    'start_fingerprint': normalized_start,
                    'cancel_request_id': None,
                    'cancel_request_fingerprint': None,
                    'worker_id': transition.worker_id,
                    'fence_epoch': transition.fence_epoch,
                    'binding_digest': normalized_binding,
                    'preflight_digest': normalized_preflight,
                    'wire_payload_digest': normalized_wire,
                    'lease_expires_at': float(
                        operation['lease_expires_at']
                    ),
                    'claimed_at': normalized_now,
                },
            )

    def assert_start_dispatch_claim(
        self,
        transition: GoalTransition,
        *,
        start_fingerprint: str,
        binding_digest: str,
        preflight_digest: str,
        wire_payload_digest: str,
        now: float,
    ) -> DispatchClaimEvidence:
        """Prove an exact start claim under its still-current authority."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'send_intent'
            or transition.expected_sample_state != 'send_intent'
        ):
            raise GazeboMonitorRoomConflictError(
                'start dispatch assertion state is invalid'
            )
        normalized_start = _digest(
            start_fingerprint, 'start_fingerprint'
        )
        normalized_binding = _digest(binding_digest, 'binding_digest')
        normalized_preflight = _digest(
            preflight_digest, 'preflight_digest'
        )
        normalized_wire = _digest(
            wire_payload_digest, 'wire_payload_digest'
        )
        with self._read_transaction() as connection:
            normalized_now = self._assert_read_clock_locked(
                connection, now
            )
            operation, sample = self._assert_goal_transition_locked(
                connection, transition, normalized_now
            )
            if normalized_now >= float(operation['deadline']):
                raise GazeboMonitorRoomDeadlineError(
                    'operation deadline prevents start dispatch'
                )
            return self._dispatch_claim_evidence_locked(
                connection,
                phase='start',
                transition=transition,
                operation=operation,
                sample=sample,
                start_fingerprint=normalized_start,
                cancel_request_id=None,
                cancel_request_fingerprint=None,
                binding_digest=normalized_binding,
                preflight_digest=normalized_preflight,
                wire_payload_digest=normalized_wire,
                checked_at=normalized_now,
            )

    def record_navigating(
        self,
        transition: GoalTransition,
        *,
        acceptance_digest: str,
        now: float,
    ) -> OperationObservation:
        """Record explicit Nav2 acceptance for the stable goal UUID."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'send_intent'
            or transition.expected_sample_state != 'send_intent'
        ):
            raise GazeboMonitorRoomConflictError(
                'navigation acceptance state is invalid'
            )
        normalized_evidence = _digest(
            acceptance_digest, 'acceptance_digest'
        )
        return self._single_transition(
            transition,
            now=now,
            target_operation_state='navigating',
            target_sample_state='navigating',
            event_type='goal_accepted',
            acceptance_digest=normalized_evidence,
            evidence_digest=normalized_evidence,
        )

    def record_sample_succeeded(
        self,
        transition: GoalTransition,
        *,
        result_evidence_digest: str,
        now: float,
    ) -> OperationObservation:
        """Checkpoint one reached goal without claiming room coverage."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'navigating'
            or transition.expected_sample_state != 'navigating'
        ):
            raise GazeboMonitorRoomConflictError(
                'sample success state is invalid'
            )
        evidence = _digest(
            result_evidence_digest, 'result_evidence_digest'
        )
        with self._write_transaction() as connection:
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            old_operation, old_sample = self._assert_goal_transition_locked(
                connection, transition, normalized_now
            )
            reached = int(old_operation['samples_reached']) + 1
            final = reached == int(old_operation['sample_count'])
            old_sample_new = self._changed_sample(
                old_sample,
                state='succeeded',
                now=normalized_now,
                terminal_evidence_digest=evidence,
                terminal_at=normalized_now,
                result_code='nav2_goal_succeeded',
            )
            next_old: Optional[sqlite3.Row] = None
            next_new: Optional[Dict[str, Any]] = None
            if final:
                target_state = 'succeeded'
                terminal_code = 'all_navigation_samples_reached'
                current_index = old_operation['current_sample_index']
            else:
                current_index = int(old_operation['current_sample_index']) + 1
                next_old = self._load_sample_locked(
                    connection,
                    transition.operation_id,
                    current_index,
                )
                if next_old['state'] != 'pending':
                    raise GazeboMonitorRoomConflictError(
                        'next semantic sample is not pending'
                    )
                if normalized_now < float(old_operation['deadline']):
                    target_state = 'preflighting'
                    terminal_code = None
                    next_new = self._changed_sample(
                        next_old,
                        state='preflighting',
                        now=normalized_now,
                    )
                else:
                    target_state = 'failed'
                    terminal_code = 'deadline_expired'
                    next_new = self._changed_sample(
                        next_old,
                        state='failed',
                        now=normalized_now,
                        terminal_at=normalized_now,
                        result_code='deadline_expired',
                    )
            new_operation = self._changed_operation(
                old_operation,
                state=target_state,
                now=normalized_now,
                terminal_code=terminal_code,
                current_sample_index=current_index,
                samples_reached=reached,
            )
            self._update_sample_locked(
                connection,
                old_operation=old_operation,
                old_sample=old_sample,
                new_sample=old_sample_new,
                worker_id=transition.worker_id,
                fence_epoch=transition.fence_epoch,
                now=normalized_now,
            )
            if next_old is not None and next_new is not None:
                self._update_sample_locked(
                    connection,
                    old_operation=old_operation,
                    old_sample=next_old,
                    new_sample=next_new,
                    worker_id=transition.worker_id,
                    fence_epoch=transition.fence_epoch,
                    now=normalized_now,
                )
            self._update_operation_locked(
                connection,
                old_operation=old_operation,
                new_operation=new_operation,
                worker_id=transition.worker_id,
                fence_epoch=transition.fence_epoch,
                now=normalized_now,
            )
            stored_operation = self._load_operation_locked(
                connection, transition.operation_id
            )
            stored_old_sample = self._load_sample_locked(
                connection,
                transition.operation_id,
                transition.sample_index,
            )
            self._append_event_locked(
                connection,
                operation=stored_operation,
                event_type='sample_reached',
                recorded_at=normalized_now,
                worker_id=transition.worker_id,
                sample=stored_old_sample,
                from_operation_state=old_operation['state'],
                from_sample_state=old_sample['state'],
                code='nav2_goal_succeeded',
                evidence_digest=evidence,
            )
            if next_old is not None:
                stored_next = self._load_sample_locked(
                    connection,
                    transition.operation_id,
                    current_index,
                )
                self._append_event_locked(
                    connection,
                    operation=stored_operation,
                    event_type=(
                        'next_preflight_started'
                        if target_state == 'preflighting'
                        else 'deadline_failed_closed'
                    ),
                    recorded_at=normalized_now,
                    worker_id=transition.worker_id,
                    sample=stored_next,
                    from_operation_state=target_state,
                    from_sample_state=next_old['state'],
                    code=terminal_code,
                )
            return self._observation_locked(
                connection, stored_operation
            )

    def request_cancel(
        self,
        request: CancelOperation,
        *,
        now: float,
    ) -> OperationObservation:
        """Linearize cancellation against success using one durable CAS."""
        request = _canonical_cancel_operation(request)
        transition = request.transition
        if transition.expected_operation_state not in {
            'prepared', 'preflighting', 'send_intent', 'navigating'
        }:
            raise GazeboMonitorRoomConflictError(
                'cancel origin state is invalid'
            )
        with self._write_transaction() as connection:
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            operation = self._load_operation_locked(
                connection, transition.operation_id
            )
            if operation['cancel_request_id'] == request.cancel_request_id:
                if (
                    operation['cancel_fingerprint']
                    != request.request_fingerprint
                ):
                    raise GazeboMonitorRoomConflictError(
                        'cancel request conflicts with durable intent'
                    )
                return replace(
                    self._observation_locked(connection, operation),
                    replayed=True,
                )
            owner = connection.execute(
                '''
                SELECT operation_id FROM gazebo_monitor_room_operations
                WHERE cancel_request_id = ?
                ''',
                (request.cancel_request_id,),
            ).fetchone()
            if owner is not None:
                raise GazeboMonitorRoomConflictError(
                    'cancel request identity is already used'
                )
            if operation['state'] in TERMINAL_STATES:
                return self._observation_locked(connection, operation)
            old_operation, old_sample = self._assert_goal_transition_locked(
                connection, transition, normalized_now
            )
            new_sample = self._changed_sample(
                old_sample,
                state='cancel_requested',
                now=normalized_now,
            )
            new_operation = self._changed_operation(
                old_operation,
                state='cancel_requested',
                now=normalized_now,
                cancel_request_id=request.cancel_request_id,
                cancel_fingerprint=request.request_fingerprint,
                cancel_origin_state=old_operation['state'],
            )
            self._update_sample_locked(
                connection,
                old_operation=old_operation,
                old_sample=old_sample,
                new_sample=new_sample,
                worker_id=transition.worker_id,
                fence_epoch=transition.fence_epoch,
                now=normalized_now,
            )
            self._update_operation_locked(
                connection,
                old_operation=old_operation,
                new_operation=new_operation,
                worker_id=transition.worker_id,
                fence_epoch=transition.fence_epoch,
                now=normalized_now,
            )
            stored_operation = self._load_operation_locked(
                connection, transition.operation_id
            )
            stored_sample = self._load_sample_locked(
                connection,
                transition.operation_id,
                transition.sample_index,
            )
            self._append_event_locked(
                connection,
                operation=stored_operation,
                event_type='cancel_requested',
                recorded_at=normalized_now,
                worker_id=transition.worker_id,
                sample=stored_sample,
                from_operation_state=old_operation['state'],
                from_sample_state=old_sample['state'],
                code=request.reason_code,
            )
            return self._observation_locked(
                connection, stored_operation
            )

    def assert_cancel_ready(
        self,
        transition: GoalTransition,
        *,
        cancel_request_id: str,
        now: float,
    ) -> OperationObservation:
        """Assert an exact current cancel target, even after its deadline."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'cancel_requested'
            or transition.expected_sample_state != 'cancel_requested'
        ):
            raise GazeboMonitorRoomConflictError(
                'cancel-ready state is invalid'
            )
        normalized_cancel = _identifier(
            cancel_request_id, 'cancel_request_id'
        )
        with self._write_transaction() as connection:
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            operation, _sample = self._assert_cancel_ready_locked(
                connection,
                transition,
                normalized_cancel,
                normalized_now,
            )
            return self._observation_locked(connection, operation)

    def claim_cancel_dispatch(
        self,
        transition: GoalTransition,
        *,
        cancel_request_id: str,
        request_fingerprint: str,
        binding_digest: str,
        wire_payload_digest: str,
        now: float,
    ) -> bool:
        """Claim the one permitted wire cancel for an exact durable intent."""
        transition = _canonical_goal_transition(transition)
        with self._write_transaction() as connection:
            if self._existing_dispatch_claim_locked(
                connection,
                store_namespace=self.store_namespace,
                phase='cancel',
                transition=transition,
            ):
                return False
            if (
                transition.expected_operation_state != 'cancel_requested'
                or transition.expected_sample_state != 'cancel_requested'
            ):
                raise GazeboMonitorRoomConflictError(
                    'cancel dispatch state is invalid'
                )
            normalized_cancel = _identifier(
                cancel_request_id, 'cancel_request_id'
            )
            normalized_request = _digest(
                request_fingerprint, 'request_fingerprint'
            )
            normalized_binding = _digest(
                binding_digest, 'binding_digest'
            )
            normalized_wire = _digest(
                wire_payload_digest, 'wire_payload_digest'
            )
            normalized_now = self._assert_write_clock_locked(
                connection, now
            )
            operation, _sample = self._assert_cancel_ready_locked(
                connection,
                transition,
                normalized_cancel,
                normalized_now,
            )
            if (
                _binding_from_operation(operation).binding_digest
                != normalized_binding
            ):
                raise GazeboMonitorRoomConflictError(
                    'cancel dispatch binding conflicts with durable state'
                )
            return self._claim_dispatch_locked(
                connection,
                {
                    'schema_version': GAZEBO_MONITOR_ROOM_SCHEMA_VERSION,
                    'store_namespace': self.store_namespace,
                    'phase': 'cancel',
                    'operation_id': transition.operation_id,
                    'sample_index': transition.sample_index,
                    'goal_uuid': transition.goal_uuid,
                    'start_fingerprint': None,
                    'cancel_request_id': normalized_cancel,
                    'cancel_request_fingerprint': normalized_request,
                    'worker_id': transition.worker_id,
                    'fence_epoch': transition.fence_epoch,
                    'binding_digest': normalized_binding,
                    'preflight_digest': None,
                    'wire_payload_digest': normalized_wire,
                    'lease_expires_at': float(
                        operation['lease_expires_at']
                    ),
                    'claimed_at': normalized_now,
                },
            )

    def assert_cancel_dispatch_claim(
        self,
        transition: GoalTransition,
        *,
        cancel_request_id: str,
        request_fingerprint: str,
        binding_digest: str,
        wire_payload_digest: str,
        now: float,
    ) -> DispatchClaimEvidence:
        """Prove an exact cancel claim under a current cancellation lease."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'cancel_requested'
            or transition.expected_sample_state != 'cancel_requested'
        ):
            raise GazeboMonitorRoomConflictError(
                'cancel dispatch assertion state is invalid'
            )
        normalized_cancel = _identifier(
            cancel_request_id, 'cancel_request_id'
        )
        normalized_request = _digest(
            request_fingerprint, 'request_fingerprint'
        )
        normalized_binding = _digest(binding_digest, 'binding_digest')
        normalized_wire = _digest(
            wire_payload_digest, 'wire_payload_digest'
        )
        with self._read_transaction() as connection:
            normalized_now = self._assert_read_clock_locked(
                connection, now
            )
            operation, sample = self._assert_cancel_ready_locked(
                connection,
                transition,
                normalized_cancel,
                normalized_now,
            )
            return self._dispatch_claim_evidence_locked(
                connection,
                phase='cancel',
                transition=transition,
                operation=operation,
                sample=sample,
                start_fingerprint=None,
                cancel_request_id=normalized_cancel,
                cancel_request_fingerprint=normalized_request,
                binding_digest=normalized_binding,
                preflight_digest=None,
                wire_payload_digest=normalized_wire,
                checked_at=normalized_now,
            )

    def record_canceled(
        self,
        transition: GoalTransition,
        *,
        terminal_evidence_digest: Optional[str],
        now: float,
    ) -> OperationObservation:
        """Record cancellation only after explicit terminal observation."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'cancel_requested'
            or transition.expected_sample_state != 'cancel_requested'
        ):
            raise GazeboMonitorRoomConflictError(
                'cancel terminal state is invalid'
            )
        observation = self.observe(transition.operation_id)
        evidence = terminal_evidence_digest
        if observation.state != 'cancel_requested':
            raise GazeboMonitorRoomConflictError(
                'cancel terminal compare-and-swap conflict'
            )
        with self._read_transaction() as connection:
            operation = self._load_operation_locked(
                connection, transition.operation_id
            )
            sent = operation['cancel_origin_state'] in {
                'send_intent', 'navigating'
            }
        if sent:
            evidence = _digest(evidence, 'terminal_evidence_digest')
        elif evidence is not None:
            evidence = _digest(evidence, 'terminal_evidence_digest')
        return self._single_transition(
            transition,
            now=now,
            target_operation_state='canceled',
            target_sample_state='canceled',
            event_type='goal_canceled',
            terminal_code='nav2_goal_canceled',
            sample_result_code='nav2_goal_canceled',
            terminal_evidence_digest=evidence,
            terminal_at=_timestamp(now, 'terminal_at'),
            evidence_digest=evidence,
        )

    def record_cancel_unknown(
        self,
        transition: GoalTransition,
        *,
        code: str,
        evidence_digest: str,
        now: float,
    ) -> OperationObservation:
        """Fail closed when cancellation terminality cannot be observed."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state != 'cancel_requested'
            or transition.expected_sample_state != 'cancel_requested'
        ):
            raise GazeboMonitorRoomConflictError(
                'cancel-unknown state is invalid'
            )
        normalized_code = _identifier(code, 'code')
        normalized_evidence = _digest(evidence_digest, 'evidence_digest')
        return self._single_transition(
            transition,
            now=now,
            target_operation_state='cancel_unknown',
            target_sample_state='cancel_unknown',
            event_type='cancel_became_unknown',
            terminal_code=normalized_code,
            sample_result_code=normalized_code,
            terminal_evidence_digest=normalized_evidence,
            terminal_at=_timestamp(now, 'terminal_at'),
            evidence_digest=normalized_evidence,
        )

    def record_delivery_unknown(
        self,
        transition: GoalTransition,
        *,
        code: str,
        evidence_digest: str,
        now: float,
    ) -> OperationObservation:
        """Record an ambiguous send/result crash window without resending."""
        transition = _canonical_goal_transition(transition)
        if (
            transition.expected_operation_state not in {
                'send_intent', 'navigating'
            }
            or transition.expected_sample_state
            != transition.expected_operation_state
        ):
            raise GazeboMonitorRoomConflictError(
                'delivery-unknown state is invalid'
            )
        normalized_code = _identifier(code, 'code')
        normalized_evidence = _digest(evidence_digest, 'evidence_digest')
        return self._single_transition(
            transition,
            now=now,
            target_operation_state='delivery_unknown',
            target_sample_state='delivery_unknown',
            event_type='delivery_became_unknown',
            terminal_code=normalized_code,
            sample_result_code=normalized_code,
            terminal_evidence_digest=normalized_evidence,
            terminal_at=_timestamp(now, 'terminal_at'),
            evidence_digest=normalized_evidence,
        )

    def record_failed(
        self,
        transition: GoalTransition,
        *,
        code: str,
        evidence_digest: Optional[str],
        now: float,
    ) -> OperationObservation:
        """Record a known failure without claiming cancellation or coverage."""
        transition = _canonical_goal_transition(transition)
        if transition.expected_operation_state not in {
            'prepared', 'preflighting', 'send_intent', 'navigating'
        }:
            raise GazeboMonitorRoomConflictError(
                'failure origin state is invalid'
            )
        expected_sample = {
            'prepared': 'pending',
            'preflighting': 'preflighting',
            'send_intent': 'send_intent',
            'navigating': 'navigating',
        }[transition.expected_operation_state]
        if transition.expected_sample_state != expected_sample:
            raise GazeboMonitorRoomConflictError(
                'failure sample state is invalid'
            )
        normalized_code = _identifier(code, 'code')
        if transition.expected_operation_state in {
            'send_intent', 'navigating'
        }:
            normalized_evidence = _digest(
                evidence_digest, 'evidence_digest'
            )
        else:
            normalized_evidence = (
                None
                if evidence_digest is None
                else _digest(evidence_digest, 'evidence_digest')
            )
        return self._single_transition(
            transition,
            now=now,
            target_operation_state='failed',
            target_sample_state='failed',
            event_type='operation_failed',
            terminal_code=normalized_code,
            sample_result_code=normalized_code,
            terminal_evidence_digest=normalized_evidence,
            terminal_at=_timestamp(now, 'terminal_at'),
            evidence_digest=normalized_evidence,
        )

    def transition_token(
        self,
        operation_id: str,
        *,
        worker_id: str,
    ) -> GoalTransition:
        """Build a strict CAS token from one validated current snapshot."""
        normalized_worker = _identifier(worker_id, 'worker_id')
        observation = self.observe(operation_id)
        if observation.fence_epoch < 1:
            raise GazeboMonitorRoomLeaseError(
                'operation has no acquired lease'
            )
        return GoalTransition(
            operation_id=observation.operation_id,
            worker_id=normalized_worker,
            fence_epoch=observation.fence_epoch,
            sample_index=observation.current_sample_index,
            goal_uuid=observation.current_goal_uuid,
            expected_operation_state=observation.state,
            expected_sample_state=observation.current_sample_state,
        )
