"""
Durable sidecar for reconciling ambiguous Nav2 goal delivery.

The core monitor-room operation remains immutable in ``delivery_unknown`` or
``cancel_unknown``.  This separate database may only collect evidence about
the exact stable goal UUID, record explicitly claimed one-shot attempts, and
mint a safety-only full-drop certificate after both an authoritative terminal
observation and independent quiescence evidence.  It never sends a goal,
changes the core database, claims operation success, or releases core
admission by itself.
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
import time
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from malbut_gazebo.gazebo_monitor_room_store import (
    GazeboMonitorRoomStore,
    OperationEvent,
    OperationObservation,
    PrivateOperationBinding,
    PrivateStoredSample,
)


NAV2_RECONCILE_SCHEMA_VERSION = 1
NAV2_RECONCILE_MAX_CASES = 4096
NAV2_RECONCILE_MAX_ATTEMPTS = 4096
NAV2_RECONCILE_MAX_EVENTS = 8192
NAV2_RECONCILE_MAX_LEASE_SECONDS = 300.0

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_GOAL_UUID = re.compile(r'^[0-9a-f]{32}$')
_STORE_NAMESPACE = re.compile(r'^[0-9a-f]{32}$')
_HOST_BOOT_ID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$'
)
_ZERO_DIGEST = '0' * 64
_MAX_FENCE = 9_223_372_036_854_775_806
_SOURCE_STATES = frozenset({'delivery_unknown', 'cancel_unknown'})
_CASE_STATES = frozenset(
    {
        'blocked_unresolved',
        'blocked_terminal_observed',
        'blocked_conflict',
        'released_quiescent',
    }
)
_ATTEMPT_KINDS = frozenset({'observe', 'cancel', 'quiescence'})
_GOAL_STATUSES = frozenset(
    {
        'accepted',
        'active',
        'succeeded',
        'aborted',
        'rejected',
        'canceled',
        'unknown',
    }
)
_TERMINAL_GOAL_STATUSES = frozenset(
    {'succeeded', 'aborted', 'canceled'}
)
_CONTRACT_DIGEST = hashlib.sha256(
    b'malbut-gazebo-monitor-room-nav2-reconcile-sidecar-v1'
).hexdigest()


class GazeboMonitorRoomNav2ReconcileStoreError(RuntimeError):
    """Base error for the private Nav2 reconciliation sidecar."""


class GazeboMonitorRoomNav2ReconcileValidationError(
    GazeboMonitorRoomNav2ReconcileStoreError, ValueError
):
    """Raised when a sidecar value is malformed or weakly typed."""


class GazeboMonitorRoomNav2ReconcileSchemaError(
    GazeboMonitorRoomNav2ReconcileStoreError
):
    """Raised when the exact private schema or row attestations fail."""


class GazeboMonitorRoomNav2ReconcileConflictError(
    GazeboMonitorRoomNav2ReconcileStoreError
):
    """Raised for an idempotency, source-anchor, or state conflict."""


class GazeboMonitorRoomNav2ReconcileNotFoundError(
    GazeboMonitorRoomNav2ReconcileStoreError
):
    """Raised when a reconciliation case does not exist."""


class GazeboMonitorRoomNav2ReconcileLeaseError(
    GazeboMonitorRoomNav2ReconcileConflictError
):
    """Raised when a reconciliation lease is absent, busy, or expired."""


class GazeboMonitorRoomNav2ReconcileFenceError(
    GazeboMonitorRoomNav2ReconcileConflictError
):
    """Raised when a stale reconciliation fence is presented."""


class GazeboMonitorRoomNav2ReconcileClockRollbackError(
    GazeboMonitorRoomNav2ReconcileStoreError
):
    """Raised when BOOTTIME precedes durable sidecar history."""


class GazeboMonitorRoomNav2ReconcileBootIdentityError(
    GazeboMonitorRoomNav2ReconcileStoreError
):
    """Raised without exposing host content when boot identity is invalid."""


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            f'{name} is invalid'
        )
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            f'{name} is invalid'
        )
    return value


def _goal_uuid(value: Any) -> str:
    if (
        type(value) is not str
        or _GOAL_UUID.fullmatch(value) is None
        or value == '0' * 32
    ):
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            'goal_uuid is invalid'
        )
    return value


def _store_namespace(value: Any) -> str:
    if type(value) is not str or _STORE_NAMESPACE.fullmatch(value) is None:
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            'store_namespace is invalid'
        )
    return value


def _host_boot_id(value: Any) -> str:
    if type(value) is not str or _HOST_BOOT_ID.fullmatch(value) is None:
        raise GazeboMonitorRoomNav2ReconcileBootIdentityError(
            'host boot identity is unavailable'
        )
    return value


def _read_host_boot_id() -> str:
    try:
        with open(
            '/proc/sys/kernel/random/boot_id',
            'r',
            encoding='ascii',
        ) as stream:
            value = stream.read(38)
    except (OSError, UnicodeError):
        raise GazeboMonitorRoomNav2ReconcileBootIdentityError(
            'host boot identity is unavailable'
        ) from None
    if value.endswith('\n'):
        value = value[:-1]
    return _host_boot_id(value)


def _boottime() -> float:
    try:
        value = time.clock_gettime(time.CLOCK_BOOTTIME)
    except Exception:
        raise GazeboMonitorRoomNav2ReconcileClockRollbackError(
            'host clock is unavailable'
        ) from None
    return _timestamp(value, 'now')


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            f'{name} is invalid'
        )
    return value


def _timestamp(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            f'{name} is invalid'
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            f'{name} is invalid'
        )
    return 0.0 if normalized == 0.0 else normalized


def _optional_timestamp(value: Any, name: str) -> Optional[float]:
    return None if value is None else _timestamp(value, name)


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
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            'canonical value is invalid'
        ) from None
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReconcileSourceAnchor:
    """Coordinate-free immutable activation snapshot from one core unknown."""

    store_namespace: str = field(repr=False)
    operation_id: str
    robot_id: str
    source_state: str
    terminal_code: str
    sample_index: int
    goal_uuid: str
    binding_digest: str = field(repr=False)
    source_fence_epoch: int
    terminal_event_seq: int
    terminal_event_type: str
    terminal_event_recorded_at: float
    terminal_event_evidence_digest: str = field(repr=False)
    terminal_event_digest: str = field(repr=False)

    def __post_init__(self) -> None:
        """Validate the exact immutable source anchor shape."""
        _store_namespace(self.store_namespace)
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.robot_id, 'robot_id')
        if type(self.source_state) is not str or (
            self.source_state not in _SOURCE_STATES
        ):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'source_state is invalid'
            )
        _identifier(self.terminal_code, 'terminal_code')
        _integer(self.sample_index, 'sample_index', 0, 63)
        _goal_uuid(self.goal_uuid)
        _digest(self.binding_digest, 'binding_digest')
        _integer(self.source_fence_epoch, 'source_fence_epoch', 1, _MAX_FENCE)
        _integer(
            self.terminal_event_seq,
            'terminal_event_seq',
            1,
            1024,
        )
        expected_type = (
            'delivery_became_unknown'
            if self.source_state == 'delivery_unknown'
            else 'cancel_became_unknown'
        )
        if (
            type(self.terminal_event_type) is not str
            or self.terminal_event_type != expected_type
        ):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'terminal_event_type is invalid'
            )
        object.__setattr__(
            self,
            'terminal_event_recorded_at',
            _timestamp(
                self.terminal_event_recorded_at,
                'terminal_event_recorded_at',
            ),
        )
        _digest(
            self.terminal_event_evidence_digest,
            'terminal_event_evidence_digest',
        )
        _digest(self.terminal_event_digest, 'terminal_event_digest')

    @property
    def anchor_digest(self) -> str:
        """Bind every source identity without persisting coordinates."""
        return _hash_json(
            {
                'contract': 'malbut-nav2-reconcile-source-anchor-v1',
                'store_namespace': self.store_namespace,
                'operation_id': self.operation_id,
                'robot_id': self.robot_id,
                'source_state': self.source_state,
                'terminal_code': self.terminal_code,
                'sample_index': self.sample_index,
                'goal_uuid': self.goal_uuid,
                'binding_digest': self.binding_digest,
                'source_fence_epoch': self.source_fence_epoch,
                'terminal_event_seq': self.terminal_event_seq,
                'terminal_event_type': self.terminal_event_type,
                'terminal_event_recorded_at': (
                    self.terminal_event_recorded_at
                ),
                'terminal_event_evidence_digest': (
                    self.terminal_event_evidence_digest
                ),
                'terminal_event_digest': self.terminal_event_digest,
            }
        )


@dataclass(frozen=True)
class Nav2ReconcileObservation:
    """Public safety state without reinterpreting the core operation."""

    operation_id: str
    robot_id: str
    source_state: str
    terminal_code: str
    goal_uuid: str
    source_anchor_digest: str
    state: str
    fence_epoch: int
    lease_owner: Optional[str]
    lease_expires_at: Optional[float]
    terminal_status: Optional[str]
    terminal_evidence_digest: Optional[str]
    terminal_observed_at: Optional[float]
    quiescence_not_before: Optional[float]
    quiescence_evidence_digest: Optional[str]
    quiescence_observed_at: Optional[float]
    full_drop_certificate_digest: Optional[str]
    attempt_count: int
    event_count: int
    replayed: bool = False
    operation_success: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    core_admission_released: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Reject any observation that could overclaim safety or success."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.robot_id, 'robot_id')
        if type(self.source_state) is not str or (
            self.source_state not in _SOURCE_STATES
        ):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'source_state is invalid'
            )
        _identifier(self.terminal_code, 'terminal_code')
        _goal_uuid(self.goal_uuid)
        _digest(self.source_anchor_digest, 'source_anchor_digest')
        if type(self.state) is not str or self.state not in _CASE_STATES:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'state is invalid'
            )
        _integer(self.fence_epoch, 'fence_epoch', 0, _MAX_FENCE)
        if self.lease_owner is not None:
            _identifier(self.lease_owner, 'lease_owner')
        lease_expires = _optional_timestamp(
            self.lease_expires_at, 'lease_expires_at'
        )
        if (self.lease_owner is None) != (lease_expires is None):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'lease binding is invalid'
            )
        if self.lease_owner is not None and self.fence_epoch < 1:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'lease binding is invalid'
            )
        terminal_fields = (
            self.terminal_status,
            self.terminal_evidence_digest,
            self.terminal_observed_at,
            self.quiescence_not_before,
        )
        if self.terminal_status is None:
            if any(value is not None for value in terminal_fields):
                raise GazeboMonitorRoomNav2ReconcileValidationError(
                    'terminal binding is invalid'
                )
        else:
            if (
                type(self.terminal_status) is not str
                or self.terminal_status not in _TERMINAL_GOAL_STATUSES
            ):
                raise GazeboMonitorRoomNav2ReconcileValidationError(
                    'terminal_status is invalid'
                )
            _digest(
                self.terminal_evidence_digest,
                'terminal_evidence_digest',
            )
            terminal_at = _timestamp(
                self.terminal_observed_at, 'terminal_observed_at'
            )
            not_before = _timestamp(
                self.quiescence_not_before, 'quiescence_not_before'
            )
            if not_before < terminal_at:
                raise GazeboMonitorRoomNav2ReconcileValidationError(
                    'quiescence timing is invalid'
                )
        quiescence_fields = (
            self.quiescence_evidence_digest,
            self.quiescence_observed_at,
            self.full_drop_certificate_digest,
        )
        if self.state == 'released_quiescent':
            for value, name in zip(
                quiescence_fields,
                (
                    'quiescence_evidence_digest',
                    'quiescence_observed_at',
                    'full_drop_certificate_digest',
                ),
            ):
                if name.endswith('_digest'):
                    _digest(value, name)
                else:
                    _timestamp(value, name)
            if self.terminal_status is None:
                raise GazeboMonitorRoomNav2ReconcileValidationError(
                    'release terminal binding is invalid'
                )
        elif any(value is not None for value in quiescence_fields):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'quiescence binding is invalid'
            )
        _integer(
            self.attempt_count,
            'attempt_count',
            0,
            NAV2_RECONCILE_MAX_ATTEMPTS,
        )
        _integer(
            self.event_count,
            'event_count',
            1,
            NAV2_RECONCILE_MAX_EVENTS,
        )
        if type(self.replayed) is not bool:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'replayed is invalid'
            )

    @property
    def robot_blocked(self) -> bool:
        """Remain blocked until the safety-only full-drop certificate exists."""
        return self.state != 'released_quiescent'

    @property
    def terminal_goal_observed(self) -> bool:
        """Report exact goal terminality without claiming operation success."""
        return self.terminal_status in _TERMINAL_GOAL_STATUSES

    @property
    def safe_block_released(self) -> bool:
        """Report only sidecar safety release, never core admission release."""
        return self.state == 'released_quiescent'

    def to_public_dict(self) -> Dict[str, Any]:
        """Expose bounded non-claims and the exact reconciliation state."""
        return {
            'schema_version': NAV2_RECONCILE_SCHEMA_VERSION,
            'operation_id': self.operation_id,
            'robot_id': self.robot_id,
            'source_state': self.source_state,
            'terminal_code': self.terminal_code,
            'goal_uuid': self.goal_uuid,
            'source_anchor_digest': self.source_anchor_digest,
            'state': self.state,
            'fence_epoch': self.fence_epoch,
            'lease_expires_at': self.lease_expires_at,
            'terminal_status': self.terminal_status,
            'terminal_goal_observed': self.terminal_goal_observed,
            'full_drop_certificate_digest': (
                self.full_drop_certificate_digest
            ),
            'robot_blocked': self.robot_blocked,
            'safe_block_released': self.safe_block_released,
            'operation_success': False,
            'coverage_achieved': False,
            'core_admission_released': False,
            'replayed': self.replayed,
        }


@dataclass(frozen=True)
class Nav2ReconcileLeaseGrant:
    """One independent reconciliation lease and monotonic fence."""

    observation: Nav2ReconcileObservation
    worker_id: str
    fence_epoch: int
    lease_expires_at: float
    taken_over: bool

    def __post_init__(self) -> None:
        """Require an exact lease-to-observation binding."""
        if type(self.observation) is not Nav2ReconcileObservation:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'lease observation is invalid'
            )
        _identifier(self.worker_id, 'worker_id')
        _integer(self.fence_epoch, 'fence_epoch', 1, _MAX_FENCE)
        _timestamp(self.lease_expires_at, 'lease_expires_at')
        if (
            self.observation.lease_owner != self.worker_id
            or self.observation.fence_epoch != self.fence_epoch
            or self.observation.lease_expires_at != self.lease_expires_at
            or type(self.taken_over) is not bool
        ):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'lease grant binding is invalid'
            )


@dataclass(frozen=True)
class Nav2ReconcileAttemptToken:
    """Durable pre-call claim for one exact read or side effect attempt."""

    operation_id: str
    attempt_seq: int
    attempt_id: str
    kind: str
    worker_id: str
    fence_epoch: int
    lease_expires_at: float
    goal_uuid: str
    binding_digest: str = field(repr=False)
    request_fingerprint: str
    wire_payload_digest: Optional[str]
    claimed_at: float
    attempt_digest: str

    def __post_init__(self) -> None:
        """Validate one immutable exact-attempt token."""
        _identifier(self.operation_id, 'operation_id')
        _integer(
            self.attempt_seq,
            'attempt_seq',
            1,
            NAV2_RECONCILE_MAX_ATTEMPTS,
        )
        _identifier(self.attempt_id, 'attempt_id')
        if type(self.kind) is not str or self.kind not in _ATTEMPT_KINDS:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'attempt kind is invalid'
            )
        _identifier(self.worker_id, 'worker_id')
        _integer(self.fence_epoch, 'fence_epoch', 1, _MAX_FENCE)
        _timestamp(self.lease_expires_at, 'lease_expires_at')
        _goal_uuid(self.goal_uuid)
        _digest(self.binding_digest, 'binding_digest')
        _digest(self.request_fingerprint, 'request_fingerprint')
        if self.kind == 'cancel':
            _digest(self.wire_payload_digest, 'wire_payload_digest')
        elif self.wire_payload_digest is not None:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'wire_payload_digest is invalid'
            )
        _timestamp(self.claimed_at, 'claimed_at')
        _digest(self.attempt_digest, 'attempt_digest')


@dataclass(frozen=True)
class Nav2ReconcileAttemptClaim:
    """Tell a caller whether a one-shot attempt was newly persisted."""

    token: Nav2ReconcileAttemptToken
    claimed: bool

    def __post_init__(self) -> None:
        """Validate the exact one-shot claim result."""
        if (
            type(self.token) is not Nav2ReconcileAttemptToken
            or type(self.claimed) is not bool
        ):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'attempt claim is invalid'
            )


@dataclass(frozen=True)
class Nav2ReconcileEvent:
    """Append-only hash-chained reconciliation audit event."""

    operation_id: str
    event_seq: int
    event_type: str
    recorded_at: float
    worker_id: str
    fence_epoch: int
    lease_expires_at: Optional[float]
    attempt_id: Optional[str]
    status: Optional[str]
    evidence_digest: Optional[str]
    previous_event_digest: str
    event_digest: str

    def __post_init__(self) -> None:
        """Validate one bounded coordinate-free event."""
        _identifier(self.operation_id, 'operation_id')
        _integer(
            self.event_seq,
            'event_seq',
            1,
            NAV2_RECONCILE_MAX_EVENTS,
        )
        _identifier(self.event_type, 'event_type')
        _timestamp(self.recorded_at, 'recorded_at')
        _identifier(self.worker_id, 'worker_id')
        _integer(self.fence_epoch, 'fence_epoch', 0, _MAX_FENCE)
        lease_expires_at = _optional_timestamp(
            self.lease_expires_at, 'lease_expires_at'
        )
        if self.event_type in {
            'lease_acquired', 'lease_renewed', 'lease_taken_over'
        }:
            if (
                lease_expires_at is None
                or lease_expires_at <= self.recorded_at
                or self.fence_epoch < 1
            ):
                raise GazeboMonitorRoomNav2ReconcileValidationError(
                    'lease event binding is invalid'
                )
        elif lease_expires_at is not None:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'lease event binding is invalid'
            )
        if self.attempt_id is not None:
            _identifier(self.attempt_id, 'attempt_id')
        if self.status is not None:
            _identifier(self.status, 'status')
        if self.evidence_digest is not None:
            _digest(self.evidence_digest, 'evidence_digest')
        _digest(self.previous_event_digest, 'previous_event_digest')
        _digest(self.event_digest, 'event_digest')


_METADATA_SQL = '''
CREATE TABLE nav2_reconcile_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    core_store_namespace TEXT NOT NULL CHECK (
        length(core_store_namespace) = 32
        AND core_store_namespace NOT GLOB '*[^0-9a-f]*'
    ),
    host_boot_id TEXT NOT NULL CHECK (length(host_boot_id) = 36),
    contract_digest TEXT NOT NULL CHECK (length(contract_digest) = 64),
    quiescence_dwell_seconds REAL NOT NULL CHECK (
        quiescence_dwell_seconds >= 0.0
        AND quiescence_dwell_seconds <= 300.0
    )
)
'''

_CLOCK_SQL = '''
CREATE TABLE nav2_reconcile_clock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_now REAL NOT NULL CHECK (last_now >= 0.0),
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64)
)
'''

_CASES_SQL = '''
CREATE TABLE nav2_reconcile_cases (
    operation_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL,
    source_state TEXT NOT NULL CHECK (
        source_state IN ('delivery_unknown', 'cancel_unknown')
    ),
    terminal_code TEXT NOT NULL,
    sample_index INTEGER NOT NULL CHECK (
        sample_index >= 0 AND sample_index < 64
    ),
    goal_uuid TEXT NOT NULL CHECK (length(goal_uuid) = 32),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    source_fence_epoch INTEGER NOT NULL CHECK (source_fence_epoch >= 1),
    terminal_event_seq INTEGER NOT NULL CHECK (terminal_event_seq >= 1),
    terminal_event_type TEXT NOT NULL,
    terminal_event_recorded_at REAL NOT NULL CHECK (
        terminal_event_recorded_at >= 0.0
    ),
    terminal_event_evidence_digest TEXT NOT NULL CHECK (
        length(terminal_event_evidence_digest) = 64
    ),
    terminal_event_digest TEXT NOT NULL CHECK (
        length(terminal_event_digest) = 64
    ),
    source_anchor_digest TEXT NOT NULL UNIQUE CHECK (
        length(source_anchor_digest) = 64
    ),
    state TEXT NOT NULL CHECK (
        state IN (
            'blocked_unresolved', 'blocked_terminal_observed',
            'blocked_conflict', 'released_quiescent'
        )
    ),
    fence_epoch INTEGER NOT NULL CHECK (fence_epoch >= 0),
    lease_owner TEXT,
    lease_expires_at REAL,
    terminal_status TEXT,
    terminal_evidence_digest TEXT,
    terminal_observed_at REAL,
    quiescence_not_before REAL,
    quiescence_evidence_digest TEXT,
    quiescence_observed_at REAL,
    full_drop_certificate_digest TEXT,
    attempt_count INTEGER NOT NULL CHECK (
        attempt_count >= 0 AND attempt_count <= 4096
    ),
    event_count INTEGER NOT NULL CHECK (
        event_count >= 1 AND event_count <= 8192
    ),
    last_event_digest TEXT NOT NULL CHECK (length(last_event_digest) = 64),
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    CHECK (lease_owner IS NULL OR fence_epoch >= 1),
    CHECK (
        (terminal_status IS NULL
         AND terminal_evidence_digest IS NULL
         AND terminal_observed_at IS NULL
         AND quiescence_not_before IS NULL)
        OR
        (terminal_status IN ('succeeded', 'aborted', 'canceled')
         AND terminal_evidence_digest IS NOT NULL
         AND terminal_observed_at IS NOT NULL
         AND quiescence_not_before IS NOT NULL)
    ),
    CHECK (
        (state = 'released_quiescent'
         AND quiescence_evidence_digest IS NOT NULL
         AND quiescence_observed_at IS NOT NULL
         AND full_drop_certificate_digest IS NOT NULL)
        OR
        (state != 'released_quiescent'
         AND quiescence_evidence_digest IS NULL
         AND quiescence_observed_at IS NULL
         AND full_drop_certificate_digest IS NULL)
    )
)
'''

_ATTEMPTS_SQL = '''
CREATE TABLE nav2_reconcile_attempts (
    operation_id TEXT NOT NULL,
    attempt_seq INTEGER NOT NULL CHECK (
        attempt_seq >= 1 AND attempt_seq <= 4096
    ),
    attempt_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('observe', 'cancel', 'quiescence')),
    worker_id TEXT NOT NULL,
    fence_epoch INTEGER NOT NULL CHECK (fence_epoch >= 1),
    lease_expires_at REAL NOT NULL CHECK (lease_expires_at >= 0.0),
    goal_uuid TEXT NOT NULL CHECK (length(goal_uuid) = 32),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    request_fingerprint TEXT NOT NULL CHECK (
        length(request_fingerprint) = 64
    ),
    wire_payload_digest TEXT,
    claimed_at REAL NOT NULL CHECK (claimed_at >= 0.0),
    attempt_digest TEXT NOT NULL CHECK (length(attempt_digest) = 64),
    PRIMARY KEY (operation_id, attempt_seq),
    UNIQUE (operation_id, attempt_id),
    FOREIGN KEY (operation_id) REFERENCES nav2_reconcile_cases(operation_id),
    CHECK (
        (kind = 'cancel' AND wire_payload_digest IS NOT NULL
         AND length(wire_payload_digest) = 64)
        OR
        (kind != 'cancel' AND wire_payload_digest IS NULL)
    )
)
'''

_EVENTS_SQL = '''
CREATE TABLE nav2_reconcile_events (
    operation_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK (
        event_seq >= 1 AND event_seq <= 8192
    ),
    event_type TEXT NOT NULL,
    recorded_at REAL NOT NULL CHECK (recorded_at >= 0.0),
    worker_id TEXT NOT NULL,
    fence_epoch INTEGER NOT NULL CHECK (fence_epoch >= 0),
    lease_expires_at REAL,
    attempt_id TEXT,
    status TEXT,
    evidence_digest TEXT,
    previous_event_digest TEXT NOT NULL CHECK (
        length(previous_event_digest) = 64
    ),
    event_digest TEXT NOT NULL CHECK (length(event_digest) = 64),
    PRIMARY KEY (operation_id, event_seq),
    FOREIGN KEY (operation_id) REFERENCES nav2_reconcile_cases(operation_id),
    CHECK (
        (event_type IN (
            'lease_acquired', 'lease_renewed', 'lease_taken_over'
         ) AND lease_expires_at IS NOT NULL
           AND lease_expires_at > recorded_at AND fence_epoch >= 1)
        OR
        (event_type NOT IN (
            'lease_acquired', 'lease_renewed', 'lease_taken_over'
         ) AND lease_expires_at IS NULL)
    )
)
'''

_METADATA_NO_UPDATE_SQL = '''
CREATE TRIGGER nav2_reconcile_metadata_no_update
BEFORE UPDATE ON nav2_reconcile_metadata
BEGIN
    SELECT RAISE(ABORT, 'immutable metadata');
END
'''

_METADATA_NO_DELETE_SQL = '''
CREATE TRIGGER nav2_reconcile_metadata_no_delete
BEFORE DELETE ON nav2_reconcile_metadata
BEGIN
    SELECT RAISE(ABORT, 'immutable metadata');
END
'''

_CASE_NO_DELETE_SQL = '''
CREATE TRIGGER nav2_reconcile_case_no_delete
BEFORE DELETE ON nav2_reconcile_cases
BEGIN
    SELECT RAISE(ABORT, 'immutable case');
END
'''

_CASE_IDENTITY_SQL = '''
CREATE TRIGGER nav2_reconcile_case_identity
BEFORE UPDATE OF
    operation_id, robot_id, source_state, terminal_code, sample_index,
    goal_uuid, binding_digest, source_fence_epoch, terminal_event_seq,
    terminal_event_type, terminal_event_recorded_at,
    terminal_event_evidence_digest, terminal_event_digest,
    source_anchor_digest
ON nav2_reconcile_cases
BEGIN
    SELECT RAISE(ABORT, 'immutable source anchor');
END
'''

_CASE_TRANSITION_SQL = '''
CREATE TRIGGER nav2_reconcile_case_transition
BEFORE UPDATE OF state ON nav2_reconcile_cases
WHEN NOT (
    OLD.state = NEW.state
    OR (OLD.state = 'blocked_unresolved'
        AND NEW.state IN ('blocked_terminal_observed', 'blocked_conflict'))
    OR (OLD.state = 'blocked_terminal_observed'
        AND NEW.state IN ('released_quiescent', 'blocked_conflict'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid case transition');
END
'''

_ATTEMPT_NO_UPDATE_SQL = '''
CREATE TRIGGER nav2_reconcile_attempt_no_update
BEFORE UPDATE ON nav2_reconcile_attempts
BEGIN
    SELECT RAISE(ABORT, 'immutable attempt');
END
'''

_ATTEMPT_NO_DELETE_SQL = '''
CREATE TRIGGER nav2_reconcile_attempt_no_delete
BEFORE DELETE ON nav2_reconcile_attempts
BEGIN
    SELECT RAISE(ABORT, 'immutable attempt');
END
'''

_EVENT_NO_UPDATE_SQL = '''
CREATE TRIGGER nav2_reconcile_event_no_update
BEFORE UPDATE ON nav2_reconcile_events
BEGIN
    SELECT RAISE(ABORT, 'immutable event');
END
'''

_EVENT_NO_DELETE_SQL = '''
CREATE TRIGGER nav2_reconcile_event_no_delete
BEFORE DELETE ON nav2_reconcile_events
BEGIN
    SELECT RAISE(ABORT, 'immutable event');
END
'''

_SCHEMA_OBJECTS = (
    ('table', 'nav2_reconcile_metadata', _METADATA_SQL),
    ('table', 'nav2_reconcile_clock', _CLOCK_SQL),
    ('table', 'nav2_reconcile_cases', _CASES_SQL),
    ('table', 'nav2_reconcile_attempts', _ATTEMPTS_SQL),
    ('table', 'nav2_reconcile_events', _EVENTS_SQL),
    (
        'trigger',
        'nav2_reconcile_metadata_no_update',
        _METADATA_NO_UPDATE_SQL,
    ),
    (
        'trigger',
        'nav2_reconcile_metadata_no_delete',
        _METADATA_NO_DELETE_SQL,
    ),
    ('trigger', 'nav2_reconcile_case_no_delete', _CASE_NO_DELETE_SQL),
    ('trigger', 'nav2_reconcile_case_identity', _CASE_IDENTITY_SQL),
    ('trigger', 'nav2_reconcile_case_transition', _CASE_TRANSITION_SQL),
    (
        'trigger',
        'nav2_reconcile_attempt_no_update',
        _ATTEMPT_NO_UPDATE_SQL,
    ),
    (
        'trigger',
        'nav2_reconcile_attempt_no_delete',
        _ATTEMPT_NO_DELETE_SQL,
    ),
    ('trigger', 'nav2_reconcile_event_no_update', _EVENT_NO_UPDATE_SQL),
    ('trigger', 'nav2_reconcile_event_no_delete', _EVENT_NO_DELETE_SQL),
)


def _normalized_sql(value: str) -> str:
    return ' '.join(value.strip().rstrip(';').split())


def _clock_digest(last_now: float) -> str:
    return _hash_json(
        {
            'contract': 'malbut-nav2-reconcile-clock-v1',
            'last_now': last_now,
        }
    )


def _case_digest(row: Mapping[str, Any]) -> str:
    names = (
        'operation_id',
        'robot_id',
        'source_state',
        'terminal_code',
        'sample_index',
        'goal_uuid',
        'binding_digest',
        'source_fence_epoch',
        'terminal_event_seq',
        'terminal_event_type',
        'terminal_event_recorded_at',
        'terminal_event_evidence_digest',
        'terminal_event_digest',
        'source_anchor_digest',
        'state',
        'fence_epoch',
        'lease_owner',
        'lease_expires_at',
        'terminal_status',
        'terminal_evidence_digest',
        'terminal_observed_at',
        'quiescence_not_before',
        'quiescence_evidence_digest',
        'quiescence_observed_at',
        'full_drop_certificate_digest',
        'attempt_count',
        'event_count',
        'last_event_digest',
    )
    return _hash_json(
        {
            'contract': 'malbut-nav2-reconcile-case-row-v1',
            **{name: row[name] for name in names},
        }
    )


def _attempt_digest(values: Mapping[str, Any]) -> str:
    names = (
        'operation_id',
        'attempt_seq',
        'attempt_id',
        'kind',
        'worker_id',
        'fence_epoch',
        'lease_expires_at',
        'goal_uuid',
        'binding_digest',
        'request_fingerprint',
        'wire_payload_digest',
        'claimed_at',
    )
    return _hash_json(
        {
            'contract': 'malbut-nav2-reconcile-attempt-v1',
            **{name: values[name] for name in names},
        }
    )


def _event_digest(values: Mapping[str, Any]) -> str:
    names = (
        'operation_id',
        'event_seq',
        'event_type',
        'recorded_at',
        'worker_id',
        'fence_epoch',
        'lease_expires_at',
        'attempt_id',
        'status',
        'evidence_digest',
        'previous_event_digest',
    )
    return _hash_json(
        {
            'contract': 'malbut-nav2-reconcile-event-v1',
            **{name: values[name] for name in names},
        }
    )


def _lease_event_evidence(
    *,
    operation_id: str,
    event_type: str,
    worker_id: str,
    fence_epoch: int,
    recorded_at: float,
    lease_expires_at: float,
    source_anchor_digest: str,
) -> str:
    """Bind one append-only lease transition to exact durable authority."""
    return _hash_json(
        {
            'contract': 'malbut-nav2-reconcile-lease-event-v1',
            'operation_id': operation_id,
            'event_type': event_type,
            'worker_id': worker_id,
            'fence_epoch': fence_epoch,
            'recorded_at': recorded_at,
            'lease_expires_at': lease_expires_at,
            'source_anchor_digest': source_anchor_digest,
        }
    )


def _full_drop_digest(
    row: Mapping[str, Any],
    token: Nav2ReconcileAttemptToken,
    evidence_digest: str,
    observed_at: float,
) -> str:
    return _hash_json(
        {
            'contract': 'malbut-nav2-reconcile-full-drop-v1',
            'source_anchor_digest': row['source_anchor_digest'],
            'source_state': row['source_state'],
            'goal_uuid': row['goal_uuid'],
            'binding_digest': row['binding_digest'],
            'terminal_status': row['terminal_status'],
            'terminal_evidence_digest': row['terminal_evidence_digest'],
            'terminal_observed_at': row['terminal_observed_at'],
            'quiescence_not_before': row['quiescence_not_before'],
            'quiescence_attempt_digest': token.attempt_digest,
            'quiescence_evidence_digest': evidence_digest,
            'quiescence_observed_at': observed_at,
            'worker_id': token.worker_id,
            'fence_epoch': token.fence_epoch,
        }
    )


def _source_anchor_from_core(
    core_store: GazeboMonitorRoomStore,
    operation_id: str,
) -> ReconcileSourceAnchor:
    """Double-read an exact immutable core unknown activation anchor."""
    if type(core_store) is not GazeboMonitorRoomStore:
        raise GazeboMonitorRoomNav2ReconcileValidationError(
            'core store is invalid'
        )
    normalized_operation = _identifier(operation_id, 'operation_id')

    def read_snapshot():
        namespace = core_store.store_namespace
        observation = core_store.observe(normalized_operation)
        binding = core_store.private_operation_binding(normalized_operation)
        sample = core_store.private_current_sample(normalized_operation)
        events = core_store.events(normalized_operation)
        if (
            type(namespace) is not str
            or type(observation) is not OperationObservation
            or type(binding) is not PrivateOperationBinding
            or type(sample) is not PrivateStoredSample
            or type(events) is not tuple
            or not events
            or any(type(event) is not OperationEvent for event in events)
        ):
            raise GazeboMonitorRoomNav2ReconcileConflictError(
                'core source snapshot is invalid'
            )
        return (
            namespace,
            observation,
            binding,
            binding.binding_digest,
            sample,
            events,
        )

    first = read_snapshot()
    second = read_snapshot()
    if first != second:
        raise GazeboMonitorRoomNav2ReconcileConflictError(
            'core source snapshot changed'
        )
    namespace, observation, binding, binding_digest, sample, events = second
    final_event = events[-1]
    expected_event_type = (
        'delivery_became_unknown'
        if observation.state == 'delivery_unknown'
        else 'cancel_became_unknown'
    )
    if (
        observation.operation_id != normalized_operation
        or observation.state not in _SOURCE_STATES
        or observation.current_sample_state != observation.state
        or observation.terminal is not True
        or observation.robot_blocked is not True
        or observation.terminal_code is None
        or observation.fence_epoch < 1
        or binding.operation_id != normalized_operation
        or binding.robot_id != observation.robot_id
        or binding.sample_count != observation.navigation_samples_total
        or binding.deadline != observation.deadline
        or sample.operation_id != normalized_operation
        or sample.store_namespace != namespace
        or sample.index != observation.current_sample_index
        or sample.goal_uuid != observation.current_goal_uuid
        or sample.state != observation.current_sample_state
        or final_event.operation_id != normalized_operation
        or final_event.event_seq != len(events)
        or final_event.event_type != expected_event_type
        or final_event.to_operation_state != observation.state
        or final_event.to_sample_state != observation.current_sample_state
        or final_event.sample_index != observation.current_sample_index
        or final_event.goal_uuid != observation.current_goal_uuid
        or final_event.fence_epoch != observation.fence_epoch
        or final_event.code != observation.terminal_code
        or final_event.evidence_digest is None
    ):
        raise GazeboMonitorRoomNav2ReconcileConflictError(
            'core source is not an exact unknown terminal'
        )
    return ReconcileSourceAnchor(
        store_namespace=namespace,
        operation_id=normalized_operation,
        robot_id=observation.robot_id,
        source_state=observation.state,
        terminal_code=observation.terminal_code,
        sample_index=sample.index,
        goal_uuid=sample.goal_uuid,
        binding_digest=binding_digest,
        source_fence_epoch=observation.fence_epoch,
        terminal_event_seq=final_event.event_seq,
        terminal_event_type=final_event.event_type,
        terminal_event_recorded_at=final_event.recorded_at,
        terminal_event_evidence_digest=final_event.evidence_digest,
        terminal_event_digest=final_event.event_digest,
    )


class GazeboMonitorRoomNav2ReconcileStore:
    """Exact-schema durable sidecar for one or more core unknown cases."""

    def __init__(
        self,
        path,
        *,
        core_store_namespace: str,
        quiescence_dwell_seconds: float = 1.0,
        boot_id_reader=None,
    ) -> None:
        """Open or create the private v1 database without external calls."""
        namespace = _store_namespace(core_store_namespace)
        dwell = _timestamp(
            quiescence_dwell_seconds, 'quiescence_dwell_seconds'
        )
        if dwell > NAV2_RECONCILE_MAX_LEASE_SECONDS:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'quiescence_dwell_seconds is invalid'
            )
        reader = _read_host_boot_id if boot_id_reader is None else (
            boot_id_reader
        )
        if not callable(reader):
            raise GazeboMonitorRoomNav2ReconcileBootIdentityError(
                'host boot identity is unavailable'
            )
        try:
            boot_id = _host_boot_id(reader())
        except GazeboMonitorRoomNav2ReconcileBootIdentityError:
            raise
        except Exception:
            raise GazeboMonitorRoomNav2ReconcileBootIdentityError(
                'host boot identity is unavailable'
            ) from None
        try:
            selected_path = Path(path)
        except (TypeError, ValueError, OSError):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'database path is invalid'
            ) from None
        if not selected_path.name or selected_path.name in {'.', '..'}:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'database path is invalid'
            )
        parent = selected_path.parent
        try:
            parent_stat = parent.stat()
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise OSError
            if selected_path.is_symlink():
                raise OSError
            existed = selected_path.exists()
        except OSError:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'database path is invalid'
            ) from None
        try:
            connection = sqlite3.connect(
                os.fspath(selected_path),
                isolation_level=None,
                check_same_thread=False,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('PRAGMA trusted_schema = OFF')
            connection.execute('PRAGMA busy_timeout = 5000')
            connection.execute('PRAGMA journal_mode = DELETE')
            connection.execute('PRAGMA synchronous = FULL')
        except sqlite3.Error:
            raise GazeboMonitorRoomNav2ReconcileSchemaError(
                'sidecar database is unavailable'
            ) from None
        self._lock = RLock()
        self._connection: Optional[sqlite3.Connection] = connection
        self._path = selected_path
        self._core_store_namespace = namespace
        self._host_boot_id = boot_id
        self._quiescence_dwell_seconds = dwell
        try:
            if not existed:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    for _kind, _name, sql in _SCHEMA_OBJECTS:
                        connection.execute(sql)
                    connection.execute(
                        'PRAGMA user_version = '
                        f'{NAV2_RECONCILE_SCHEMA_VERSION}'
                    )
                    connection.execute(
                        '''
                        INSERT INTO nav2_reconcile_metadata (
                            singleton, schema_version, core_store_namespace,
                            host_boot_id, contract_digest,
                            quiescence_dwell_seconds
                        ) VALUES (1, ?, ?, ?, ?, ?)
                        ''',
                        (
                            NAV2_RECONCILE_SCHEMA_VERSION,
                            namespace,
                            boot_id,
                            _CONTRACT_DIGEST,
                            dwell,
                        ),
                    )
                    connection.execute(
                        '''
                        INSERT INTO nav2_reconcile_clock (
                            singleton, last_now, row_digest
                        ) VALUES (1, 0.0, ?)
                        ''',
                        (_clock_digest(0.0),),
                    )
                    connection.execute('COMMIT')
                except BaseException:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                    raise
                try:
                    os.chmod(selected_path, 0o600)
                except OSError:
                    raise GazeboMonitorRoomNav2ReconcileSchemaError(
                        'sidecar database is unavailable'
                    ) from None
            self._attest_locked(connection)
        except BaseException:
            connection.close()
            self._connection = None
            raise

    @property
    def core_store_namespace(self) -> str:
        """Return the exact core namespace bound into this sidecar."""
        with self._lock:
            self._require_connection()
            return self._core_store_namespace

    def __enter__(self) -> 'GazeboMonitorRoomNav2ReconcileStore':
        """Return this open store as a context manager."""
        with self._lock:
            self._require_connection()
        return self

    def __exit__(self, *_arguments: Any) -> None:
        """Close the database handle."""
        self.close()

    def close(self) -> None:
        """Close locally without sending, canceling, or releasing safety."""
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise GazeboMonitorRoomNav2ReconcileStoreError(
                'sidecar store is closed'
            )
        return connection

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            connection.execute('BEGIN')
            try:
                self._attest_locked(connection)
                yield connection
                self._attest_locked(connection)
                connection.execute('COMMIT')
            except BaseException:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._attest_locked(connection)
                yield connection
                self._attest_locked(connection)
                connection.execute('COMMIT')
            except BaseException:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def _attest_locked(self, connection: sqlite3.Connection) -> None:
        """Validate exact schema, metadata, hash chains, and row digests."""
        try:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar database integrity is invalid'
                )
            if connection.execute('PRAGMA user_version').fetchone()[0] != (
                NAV2_RECONCILE_SCHEMA_VERSION
            ):
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar schema is invalid'
                )
            rows = connection.execute(
                '''
                SELECT type, name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                '''
            ).fetchall()
            actual = {
                (str(row['type']), str(row['name'])): _normalized_sql(
                    str(row['sql'])
                )
                for row in rows
            }
            expected = {
                (kind, name): _normalized_sql(sql)
                for kind, name, sql in _SCHEMA_OBJECTS
            }
            if actual != expected:
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar schema is invalid'
                )
            metadata = connection.execute(
                'SELECT * FROM nav2_reconcile_metadata'
            ).fetchall()
            if len(metadata) != 1:
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar metadata is invalid'
                )
            metadata = metadata[0]
            if (
                metadata['singleton'] != 1
                or metadata['schema_version']
                != NAV2_RECONCILE_SCHEMA_VERSION
                or metadata['core_store_namespace']
                != self._core_store_namespace
                or metadata['host_boot_id'] != self._host_boot_id
                or metadata['contract_digest'] != _CONTRACT_DIGEST
                or type(metadata['quiescence_dwell_seconds']) is not float
                or metadata['quiescence_dwell_seconds']
                != self._quiescence_dwell_seconds
            ):
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar metadata is invalid'
                )
            clock_rows = connection.execute(
                'SELECT * FROM nav2_reconcile_clock'
            ).fetchall()
            if len(clock_rows) != 1:
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar clock is invalid'
                )
            clock_row = clock_rows[0]
            last_now = _timestamp(clock_row['last_now'], 'last_now')
            if (
                clock_row['singleton'] != 1
                or clock_row['row_digest'] != _clock_digest(last_now)
            ):
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar clock is invalid'
                )
            self._attest_cases_locked(connection)
        except GazeboMonitorRoomNav2ReconcileStoreError:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError, IndexError):
            raise GazeboMonitorRoomNav2ReconcileSchemaError(
                'sidecar database is invalid'
            ) from None

    def _attest_cases_locked(self, connection: sqlite3.Connection) -> None:
        case_rows = connection.execute(
            'SELECT * FROM nav2_reconcile_cases ORDER BY operation_id'
        ).fetchall()
        if len(case_rows) > NAV2_RECONCILE_MAX_CASES:
            raise GazeboMonitorRoomNav2ReconcileSchemaError(
                'sidecar case bound is invalid'
            )
        for row in case_rows:
            values = dict(row)
            anchor = self._anchor_from_row(values)
            if (
                values['source_anchor_digest'] != anchor.anchor_digest
                or values['row_digest'] != _case_digest(values)
            ):
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar case digest is invalid'
                )
            observation = self._observation_from_row(values)
            if (
                observation.state == 'blocked_unresolved'
                and observation.terminal_status is not None
            ) or (
                observation.state
                in {'blocked_terminal_observed', 'released_quiescent'}
                and observation.terminal_status is None
            ):
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar case state is invalid'
                )
            attempts = connection.execute(
                '''
                SELECT * FROM nav2_reconcile_attempts
                WHERE operation_id = ? ORDER BY attempt_seq
                ''',
                (values['operation_id'],),
            ).fetchall()
            if len(attempts) != values['attempt_count']:
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar attempt count is invalid'
                )
            for expected_seq, attempt in enumerate(attempts, 1):
                attempt_values = dict(attempt)
                token = self._attempt_from_row(attempt_values)
                if (
                    token.attempt_seq != expected_seq
                    or token.operation_id != values['operation_id']
                    or token.goal_uuid != values['goal_uuid']
                    or token.binding_digest != values['binding_digest']
                    or token.attempt_digest
                    != _attempt_digest(attempt_values)
                ):
                    raise GazeboMonitorRoomNav2ReconcileSchemaError(
                        'sidecar attempt digest is invalid'
                    )
            events = connection.execute(
                '''
                SELECT * FROM nav2_reconcile_events
                WHERE operation_id = ? ORDER BY event_seq
                ''',
                (values['operation_id'],),
            ).fetchall()
            if len(events) != values['event_count']:
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar event count is invalid'
                )
            previous = _ZERO_DIGEST
            lease_history = None
            for expected_seq, event in enumerate(events, 1):
                event_values = dict(event)
                canonical = self._event_from_row(event_values)
                if (
                    canonical.event_seq != expected_seq
                    or canonical.operation_id != values['operation_id']
                    or canonical.previous_event_digest != previous
                    or canonical.event_digest != _event_digest(event_values)
                ):
                    raise GazeboMonitorRoomNav2ReconcileSchemaError(
                        'sidecar event chain is invalid'
                    )
                if canonical.event_type in {
                    'lease_acquired',
                    'lease_renewed',
                    'lease_taken_over',
                }:
                    expected_evidence = _lease_event_evidence(
                        operation_id=canonical.operation_id,
                        event_type=canonical.event_type,
                        worker_id=canonical.worker_id,
                        fence_epoch=canonical.fence_epoch,
                        recorded_at=canonical.recorded_at,
                        lease_expires_at=canonical.lease_expires_at,
                        source_anchor_digest=values[
                            'source_anchor_digest'
                        ],
                    )
                    if canonical.evidence_digest != expected_evidence:
                        raise GazeboMonitorRoomNav2ReconcileSchemaError(
                            'sidecar lease evidence is invalid'
                        )
                    if canonical.event_type == 'lease_acquired':
                        if lease_history is not None or (
                            canonical.fence_epoch != 1
                        ):
                            raise GazeboMonitorRoomNav2ReconcileSchemaError(
                                'sidecar lease order is invalid'
                            )
                    elif canonical.event_type == 'lease_renewed':
                        if (
                            lease_history is None
                            or canonical.worker_id
                            != lease_history['worker_id']
                            or canonical.fence_epoch
                            != lease_history['fence_epoch']
                            or canonical.recorded_at
                            >= lease_history['lease_expires_at']
                        ):
                            raise GazeboMonitorRoomNav2ReconcileSchemaError(
                                'sidecar lease order is invalid'
                            )
                    elif (
                        lease_history is None
                        or canonical.fence_epoch
                        != lease_history['fence_epoch'] + 1
                        or canonical.recorded_at
                        < lease_history['lease_expires_at']
                    ):
                        raise GazeboMonitorRoomNav2ReconcileSchemaError(
                            'sidecar lease order is invalid'
                        )
                    lease_history = {
                        'worker_id': canonical.worker_id,
                        'fence_epoch': canonical.fence_epoch,
                        'lease_expires_at': canonical.lease_expires_at,
                    }
                previous = canonical.event_digest
            if not events or values['last_event_digest'] != previous:
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar event head is invalid'
                )
            if lease_history is None:
                if (
                    values['fence_epoch'] != 0
                    or values['lease_owner'] is not None
                    or values['lease_expires_at'] is not None
                ):
                    raise GazeboMonitorRoomNav2ReconcileSchemaError(
                        'sidecar lease head is invalid'
                    )
            elif (
                values['fence_epoch'] != lease_history['fence_epoch']
                or (
                    values['state'] != 'released_quiescent'
                    and (
                        values['lease_owner']
                        != lease_history['worker_id']
                        or values['lease_expires_at']
                        != lease_history['lease_expires_at']
                    )
                )
                or (
                    values['state'] == 'released_quiescent'
                    and (
                        values['lease_owner'] is not None
                        or values['lease_expires_at'] is not None
                    )
                )
            ):
                raise GazeboMonitorRoomNav2ReconcileSchemaError(
                    'sidecar lease head is invalid'
                )

    def _advance_clock_locked(
        self, connection: sqlite3.Connection, now: Any
    ) -> float:
        normalized = _timestamp(now, 'now')
        row = connection.execute(
            'SELECT * FROM nav2_reconcile_clock WHERE singleton = 1'
        ).fetchone()
        if row is None:
            raise GazeboMonitorRoomNav2ReconcileSchemaError(
                'sidecar clock is invalid'
            )
        last_now = _timestamp(row['last_now'], 'last_now')
        if normalized < last_now:
            raise GazeboMonitorRoomNav2ReconcileClockRollbackError(
                'host clock moved backwards'
            )
        if normalized != last_now:
            connection.execute(
                '''
                UPDATE nav2_reconcile_clock
                SET last_now = ?, row_digest = ? WHERE singleton = 1
                ''',
                (normalized, _clock_digest(normalized)),
            )
        return normalized

    def _anchor_from_row(
        self, row: Mapping[str, Any]
    ) -> ReconcileSourceAnchor:
        return ReconcileSourceAnchor(
            store_namespace=row.get(
                'store_namespace', self._core_store_namespace
            ),
            operation_id=row['operation_id'],
            robot_id=row['robot_id'],
            source_state=row['source_state'],
            terminal_code=row['terminal_code'],
            sample_index=row['sample_index'],
            goal_uuid=row['goal_uuid'],
            binding_digest=row['binding_digest'],
            source_fence_epoch=row['source_fence_epoch'],
            terminal_event_seq=row['terminal_event_seq'],
            terminal_event_type=row['terminal_event_type'],
            terminal_event_recorded_at=row['terminal_event_recorded_at'],
            terminal_event_evidence_digest=(
                row['terminal_event_evidence_digest']
            ),
            terminal_event_digest=row['terminal_event_digest'],
        )

    def _observation_from_row(
        self, row: Mapping[str, Any]
    ) -> Nav2ReconcileObservation:
        return Nav2ReconcileObservation(
            operation_id=row['operation_id'],
            robot_id=row['robot_id'],
            source_state=row['source_state'],
            terminal_code=row['terminal_code'],
            goal_uuid=row['goal_uuid'],
            source_anchor_digest=row['source_anchor_digest'],
            state=row['state'],
            fence_epoch=row['fence_epoch'],
            lease_owner=row['lease_owner'],
            lease_expires_at=row['lease_expires_at'],
            terminal_status=row['terminal_status'],
            terminal_evidence_digest=row['terminal_evidence_digest'],
            terminal_observed_at=row['terminal_observed_at'],
            quiescence_not_before=row['quiescence_not_before'],
            quiescence_evidence_digest=(
                row['quiescence_evidence_digest']
            ),
            quiescence_observed_at=row['quiescence_observed_at'],
            full_drop_certificate_digest=(
                row['full_drop_certificate_digest']
            ),
            attempt_count=row['attempt_count'],
            event_count=row['event_count'],
        )

    @staticmethod
    def _attempt_from_row(
        row: Mapping[str, Any]
    ) -> Nav2ReconcileAttemptToken:
        return Nav2ReconcileAttemptToken(
            operation_id=row['operation_id'],
            attempt_seq=row['attempt_seq'],
            attempt_id=row['attempt_id'],
            kind=row['kind'],
            worker_id=row['worker_id'],
            fence_epoch=row['fence_epoch'],
            lease_expires_at=row['lease_expires_at'],
            goal_uuid=row['goal_uuid'],
            binding_digest=row['binding_digest'],
            request_fingerprint=row['request_fingerprint'],
            wire_payload_digest=row['wire_payload_digest'],
            claimed_at=row['claimed_at'],
            attempt_digest=row['attempt_digest'],
        )

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> Nav2ReconcileEvent:
        return Nav2ReconcileEvent(
            operation_id=row['operation_id'],
            event_seq=row['event_seq'],
            event_type=row['event_type'],
            recorded_at=row['recorded_at'],
            worker_id=row['worker_id'],
            fence_epoch=row['fence_epoch'],
            lease_expires_at=row['lease_expires_at'],
            attempt_id=row['attempt_id'],
            status=row['status'],
            evidence_digest=row['evidence_digest'],
            previous_event_digest=row['previous_event_digest'],
            event_digest=row['event_digest'],
        )

    @staticmethod
    def _load_case_locked(
        connection: sqlite3.Connection, operation_id: str
    ) -> Dict[str, Any]:
        row = connection.execute(
            'SELECT * FROM nav2_reconcile_cases WHERE operation_id = ?',
            (operation_id,),
        ).fetchone()
        if row is None:
            raise GazeboMonitorRoomNav2ReconcileNotFoundError(
                'reconciliation case was not found'
            )
        return dict(row)

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        case: Dict[str, Any],
        *,
        event_type: str,
        recorded_at: float,
        worker_id: str,
        fence_epoch: int,
        lease_expires_at: Optional[float] = None,
        attempt_id: Optional[str] = None,
        status: Optional[str] = None,
        evidence_digest: Optional[str] = None,
    ) -> Nav2ReconcileEvent:
        next_seq = case['event_count'] + 1
        if next_seq > NAV2_RECONCILE_MAX_EVENTS:
            raise GazeboMonitorRoomNav2ReconcileConflictError(
                'reconciliation event bound is exhausted'
            )
        values = {
            'operation_id': case['operation_id'],
            'event_seq': next_seq,
            'event_type': _identifier(event_type, 'event_type'),
            'recorded_at': _timestamp(recorded_at, 'recorded_at'),
            'worker_id': _identifier(worker_id, 'worker_id'),
            'fence_epoch': _integer(
                fence_epoch, 'fence_epoch', 0, _MAX_FENCE
            ),
            'lease_expires_at': _optional_timestamp(
                lease_expires_at, 'lease_expires_at'
            ),
            'attempt_id': (
                None
                if attempt_id is None
                else _identifier(attempt_id, 'attempt_id')
            ),
            'status': (
                None if status is None else _identifier(status, 'status')
            ),
            'evidence_digest': (
                None
                if evidence_digest is None
                else _digest(evidence_digest, 'evidence_digest')
            ),
            'previous_event_digest': case['last_event_digest'],
        }
        values['event_digest'] = _event_digest(values)
        connection.execute(
            '''
            INSERT INTO nav2_reconcile_events (
                operation_id, event_seq, event_type, recorded_at,
                worker_id, fence_epoch, lease_expires_at, attempt_id, status,
                evidence_digest, previous_event_digest, event_digest
            ) VALUES (
                :operation_id, :event_seq, :event_type, :recorded_at,
                :worker_id, :fence_epoch, :lease_expires_at,
                :attempt_id, :status,
                :evidence_digest, :previous_event_digest, :event_digest
            )
            ''',
            values,
        )
        case['event_count'] = next_seq
        case['last_event_digest'] = values['event_digest']
        return self._event_from_row(values)

    @staticmethod
    def _update_case_locked(
        connection: sqlite3.Connection, case: Dict[str, Any]
    ) -> None:
        case['row_digest'] = _case_digest(case)
        cursor = connection.execute(
            '''
            UPDATE nav2_reconcile_cases SET
                state = :state,
                fence_epoch = :fence_epoch,
                lease_owner = :lease_owner,
                lease_expires_at = :lease_expires_at,
                terminal_status = :terminal_status,
                terminal_evidence_digest = :terminal_evidence_digest,
                terminal_observed_at = :terminal_observed_at,
                quiescence_not_before = :quiescence_not_before,
                quiescence_evidence_digest = :quiescence_evidence_digest,
                quiescence_observed_at = :quiescence_observed_at,
                full_drop_certificate_digest =
                    :full_drop_certificate_digest,
                attempt_count = :attempt_count,
                event_count = :event_count,
                last_event_digest = :last_event_digest,
                row_digest = :row_digest
            WHERE operation_id = :operation_id
            ''',
            case,
        )
        if cursor.rowcount != 1:
            raise GazeboMonitorRoomNav2ReconcileConflictError(
                'reconciliation case changed'
            )

    @staticmethod
    def _require_live_lease(
        case: Mapping[str, Any],
        *,
        worker_id: str,
        fence_epoch: int,
        now: float,
        token_expires_at: Optional[float] = None,
    ) -> None:
        if case['fence_epoch'] != fence_epoch:
            raise GazeboMonitorRoomNav2ReconcileFenceError(
                'reconciliation fence is stale'
            )
        if case['lease_owner'] != worker_id:
            raise GazeboMonitorRoomNav2ReconcileLeaseError(
                'reconciliation lease is unavailable'
            )
        expires_at = case['lease_expires_at']
        if (
            type(expires_at) is not float
            or now >= expires_at
            or (
                token_expires_at is not None
                and now >= token_expires_at
            )
        ):
            raise GazeboMonitorRoomNav2ReconcileLeaseError(
                'reconciliation lease expired'
            )

    def register_unknown(
        self,
        core_store: GazeboMonitorRoomStore,
        operation_id: str,
        *,
        now: Optional[float] = None,
    ) -> Nav2ReconcileObservation:
        """Activate exactly one immutable core unknown without changing it."""
        anchor = _source_anchor_from_core(core_store, operation_id)
        if anchor.store_namespace != self._core_store_namespace:
            raise GazeboMonitorRoomNav2ReconcileConflictError(
                'core store namespace changed'
            )
        selected_now = _boottime() if now is None else now
        with self._write_transaction() as connection:
            recorded_at = self._advance_clock_locked(
                connection, selected_now
            )
            existing = connection.execute(
                'SELECT * FROM nav2_reconcile_cases WHERE operation_id = ?',
                (anchor.operation_id,),
            ).fetchone()
            if existing is not None:
                values = dict(existing)
                if (
                    values['source_anchor_digest'] != anchor.anchor_digest
                    or self._anchor_from_row(
                        {
                            **values,
                            'store_namespace': self._core_store_namespace,
                        }
                    )
                    != anchor
                ):
                    raise GazeboMonitorRoomNav2ReconcileConflictError(
                        'reconciliation source anchor conflicts'
                    )
                return replace(
                    self._observation_from_row(values), replayed=True
                )
            count = connection.execute(
                'SELECT COUNT(*) FROM nav2_reconcile_cases'
            ).fetchone()[0]
            if count >= NAV2_RECONCILE_MAX_CASES:
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'reconciliation case bound is exhausted'
                )
            initial_event = {
                'operation_id': anchor.operation_id,
                'event_seq': 1,
                'event_type': 'case_registered',
                'recorded_at': recorded_at,
                'worker_id': 'reconcile-store',
                'fence_epoch': 0,
                'lease_expires_at': None,
                'attempt_id': None,
                'status': anchor.source_state,
                'evidence_digest': anchor.anchor_digest,
                'previous_event_digest': _ZERO_DIGEST,
            }
            initial_event['event_digest'] = _event_digest(initial_event)
            case = {
                'operation_id': anchor.operation_id,
                'robot_id': anchor.robot_id,
                'source_state': anchor.source_state,
                'terminal_code': anchor.terminal_code,
                'sample_index': anchor.sample_index,
                'goal_uuid': anchor.goal_uuid,
                'binding_digest': anchor.binding_digest,
                'source_fence_epoch': anchor.source_fence_epoch,
                'terminal_event_seq': anchor.terminal_event_seq,
                'terminal_event_type': anchor.terminal_event_type,
                'terminal_event_recorded_at': (
                    anchor.terminal_event_recorded_at
                ),
                'terminal_event_evidence_digest': (
                    anchor.terminal_event_evidence_digest
                ),
                'terminal_event_digest': anchor.terminal_event_digest,
                'source_anchor_digest': anchor.anchor_digest,
                'state': 'blocked_unresolved',
                'fence_epoch': 0,
                'lease_owner': None,
                'lease_expires_at': None,
                'terminal_status': None,
                'terminal_evidence_digest': None,
                'terminal_observed_at': None,
                'quiescence_not_before': None,
                'quiescence_evidence_digest': None,
                'quiescence_observed_at': None,
                'full_drop_certificate_digest': None,
                'attempt_count': 0,
                'event_count': 1,
                'last_event_digest': initial_event['event_digest'],
            }
            case['row_digest'] = _case_digest(case)
            connection.execute(
                '''
                INSERT INTO nav2_reconcile_cases VALUES (
                    :operation_id, :robot_id, :source_state, :terminal_code,
                    :sample_index, :goal_uuid, :binding_digest,
                    :source_fence_epoch, :terminal_event_seq,
                    :terminal_event_type, :terminal_event_recorded_at,
                    :terminal_event_evidence_digest, :terminal_event_digest,
                    :source_anchor_digest, :state, :fence_epoch,
                    :lease_owner, :lease_expires_at, :terminal_status,
                    :terminal_evidence_digest, :terminal_observed_at,
                    :quiescence_not_before, :quiescence_evidence_digest,
                    :quiescence_observed_at,
                    :full_drop_certificate_digest, :attempt_count,
                    :event_count, :last_event_digest, :row_digest
                )
                ''',
                case,
            )
            connection.execute(
                '''
                INSERT INTO nav2_reconcile_events VALUES (
                    :operation_id, :event_seq, :event_type, :recorded_at,
                    :worker_id, :fence_epoch, :lease_expires_at,
                    :attempt_id, :status,
                    :evidence_digest, :previous_event_digest, :event_digest
                )
                ''',
                initial_event,
            )
            return self._observation_from_row(case)

    def assert_source_unchanged(
        self,
        core_store: GazeboMonitorRoomStore,
        operation_id: str,
    ) -> ReconcileSourceAnchor:
        """Recheck the immutable core activation anchor before a call."""
        anchor = _source_anchor_from_core(core_store, operation_id)
        with self._read_transaction() as connection:
            case = self._load_case_locked(connection, anchor.operation_id)
            expected = self._anchor_from_row(
                {
                    **case,
                    'store_namespace': self._core_store_namespace,
                }
            )
            if anchor != expected or anchor.anchor_digest != (
                case['source_anchor_digest']
            ):
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'core source anchor changed'
                )
            return anchor

    def source_anchor(self, operation_id: str) -> ReconcileSourceAnchor:
        """Return the stored coordinate-free source activation anchor."""
        normalized = _identifier(operation_id, 'operation_id')
        with self._read_transaction() as connection:
            case = self._load_case_locked(connection, normalized)
            return self._anchor_from_row(
                {
                    **case,
                    'store_namespace': self._core_store_namespace,
                }
            )

    def observe(self, operation_id: str) -> Nav2ReconcileObservation:
        """Return one validated safety-only sidecar observation."""
        normalized = _identifier(operation_id, 'operation_id')
        with self._read_transaction() as connection:
            case = self._load_case_locked(connection, normalized)
            return self._observation_from_row(case)

    def attempts(
        self, operation_id: str
    ) -> Tuple[Nav2ReconcileAttemptToken, ...]:
        """Return immutable attempts in durable claim order."""
        normalized = _identifier(operation_id, 'operation_id')
        with self._read_transaction() as connection:
            self._load_case_locked(connection, normalized)
            rows = connection.execute(
                '''
                SELECT * FROM nav2_reconcile_attempts
                WHERE operation_id = ? ORDER BY attempt_seq
                ''',
                (normalized,),
            ).fetchall()
            return tuple(self._attempt_from_row(dict(row)) for row in rows)

    def events(
        self, operation_id: str
    ) -> Tuple[Nav2ReconcileEvent, ...]:
        """Return immutable hash-chained events in durable order."""
        normalized = _identifier(operation_id, 'operation_id')
        with self._read_transaction() as connection:
            self._load_case_locked(connection, normalized)
            rows = connection.execute(
                '''
                SELECT * FROM nav2_reconcile_events
                WHERE operation_id = ? ORDER BY event_seq
                ''',
                (normalized,),
            ).fetchall()
            return tuple(self._event_from_row(dict(row)) for row in rows)

    def acquire_lease(
        self,
        operation_id: str,
        *,
        worker_id: str,
        expected_fence: int,
        lease_seconds: float,
        now: Optional[float] = None,
    ) -> Nav2ReconcileLeaseGrant:
        """Acquire, renew, or take over an independent BOOTTIME lease."""
        normalized_operation = _identifier(operation_id, 'operation_id')
        normalized_worker = _identifier(worker_id, 'worker_id')
        normalized_fence = _integer(
            expected_fence, 'expected_fence', 0, _MAX_FENCE
        )
        normalized_lease = _timestamp(lease_seconds, 'lease_seconds')
        if (
            normalized_lease <= 0.0
            or normalized_lease > NAV2_RECONCILE_MAX_LEASE_SECONDS
        ):
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'lease_seconds is invalid'
            )
        selected_now = _boottime() if now is None else now
        with self._write_transaction() as connection:
            checked_at = self._advance_clock_locked(connection, selected_now)
            case = self._load_case_locked(connection, normalized_operation)
            if case['state'] in {'released_quiescent', 'blocked_conflict'}:
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'reconciliation case is closed'
                )
            current_fence = case['fence_epoch']
            current_owner = case['lease_owner']
            current_expiry = case['lease_expires_at']
            taken_over = False
            if current_owner is None:
                if current_fence != 0 or normalized_fence != 0:
                    raise GazeboMonitorRoomNav2ReconcileFenceError(
                        'reconciliation fence is stale'
                    )
                new_fence = 1
                event_type = 'lease_acquired'
            elif checked_at < current_expiry:
                if current_fence != normalized_fence:
                    raise GazeboMonitorRoomNav2ReconcileFenceError(
                        'reconciliation fence is stale'
                    )
                if current_owner != normalized_worker:
                    raise GazeboMonitorRoomNav2ReconcileLeaseError(
                        'reconciliation lease is busy'
                    )
                new_fence = current_fence
                event_type = 'lease_renewed'
            else:
                if current_fence != normalized_fence:
                    raise GazeboMonitorRoomNav2ReconcileFenceError(
                        'reconciliation fence is stale'
                    )
                if current_fence >= _MAX_FENCE:
                    raise GazeboMonitorRoomNav2ReconcileFenceError(
                        'reconciliation fence is exhausted'
                    )
                new_fence = current_fence + 1
                event_type = 'lease_taken_over'
                taken_over = True
            expires_at = checked_at + normalized_lease
            if not math.isfinite(expires_at):
                raise GazeboMonitorRoomNav2ReconcileValidationError(
                    'lease expiry is invalid'
                )
            case['fence_epoch'] = new_fence
            case['lease_owner'] = normalized_worker
            case['lease_expires_at'] = expires_at
            self._append_event_locked(
                connection,
                case,
                event_type=event_type,
                recorded_at=checked_at,
                worker_id=normalized_worker,
                fence_epoch=new_fence,
                lease_expires_at=expires_at,
                status=case['state'],
                evidence_digest=_lease_event_evidence(
                    operation_id=case['operation_id'],
                    event_type=event_type,
                    worker_id=normalized_worker,
                    fence_epoch=new_fence,
                    recorded_at=checked_at,
                    lease_expires_at=expires_at,
                    source_anchor_digest=case['source_anchor_digest'],
                ),
            )
            self._update_case_locked(connection, case)
            observation = self._observation_from_row(case)
            return Nav2ReconcileLeaseGrant(
                observation=observation,
                worker_id=normalized_worker,
                fence_epoch=new_fence,
                lease_expires_at=expires_at,
                taken_over=taken_over,
            )

    def claim_attempt(
        self,
        operation_id: str,
        *,
        attempt_id: str,
        kind: str,
        worker_id: str,
        fence_epoch: int,
        request_fingerprint: str,
        wire_payload_digest: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Nav2ReconcileAttemptClaim:
        """Persist a one-shot claim before any exact observe or cancel call."""
        normalized_operation = _identifier(operation_id, 'operation_id')
        normalized_attempt = _identifier(attempt_id, 'attempt_id')
        if type(kind) is not str or kind not in _ATTEMPT_KINDS:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'attempt kind is invalid'
            )
        normalized_worker = _identifier(worker_id, 'worker_id')
        normalized_fence = _integer(
            fence_epoch, 'fence_epoch', 1, _MAX_FENCE
        )
        normalized_request = _digest(
            request_fingerprint, 'request_fingerprint'
        )
        if kind == 'cancel':
            normalized_wire = _digest(
                wire_payload_digest, 'wire_payload_digest'
            )
        elif wire_payload_digest is None:
            normalized_wire = None
        else:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'wire_payload_digest is invalid'
            )
        selected_now = _boottime() if now is None else now
        with self._write_transaction() as connection:
            claimed_at = self._advance_clock_locked(connection, selected_now)
            case = self._load_case_locked(connection, normalized_operation)
            existing = connection.execute(
                '''
                SELECT * FROM nav2_reconcile_attempts
                WHERE operation_id = ? AND attempt_id = ?
                ''',
                (normalized_operation, normalized_attempt),
            ).fetchone()
            if existing is not None:
                token = self._attempt_from_row(dict(existing))
                if (
                    token.kind != kind
                    or token.worker_id != normalized_worker
                    or token.fence_epoch != normalized_fence
                    or token.goal_uuid != case['goal_uuid']
                    or token.binding_digest != case['binding_digest']
                    or token.request_fingerprint != normalized_request
                    or token.wire_payload_digest != normalized_wire
                ):
                    raise GazeboMonitorRoomNav2ReconcileConflictError(
                        'attempt identity conflicts'
                    )
                return Nav2ReconcileAttemptClaim(
                    token=token, claimed=False
                )
            if case['state'] in {'released_quiescent', 'blocked_conflict'}:
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'reconciliation case is closed'
                )
            if kind == 'cancel' and case['state'] != 'blocked_unresolved':
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'terminal goal cannot be canceled'
                )
            if (
                kind == 'quiescence'
                and case['state'] != 'blocked_terminal_observed'
            ):
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'quiescence requires terminal evidence'
                )
            self._require_live_lease(
                case,
                worker_id=normalized_worker,
                fence_epoch=normalized_fence,
                now=claimed_at,
            )
            next_seq = case['attempt_count'] + 1
            if next_seq > NAV2_RECONCILE_MAX_ATTEMPTS:
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'reconciliation attempt bound is exhausted'
                )
            values = {
                'operation_id': normalized_operation,
                'attempt_seq': next_seq,
                'attempt_id': normalized_attempt,
                'kind': kind,
                'worker_id': normalized_worker,
                'fence_epoch': normalized_fence,
                'lease_expires_at': case['lease_expires_at'],
                'goal_uuid': case['goal_uuid'],
                'binding_digest': case['binding_digest'],
                'request_fingerprint': normalized_request,
                'wire_payload_digest': normalized_wire,
                'claimed_at': claimed_at,
            }
            values['attempt_digest'] = _attempt_digest(values)
            connection.execute(
                '''
                INSERT INTO nav2_reconcile_attempts VALUES (
                    :operation_id, :attempt_seq, :attempt_id, :kind,
                    :worker_id, :fence_epoch, :lease_expires_at,
                    :goal_uuid, :binding_digest, :request_fingerprint,
                    :wire_payload_digest, :claimed_at, :attempt_digest
                )
                ''',
                values,
            )
            case['attempt_count'] = next_seq
            self._append_event_locked(
                connection,
                case,
                event_type='attempt_claimed',
                recorded_at=claimed_at,
                worker_id=normalized_worker,
                fence_epoch=normalized_fence,
                attempt_id=normalized_attempt,
                status=kind,
                evidence_digest=values['attempt_digest'],
            )
            self._update_case_locked(connection, case)
            return Nav2ReconcileAttemptClaim(
                token=self._attempt_from_row(values), claimed=True
            )

    def _load_exact_token_locked(
        self,
        connection: sqlite3.Connection,
        token: Nav2ReconcileAttemptToken,
    ) -> Nav2ReconcileAttemptToken:
        if type(token) is not Nav2ReconcileAttemptToken:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'attempt token is invalid'
            )
        row = connection.execute(
            '''
            SELECT * FROM nav2_reconcile_attempts
            WHERE operation_id = ? AND attempt_seq = ?
            ''',
            (token.operation_id, token.attempt_seq),
        ).fetchone()
        if row is None:
            raise GazeboMonitorRoomNav2ReconcileConflictError(
                'attempt token was not claimed'
            )
        canonical = self._attempt_from_row(dict(row))
        if canonical != token:
            raise GazeboMonitorRoomNav2ReconcileConflictError(
                'attempt token changed'
            )
        return canonical

    def assert_attempt_current(
        self,
        token: Nav2ReconcileAttemptToken,
        *,
        now: Optional[float] = None,
    ) -> Nav2ReconcileObservation:
        """Recheck a persisted claim at the immediate external-call edge."""
        selected_now = _boottime() if now is None else now
        with self._write_transaction() as connection:
            checked_at = self._advance_clock_locked(connection, selected_now)
            canonical = self._load_exact_token_locked(connection, token)
            case = self._load_case_locked(
                connection, canonical.operation_id
            )
            self._require_live_lease(
                case,
                worker_id=canonical.worker_id,
                fence_epoch=canonical.fence_epoch,
                now=checked_at,
                token_expires_at=canonical.lease_expires_at,
            )
            if case['state'] in {'released_quiescent', 'blocked_conflict'}:
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'reconciliation case is closed'
                )
            if (
                canonical.kind == 'cancel'
                and case['state'] != 'blocked_unresolved'
            ):
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'terminal goal cannot be canceled'
                )
            if (
                canonical.kind == 'quiescence'
                and case['state'] != 'blocked_terminal_observed'
            ):
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'quiescence requires terminal evidence'
                )
            return self._observation_from_row(case)

    def record_goal_observation(
        self,
        token: Nav2ReconcileAttemptToken,
        *,
        status: str,
        evidence_digest: str,
        now: Optional[float] = None,
    ) -> Nav2ReconcileObservation:
        """Record an exact observe result; absence never proves not-sent."""
        return self._record_goal_evidence(
            token,
            required_kind='observe',
            status=status,
            evidence_digest=evidence_digest,
            now=now,
        )

    def record_cancel_observation(
        self,
        token: Nav2ReconcileAttemptToken,
        *,
        status: str,
        evidence_digest: str,
        now: Optional[float] = None,
    ) -> Nav2ReconcileObservation:
        """Record one exact cancel outcome; only canceled is terminal."""
        if type(status) is not str or status not in {
            'active', 'canceled', 'rejected', 'unknown'
        }:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'cancel status is invalid'
            )
        return self._record_goal_evidence(
            token,
            required_kind='cancel',
            status=status,
            evidence_digest=evidence_digest,
            now=now,
        )

    def _record_goal_evidence(
        self,
        token: Nav2ReconcileAttemptToken,
        *,
        required_kind: str,
        status: str,
        evidence_digest: str,
        now: Optional[float],
    ) -> Nav2ReconcileObservation:
        if type(status) is not str or status not in _GOAL_STATUSES:
            raise GazeboMonitorRoomNav2ReconcileValidationError(
                'goal status is invalid'
            )
        normalized_evidence = _digest(
            evidence_digest, 'evidence_digest'
        )
        selected_now = _boottime() if now is None else now
        with self._write_transaction() as connection:
            observed_at = self._advance_clock_locked(connection, selected_now)
            canonical = self._load_exact_token_locked(connection, token)
            if canonical.kind != required_kind:
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'attempt kind conflicts'
                )
            case = self._load_case_locked(
                connection, canonical.operation_id
            )
            self._require_live_lease(
                case,
                worker_id=canonical.worker_id,
                fence_epoch=canonical.fence_epoch,
                now=observed_at,
                token_expires_at=canonical.lease_expires_at,
            )
            if case['state'] == 'released_quiescent':
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'released certificate is immutable'
                )
            if case['state'] == 'blocked_conflict':
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'reconciliation case is conflicted'
                )
            if status not in _TERMINAL_GOAL_STATUSES:
                self._append_event_locked(
                    connection,
                    case,
                    event_type='goal_observation_inconclusive',
                    recorded_at=observed_at,
                    worker_id=canonical.worker_id,
                    fence_epoch=canonical.fence_epoch,
                    attempt_id=canonical.attempt_id,
                    status=status,
                    evidence_digest=normalized_evidence,
                )
            elif case['state'] == 'blocked_unresolved':
                case['state'] = 'blocked_terminal_observed'
                case['terminal_status'] = status
                case['terminal_evidence_digest'] = normalized_evidence
                case['terminal_observed_at'] = observed_at
                case['quiescence_not_before'] = (
                    observed_at + self._quiescence_dwell_seconds
                )
                self._append_event_locked(
                    connection,
                    case,
                    event_type='goal_terminal_observed',
                    recorded_at=observed_at,
                    worker_id=canonical.worker_id,
                    fence_epoch=canonical.fence_epoch,
                    attempt_id=canonical.attempt_id,
                    status=status,
                    evidence_digest=normalized_evidence,
                )
            elif (
                case['terminal_status'] == status
                and case['terminal_evidence_digest'] == normalized_evidence
            ):
                self._append_event_locked(
                    connection,
                    case,
                    event_type='goal_terminal_reconfirmed',
                    recorded_at=observed_at,
                    worker_id=canonical.worker_id,
                    fence_epoch=canonical.fence_epoch,
                    attempt_id=canonical.attempt_id,
                    status=status,
                    evidence_digest=normalized_evidence,
                )
            else:
                case['state'] = 'blocked_conflict'
                self._append_event_locked(
                    connection,
                    case,
                    event_type='goal_terminal_conflict',
                    recorded_at=observed_at,
                    worker_id=canonical.worker_id,
                    fence_epoch=canonical.fence_epoch,
                    attempt_id=canonical.attempt_id,
                    status=status,
                    evidence_digest=normalized_evidence,
                )
            self._update_case_locked(connection, case)
            return self._observation_from_row(case)

    def record_quiescence(
        self,
        token: Nav2ReconcileAttemptToken,
        *,
        evidence_digest: str,
        now: Optional[float] = None,
    ) -> Nav2ReconcileObservation:
        """Mint a full-drop certificate after terminality and trusted quiet."""
        normalized_evidence = _digest(
            evidence_digest, 'evidence_digest'
        )
        selected_now = _boottime() if now is None else now
        with self._write_transaction() as connection:
            observed_at = self._advance_clock_locked(connection, selected_now)
            canonical = self._load_exact_token_locked(connection, token)
            if canonical.kind != 'quiescence':
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'attempt kind conflicts'
                )
            case = self._load_case_locked(
                connection, canonical.operation_id
            )
            self._require_live_lease(
                case,
                worker_id=canonical.worker_id,
                fence_epoch=canonical.fence_epoch,
                now=observed_at,
                token_expires_at=canonical.lease_expires_at,
            )
            if case['state'] != 'blocked_terminal_observed':
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'quiescence requires terminal evidence'
                )
            if observed_at < case['quiescence_not_before']:
                raise GazeboMonitorRoomNav2ReconcileConflictError(
                    'quiescence dwell is incomplete'
                )
            certificate = _full_drop_digest(
                case, canonical, normalized_evidence, observed_at
            )
            case['state'] = 'released_quiescent'
            case['quiescence_evidence_digest'] = normalized_evidence
            case['quiescence_observed_at'] = observed_at
            case['full_drop_certificate_digest'] = certificate
            case['lease_owner'] = None
            case['lease_expires_at'] = None
            self._append_event_locked(
                connection,
                case,
                event_type='full_drop_certified',
                recorded_at=observed_at,
                worker_id=canonical.worker_id,
                fence_epoch=canonical.fence_epoch,
                attempt_id=canonical.attempt_id,
                status=case['terminal_status'],
                evidence_digest=certificate,
            )
            self._update_case_locked(connection, case)
            return self._observation_from_row(case)
