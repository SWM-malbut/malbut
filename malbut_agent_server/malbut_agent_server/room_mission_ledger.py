"""Durable, simulation-only authorization ledger for room missions.

The ledger persists proposal replay fences, confirmation consumption,
execution leases, phase intents, terminal results, and a pending feedback
handoff seam.  It
does not call an adapter and it cannot make a physical adapter exactly once.
Physical enablement still requires an external executor that reconciles
stable operation IDs and enforces fencing epochs.

Confirmation objects are persistence envelopes, not authentication proof.
Integration must call the existing trusted confirmation resolver and current
authority validator before constructing an envelope or using this ledger.
Feedback claiming is owner-bound in this increment.  Recovery after owner
credential deletion remains blocked on a future server-only maintenance
capability; there is no unauthenticated list or orphan mutation surface.
Delivery must use ``feedback_id`` as its destination idempotency key.  A
database lease fences receipt commits but cannot prevent duplicate external
sends, so this handoff seam does not claim external exactly-once delivery.

Version one is intentionally fixed to honest simulation markers.  Adding a
physical runtime is a schema and controller change, not a configuration
switch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from malbut_agent_server.schemas import validate_user_id


ROOM_MISSION_SCHEMA_VERSION = 1
ROOM_MISSION_WRITER_PROTOCOL_VERSION = 1
MAX_MISSION_RECORDS = 4096
MAX_EVENTS_PER_MISSION = 256
MAX_IDENTIFIER_LENGTH = 128
MAX_EVENT_PAYLOAD_BYTES = 4096
MAX_CLOCK_SKEW_SECONDS = 1.0
MAX_AUTHORIZATION_TTL_SECONDS = 10.0
MAX_CONNECTION_ATTESTATION_ROWS = 64
CONNECTION_ATTESTATION_TTL_SECONDS = 300.0

PROPOSAL_STATUSES = frozenset({
    'proposed',
    'confirmed',
    'denied',
    'timed_out',
    'failed',
})
PROPOSAL_INVALIDATION_CODES = frozenset({
    'authority_revoked',
    'source_changed',
    'map_changed',
    'device_changed',
})
EXECUTION_STATUSES = frozenset({
    'pending',
    'leased',
    'running',
    'cancelling',
    'reconcile_required',
    'succeeded',
    'failed',
    'cancelled',
    'timed_out',
})
ACTIVE_EXECUTION_STATUSES = frozenset({
    'pending',
    'leased',
    'running',
    'cancelling',
    'reconcile_required',
})
TERMINAL_EXECUTION_STATUSES = frozenset({
    'succeeded',
    'failed',
    'cancelled',
    'timed_out',
})
MISSION_PHASES = (
    'confirmation',
    'preflight',
    'navigating',
    'coverage',
    'live_ready',
    'terminal',
)
EXECUTABLE_PHASES = MISSION_PHASES[1:-1]
PHASE_OUTCOMES = frozenset({'succeeded', 'failed', 'timed_out'})
FEEDBACK_ORPHAN_CODES = frozenset({
    'conversation_missing',
    'conversation_closed',
    'conversation_reset',
    'owner_unavailable',
    'delivery_rejected',
})
ABORT_EXECUTION_CODES = frozenset({
    'authority_revoked',
    'state_unavailable',
    'state_stale',
    'privacy_blocked',
    'emergency_stop',
    'map_changed',
    'device_unavailable',
})
RECONCILIATION_FAILURE_CODE = 'recovery_unavailable'

_TABLES = (
    'room_mission_schema_metadata',
    'room_mission_store_state',
    'room_mission_proposals',
    'room_mission_confirmations',
    'room_mission_executions',
    'room_mission_events',
    'room_mission_feedback',
    'room_mission_connection_attestations',
)


class RoomMissionLedgerError(RuntimeError):
    """Base class for sanitized durable-ledger failures."""


class RoomMissionLedgerValidationError(ValueError):
    """Report an invalid ledger input without reflecting its value."""


class RoomMissionLedgerConflictError(RoomMissionLedgerError):
    """Report reuse of an immutable identifier with different input."""


class RoomMissionLedgerAuthorityError(RoomMissionLedgerError):
    """Hide whether an inaccessible mission record exists."""


class RoomMissionLedgerCapacityError(RoomMissionLedgerError):
    """Report the fail-closed durable record cap."""


class RoomMissionLedgerBusyError(RoomMissionLedgerError):
    """Report an already-active device or execution lease."""


class RoomMissionLedgerStateError(RoomMissionLedgerError):
    """Report an invalid durable state transition."""


class RoomMissionLedgerClockError(RoomMissionLedgerError):
    """Report an invalid or rolled-back durable wall clock."""


class RoomMissionLedgerSchemaError(RoomMissionLedgerError):
    """Report an incompatible or corrupt room-mission schema."""


@dataclass(frozen=True, repr=False)
class DurableMissionAuthority:
    """Full restart-safe principal and conversation ownership binding."""

    subject_id: str
    auth_session_id: str
    conversation_id: str
    conversation_session_instance_id: str
    proposal_turn_id: str
    request_id: str
    conversation_generation: int
    conversation_revision: int
    conversation_ordinal: int
    authority_digest: str

    def __post_init__(self) -> None:
        """Validate the immutable server-resolved binding."""
        if validate_user_id(self.subject_id) != self.subject_id:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        for value in (
            self.auth_session_id,
            self.conversation_id,
            self.conversation_session_instance_id,
            self.proposal_turn_id,
            self.request_id,
        ):
            _identifier(value)
        for value in (
            self.conversation_generation,
            self.conversation_revision,
            self.conversation_ordinal,
        ):
            if type(value) is not int or value < 0:
                raise RoomMissionLedgerValidationError(
                    'mission authority is invalid'
                )
        _digest(self.authority_digest)

    @property
    def binding_digest(self) -> str:
        """Return a canonical owner digest used for restart comparisons."""
        return _authority_binding_digest({
            'subject_id': self.subject_id,
            'auth_session_digest': _text_digest(self.auth_session_id),
            'conversation_id': self.conversation_id,
            'conversation_session_instance_id': (
                self.conversation_session_instance_id
            ),
            'proposal_turn_id': self.proposal_turn_id,
            'request_id': self.request_id,
            'conversation_generation': self.conversation_generation,
            'conversation_revision': self.conversation_revision,
            'conversation_ordinal': self.conversation_ordinal,
            'authority_digest': self.authority_digest,
        })

    def __repr__(self) -> str:
        """Avoid putting principal and conversation data in logs."""
        return '<DurableMissionAuthority trusted>'


@dataclass(frozen=True, repr=False)
class DurableMissionProposal:
    """Validated immutable proposal material safe for durable binding."""

    authority: DurableMissionAuthority
    decision_id: str
    arguments_digest: str
    device_id: str
    device_binding_digest: str
    map_id: str
    map_revision: str
    room_id: str
    plan_digest: str
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        """Reject incomplete, unbounded, or non-simulation bindings."""
        if type(self.authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission proposal is invalid'
            )
        for value in (
            self.decision_id,
            self.device_id,
            self.map_id,
            self.room_id,
        ):
            _identifier(value)
        for value in (
            self.arguments_digest,
            self.device_binding_digest,
            self.map_revision,
            self.plan_digest,
        ):
            _digest(value)
        issued = _timestamp(self.issued_at)
        expires = _timestamp(self.expires_at)
        if (
            issued >= expires
            or expires - issued > MAX_AUTHORIZATION_TTL_SECONDS
        ):
            raise RoomMissionLedgerValidationError(
                'mission proposal time is invalid'
            )

    @property
    def request_fingerprint(self) -> str:
        """Return the complete immutable proposal fingerprint."""
        return _json_digest({
            'authority_binding_digest': self.authority.binding_digest,
            'decision_id': self.decision_id,
            'arguments_digest': self.arguments_digest,
            'device_id': self.device_id,
            'device_binding_digest': self.device_binding_digest,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'room_id': self.room_id,
            'plan_digest': self.plan_digest,
            'issued_at': float(self.issued_at),
            'expires_at': float(self.expires_at),
            'runtime_mode': 'simulation',
        })

    def __repr__(self) -> str:
        """Avoid reflecting room, device, or authority details."""
        return '<DurableMissionProposal trusted>'


@dataclass(frozen=True, repr=False)
class DurableMissionConfirmation:
    """Resolved evidence envelope bound to one complete proposal."""

    confirmation_id: str
    authority: DurableMissionAuthority
    decision_id: str
    arguments_digest: str
    evidence_digest: str
    issuer_id: str
    person_subject_id: str
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        """Validate the content-free confirmation envelope."""
        if type(self.authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission confirmation is invalid'
            )
        for value in (
            self.confirmation_id,
            self.decision_id,
            self.issuer_id,
            self.person_subject_id,
        ):
            _identifier(value)
        _digest(self.arguments_digest)
        _digest(self.evidence_digest)
        issued = _timestamp(self.issued_at)
        expires = _timestamp(self.expires_at)
        if (
            issued >= expires
            or expires - issued > MAX_AUTHORIZATION_TTL_SECONDS
        ):
            raise RoomMissionLedgerValidationError(
                'mission confirmation time is invalid'
            )

    def fingerprint(self, proposal_id: str) -> str:
        """Return the complete consume-once evidence fingerprint."""
        _identifier(proposal_id)
        return _json_digest({
            'proposal_id': proposal_id,
            'authority_binding_digest': self.authority.binding_digest,
            'decision_id': self.decision_id,
            'arguments_digest': self.arguments_digest,
            'evidence_digest': self.evidence_digest,
            'issuer_id': self.issuer_id,
            'person_subject_id': self.person_subject_id,
            'issued_at': float(self.issued_at),
            'expires_at': float(self.expires_at),
        })

    def __repr__(self) -> str:
        """Avoid reflecting identity evidence in logs."""
        return '<DurableMissionConfirmation trusted>'


@dataclass(frozen=True)
class StoredMissionProposal:
    """Content-free durable proposal result."""

    proposal_id: str
    status: str
    cached: bool


@dataclass(frozen=True)
class StoredMissionAuthorization:
    """Content-free confirmation consumption result."""

    proposal_id: str
    status: str
    tool_call_id: Optional[str]
    cached: bool


@dataclass(frozen=True, repr=False)
class ExecutionLease:
    """Opaque short-lived fencing capability for one execution worker."""

    tool_call_id: str
    lease_epoch: int
    lease_token: str
    expires_at: float
    recovery_required: bool

    def __repr__(self) -> str:
        """Hide the lease bearer token."""
        return '<ExecutionLease opaque>'


@dataclass(frozen=True)
class PhaseIntent:
    """One stable adapter operation written before any external call."""

    tool_call_id: str
    phase: str
    operation_id: str
    state_revision: int
    cached: bool = False


@dataclass(frozen=True)
class RecoveryPhaseIntent:
    """An unresolved operation that must be observed, not dispatched."""

    tool_call_id: str
    phase: str
    operation_id: str
    state_revision: int


@dataclass(frozen=True)
class CancelIntent:
    """Stable cancellation operation written before adapter dispatch."""

    tool_call_id: str
    operation_id: str
    state_revision: int
    superseded_phase_operation_id: Optional[str]
    cached: bool = False


@dataclass(frozen=True, repr=False)
class CancellationRequest:
    """Cancellation intent plus an optional already-proven lease."""

    intent: CancelIntent
    lease: Optional[ExecutionLease]
    pending_lease: bool

    def __repr__(self) -> str:
        """Hide a nested lease bearer token, when present."""
        return '<CancellationRequest opaque>'


@dataclass(frozen=True)
class StoredMissionExecution:
    """Content-free execution state safe for replay and recovery."""

    tool_call_id: str
    status: str
    phase: str
    code: str
    state_revision: int
    active_operation_id: Optional[str]
    lease_epoch: int
    lease_expires_at: Optional[float]
    terminal_digest: Optional[str]
    cancel_requested: bool = False
    cancel_operation_id: Optional[str] = None
    viewer_live: bool = False
    simulated: bool = True
    physical_effects: bool = False
    durability: str = 'sqlite_local'
    lease_scope: str = 'database_device'


@dataclass(frozen=True)
class RecoveryCandidate:
    """Content-free nonterminal row discovered after a restart."""

    tool_call_id: str
    status: str
    phase: str
    device_id: str
    has_unresolved_intent: bool
    lease_epoch: int
    lease_expires_at: Optional[float]
    cancel_requested: bool = False
    cancel_operation_id: Optional[str] = None


@dataclass(frozen=True)
class MissionLedgerEvent:
    """Bounded, content-free durable event."""

    tool_call_id: str
    sequence: int
    event_kind: str
    phase: str
    source: str
    status: str
    code: str
    operation_id: Optional[str]
    observed_at: float


@dataclass(frozen=True, repr=False)
class FeedbackLease:
    """Opaque short-lived fencing capability for feedback delivery."""

    feedback_id: str
    lease_epoch: int
    lease_token: str
    expires_at: Optional[float]
    cached: bool = False

    def __repr__(self) -> str:
        """Hide the feedback bearer token."""
        return '<FeedbackLease opaque>'


@dataclass(frozen=True)
class StoredFeedback:
    """Content-free feedback handoff state safe for owner reads."""

    feedback_id: str
    tool_call_id: str
    state: str
    terminal_digest: str
    lease_epoch: int
    lease_expires_at: Optional[float]
    response_commit_id: Optional[str]
    conversation_revision_after: Optional[int]
    orphan_code: Optional[str]
    cached: bool = False


class _StoreRuntimeState:
    """Process-local synchronization and monotonic clock anchor."""

    __slots__ = (
        'lock',
        'last_wall_snapshot',
        'last_snapshot_started',
        'clock_offset',
        'identity_bound',
        'connection',
        'database_path',
        'configured_database_path',
        'durability',
        'lease_scope',
        'attested_main_path',
        'attested_file_device',
        'attested_file_inode',
        'closed',
    )

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.last_wall_snapshot = None
        self.last_snapshot_started = None
        self.clock_offset = 0.0
        self.identity_bound = False
        self.connection = None
        self.database_path = None
        self.configured_database_path = None
        self.durability = None
        self.lease_scope = None
        self.attested_main_path = None
        self.attested_file_device = None
        self.attested_file_inode = None
        self.closed = False


_STORE_RUNTIME_REGISTRY_GUARD = threading.Lock()
_STORE_RUNTIMES = weakref.WeakKeyDictionary()


def _register_store_lock(store: Any) -> None:
    """Keep the synchronization primitive outside mutable instance state."""
    with _STORE_RUNTIME_REGISTRY_GUARD:
        if store in _STORE_RUNTIMES:
            raise RoomMissionLedgerStateError(
                'room mission store lock is unavailable'
            )
        _STORE_RUNTIMES[store] = _StoreRuntimeState()


def _registered_store_runtime(store: Any) -> _StoreRuntimeState:
    """Return process state that cannot be replaced through the instance."""
    with _STORE_RUNTIME_REGISTRY_GUARD:
        runtime = _STORE_RUNTIMES.get(store)
    if runtime is None:
        raise RoomMissionLedgerStateError(
            'room mission store runtime is unavailable'
        )
    return runtime


def _bind_store_identity(
    store: Any,
    *,
    connection: sqlite3.Connection,
    database_path: str,
    configured_database_path: str,
    durability: str,
    lease_scope: str,
    attested_main_path: Optional[str],
    attested_file_device: Optional[int],
    attested_file_inode: Optional[int],
) -> None:
    """Bind the original persistence identity outside instance state."""
    with _STORE_RUNTIME_REGISTRY_GUARD:
        runtime = _STORE_RUNTIMES.get(store)
        if runtime is None or runtime.identity_bound:
            raise RoomMissionLedgerStateError(
                'room mission store identity is unavailable'
            )
        runtime.connection = connection
        runtime.database_path = database_path
        runtime.configured_database_path = configured_database_path
        runtime.durability = durability
        runtime.lease_scope = lease_scope
        runtime.attested_main_path = attested_main_path
        runtime.attested_file_device = attested_file_device
        runtime.attested_file_inode = attested_file_inode
        runtime.identity_bound = True


def _registered_store_lock(store: Any) -> Any:
    """Return the original store lock despite instance-dict shadows."""
    return _registered_store_runtime(store).lock


class _StoreLockDescriptor:
    """Data descriptor for an unshadowable registered store lock."""

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is None:
            return self
        return _registered_store_lock(instance)

    def __set__(self, instance: Any, value: Any) -> None:
        del instance, value
        raise AttributeError('room mission store identity is immutable')

    def __delete__(self, instance: Any) -> None:
        del instance
        raise AttributeError('room mission store identity is immutable')


class SQLiteRoomMissionStore:
    """Thread-safe SQLite source of truth for simulated room missions."""

    _lock = _StoreLockDescriptor()

    def __setattr__(self, name: str, value: Any) -> None:
        """Prevent runtime mutation of persistence identity markers."""
        if (
            name in (
                'durability', 'lease_scope',
                '_IMMUTABLE_IDENTITY_FIELDS',
                'assert_durable_identity',
                '_assert_durable_identity_impl',
                '_durable_identity_matches_locked',
                '_lock', '__class__', '__dict__',
            )
            or (
                name in (
                    'database_path', '_durability', '_lease_scope',
                    '_configured_database_path',
                    '_connection', '_attested_connection',
                    '_attested_main_path', '_attested_file_device',
                    '_attested_file_inode', '_closed',
                )
                and name in self.__dict__
            )
        ):
            raise AttributeError(
                'room mission store identity is immutable'
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        """Prevent deletion of persistence identity markers."""
        if name in (
            'database_path', 'durability', 'lease_scope',
            '_durability', '_lease_scope',
            '_configured_database_path', '_connection',
            '_attested_connection', '_attested_main_path',
            '_attested_file_device', '_attested_file_inode',
            '_closed', '_lock', '_IMMUTABLE_IDENTITY_FIELDS',
            'assert_durable_identity',
            '_assert_durable_identity_impl',
            '_durable_identity_matches_locked',
            '__class__', '__dict__',
        ):
            raise AttributeError(
                'room mission store identity is immutable'
            )
        object.__delattr__(self, name)

    @property
    def durability(self) -> str:
        """Return the immutable truthful persistence scope."""
        SQLiteRoomMissionStore._assert_durable_identity_impl(self)
        return self._durability

    @property
    def lease_scope(self) -> str:
        """Return the immutable truthful lease coordination scope."""
        SQLiteRoomMissionStore._assert_durable_identity_impl(self)
        return self._lease_scope

    @property
    def assert_durable_identity(self) -> Callable[[], None]:
        """Return the fixed live SQLite identity verifier."""
        return SQLiteRoomMissionStore._assert_durable_identity_impl.__get__(
            self, SQLiteRoomMissionStore
        )

    def _assert_durable_identity_impl(self) -> None:
        """Verify that this store still uses its attested SQLite main DB."""
        valid = False
        try:
            with self._lock:
                valid = (
                    SQLiteRoomMissionStore
                    ._durable_identity_matches_locked(
                        self
                    )
                )
        except Exception:
            pass
        if not valid:
            _raise_sanitized(
                'room mission ledger identity verification failed'
            )

    def _durable_identity_matches_locked(
        self,
        *,
        require_private_permissions: bool = True,
    ) -> bool:
        """Return whether immutable markers match the live connection."""
        runtime = _registered_store_runtime(self)
        if (
            not runtime.identity_bound
            or runtime.closed
            or self._closed is not runtime.closed
            or self._connection is not runtime.connection
            or self._attested_connection is not runtime.connection
            or self.database_path != runtime.database_path
            or self._configured_database_path
            != runtime.configured_database_path
            or self._durability != runtime.durability
            or self._lease_scope != runtime.lease_scope
            or self._attested_main_path != runtime.attested_main_path
            or self._attested_file_device
            != runtime.attested_file_device
            or self._attested_file_inode != runtime.attested_file_inode
        ):
            return False
        connection = runtime.connection
        main_rows = [
            row
            for row in connection.execute('PRAGMA database_list')
            if str(row[1]) == 'main'
        ]
        if len(main_rows) != 1:
            return False
        main_path = str(main_rows[0][2])
        foreign_keys = connection.execute(
            'PRAGMA foreign_keys'
        ).fetchone()
        synchronous = connection.execute(
            'PRAGMA synchronous'
        ).fetchone()
        journal_mode = connection.execute(
            'PRAGMA journal_mode'
        ).fetchone()
        expected_journal_mode = (
            'memory'
            if runtime.database_path == ':memory:'
            else 'wal'
        )
        if (
            foreign_keys is None
            or int(foreign_keys[0]) != 1
            or synchronous is None
            or int(synchronous[0]) != 2
            or journal_mode is None
            or str(journal_mode[0]).lower() != expected_journal_mode
        ):
            return False
        if runtime.database_path == ':memory:':
            return (
                runtime.database_path == runtime.configured_database_path
                and runtime.durability == 'process_local'
                and runtime.lease_scope == 'store_connection'
                and runtime.attested_main_path is None
                and runtime.attested_file_device is None
                and runtime.attested_file_inode is None
                and main_path == ''
            )
        if (
            runtime.durability != 'sqlite_local'
            or runtime.lease_scope != 'database_device'
            or type(runtime.attested_main_path) is not str
            or type(runtime.attested_file_device) is not int
            or type(runtime.attested_file_inode) is not int
            or not main_path
        ):
            return False
        connected_path = str(Path(main_path).resolve(strict=True))
        connected_stat = os.lstat(connected_path)
        if not (
            runtime.database_path == runtime.configured_database_path
            and connected_path == runtime.attested_main_path
            and stat.S_ISREG(connected_stat.st_mode)
            and connected_stat.st_nlink == 1
            and connected_stat.st_uid == os.geteuid()
            and connected_stat.st_dev == runtime.attested_file_device
            and connected_stat.st_ino == runtime.attested_file_inode
        ):
            return False
        SQLiteRoomMissionStore._validate_parent_directory(connected_path)
        for suffix in ('', '-wal', '-shm'):
            candidate = connected_path + suffix
            if not os.path.lexists(candidate):
                if not suffix:
                    return False
                continue
            try:
                candidate_stat = os.lstat(candidate)
            except FileNotFoundError:
                if suffix:
                    continue
                return False
            if (
                stat.S_ISLNK(candidate_stat.st_mode)
                or not stat.S_ISREG(candidate_stat.st_mode)
                or candidate_stat.st_nlink != 1
                or candidate_stat.st_uid != os.geteuid()
                or (
                    require_private_permissions
                    and stat.S_IMODE(candidate_stat.st_mode) != 0o600
                )
            ):
                return False
        return True

    def __init__(
        self,
        database_path: str,
        *,
        max_mission_records: int = 256,
        lease_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        """Open or create the versioned room-mission ledger."""
        if type(database_path) is not str or not database_path:
            raise ValueError('database_path must not be empty')
        if (
            type(max_mission_records) is not int
            or not 1 <= max_mission_records <= MAX_MISSION_RECORDS
        ):
            raise ValueError('max_mission_records is invalid')
        if (
            type(lease_seconds) not in {int, float}
            or not math.isfinite(float(lease_seconds))
            or not 0.05 <= float(lease_seconds) <= 120.0
        ):
            raise ValueError('lease_seconds is invalid')
        if not callable(clock) or not callable(id_factory):
            raise ValueError('ledger dependency is invalid')
        self.database_path = database_path
        self._configured_database_path = database_path
        self._max_mission_records = max_mission_records
        self._lease_seconds = float(lease_seconds)
        self._clock = clock
        self._clock_is_system = clock is time.time
        self._id_factory = id_factory
        _register_store_lock(self)
        self._closed = False
        durability = (
            'process_local'
            if database_path == ':memory:'
            else 'sqlite_local'
        )
        lease_scope = (
            'store_connection'
            if database_path == ':memory:'
            else 'database_device'
        )
        self._durability = durability
        self._lease_scope = lease_scope
        connection = None
        failed = False
        try:
            if database_path != ':memory:':
                Path(database_path).expanduser().parent.mkdir(
                    parents=True,
                    exist_ok=True,
                    mode=0o700,
                )
                (
                    path,
                    attested_file_device,
                    attested_file_inode,
                ) = self._prepare_database_file()
                attested_main_path = path
            else:
                path = database_path
                attested_main_path = None
                attested_file_device = None
                attested_file_inode = None
            connection = sqlite3.connect(
                path,
                check_same_thread=False,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.create_function(
                'room_mission_writer_protocol_version',
                0,
                lambda: ROOM_MISSION_WRITER_PROTOCOL_VERSION,
                deterministic=True,
            )
            self._connection = connection
            self._attested_connection = connection
            self._attested_main_path = attested_main_path
            self._attested_file_device = attested_file_device
            self._attested_file_inode = attested_file_inode
            _bind_store_identity(
                self,
                connection=connection,
                database_path=database_path,
                configured_database_path=database_path,
                durability=durability,
                lease_scope=lease_scope,
                attested_main_path=attested_main_path,
                attested_file_device=attested_file_device,
                attested_file_inode=attested_file_inode,
            )
            with self._lock:
                self._configure_connection_locked()
            SQLiteRoomMissionStore._secure_file_permissions(
                self, provision=True
            )
            self.assert_durable_identity()
            self._initialize()
            SQLiteRoomMissionStore._secure_file_permissions(
                self, provision=True
            )
            self.assert_durable_identity()
            self._verify_connection_path_binding()
            SQLiteRoomMissionStore._secure_file_permissions(
                self, provision=True
            )
            self.assert_durable_identity()
        except RoomMissionLedgerError:
            if connection is not None:
                connection.close()
            raise
        except Exception:
            if connection is not None:
                connection.close()
            failed = True
        if failed:
            _raise_sanitized('room mission ledger initialization failed')

    def _initialize(self) -> None:
        with self._lock:
            self._configure_connection_locked()
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                existing = self._require_compatible_schema_locked()
                if existing:
                    self._verify_schema_locked()
                    self._verify_writer_gates_locked()
                else:
                    self._create_schema_locked()
                    self._verify_schema_locked()
                    self._install_writer_gates_locked()
                    self._store_schema_fingerprint_locked()
                self._verify_invariants_locked()
                self._connection.commit()
            except Exception:
                self._rollback_locked()
                raise

    def _configure_connection_locked(self) -> None:
        """Apply the fixed SQLite durability policy."""
        self._connection.execute('PRAGMA busy_timeout=5000')
        self._connection.execute('PRAGMA foreign_keys=ON')
        self._connection.execute('PRAGMA synchronous=FULL')
        if self.database_path != ':memory:':
            self._enable_wal_locked()

    def _verify_connection_path_binding(self) -> None:
        """Prove that the live connection writes the attested main path."""
        if self.database_path == ':memory:':
            return
        probe_digest = _text_digest(
            'room-mission-connection-probe/v1|'
            + secrets.token_hex(32)
        )
        observed_at = time.time()
        inserted = False
        observed = False
        failure = False
        with self._lock:
            try:
                self._require_durable_identity_locked()
                self._connection.execute('BEGIN IMMEDIATE')
                self._connection.execute(
                    '''
                    DELETE FROM room_mission_connection_attestations
                    WHERE created_at < ?
                    ''',
                    (
                        observed_at
                        - CONNECTION_ATTESTATION_TTL_SECONDS,
                    ),
                )
                count = self._connection.execute(
                    'SELECT COUNT(*) '
                    'FROM room_mission_connection_attestations'
                ).fetchone()
                if int(count[0]) >= MAX_CONNECTION_ATTESTATION_ROWS:
                    raise RoomMissionLedgerSchemaError(
                        'room mission connection probe capacity reached'
                    )
                self._connection.execute(
                    '''
                    INSERT INTO room_mission_connection_attestations (
                        probe_digest,
                        created_at
                    ) VALUES (?, ?)
                    ''',
                    (probe_digest, observed_at),
                )
                self._connection.commit()
                inserted = True
                self._checkpoint_attestation_locked()
            except Exception:
                self._rollback_locked()
                failure = True

        witness = None
        if not failure:
            try:
                with self._lock:
                    self._require_durable_identity_locked()
                witness_uri = (
                    Path(self._attested_main_path).as_uri()
                    + '?mode=ro&immutable=1'
                )
                witness = sqlite3.connect(
                    witness_uri,
                    uri=True,
                    timeout=5.0,
                )
                row = witness.execute(
                    '''
                    SELECT probe_digest
                    FROM room_mission_connection_attestations
                    WHERE probe_digest = ?
                    ''',
                    (probe_digest,),
                ).fetchone()
                observed = (
                    row is not None
                    and hmac.compare_digest(str(row[0]), probe_digest)
                )
                with self._lock:
                    self._require_durable_identity_locked()
            except Exception:
                failure = True
            finally:
                if witness is not None:
                    try:
                        witness.close()
                    except Exception:
                        failure = True

        if inserted:
            with self._lock:
                try:
                    self._connection.execute('BEGIN IMMEDIATE')
                    deleted = self._connection.execute(
                        '''
                        DELETE FROM room_mission_connection_attestations
                        WHERE probe_digest = ?
                        ''',
                        (probe_digest,),
                    )
                    self._connection.commit()
                    self._checkpoint_attestation_locked()
                    if deleted.rowcount != 1:
                        failure = True
                except Exception:
                    self._rollback_locked()
                    failure = True
        if failure or not observed:
            raise RoomMissionLedgerSchemaError(
                'room mission connection binding failed'
            )

    def _checkpoint_attestation_locked(self) -> None:
        """Force a connection probe through the live main file handle."""
        for attempt in range(50):
            row = self._connection.execute(
                'PRAGMA wal_checkpoint(TRUNCATE)'
            ).fetchone()
            if row is not None and int(row[0]) == 0:
                return
            time.sleep(min(0.005 * (attempt + 1), 0.05))
        raise RoomMissionLedgerSchemaError(
            'room mission connection checkpoint failed'
        )

    def _enable_wal_locked(self) -> None:
        """Enable WAL with bounded retry during concurrent first opens."""
        failure = None
        for attempt in range(50):
            try:
                row = self._connection.execute(
                    'PRAGMA journal_mode=WAL'
                ).fetchone()
                if row is not None and str(row[0]).lower() == 'wal':
                    return
                failure = True
            except sqlite3.OperationalError as error:
                failure = error
                if 'locked' not in str(error).lower():
                    break
            time.sleep(min(0.005 * (attempt + 1), 0.05))
        del failure
        raise RoomMissionLedgerSchemaError(
            'room mission WAL initialization failed'
        )

    def _require_compatible_schema_locked(self) -> bool:
        tables = {
            str(row['name'])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row['name']).startswith('room_mission_')
        }
        metadata_exists = 'room_mission_schema_metadata' in tables
        if not metadata_exists:
            if tables:
                raise RoomMissionLedgerSchemaError(
                    'room mission schema metadata is missing'
                )
            return False
        row = self._connection.execute(
            '''
            SELECT schema_version,
                   min_writer_protocol,
                   max_writer_protocol,
                   schema_fingerprint
            FROM room_mission_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if row is None:
            raise RoomMissionLedgerSchemaError(
                'room mission schema metadata is incomplete'
            )
        if (
            int(row['schema_version']) != ROOM_MISSION_SCHEMA_VERSION
            or not (
                int(row['min_writer_protocol'])
                <= ROOM_MISSION_WRITER_PROTOCOL_VERSION
                <= int(row['max_writer_protocol'])
            )
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission schema is incompatible'
            )
        _digest(str(row['schema_fingerprint']))
        self._verify_schema_fingerprint_locked(
            str(row['schema_fingerprint'])
        )
        return True

    def _create_schema_locked(self) -> None:
        now = self._now()
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS room_mission_schema_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                min_writer_protocol INTEGER NOT NULL,
                max_writer_protocol INTEGER NOT NULL,
                schema_fingerprint TEXT NOT NULL,
                migrated_at REAL NOT NULL
            )
            '''
        )
        row = self._connection.execute(
            '''
            SELECT singleton
            FROM room_mission_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if row is None:
            count = self._connection.execute(
                'SELECT COUNT(*) AS row_count '
                'FROM room_mission_schema_metadata'
            ).fetchone()
            if int(count['row_count']) != 0:
                raise RoomMissionLedgerSchemaError(
                    'room mission schema metadata is invalid'
                )
            self._connection.execute(
                '''
                INSERT INTO room_mission_schema_metadata (
                    singleton,
                    schema_version,
                    min_writer_protocol,
                    max_writer_protocol,
                    schema_fingerprint,
                    migrated_at
                ) VALUES (1, ?, ?, ?, ?, ?)
                ''',
                (
                    ROOM_MISSION_SCHEMA_VERSION,
                    ROOM_MISSION_WRITER_PROTOCOL_VERSION,
                    ROOM_MISSION_WRITER_PROTOCOL_VERSION,
                    _text_digest('schema-pending'),
                    now,
                ),
            )
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS room_mission_store_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                proposal_count INTEGER NOT NULL
                    CHECK (proposal_count >= 0),
                record_capacity INTEGER NOT NULL
                    CHECK (record_capacity >= 1 AND record_capacity <= 4096),
                last_observed_at REAL NOT NULL
            )
            '''
        )
        self._connection.execute(
            '''
            INSERT OR IGNORE INTO room_mission_store_state (
                singleton,
                revision,
                proposal_count,
                record_capacity,
                last_observed_at
            ) VALUES (1, 0, 0, ?, ?)
            ''',
            (self._max_mission_records, now),
        )
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS room_mission_proposals (
                proposal_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL,
                owner_binding_digest TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                auth_session_digest TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                conversation_session_instance_id TEXT NOT NULL,
                proposal_turn_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                conversation_generation INTEGER NOT NULL
                    CHECK (conversation_generation >= 0),
                conversation_revision INTEGER NOT NULL
                    CHECK (conversation_revision >= 0),
                conversation_ordinal INTEGER NOT NULL
                    CHECK (conversation_ordinal >= 0),
                authority_digest TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                device_id TEXT NOT NULL,
                device_binding_digest TEXT NOT NULL,
                map_id TEXT NOT NULL,
                map_revision TEXT NOT NULL,
                room_id TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'proposed',
                        'confirmed',
                        'denied',
                        'timed_out',
                        'failed'
                    )
                ),
                terminal_code TEXT,
                runtime_mode TEXT NOT NULL DEFAULT 'simulation'
                    CHECK (runtime_mode = 'simulation'),
                simulated INTEGER NOT NULL DEFAULT 1
                    CHECK (simulated = 1),
                physical_effects INTEGER NOT NULL DEFAULT 0
                    CHECK (physical_effects = 0),
                viewer_live INTEGER NOT NULL DEFAULT 0
                    CHECK (viewer_live = 0),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (subject_id, request_id),
                CHECK (expires_at > issued_at)
            )
            '''
        )
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS room_mission_confirmations (
                confirmation_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL UNIQUE,
                confirmation_fingerprint TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                issuer_id TEXT NOT NULL,
                person_subject_id TEXT NOT NULL,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('consumed', 'timed_out')
                ),
                consumed_at REAL,
                tool_call_id TEXT UNIQUE,
                FOREIGN KEY (proposal_id)
                    REFERENCES room_mission_proposals (proposal_id)
                    ON DELETE RESTRICT,
                CHECK (expires_at > issued_at),
                CHECK (
                    (status = 'consumed'
                     AND consumed_at IS NOT NULL
                     AND tool_call_id IS NOT NULL)
                    OR
                    (status = 'timed_out'
                     AND consumed_at IS NULL
                     AND tool_call_id IS NULL)
                )
            )
            '''
        )
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS room_mission_executions (
                tool_call_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL UNIQUE,
                confirmation_id TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending',
                        'leased',
                        'running',
                        'cancelling',
                        'reconcile_required',
                        'succeeded',
                        'failed',
                        'cancelled',
                        'timed_out'
                    )
                ),
                phase TEXT NOT NULL CHECK (
                    phase IN (
                        'confirmation',
                        'preflight',
                        'navigating',
                        'coverage',
                        'live_ready',
                        'terminal'
                    )
                ),
                code TEXT NOT NULL,
                state_revision INTEGER NOT NULL
                    CHECK (state_revision >= 1),
                audit_sequence INTEGER NOT NULL
                    CHECK (audit_sequence >= 1),
                active_operation_id TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0
                    CHECK (cancel_requested IN (0, 1)),
                cancel_operation_id TEXT,
                cancel_target_operation_id TEXT,
                cancel_target_result_code TEXT,
                authorization_deadline REAL NOT NULL,
                lease_owner TEXT,
                lease_token_digest TEXT,
                lease_epoch INTEGER NOT NULL DEFAULT 0
                    CHECK (lease_epoch >= 0),
                lease_expires_at REAL,
                started_at REAL,
                terminal_at REAL,
                terminal_digest TEXT,
                terminal_payload_json TEXT,
                runtime_mode TEXT NOT NULL DEFAULT 'simulation'
                    CHECK (runtime_mode = 'simulation'),
                simulated INTEGER NOT NULL DEFAULT 1
                    CHECK (simulated = 1),
                physical_effects INTEGER NOT NULL DEFAULT 0
                    CHECK (physical_effects = 0),
                viewer_live INTEGER NOT NULL DEFAULT 0
                    CHECK (viewer_live = 0),
                updated_at REAL NOT NULL,
                FOREIGN KEY (proposal_id)
                    REFERENCES room_mission_proposals (proposal_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (confirmation_id)
                    REFERENCES room_mission_confirmations (confirmation_id)
                    ON DELETE RESTRICT,
                CHECK (
                    (status IN (
                        'succeeded', 'failed', 'cancelled', 'timed_out'
                    ) AND phase = 'terminal'
                     AND terminal_at IS NOT NULL
                     AND terminal_digest IS NOT NULL
                     AND terminal_payload_json IS NOT NULL)
                    OR
                    (status NOT IN (
                        'succeeded', 'failed', 'cancelled', 'timed_out'
                    ) AND phase != 'terminal'
                     AND terminal_at IS NULL
                     AND terminal_digest IS NULL
                     AND terminal_payload_json IS NULL)
                ),
                CHECK (
                    (cancel_requested = 0
                     AND cancel_operation_id IS NULL
                     AND cancel_target_operation_id IS NULL
                     AND cancel_target_result_code IS NULL
                     AND status != 'cancelling')
                    OR
                    (cancel_requested = 1
                     AND cancel_operation_id IS NOT NULL
                     AND (
                         cancel_target_result_code IS NULL
                         OR cancel_target_operation_id IS NOT NULL
                     )
                     AND status IN (
                         'cancelling', 'succeeded', 'failed',
                         'cancelled', 'timed_out'
                     ))
                )
            )
            '''
        )
        self._connection.execute(
            '''
            CREATE UNIQUE INDEX IF NOT EXISTS
                room_mission_one_active_device_idx
            ON room_mission_executions (device_id)
            WHERE status IN (
                'pending',
                'leased',
                'running',
                'cancelling',
                'reconcile_required'
            )
            '''
        )
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS room_mission_events (
                tool_call_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (
                    sequence >= 1 AND sequence <= 256
                ),
                event_kind TEXT NOT NULL CHECK (
                    event_kind IN (
                        'authorized',
                        'lease',
                        'intent',
                        'observation',
                        'recovery',
                        'cancel',
                        'terminal',
                        'late_discarded'
                    )
                ),
                phase TEXT NOT NULL CHECK (
                    phase IN (
                        'confirmation',
                        'preflight',
                        'navigating',
                        'coverage',
                        'live_ready',
                        'terminal'
                    )
                ),
                source TEXT NOT NULL CHECK (
                    source IN (
                        'controller',
                        'simulation_adapter',
                        'recovery'
                    )
                ),
                status TEXT NOT NULL,
                code TEXT NOT NULL,
                operation_id TEXT,
                observed_at REAL NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
                    CHECK (length(payload_json) <= 4096),
                PRIMARY KEY (tool_call_id, sequence),
                FOREIGN KEY (tool_call_id)
                    REFERENCES room_mission_executions (tool_call_id)
                    ON DELETE RESTRICT
            )
            '''
        )
        self._connection.execute(
            '''
            CREATE UNIQUE INDEX IF NOT EXISTS
                room_mission_unique_intent_idx
            ON room_mission_events (tool_call_id, operation_id)
            WHERE event_kind = 'intent'
            '''
        )
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS room_mission_feedback (
                feedback_id TEXT PRIMARY KEY,
                tool_call_id TEXT NOT NULL UNIQUE,
                subject_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                conversation_session_instance_id TEXT NOT NULL,
                conversation_generation INTEGER NOT NULL
                    CHECK (conversation_generation >= 0),
                feedback_request_id TEXT NOT NULL UNIQUE,
                feedback_turn_id TEXT NOT NULL,
                terminal_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'leased', 'committed', 'orphaned')
                ),
                lease_owner TEXT,
                lease_token_digest TEXT,
                lease_epoch INTEGER NOT NULL DEFAULT 0
                    CHECK (lease_epoch >= 0),
                lease_expires_at REAL,
                response_commit_id TEXT UNIQUE,
                conversation_revision_after INTEGER CHECK (
                    conversation_revision_after IS NULL
                    OR conversation_revision_after >= 0
                ),
                orphan_code TEXT,
                result_digest TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (tool_call_id)
                    REFERENCES room_mission_executions (tool_call_id)
                    ON DELETE RESTRICT,
                CHECK (
                    (state = 'pending'
                     AND lease_epoch = 0
                     AND lease_owner IS NULL
                     AND lease_token_digest IS NULL
                     AND lease_expires_at IS NULL
                     AND response_commit_id IS NULL
                     AND conversation_revision_after IS NULL
                     AND orphan_code IS NULL
                     AND result_digest IS NULL)
                    OR
                    (state = 'leased'
                     AND lease_epoch >= 1
                     AND lease_owner IS NOT NULL
                     AND lease_token_digest IS NOT NULL
                     AND lease_expires_at IS NOT NULL
                     AND response_commit_id IS NULL
                     AND conversation_revision_after IS NULL
                     AND orphan_code IS NULL
                     AND result_digest IS NULL)
                    OR
                    (state = 'committed'
                     AND lease_epoch >= 1
                     AND lease_owner IS NULL
                     AND lease_token_digest IS NOT NULL
                     AND lease_expires_at IS NULL
                     AND response_commit_id IS NOT NULL
                     AND conversation_revision_after IS NOT NULL
                     AND orphan_code IS NULL
                     AND result_digest IS NOT NULL)
                    OR
                    (state = 'orphaned'
                     AND lease_epoch >= 1
                     AND lease_owner IS NULL
                     AND lease_token_digest IS NOT NULL
                     AND lease_expires_at IS NULL
                     AND response_commit_id IS NULL
                     AND conversation_revision_after IS NULL
                     AND orphan_code IN (
                         'conversation_missing',
                         'conversation_closed',
                         'conversation_reset',
                         'owner_unavailable',
                         'delivery_rejected'
                     )
                     AND result_digest IS NOT NULL)
                )
            )
            '''
        )
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS
                room_mission_connection_attestations (
                    probe_digest TEXT PRIMARY KEY CHECK (
                        length(probe_digest) = 64
                        AND probe_digest NOT GLOB '*[^0-9a-f]*'
                    ),
                    created_at REAL NOT NULL CHECK (
                        created_at >= 0 AND created_at < 1.0e20
                    )
                )
            '''
        )

    def _verify_schema_locked(self) -> None:
        expected = {
            'room_mission_schema_metadata': {
                'singleton', 'schema_version', 'min_writer_protocol',
                'max_writer_protocol', 'schema_fingerprint',
                'migrated_at',
            },
            'room_mission_store_state': {
                'singleton', 'revision', 'proposal_count',
                'record_capacity', 'last_observed_at',
            },
            'room_mission_proposals': {
                'proposal_id', 'decision_id', 'request_fingerprint',
                'owner_binding_digest', 'subject_id',
                'auth_session_digest',
                'conversation_id', 'conversation_session_instance_id',
                'proposal_turn_id', 'request_id',
                'conversation_generation', 'conversation_revision',
                'conversation_ordinal', 'authority_digest',
                'arguments_digest', 'device_id',
                'device_binding_digest', 'map_id', 'map_revision',
                'room_id', 'plan_digest', 'issued_at', 'expires_at',
                'status', 'terminal_code', 'runtime_mode', 'simulated',
                'physical_effects', 'viewer_live', 'created_at',
                'updated_at',
            },
            'room_mission_confirmations': {
                'confirmation_id', 'proposal_id',
                'confirmation_fingerprint', 'evidence_digest',
                'issuer_id', 'person_subject_id', 'issued_at',
                'expires_at', 'status', 'consumed_at', 'tool_call_id',
            },
            'room_mission_executions': {
                'tool_call_id', 'proposal_id', 'confirmation_id',
                'device_id', 'status', 'phase', 'code',
                'state_revision', 'audit_sequence',
                'active_operation_id', 'cancel_requested',
                'cancel_operation_id',
                'cancel_target_operation_id',
                'cancel_target_result_code',
                'authorization_deadline', 'lease_owner',
                'lease_token_digest', 'lease_epoch',
                'lease_expires_at', 'started_at', 'terminal_at',
                'terminal_digest', 'terminal_payload_json',
                'runtime_mode', 'simulated', 'physical_effects',
                'viewer_live', 'updated_at',
            },
            'room_mission_events': {
                'tool_call_id', 'sequence', 'event_kind', 'phase',
                'source', 'status', 'code', 'operation_id',
                'observed_at', 'payload_json',
            },
            'room_mission_feedback': {
                'feedback_id', 'tool_call_id', 'subject_id',
                'conversation_id', 'conversation_session_instance_id',
                'conversation_generation', 'feedback_request_id',
                'feedback_turn_id', 'terminal_digest', 'state',
                'lease_owner', 'lease_token_digest', 'lease_epoch',
                'lease_expires_at', 'response_commit_id',
                'conversation_revision_after', 'created_at',
                'updated_at', 'orphan_code', 'result_digest',
            },
            'room_mission_connection_attestations': {
                'probe_digest', 'created_at',
            },
        }
        for table, required in expected.items():
            columns = {
                str(row['name'])
                for row in self._connection.execute(
                    f'PRAGMA table_info({table})'
                ).fetchall()
            }
            if required != columns:
                raise RoomMissionLedgerSchemaError(
                    'room mission schema is incomplete'
                )
        indexes = {
            str(row['name'])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        if not {
            'room_mission_one_active_device_idx',
            'room_mission_unique_intent_idx',
        } <= indexes:
            raise RoomMissionLedgerSchemaError(
                'room mission schema indexes are incomplete'
            )
        self._verify_index_locked(
            'room_mission_executions',
            'room_mission_one_active_device_idx',
            ('device_id',),
            required_sql=(
                "WHERE status IN ( 'pending', 'leased', 'running', "
                "'cancelling', 'reconcile_required' )"
            ),
        )
        self._verify_index_locked(
            'room_mission_events',
            'room_mission_unique_intent_idx',
            ('tool_call_id', 'operation_id'),
            required_sql="WHERE event_kind = 'intent'",
        )

    def _verify_index_locked(
        self,
        table: str,
        index_name: str,
        columns: Tuple[str, ...],
        *,
        required_sql: str,
    ) -> None:
        index_rows = {
            str(row['name']): row
            for row in self._connection.execute(
                f'PRAGMA index_list({table})'
            ).fetchall()
        }
        row = index_rows.get(index_name)
        observed_columns = tuple(
            str(item['name'])
            for item in self._connection.execute(
                f'PRAGMA index_info({index_name})'
            ).fetchall()
        )
        sql_row = self._connection.execute(
            '''
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index' AND name = ? AND tbl_name = ?
            ''',
            (index_name, table),
        ).fetchone()
        normalized_sql = (
            ' '.join(str(sql_row['sql']).split())
            if sql_row is not None
            else ''
        )
        if (
            row is None
            or int(row['unique']) != 1
            or int(row['partial']) != 1
            or observed_columns != columns
            or required_sql not in normalized_sql
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission schema index is invalid'
            )

    def _verify_writer_gates_locked(self) -> None:
        """Reject missing or replaced writer gates instead of repairing."""
        rows = self._connection.execute(
            '''
            SELECT name, tbl_name, sql
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name LIKE 'room_mission_writer_gate_%'
            '''
        ).fetchall()
        observed = {str(row['name']): row for row in rows}
        expected = {}
        for table in _TABLES:
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                name = (
                    'room_mission_writer_gate_'
                    f'{table}_{operation.lower()}'
                )
                expected[name] = (table, operation)
        if set(observed) != set(expected):
            raise RoomMissionLedgerSchemaError(
                'room mission writer gates are incomplete'
            )
        for name, (table, operation) in expected.items():
            row = observed[name]
            sql = ' '.join(str(row['sql']).split()).upper()
            required = (
                str(row['tbl_name']) == table
                and f'BEFORE {operation} ON {table}'.upper() in sql
                and 'ROOM_MISSION_WRITER_PROTOCOL_VERSION()' in sql
                and (
                    f'!= {ROOM_MISSION_WRITER_PROTOCOL_VERSION}' in sql
                )
            )
            if not required:
                raise RoomMissionLedgerSchemaError(
                    'room mission writer gates are invalid'
                )
        guards = {
            str(row['name']): ' '.join(str(row['sql']).split()).upper()
            for row in self._connection.execute(
                '''
                SELECT name, sql
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND name IN (
                      'room_mission_feedback_terminal_update_guard',
                      'room_mission_feedback_terminal_delete_guard',
                      'room_mission_feedback_terminal_insert_guard'
                  )
                '''
            ).fetchall()
        }
        required_guards = {
            'room_mission_feedback_terminal_update_guard': (
                'BEFORE UPDATE ON ROOM_MISSION_FEEDBACK',
                "OLD.STATE IN ('COMMITTED', 'ORPHANED')",
            ),
            'room_mission_feedback_terminal_delete_guard': (
                'BEFORE DELETE ON ROOM_MISSION_FEEDBACK',
                "OLD.STATE IN ('COMMITTED', 'ORPHANED')",
            ),
            'room_mission_feedback_terminal_insert_guard': (
                'BEFORE INSERT ON ROOM_MISSION_FEEDBACK',
                "EXISTING.STATE IN ('COMMITTED', 'ORPHANED')",
            ),
        }
        if set(guards) != set(required_guards) or any(
            operation not in guards[name]
            or terminal_test not in guards[name]
            for name, (operation, terminal_test)
            in required_guards.items()
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission terminal feedback guards are invalid'
            )

    def _schema_fingerprint_locked(self) -> str:
        rows = self._connection.execute(
            '''
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger')
              AND name LIKE 'room_mission_%'
              AND sql IS NOT NULL
            ORDER BY type ASC, name ASC
            '''
        ).fetchall()
        return _schema_json_digest({
            'schema_version': ROOM_MISSION_SCHEMA_VERSION,
            'objects': [
                {
                    'type': str(row['type']),
                    'name': str(row['name']),
                    'table': str(row['tbl_name']),
                    'sql': ' '.join(str(row['sql']).split()),
                }
                for row in rows
            ],
        })

    def _store_schema_fingerprint_locked(self) -> None:
        fingerprint = self._schema_fingerprint_locked()
        cursor = self._connection.execute(
            '''
            UPDATE room_mission_schema_metadata
            SET schema_fingerprint = ?
            WHERE singleton = 1
            ''',
            (fingerprint,),
        )
        if cursor.rowcount != 1:
            raise RoomMissionLedgerSchemaError(
                'room mission schema metadata is incomplete'
            )

    def _verify_schema_fingerprint_locked(self, expected: str) -> None:
        if not hmac.compare_digest(
            expected,
            self._schema_fingerprint_locked(),
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission schema fingerprint differs'
            )

    def _verify_invariants_locked(self) -> None:
        quick_check = self._connection.execute(
            'PRAGMA quick_check(1000)'
        ).fetchall()
        if (
            len(quick_check) != 1
            or str(quick_check[0][0]).lower() != 'ok'
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission database integrity check failed'
            )
        state = self._connection.execute(
            '''
            SELECT proposal_count, record_capacity
            FROM room_mission_store_state
            WHERE singleton = 1
            '''
        ).fetchone()
        metadata_count = self._connection.execute(
            'SELECT COUNT(*) AS row_count '
            'FROM room_mission_schema_metadata'
        ).fetchone()
        state_count = self._connection.execute(
            'SELECT COUNT(*) AS row_count '
            'FROM room_mission_store_state'
        ).fetchone()
        proposal_count = self._connection.execute(
            'SELECT COUNT(*) AS row_count FROM room_mission_proposals'
        ).fetchone()
        attestation_count = self._connection.execute(
            'SELECT COUNT(*) AS row_count '
            'FROM room_mission_connection_attestations'
        ).fetchone()
        invalid_attestation = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_connection_attestations
            WHERE typeof(probe_digest) != 'text'
               OR length(probe_digest) != 64
               OR probe_digest GLOB '*[^0-9a-f]*'
               OR typeof(created_at) NOT IN ('integer', 'real')
               OR created_at < 0
               OR created_at >= 1.0e20
            LIMIT 1
            '''
        ).fetchone()
        if (
            state is None
            or int(metadata_count['row_count']) != 1
            or int(state_count['row_count']) != 1
            or int(state['proposal_count'])
            != int(proposal_count['row_count'])
            or int(state['record_capacity'])
            != self._max_mission_records
            or int(attestation_count['row_count'])
            > MAX_CONNECTION_ATTESTATION_ROWS
            or invalid_attestation is not None
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission ledger state is inconsistent'
            )
        foreign_keys = self._connection.execute(
            'PRAGMA foreign_key_check'
        ).fetchall()
        impossible = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_confirmations AS confirmation
            LEFT JOIN room_mission_executions AS execution
              ON execution.confirmation_id = confirmation.confirmation_id
             AND execution.proposal_id = confirmation.proposal_id
             AND execution.tool_call_id = confirmation.tool_call_id
            WHERE (
                confirmation.status = 'consumed'
                AND execution.tool_call_id IS NULL
            ) OR (
                confirmation.status = 'timed_out'
                AND execution.tool_call_id IS NOT NULL
            )
            LIMIT 1
            '''
        ).fetchone()
        missing_feedback = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            LEFT JOIN room_mission_feedback AS feedback
              ON feedback.tool_call_id = execution.tool_call_id
            WHERE execution.status IN (
                'succeeded', 'failed', 'cancelled', 'timed_out'
            ) AND feedback.tool_call_id IS NULL
            LIMIT 1
            '''
        ).fetchone()
        unexpected_feedback = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            JOIN room_mission_feedback AS feedback
              ON feedback.tool_call_id = execution.tool_call_id
            WHERE execution.status NOT IN (
                'succeeded', 'failed', 'cancelled', 'timed_out'
            )
            LIMIT 1
            '''
        ).fetchone()
        mismatched_feedback = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_feedback AS feedback
            JOIN room_mission_executions AS execution
              ON execution.tool_call_id = feedback.tool_call_id
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE feedback.subject_id != proposal.subject_id
               OR feedback.conversation_id != proposal.conversation_id
               OR feedback.conversation_session_instance_id
                  != proposal.conversation_session_instance_id
               OR feedback.conversation_generation
                  != proposal.conversation_generation
               OR feedback.terminal_digest != execution.terminal_digest
            LIMIT 1
            '''
        ).fetchone()
        missing_terminal_event = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            LEFT JOIN room_mission_events AS event
              ON event.tool_call_id = execution.tool_call_id
             AND event.sequence = execution.audit_sequence
             AND event.event_kind = 'terminal'
             AND event.phase = 'terminal'
             AND event.status = execution.status
             AND event.code = execution.code
            WHERE execution.status IN (
                'succeeded', 'failed', 'cancelled', 'timed_out'
            ) AND event.tool_call_id IS NULL
            LIMIT 1
            '''
        ).fetchone()
        invalid_terminal_source = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            JOIN room_mission_events AS terminal
              ON terminal.tool_call_id = execution.tool_call_id
             AND terminal.sequence = execution.audit_sequence
             AND terminal.event_kind = 'terminal'
            WHERE terminal.source != CASE
                WHEN execution.code = 'event_capacity_reached'
                    THEN 'controller'
                WHEN execution.code IN (
                    'authority_revoked',
                    'state_unavailable',
                    'state_stale',
                    'privacy_blocked',
                    'emergency_stop',
                    'map_changed',
                    'device_unavailable'
                ) THEN 'controller'
                WHEN execution.code = 'recovery_unavailable'
                    THEN 'recovery'
                WHEN execution.code IN (
                    'simulation_cancelled',
                    'simulation_cancel_failed',
                    'simulation_cancel_timeout'
                ) THEN 'simulation_adapter'
                WHEN execution.code = 'authorization_expired'
                     AND EXISTS (
                         SELECT 1
                         FROM room_mission_events AS prior
                         WHERE prior.tool_call_id = execution.tool_call_id
                           AND prior.sequence
                               = execution.audit_sequence - 1
                           AND prior.event_kind = 'observation'
                           AND prior.source = 'recovery'
                     ) THEN 'recovery'
                WHEN execution.code = 'authorization_expired'
                    THEN 'controller'
                WHEN EXISTS (
                    SELECT 1
                    FROM room_mission_events AS recovered
                    WHERE recovered.tool_call_id
                          = execution.tool_call_id
                      AND recovered.event_kind = 'recovery'
                      AND recovered.operation_id
                          = terminal.operation_id
                ) THEN 'recovery'
                ELSE 'simulation_adapter'
            END
            LIMIT 1
            '''
        ).fetchone()
        invalid_policy_terminal = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            JOIN room_mission_events AS terminal
              ON terminal.tool_call_id = execution.tool_call_id
             AND terminal.sequence = execution.audit_sequence
             AND terminal.event_kind = 'terminal'
            WHERE (
                execution.code IN (
                    'authority_revoked',
                    'state_unavailable',
                    'state_stale',
                    'privacy_blocked',
                    'emergency_stop',
                    'map_changed',
                    'device_unavailable'
                ) AND (
                    execution.status != 'failed'
                    OR execution.cancel_requested != 0
                    OR terminal.source != 'controller'
                    OR terminal.operation_id IS NOT NULL
                )
            ) OR (
                execution.code = 'recovery_unavailable'
                AND (
                    execution.status != 'failed'
                    OR terminal.source != 'recovery'
                    OR terminal.operation_id IS NULL
                    OR (
                        execution.cancel_requested = 1
                        AND terminal.operation_id
                            != execution.cancel_operation_id
                    )
                    OR (
                        execution.cancel_requested = 0
                        AND NOT EXISTS (
                            SELECT 1
                            FROM room_mission_events AS intent
                            WHERE intent.tool_call_id
                                  = execution.tool_call_id
                              AND intent.event_kind = 'intent'
                              AND intent.operation_id
                                  = terminal.operation_id
                        )
                    )
                )
            )
            LIMIT 1
            '''
        ).fetchone()
        invalid_abort_recovery = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_events AS abort
            WHERE abort.event_kind = 'recovery'
              AND abort.code IN (
                  'authority_revoked',
                  'state_unavailable',
                  'state_stale',
                  'privacy_blocked',
                  'emergency_stop',
                  'map_changed',
                  'device_unavailable'
              )
              AND (
                  abort.source != 'recovery'
                  OR abort.status != 'reconcile_required'
                  OR abort.operation_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1
                      FROM room_mission_events AS intent
                      WHERE intent.tool_call_id = abort.tool_call_id
                        AND intent.event_kind = 'intent'
                        AND intent.operation_id = abort.operation_id
                        AND intent.phase = abort.phase
                        AND intent.sequence < abort.sequence
                  )
              )
            UNION ALL
            SELECT 1
            FROM room_mission_events AS abort
            WHERE abort.event_kind = 'recovery'
              AND abort.code IN (
                  'authority_revoked',
                  'state_unavailable',
                  'state_stale',
                  'privacy_blocked',
                  'emergency_stop',
                  'map_changed',
                  'device_unavailable'
              )
            GROUP BY abort.tool_call_id
            HAVING COUNT(*) > 1
            LIMIT 1
            '''
        ).fetchone()
        invalid_execution_link = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            JOIN room_mission_confirmations AS confirmation
              ON confirmation.confirmation_id
                 = execution.confirmation_id
            WHERE execution.device_id != proposal.device_id
               OR confirmation.proposal_id != proposal.proposal_id
               OR confirmation.tool_call_id != execution.tool_call_id
               OR confirmation.status != 'consumed'
               OR confirmation.person_subject_id != proposal.subject_id
               OR execution.authorization_deadline
                  != min(proposal.expires_at, confirmation.expires_at)
            LIMIT 1
            '''
        ).fetchone()
        invalid_event_chain = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            LEFT JOIN room_mission_events AS event
              ON event.tool_call_id = execution.tool_call_id
            GROUP BY execution.tool_call_id, execution.audit_sequence
            HAVING execution.audit_sequence > ?
                OR COUNT(event.sequence) != execution.audit_sequence
                OR MIN(event.sequence) != 1
                OR MAX(event.sequence) != execution.audit_sequence
            LIMIT 1
            ''',
            (MAX_EVENTS_PER_MISSION,),
        ).fetchone()
        invalid_active_operation = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions AS execution
            WHERE (
                execution.active_operation_id IS NOT NULL
                AND (
                    NOT EXISTS (
                        SELECT 1
                        FROM room_mission_events AS intent
                        WHERE intent.tool_call_id
                              = execution.tool_call_id
                          AND intent.operation_id
                              = execution.active_operation_id
                          AND intent.event_kind = 'intent'
                          AND intent.phase = execution.phase
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM room_mission_events AS result
                        WHERE result.tool_call_id
                              = execution.tool_call_id
                          AND result.operation_id
                              = execution.active_operation_id
                          AND result.event_kind IN (
                              'observation', 'terminal', 'late_discarded'
                          )
                    )
                )
            ) OR (
                execution.active_operation_id IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM room_mission_events AS intent
                    WHERE intent.tool_call_id = execution.tool_call_id
                      AND intent.event_kind = 'intent'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM room_mission_events AS result
                          WHERE result.tool_call_id
                                = intent.tool_call_id
                            AND result.operation_id
                                = intent.operation_id
                            AND result.event_kind IN (
                                'observation', 'terminal',
                                'late_discarded'
                            )
                      )
                      AND NOT (
                          execution.cancel_requested = 1
                          AND execution.status IN (
                              'succeeded', 'failed',
                              'cancelled', 'timed_out'
                          )
                          AND execution.cancel_target_operation_id
                              = intent.operation_id
                      )
                )
            )
            LIMIT 1
            '''
        ).fetchone()
        invalid_lease = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_executions
            WHERE (lease_expires_at IS NOT NULL AND (
                       lease_owner IS NULL
                       OR lease_token_digest IS NULL
                       OR status NOT IN (
                           'leased', 'running', 'cancelling',
                           'reconcile_required'
                       )
                   ))
               OR (lease_owner IS NOT NULL AND (
                       lease_expires_at IS NULL
                       OR lease_token_digest IS NULL
                   ))
               OR (lease_epoch = 0 AND (
                       lease_owner IS NOT NULL
                       OR lease_token_digest IS NOT NULL
                       OR lease_expires_at IS NOT NULL
                   ))
            LIMIT 1
            '''
        ).fetchone()
        invalid_proposal_link = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_proposals AS proposal
            LEFT JOIN room_mission_confirmations AS confirmation
              ON confirmation.proposal_id = proposal.proposal_id
            LEFT JOIN room_mission_executions AS execution
              ON execution.proposal_id = proposal.proposal_id
            WHERE (
                proposal.status = 'confirmed'
                AND (
                    confirmation.status IS NULL
                    OR confirmation.status != 'consumed'
                    OR execution.tool_call_id IS NULL
                )
            ) OR (
                proposal.status = 'proposed'
                AND confirmation.confirmation_id IS NOT NULL
            ) OR (
                confirmation.status = 'timed_out'
                AND proposal.status != 'timed_out'
            ) OR (
                confirmation.status = 'consumed'
                AND proposal.status != 'confirmed'
            ) OR (
                proposal.status IN ('proposed', 'confirmed')
                AND proposal.terminal_code IS NOT NULL
            ) OR (
                proposal.status = 'denied'
                AND (
                    proposal.terminal_code IS NULL
                    OR proposal.terminal_code != 'user_denied'
                )
            ) OR (
                proposal.status = 'timed_out'
                AND (
                    proposal.terminal_code IS NULL
                    OR proposal.terminal_code NOT IN (
                        'proposal_expired', 'confirmation_expired'
                    )
                )
            ) OR (
                proposal.status = 'failed'
                AND (
                    proposal.terminal_code IS NULL
                    OR proposal.terminal_code NOT IN (
                        'authority_revoked',
                        'source_changed',
                        'map_changed',
                        'device_changed'
                    )
                )
            )
            LIMIT 1
            '''
        ).fetchone()
        invalid_feedback_state = self._connection.execute(
            '''
            SELECT 1
            FROM room_mission_feedback AS feedback
            JOIN room_mission_executions AS execution
              ON execution.tool_call_id = feedback.tool_call_id
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE (feedback.state = 'pending' AND (
                       feedback.lease_epoch != 0
                       OR feedback.lease_owner IS NOT NULL
                       OR feedback.lease_token_digest IS NOT NULL
                       OR feedback.lease_expires_at IS NOT NULL
                       OR feedback.response_commit_id IS NOT NULL
                       OR feedback.conversation_revision_after IS NOT NULL
                       OR feedback.orphan_code IS NOT NULL
                       OR feedback.result_digest IS NOT NULL
                   ))
               OR (feedback.state = 'leased' AND (
                       feedback.lease_epoch < 1
                       OR feedback.lease_owner IS NULL
                       OR feedback.lease_token_digest IS NULL
                       OR feedback.lease_expires_at IS NULL
                       OR feedback.response_commit_id IS NOT NULL
                       OR feedback.conversation_revision_after IS NOT NULL
                       OR feedback.orphan_code IS NOT NULL
                       OR feedback.result_digest IS NOT NULL
                   ))
               OR (feedback.state = 'committed' AND (
                       feedback.lease_epoch < 1
                       OR feedback.lease_owner IS NOT NULL
                       OR feedback.lease_token_digest IS NULL
                       OR feedback.lease_expires_at IS NOT NULL
                       OR feedback.response_commit_id IS NULL
                       OR feedback.conversation_revision_after IS NULL
                       OR feedback.conversation_revision_after
                          <= proposal.conversation_revision
                       OR feedback.orphan_code IS NOT NULL
                       OR feedback.result_digest IS NULL
                   ))
               OR (feedback.state = 'orphaned' AND (
                       feedback.lease_epoch < 1
                       OR feedback.lease_owner IS NOT NULL
                       OR feedback.lease_token_digest IS NULL
                       OR feedback.lease_expires_at IS NOT NULL
                       OR feedback.response_commit_id IS NOT NULL
                       OR feedback.conversation_revision_after IS NOT NULL
                       OR feedback.orphan_code IS NULL
                       OR feedback.orphan_code NOT IN (
                           'conversation_missing',
                           'conversation_closed',
                           'conversation_reset',
                           'owner_unavailable',
                           'delivery_rejected'
                       )
                       OR feedback.result_digest IS NULL
                   ))
            LIMIT 1
            '''
        ).fetchone()
        if (
            foreign_keys
            or impossible
            or missing_feedback
            or unexpected_feedback
            or mismatched_feedback
            or missing_terminal_event
            or invalid_terminal_source
            or invalid_policy_terminal
            or invalid_abort_recovery
            or invalid_execution_link
            or invalid_event_chain
            or invalid_active_operation
            or invalid_lease
            or invalid_proposal_link
            or invalid_feedback_state
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission ledger records are inconsistent'
            )
        terminal_rows = self._connection.execute(
            '''
            SELECT execution.tool_call_id,
                   execution.status,
                   execution.code,
                   execution.terminal_digest,
                   execution.terminal_payload_json,
                   execution.cancel_target_operation_id,
                   execution.cancel_target_result_code,
                   event.source AS terminal_source
            FROM room_mission_executions AS execution
            JOIN room_mission_events AS event
              ON event.tool_call_id = execution.tool_call_id
             AND event.sequence = execution.audit_sequence
             AND event.event_kind = 'terminal'
            WHERE execution.status IN (
                'succeeded', 'failed', 'cancelled', 'timed_out'
            )
            '''
        ).fetchall()
        for row in terminal_rows:
            payload_json = str(row['terminal_payload_json'])
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                payload = None
            expected = {
                'status': str(row['status']),
                'phase': 'terminal',
                'code': str(row['code']),
                'tool_call_id': str(row['tool_call_id']),
                'runtime_mode': 'simulation',
                'simulated': True,
                'physical_effects': False,
                'viewer_live': False,
                'durability': self._durability,
                'lease_scope': self._lease_scope,
                'terminal_source': str(row['terminal_source']),
                'superseded_phase_operation_id': (
                    str(row['cancel_target_operation_id'])
                    if row['cancel_target_operation_id'] is not None
                    else None
                ),
                'superseded_phase_result_code': (
                    str(row['cancel_target_result_code'])
                    if row['cancel_target_result_code'] is not None
                    else None
                ),
            }
            if (
                payload != expected
                or payload_json != _canonical_json(expected)
                or not hmac.compare_digest(
                    str(row['terminal_digest']),
                    _text_digest(payload_json),
                )
            ):
                raise RoomMissionLedgerSchemaError(
                    'room mission terminal result is invalid'
                )
        self._verify_record_digests_locked()

    def _verify_record_digests_locked(self) -> None:
        """Recompute immutable content-free bindings on every reopen."""
        proposals = self._connection.execute(
            'SELECT * FROM room_mission_proposals'
        ).fetchall()
        try:
            for row in proposals:
                owner_fields = {
                    'subject_id': str(row['subject_id']),
                    'auth_session_digest': str(
                        row['auth_session_digest']
                    ),
                    'conversation_id': str(row['conversation_id']),
                    'conversation_session_instance_id': str(
                        row['conversation_session_instance_id']
                    ),
                    'proposal_turn_id': str(row['proposal_turn_id']),
                    'request_id': str(row['request_id']),
                    'conversation_generation': int(
                        row['conversation_generation']
                    ),
                    'conversation_revision': int(
                        row['conversation_revision']
                    ),
                    'conversation_ordinal': int(
                        row['conversation_ordinal']
                    ),
                    'authority_digest': str(row['authority_digest']),
                }
                _digest(owner_fields['auth_session_digest'])
                _digest(owner_fields['authority_digest'])
                owner_digest = _authority_binding_digest(owner_fields)
                request_digest = _json_digest({
                    'authority_binding_digest': owner_digest,
                    'decision_id': str(row['decision_id']),
                    'arguments_digest': str(row['arguments_digest']),
                    'device_id': str(row['device_id']),
                    'device_binding_digest': str(
                        row['device_binding_digest']
                    ),
                    'map_id': str(row['map_id']),
                    'map_revision': str(row['map_revision']),
                    'room_id': str(row['room_id']),
                    'plan_digest': str(row['plan_digest']),
                    'issued_at': float(row['issued_at']),
                    'expires_at': float(row['expires_at']),
                    'runtime_mode': str(row['runtime_mode']),
                })
                if (
                    not hmac.compare_digest(
                        owner_digest,
                        str(row['owner_binding_digest']),
                    )
                    or not hmac.compare_digest(
                        request_digest,
                        str(row['request_fingerprint']),
                    )
                ):
                    raise RoomMissionLedgerSchemaError(
                        'room mission proposal binding is invalid'
                    )
            confirmations = self._connection.execute(
                '''
                SELECT confirmation.*,
                       proposal.owner_binding_digest,
                       proposal.decision_id,
                       proposal.arguments_digest
                FROM room_mission_confirmations AS confirmation
                JOIN room_mission_proposals AS proposal
                  ON proposal.proposal_id = confirmation.proposal_id
                '''
            ).fetchall()
            for row in confirmations:
                confirmation_digest = _json_digest({
                    'proposal_id': str(row['proposal_id']),
                    'authority_binding_digest': str(
                        row['owner_binding_digest']
                    ),
                    'decision_id': str(row['decision_id']),
                    'arguments_digest': str(row['arguments_digest']),
                    'evidence_digest': str(row['evidence_digest']),
                    'issuer_id': str(row['issuer_id']),
                    'person_subject_id': str(
                        row['person_subject_id']
                    ),
                    'issued_at': float(row['issued_at']),
                    'expires_at': float(row['expires_at']),
                })
                if not hmac.compare_digest(
                    confirmation_digest,
                    str(row['confirmation_fingerprint']),
                ):
                    raise RoomMissionLedgerSchemaError(
                        'room mission confirmation binding is invalid'
                    )
            feedback_rows = self._connection.execute(
                '''
                SELECT *
                FROM room_mission_feedback
                '''
            ).fetchall()
            for row in feedback_rows:
                token = _text_digest(str(row['tool_call_id']))
                expected_ids = (
                    f'room-feedback-{token}',
                    f'room-feedback-request-{token}',
                    f'room-feedback-turn-{token}',
                )
                observed_ids = (
                    str(row['feedback_id']),
                    str(row['feedback_request_id']),
                    str(row['feedback_turn_id']),
                )
                if observed_ids != expected_ids:
                    raise RoomMissionLedgerSchemaError(
                        'room mission feedback binding is invalid'
                    )
                state = str(row['state'])
                if state in {'committed', 'orphaned'}:
                    expected_result_digest = _feedback_result_digest(
                        feedback_id=str(row['feedback_id']),
                        tool_call_id=str(row['tool_call_id']),
                        terminal_digest=str(row['terminal_digest']),
                        state=state,
                        lease_epoch=int(row['lease_epoch']),
                        lease_token_digest=str(
                            row['lease_token_digest']
                        ),
                        response_commit_id=(
                            str(row['response_commit_id'])
                            if row['response_commit_id'] is not None
                            else None
                        ),
                        conversation_revision_after=(
                            int(row['conversation_revision_after'])
                            if row['conversation_revision_after']
                            is not None
                            else None
                        ),
                        orphan_code=(
                            str(row['orphan_code'])
                            if row['orphan_code'] is not None
                            else None
                        ),
                    )
                    if not hmac.compare_digest(
                        str(row['result_digest']),
                        expected_result_digest,
                    ):
                        raise RoomMissionLedgerSchemaError(
                            'room mission feedback receipt is invalid'
                        )
            execution_rows = self._connection.execute(
                '''
                SELECT tool_call_id,
                       active_operation_id,
                       cancel_requested,
                       cancel_operation_id,
                       cancel_target_operation_id,
                       cancel_target_result_code
                FROM room_mission_executions
                '''
            ).fetchall()
            for row in execution_rows:
                requested = bool(int(row['cancel_requested']))
                operation_id = row['cancel_operation_id']
                active_operation_id = row['active_operation_id']
                target_operation_id = row[
                    'cancel_target_operation_id'
                ]
                target_result_code = row['cancel_target_result_code']
                cancel_events = self._connection.execute(
                    '''
                    SELECT sequence, phase, operation_id
                    FROM room_mission_events
                    WHERE tool_call_id = ?
                      AND event_kind = 'cancel'
                    ''',
                    (row['tool_call_id'],),
                ).fetchall()
                if requested:
                    expected_operation = _cancel_operation_id(
                        str(row['tool_call_id'])
                    )
                    target_intents = (
                        ()
                        if target_operation_id is None
                        else self._connection.execute(
                            '''
                            SELECT sequence, phase
                            FROM room_mission_events
                            WHERE tool_call_id = ?
                              AND operation_id = ?
                              AND event_kind = 'intent'
                            ''',
                            (
                                row['tool_call_id'],
                                target_operation_id,
                            ),
                        ).fetchall()
                    )
                    target_prior_results = (
                        ()
                        if (
                            target_operation_id is None
                            or len(cancel_events) != 1
                        )
                        else self._connection.execute(
                            '''
                            SELECT 1
                            FROM room_mission_events
                            WHERE tool_call_id = ?
                              AND operation_id = ?
                              AND event_kind IN (
                                  'observation', 'terminal',
                                  'late_discarded'
                              )
                              AND sequence < ?
                            ''',
                            (
                                row['tool_call_id'],
                                target_operation_id,
                                cancel_events[0]['sequence'],
                            ),
                        ).fetchall()
                    )
                    target_late_results = (
                        ()
                        if target_operation_id is None
                        else self._connection.execute(
                            '''
                            SELECT phase, status, code
                            FROM room_mission_events
                            WHERE tool_call_id = ?
                              AND operation_id = ?
                              AND event_kind = 'late_discarded'
                            ''',
                            (
                                row['tool_call_id'],
                                target_operation_id,
                            ),
                        ).fetchall()
                    )
                    target_phase = (
                        str(target_intents[0]['phase'])
                        if len(target_intents) == 1
                        else None
                    )
                    allowed_late_codes = (
                        set()
                        if target_phase is None
                        else {
                            f'{self._outcome_code(target_phase, outcome)}'
                            '_late_discarded'
                            for outcome in PHASE_OUTCOMES
                        }
                    )
                    invalid = (
                        operation_id is None
                        or str(operation_id) != expected_operation
                        or len(cancel_events) != 1
                        or str(cancel_events[0]['operation_id'])
                        != expected_operation
                        or (
                            active_operation_id is not None
                            and target_operation_id
                            != active_operation_id
                        )
                        or (
                            target_operation_id is not None
                            and (
                                len(target_intents) != 1
                                or int(target_intents[0]['sequence'])
                                >= int(cancel_events[0]['sequence'])
                                or str(target_intents[0]['phase'])
                                != str(cancel_events[0]['phase'])
                                or len(target_prior_results) != 0
                                or str(target_operation_id)
                                != _operation_id(
                                    str(row['tool_call_id']),
                                    target_phase,
                                )
                            )
                        )
                        or (
                            target_result_code is None
                            and len(target_late_results) != 0
                        )
                        or (
                            target_result_code is not None
                            and (
                                active_operation_id is not None
                                or len(target_late_results) != 1
                                or str(target_result_code)
                                not in allowed_late_codes
                                or str(target_late_results[0]['phase'])
                                != target_phase
                                or str(target_late_results[0]['status'])
                                != 'cancelling'
                                or str(target_late_results[0]['code'])
                                != str(target_result_code)
                            )
                        )
                    )
                else:
                    invalid = (
                        operation_id is not None
                        or target_operation_id is not None
                        or target_result_code is not None
                        or len(cancel_events) != 0
                    )
                if invalid:
                    raise RoomMissionLedgerSchemaError(
                        'room mission cancellation binding is invalid'
                    )
        except RoomMissionLedgerSchemaError:
            raise
        except Exception:
            raise RoomMissionLedgerSchemaError(
                'room mission durable binding is invalid'
            ) from None

    def _install_writer_gates_locked(self) -> None:
        for table in _TABLES:
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                trigger = (
                    'room_mission_writer_gate_'
                    f'{table}_{operation.lower()}'
                )
                self._connection.execute(
                    f'DROP TRIGGER IF EXISTS {trigger}'
                )
                self._connection.execute(
                    f'''
                    CREATE TRIGGER {trigger}
                    BEFORE {operation} ON {table}
                    BEGIN
                        SELECT CASE
                            WHEN room_mission_writer_protocol_version()
                                 != {ROOM_MISSION_WRITER_PROTOCOL_VERSION}
                            THEN RAISE(
                                ABORT,
                                'incompatible room mission writer protocol'
                            )
                        END;
                    END
                    '''
                )
        self._connection.execute(
            'DROP TRIGGER IF EXISTS '
            'room_mission_feedback_terminal_update_guard'
        )
        self._connection.execute(
            '''
            CREATE TRIGGER room_mission_feedback_terminal_update_guard
            BEFORE UPDATE ON room_mission_feedback
            WHEN OLD.state IN ('committed', 'orphaned')
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'terminal room mission feedback is immutable'
                );
            END
            '''
        )
        self._connection.execute(
            'DROP TRIGGER IF EXISTS '
            'room_mission_feedback_terminal_delete_guard'
        )
        self._connection.execute(
            '''
            CREATE TRIGGER room_mission_feedback_terminal_delete_guard
            BEFORE DELETE ON room_mission_feedback
            WHEN OLD.state IN ('committed', 'orphaned')
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'terminal room mission feedback is immutable'
                );
            END
            '''
        )
        self._connection.execute(
            'DROP TRIGGER IF EXISTS '
            'room_mission_feedback_terminal_insert_guard'
        )
        self._connection.execute(
            '''
            CREATE TRIGGER room_mission_feedback_terminal_insert_guard
            BEFORE INSERT ON room_mission_feedback
            WHEN EXISTS (
                SELECT 1
                FROM room_mission_feedback AS existing
                WHERE existing.state IN ('committed', 'orphaned')
                  AND (
                      existing.feedback_id = NEW.feedback_id
                      OR existing.tool_call_id = NEW.tool_call_id
                      OR existing.feedback_request_id
                         = NEW.feedback_request_id
                  )
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'terminal room mission feedback is immutable'
                );
            END
            '''
        )

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            runtime = _registered_store_runtime(self)
            if not runtime.closed:
                runtime.connection.close()
                runtime.closed = True
                object.__setattr__(self, '_closed', True)

    def register_proposal(
        self,
        proposal: DurableMissionProposal,
    ) -> StoredMissionProposal:
        """Insert one proposal or replay its exact durable identity."""
        if type(proposal) is not DurableMissionProposal:
            raise RoomMissionLedgerValidationError(
                'mission proposal is invalid'
            )
        preflight_failure = False
        try:
            with self._lock:
                known_decision = self._connection.execute(
                    'SELECT 1 FROM room_mission_proposals '
                    'WHERE decision_id = ?',
                    (proposal.decision_id,),
                ).fetchone()
            proposal_ids = (
                ()
                if known_decision is not None
                else self._candidate_ids('room-proposal')
            )
            now = self._now()
        except (
            RoomMissionLedgerClockError,
            RoomMissionLedgerValidationError,
        ):
            raise
        except Exception:
            preflight_failure = True
        if preflight_failure:
            _raise_sanitized('room mission proposal persistence failed')
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                existing = self._connection.execute(
                    '''
                    SELECT proposal_id,
                           request_fingerprint,
                           owner_binding_digest,
                           status,
                           expires_at
                    FROM room_mission_proposals
                    WHERE decision_id = ?
                    ''',
                    (proposal.decision_id,),
                ).fetchone()
                if existing is not None:
                    self._require_owner_row(existing, proposal.authority)
                    if not hmac.compare_digest(
                        str(existing['request_fingerprint']),
                        proposal.request_fingerprint,
                    ):
                        raise RoomMissionLedgerConflictError(
                            'mission proposal conflicts with durable input'
                        )
                    status = str(existing['status'])
                    if status == 'proposed' and now >= float(
                        existing['expires_at']
                    ):
                        self._connection.execute(
                            '''
                            UPDATE room_mission_proposals
                            SET status = 'timed_out',
                                terminal_code = 'proposal_expired',
                                updated_at = ?
                            WHERE proposal_id = ? AND status = 'proposed'
                            ''',
                            (now, existing['proposal_id']),
                        )
                        self._bump_store_revision_locked()
                        status = 'timed_out'
                    self._connection.commit()
                    result = StoredMissionProposal(
                        proposal_id=str(existing['proposal_id']),
                        status=status,
                        cached=True,
                    )
                else:
                    request_row = self._connection.execute(
                        '''
                        SELECT owner_binding_digest
                        FROM room_mission_proposals
                        WHERE subject_id = ? AND request_id = ?
                        ''',
                        (
                            proposal.authority.subject_id,
                            proposal.authority.request_id,
                        ),
                    ).fetchone()
                    if request_row is not None:
                        self._require_owner_row(
                            request_row, proposal.authority
                        )
                        raise RoomMissionLedgerConflictError(
                            'mission request identifier conflicts'
                        )
                    if (
                        now
                        < float(proposal.issued_at)
                        - MAX_CLOCK_SKEW_SECONDS
                        or now >= float(proposal.expires_at)
                    ):
                        raise RoomMissionLedgerStateError(
                            'mission proposal is not current'
                        )
                    state = self._state_row_locked()
                    if int(state['proposal_count']) >= (
                        self._max_mission_records
                    ):
                        raise RoomMissionLedgerCapacityError(
                            'room mission record capacity reached'
                        )
                    if not proposal_ids:
                        raise RoomMissionLedgerStateError(
                            'server mission identifier is unavailable'
                        )
                    proposal_id = self._unused_id_locked(
                        'room_mission_proposals',
                        'proposal_id',
                        proposal_ids,
                    )
                    authority = proposal.authority
                    self._connection.execute(
                        '''
                        INSERT INTO room_mission_proposals (
                            proposal_id,
                            decision_id,
                            request_fingerprint,
                            owner_binding_digest,
                            subject_id,
                            auth_session_digest,
                            conversation_id,
                            conversation_session_instance_id,
                            proposal_turn_id,
                            request_id,
                            conversation_generation,
                            conversation_revision,
                            conversation_ordinal,
                            authority_digest,
                            arguments_digest,
                            device_id,
                            device_binding_digest,
                            map_id,
                            map_revision,
                            room_id,
                            plan_digest,
                            issued_at,
                            expires_at,
                            status,
                            created_at,
                            updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?
                        )
                        ''',
                        (
                            proposal_id,
                            proposal.decision_id,
                            proposal.request_fingerprint,
                            authority.binding_digest,
                            authority.subject_id,
                            _text_digest(authority.auth_session_id),
                            authority.conversation_id,
                            authority.conversation_session_instance_id,
                            authority.proposal_turn_id,
                            authority.request_id,
                            authority.conversation_generation,
                            authority.conversation_revision,
                            authority.conversation_ordinal,
                            authority.authority_digest,
                            proposal.arguments_digest,
                            proposal.device_id,
                            proposal.device_binding_digest,
                            proposal.map_id,
                            proposal.map_revision,
                            proposal.room_id,
                            proposal.plan_digest,
                            float(proposal.issued_at),
                            float(proposal.expires_at),
                            now,
                            now,
                        ),
                    )
                    self._connection.execute(
                        '''
                        UPDATE room_mission_store_state
                        SET proposal_count = proposal_count + 1,
                            revision = revision + 1
                        WHERE singleton = 1
                        '''
                    )
                    self._connection.commit()
                    result = StoredMissionProposal(
                        proposal_id=proposal_id,
                        status='proposed',
                        cached=False,
                    )
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerCapacityError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerStateError,
                RoomMissionLedgerClockError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission proposal persistence failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def consume_confirmation(
        self,
        proposal_id: str,
        authority: DurableMissionAuthority,
        confirmation: DurableMissionConfirmation,
    ) -> StoredMissionAuthorization:
        """Atomically consume evidence and create one pending execution."""
        _identifier(proposal_id)
        if (
            type(authority) is not DurableMissionAuthority
            or type(confirmation) is not DurableMissionConfirmation
        ):
            raise RoomMissionLedgerValidationError(
                'mission confirmation is invalid'
            )
        fingerprint = confirmation.fingerprint(proposal_id)
        preflight_failure = False
        try:
            with self._lock:
                known_confirmation = self._connection.execute(
                    'SELECT 1 FROM room_mission_confirmations '
                    'WHERE confirmation_id = ?',
                    (confirmation.confirmation_id,),
                ).fetchone()
            tool_ids = (
                ()
                if known_confirmation is not None
                else self._candidate_ids('room-tool-call')
            )
            now = self._now()
        except (
            RoomMissionLedgerClockError,
            RoomMissionLedgerValidationError,
        ):
            raise
        except Exception:
            preflight_failure = True
        if preflight_failure:
            _raise_sanitized(
                'room mission confirmation persistence failed'
            )
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                proposal = self._owned_proposal_locked(
                    proposal_id, authority
                )
                prior = self._connection.execute(
                    '''
                    SELECT confirmation.proposal_id,
                           confirmation.confirmation_fingerprint,
                           confirmation.status AS confirmation_status,
                           proposal.owner_binding_digest,
                           confirmation.tool_call_id,
                           execution.status
                    FROM room_mission_confirmations AS confirmation
                    JOIN room_mission_proposals AS proposal
                      ON proposal.proposal_id = confirmation.proposal_id
                    LEFT JOIN room_mission_executions AS execution
                      ON execution.tool_call_id = confirmation.tool_call_id
                    WHERE confirmation.confirmation_id = ?
                    ''',
                    (confirmation.confirmation_id,),
                ).fetchone()
                if prior is not None:
                    self._require_owner_row(prior, authority)
                    if (
                        str(prior['proposal_id']) != proposal_id
                        or not hmac.compare_digest(
                            str(prior['confirmation_fingerprint']),
                            fingerprint,
                        )
                    ):
                        raise RoomMissionLedgerConflictError(
                            'mission confirmation conflicts'
                        )
                    self._connection.commit()
                    timed_out = (
                        str(prior['confirmation_status']) == 'timed_out'
                    )
                    if (
                        timed_out
                        != (prior['tool_call_id'] is None)
                        or (
                            not timed_out
                            and prior['status'] is None
                        )
                    ):
                        raise RoomMissionLedgerStateError(
                            'mission confirmation record is invalid'
                        )
                    result = StoredMissionAuthorization(
                        proposal_id=proposal_id,
                        status=(
                            'timed_out'
                            if timed_out
                            else 'pending'
                        ),
                        tool_call_id=(
                            None
                            if timed_out
                            else str(prior['tool_call_id'])
                        ),
                        cached=True,
                    )
                else:
                    if str(proposal['status']) != 'proposed':
                        raise RoomMissionLedgerConflictError(
                            'mission proposal was already consumed'
                        )
                    if not self._confirmation_matches(
                        proposal, authority, confirmation, now
                    ):
                        raise RoomMissionLedgerAuthorityError(
                            'mission authority required'
                        )
                    if (
                        now >= float(proposal['expires_at'])
                        or now >= float(confirmation.expires_at)
                    ):
                        self._connection.execute(
                            '''
                            INSERT INTO room_mission_confirmations (
                                confirmation_id,
                                proposal_id,
                                confirmation_fingerprint,
                                evidence_digest,
                                issuer_id,
                                person_subject_id,
                                issued_at,
                                expires_at,
                                status,
                                consumed_at,
                                tool_call_id
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?,
                                'timed_out', NULL, NULL
                            )
                            ''',
                            (
                                confirmation.confirmation_id,
                                proposal_id,
                                fingerprint,
                                confirmation.evidence_digest,
                                confirmation.issuer_id,
                                confirmation.person_subject_id,
                                float(confirmation.issued_at),
                                float(confirmation.expires_at),
                            ),
                        )
                        self._connection.execute(
                            '''
                            UPDATE room_mission_proposals
                            SET status = 'timed_out',
                                terminal_code = 'confirmation_expired',
                                updated_at = ?
                            WHERE proposal_id = ? AND status = 'proposed'
                            ''',
                            (now, proposal_id),
                        )
                        self._bump_store_revision_locked()
                        self._connection.commit()
                        result = StoredMissionAuthorization(
                            proposal_id=proposal_id,
                            status='timed_out',
                            tool_call_id=None,
                            cached=False,
                        )
                    else:
                        self._expire_clean_device_execution_locked(
                            str(proposal['device_id']),
                            now,
                        )
                        active = self._connection.execute(
                            '''
                            SELECT 1
                            FROM room_mission_executions
                            WHERE device_id = ? AND status IN (
                                'pending',
                                'leased',
                                'running',
                                'cancelling',
                                'reconcile_required'
                            )
                            LIMIT 1
                            ''',
                            (proposal['device_id'],),
                        ).fetchone()
                        if active is not None:
                            raise RoomMissionLedgerBusyError(
                                'room mission device is busy'
                            )
                        if not tool_ids:
                            raise RoomMissionLedgerStateError(
                                'server mission identifier is unavailable'
                            )
                        tool_call_id = self._unused_id_locked(
                            'room_mission_executions',
                            'tool_call_id',
                            tool_ids,
                        )
                        self._connection.execute(
                            '''
                            INSERT INTO room_mission_confirmations (
                                confirmation_id,
                                proposal_id,
                                confirmation_fingerprint,
                                evidence_digest,
                                issuer_id,
                                person_subject_id,
                                issued_at,
                                expires_at,
                                status,
                                consumed_at,
                                tool_call_id
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, 'consumed', ?, ?
                            )
                            ''',
                            (
                                confirmation.confirmation_id,
                                proposal_id,
                                fingerprint,
                                confirmation.evidence_digest,
                                confirmation.issuer_id,
                                confirmation.person_subject_id,
                                float(confirmation.issued_at),
                                float(confirmation.expires_at),
                                now,
                                tool_call_id,
                            ),
                        )
                        deadline = min(
                            float(proposal['expires_at']),
                            float(confirmation.expires_at),
                        )
                        self._connection.execute(
                            '''
                            INSERT INTO room_mission_executions (
                                tool_call_id,
                                proposal_id,
                                confirmation_id,
                                device_id,
                                status,
                                phase,
                                code,
                                state_revision,
                                audit_sequence,
                                authorization_deadline,
                                updated_at
                            ) VALUES (
                                ?, ?, ?, ?, 'pending', 'confirmation',
                                'mission_authorized', 1, 1, ?, ?
                            )
                            ''',
                            (
                                tool_call_id,
                                proposal_id,
                                confirmation.confirmation_id,
                                proposal['device_id'],
                                deadline,
                                now,
                            ),
                        )
                        self._insert_event_locked(
                            tool_call_id=tool_call_id,
                            sequence=1,
                            event_kind='authorized',
                            phase='confirmation',
                            source='controller',
                            status='pending',
                            code='mission_authorized',
                            operation_id=None,
                            observed_at=now,
                        )
                        self._connection.execute(
                            '''
                            UPDATE room_mission_proposals
                            SET status = 'confirmed', updated_at = ?
                            WHERE proposal_id = ? AND status = 'proposed'
                            ''',
                            (now, proposal_id),
                        )
                        self._bump_store_revision_locked()
                        self._connection.commit()
                        result = StoredMissionAuthorization(
                            proposal_id=proposal_id,
                            status='pending',
                            tool_call_id=tool_call_id,
                            cached=False,
                        )
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerBusyError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission confirmation persistence failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def invalidate_proposal(
        self,
        proposal_id: str,
        authority: DurableMissionAuthority,
        code: str,
    ) -> StoredMissionProposal:
        """Fail one unconfirmed proposal for an allowlisted system reason."""
        _identifier(proposal_id)
        if (
            type(authority) is not DurableMissionAuthority
            or type(code) is not str
            or code not in PROPOSAL_INVALIDATION_CODES
        ):
            raise RoomMissionLedgerValidationError(
                'mission proposal invalidation is invalid'
            )
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._owned_proposal_locked(
                    proposal_id, authority
                )
                status = str(row['status'])
                if status == 'failed':
                    if str(row['terminal_code']) != code:
                        raise RoomMissionLedgerConflictError(
                            'mission proposal invalidation conflicts'
                        )
                    self._connection.commit()
                    result = StoredMissionProposal(
                        proposal_id=proposal_id,
                        status='failed',
                        cached=True,
                    )
                elif status == 'proposed':
                    cursor = self._connection.execute(
                        '''
                        UPDATE room_mission_proposals
                        SET status = 'failed',
                            terminal_code = ?,
                            updated_at = ?
                        WHERE proposal_id = ? AND status = 'proposed'
                        ''',
                        (code, now, proposal_id),
                    )
                    if cursor.rowcount != 1:
                        raise RoomMissionLedgerStateError(
                            'mission proposal state changed'
                        )
                    self._bump_store_revision_locked()
                    self._connection.commit()
                    result = StoredMissionProposal(
                        proposal_id=proposal_id,
                        status='failed',
                        cached=False,
                    )
                else:
                    raise RoomMissionLedgerStateError(
                        'mission proposal cannot be invalidated'
                    )
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerStateError,
            ):
                if self._closed:
                    failure = True
                else:
                    try:
                        self._rollback_locked()
                    except Exception:
                        failure = True
                    if not failure:
                        raise
            except Exception:
                try:
                    self._rollback_locked()
                except Exception:
                    pass
                failure = True
        if failure:
            _raise_sanitized(
                'room mission proposal invalidation persistence failed'
            )
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def deny_proposal(
        self,
        proposal_id: str,
        authority: DurableMissionAuthority,
    ) -> StoredMissionProposal:
        """Deny an unexpired proposal without creating an execution.

        An exact owner retry returns the durable denial.  Expired proposals
        become timeout tombstones instead.  Authority revocation is enforced
        by the integration validator before this owner-bound method.
        """
        _identifier(proposal_id)
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._owned_proposal_locked(
                    proposal_id, authority
                )
                status = str(row['status'])
                if status == 'denied':
                    self._connection.commit()
                    result = StoredMissionProposal(
                        proposal_id=proposal_id,
                        status='denied',
                        cached=True,
                    )
                elif status == 'timed_out':
                    self._connection.commit()
                    result = StoredMissionProposal(
                        proposal_id=proposal_id,
                        status='timed_out',
                        cached=True,
                    )
                elif status == 'proposed':
                    expired = now >= float(row['expires_at'])
                    next_status = 'timed_out' if expired else 'denied'
                    terminal_code = (
                        'proposal_expired' if expired else 'user_denied'
                    )
                    cursor = self._connection.execute(
                        '''
                        UPDATE room_mission_proposals
                        SET status = ?, terminal_code = ?, updated_at = ?
                        WHERE proposal_id = ? AND status = 'proposed'
                        ''',
                        (
                            next_status,
                            terminal_code,
                            now,
                            proposal_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoomMissionLedgerStateError(
                            'mission proposal state changed'
                        )
                    self._bump_store_revision_locked()
                    self._connection.commit()
                    result = StoredMissionProposal(
                        proposal_id=proposal_id,
                        status=next_status,
                        cached=False,
                    )
                else:
                    raise RoomMissionLedgerStateError(
                        'mission proposal cannot be denied'
                    )
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission denial persistence failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def abort_execution(
        self,
        tool_call_id: str,
        authority: DurableMissionAuthority,
        code: str,
        *,
        current_lease: Optional[ExecutionLease] = None,
    ) -> StoredMissionExecution:
        """Stop clean work or durably require unresolved recovery.

        This controller-only transition never calls or claims an adapter.
        A foreign live worker lease is preserved.  Supplying the exact
        current bearer lets the controller fence its own unresolved work
        without broadening the authority binding.
        """
        _identifier(tool_call_id)
        if (
            type(authority) is not DurableMissionAuthority
            or type(code) is not str
            or code not in ABORT_EXECUTION_CODES
        ):
            raise RoomMissionLedgerValidationError(
                'mission abort request is invalid'
            )
        if current_lease is not None:
            self._validate_lease(current_lease)
            if current_lease.tool_call_id != tool_call_id:
                raise RoomMissionLedgerValidationError(
                    'mission abort lease is invalid'
                )
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._owned_execution_locked(
                    tool_call_id, authority
                )
                terminal = (
                    str(row['status']) in TERMINAL_EXECUTION_STATUSES
                )
                prior_abort = self._abort_event_locked(tool_call_id)
                if current_lease is not None:
                    if terminal:
                        self._execution_receipt_matches(
                            row, current_lease
                        )
                    elif (
                        prior_abort is not None
                        and row['lease_expires_at'] is None
                    ):
                        self._execution_receipt_matches(
                            row, current_lease
                        )
                    else:
                        self._leased_execution_row_matches(
                            row, current_lease, now
                        )
                if prior_abort is not None:
                    if str(prior_abort['code']) != code:
                        raise RoomMissionLedgerConflictError(
                            'room mission abort request conflicts'
                        )
                    self._connection.commit()
                    result = self._execution_from_row(row)
                elif terminal:
                    terminal_event = self._connection.execute(
                        '''
                        SELECT source, status, code, operation_id
                        FROM room_mission_events
                        WHERE tool_call_id = ?
                          AND sequence = ?
                          AND event_kind = 'terminal'
                        ''',
                        (tool_call_id, row['audit_sequence']),
                    ).fetchone()
                    if (
                        terminal_event is None
                        or str(terminal_event['source']) != 'controller'
                        or str(terminal_event['status']) != 'failed'
                        or str(terminal_event['code']) != code
                        or terminal_event['operation_id'] is not None
                    ):
                        raise RoomMissionLedgerConflictError(
                            'room mission abort request conflicts'
                        )
                    self._connection.commit()
                    result = self._execution_from_row(row)
                else:
                    unresolved = (
                        row['active_operation_id'] is not None
                        or bool(int(row['cancel_requested']))
                    )
                    if unresolved:
                        if bool(int(row['cancel_requested'])):
                            self._connection.commit()
                            result = self._execution_from_row(row)
                            return result
                        lease_expires_at = row['lease_expires_at']
                        if (
                            current_lease is None
                            and lease_expires_at is not None
                            and float(lease_expires_at) > now
                        ):
                            raise RoomMissionLedgerBusyError(
                                'room mission execution is leased'
                            )
                        required_slots = (
                            2 if current_lease is not None else 3
                        )
                        if int(row['audit_sequence']) > (
                            MAX_EVENTS_PER_MISSION - required_slots
                        ):
                            raise RoomMissionLedgerStateError(
                                'room mission event capacity reached'
                            )
                        new_status = 'reconcile_required'
                        operation_id = row['active_operation_id']
                        sequence = int(row['audit_sequence']) + 1
                        revision = int(row['state_revision']) + 1
                        cursor = self._connection.execute(
                            '''
                            UPDATE room_mission_executions
                            SET status = ?,
                                code = ?,
                                state_revision = ?,
                                audit_sequence = ?,
                                lease_owner = CASE
                                    WHEN ? THEN lease_owner
                                    ELSE NULL
                                END,
                                lease_expires_at = CASE
                                    WHEN ? THEN lease_expires_at
                                    ELSE NULL
                                END,
                                updated_at = ?
                            WHERE tool_call_id = ?
                              AND state_revision = ?
                            ''',
                            (
                                new_status,
                                code,
                                revision,
                                sequence,
                                int(current_lease is not None),
                                int(current_lease is not None),
                                now,
                                tool_call_id,
                                row['state_revision'],
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RoomMissionLedgerBusyError(
                                'room mission execution state changed'
                            )
                        self._insert_event_locked(
                            tool_call_id=tool_call_id,
                            sequence=sequence,
                            event_kind='recovery',
                            phase=str(row['phase']),
                            source='recovery',
                            status=new_status,
                            code=code,
                            operation_id=operation_id,
                            observed_at=now,
                        )
                        self._bump_store_revision_locked()
                        current = self._execution_with_owner_locked(
                            tool_call_id
                        )
                        self._connection.commit()
                        result = self._execution_from_row(current)
                    else:
                        lease_expires_at = row['lease_expires_at']
                        if (
                            current_lease is None
                            and lease_expires_at is not None
                            and float(lease_expires_at) > now
                        ):
                            raise RoomMissionLedgerBusyError(
                                'room mission execution is leased'
                            )
                        if int(row['audit_sequence']) >= (
                            MAX_EVENTS_PER_MISSION
                        ):
                            raise RoomMissionLedgerStateError(
                                'room mission event capacity reached'
                            )
                        result = self._terminal_locked(
                            row,
                            'failed',
                            code,
                            now,
                            source='controller',
                        )
                        self._connection.commit()
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission abort persistence failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def claim_execution(
        self,
        tool_call_id: str,
        authority: DurableMissionAuthority,
        worker_id: str,
    ) -> ExecutionLease:
        """Claim pending work or fence an expired prior worker lease."""
        _identifier(tool_call_id)
        _identifier(worker_id)
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        lease_token = secrets.token_urlsafe(32)
        token_digest = _text_digest(lease_token)
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        expired = False
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._owned_execution_locked(
                    tool_call_id, authority
                )
                status = str(row['status'])
                if status in TERMINAL_EXECUTION_STATUSES:
                    raise RoomMissionLedgerStateError(
                        'room mission execution is terminal'
                    )
                lease_expires = row['lease_expires_at']
                if (
                    lease_expires is not None
                    and float(lease_expires) > now
                ):
                    raise RoomMissionLedgerBusyError(
                        'room mission execution is leased'
                    )
                if self._terminalize_event_capacity_locked(row, now):
                    self._connection.commit()
                    raise RoomMissionLedgerStateError(
                        'room mission event capacity reached'
                    )
                elif (
                    now >= float(row['authorization_deadline'])
                    and not bool(int(row['cancel_requested']))
                ):
                    if row['active_operation_id'] is None:
                        self._terminal_locked(
                            row,
                            'timed_out',
                            'authorization_expired',
                            now,
                            source='controller',
                        )
                        self._connection.commit()
                        expired = True
                    else:
                        if status != 'reconcile_required':
                            self._mark_reconcile_required_locked(
                                row, now, 'authorization_expired'
                            )
                            row = self._owned_execution_locked(
                                tool_call_id, authority
                            )
                            if self._terminalize_event_capacity_locked(
                                row, now
                            ):
                                self._connection.commit()
                                raise RoomMissionLedgerStateError(
                                    'room mission event capacity reached'
                                )
                        status = 'reconcile_required'
                if not expired:
                    lease_expires = row['lease_expires_at']
                    if (
                        lease_expires is not None
                        and float(lease_expires) > now
                    ):
                        raise RoomMissionLedgerBusyError(
                            'room mission execution is leased'
                        )
                    recovery = (
                        status in {'cancelling', 'reconcile_required'}
                        or row['active_operation_id'] is not None
                    )
                    new_status = (
                        'cancelling'
                        if bool(int(row['cancel_requested']))
                        else (
                            'reconcile_required' if recovery else 'leased'
                        )
                    )
                    claim_code = (
                        'recovery_claimed'
                        if recovery
                        else 'execution_claimed'
                    )
                    execution_code = (
                        str(row['code'])
                        if (
                            not recovery
                            and status == 'running'
                            and row['active_operation_id'] is None
                        )
                        else claim_code
                    )
                    epoch = int(row['lease_epoch']) + 1
                    expires_at = (
                        now + self._lease_seconds
                        if recovery
                        else min(
                            now + self._lease_seconds,
                            float(row['authorization_deadline']),
                        )
                    )
                    sequence = int(row['audit_sequence']) + 1
                    revision = int(row['state_revision']) + 1
                    self._connection.execute(
                        '''
                        UPDATE room_mission_executions
                        SET status = ?,
                            code = ?,
                            state_revision = ?,
                            audit_sequence = ?,
                            lease_owner = ?,
                            lease_token_digest = ?,
                            lease_epoch = ?,
                            lease_expires_at = ?,
                            updated_at = ?
                        WHERE tool_call_id = ?
                          AND state_revision = ?
                        ''',
                        (
                            new_status,
                            execution_code,
                            revision,
                            sequence,
                            worker_id,
                            token_digest,
                            epoch,
                            expires_at,
                            now,
                            tool_call_id,
                            row['state_revision'],
                        ),
                    )
                    self._insert_event_locked(
                        tool_call_id=tool_call_id,
                        sequence=sequence,
                        event_kind='recovery' if recovery else 'lease',
                        phase=str(row['phase']),
                        source='recovery' if recovery else 'controller',
                        status=new_status,
                        code=claim_code,
                        operation_id=row['active_operation_id'],
                        observed_at=now,
                    )
                    self._bump_store_revision_locked()
                    self._connection.commit()
                    result = ExecutionLease(
                        tool_call_id=tool_call_id,
                        lease_epoch=epoch,
                        lease_token=lease_token,
                        expires_at=expires_at,
                        recovery_required=recovery,
                    )
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission execution claim failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        if expired:
            raise RoomMissionLedgerStateError(
                'room mission authorization expired'
            )
        assert result is not None
        return result

    def request_cancel(
        self,
        tool_call_id: str,
        authority: DurableMissionAuthority,
        worker_id: Optional[str] = None,
        *,
        current_lease: Optional[ExecutionLease] = None,
    ) -> CancellationRequest:
        """Persist cancellation without stealing or minting a bearer.

        A valid supplied lease is returned unchanged.  Without one, an
        existing worker remains fenced and the typed result reports that a
        cancellation lease is still pending; a trusted controller can later
        use :meth:`claim_execution` and :meth:`get_cancel_intent`.
        ``worker_id`` is validated for controller API compatibility but is
        neither persisted nor sufficient to mint a bearer.
        """
        _identifier(tool_call_id)
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        if worker_id is not None:
            _identifier(worker_id)
        if current_lease is not None:
            self._validate_lease(current_lease)
            if current_lease.tool_call_id != tool_call_id:
                raise RoomMissionLedgerValidationError(
                    'mission cancellation lease is invalid'
                )
        operation_id = _cancel_operation_id(tool_call_id)
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._owned_execution_locked(
                    tool_call_id, authority
                )
                terminal = (
                    str(row['status']) in TERMINAL_EXECUTION_STATUSES
                )
                already_requested = bool(int(row['cancel_requested']))
                if terminal and not already_requested:
                    raise RoomMissionLedgerStateError(
                        'room mission execution is terminal'
                    )
                if already_requested:
                    if str(row['cancel_operation_id']) != operation_id:
                        raise RoomMissionLedgerStateError(
                            'mission cancellation intent is invalid'
                        )
                    intent_revision = (
                        self._cancel_intent_revision_locked(
                            tool_call_id, operation_id
                        )
                    )
                    proven_lease = None
                    if current_lease is not None:
                        try:
                            if terminal:
                                self._execution_receipt_matches(
                                    row, current_lease
                                )
                            else:
                                self._leased_execution_row_matches(
                                    row, current_lease, now
                                )
                            proven_lease = current_lease
                        except RoomMissionLedgerBusyError:
                            proven_lease = None
                    self._connection.commit()
                    result = CancellationRequest(
                        intent=CancelIntent(
                            tool_call_id=tool_call_id,
                            operation_id=operation_id,
                            state_revision=intent_revision,
                            superseded_phase_operation_id=(
                                str(row['cancel_target_operation_id'])
                                if row['cancel_target_operation_id']
                                is not None
                                else None
                            ),
                            cached=True,
                        ),
                        lease=proven_lease,
                        pending_lease=(
                            proven_lease is None and not terminal
                        ),
                    )
                else:
                    proven_lease = None
                    if current_lease is not None:
                        try:
                            self._leased_execution_row_matches(
                                row, current_lease, now
                            )
                            proven_lease = current_lease
                        except RoomMissionLedgerBusyError:
                            proven_lease = None
                    if self._terminalize_event_capacity_locked(row, now):
                        self._connection.commit()
                        raise RoomMissionLedgerStateError(
                            'room mission event capacity reached'
                        )
                    sequence = int(row['audit_sequence']) + 1
                    revision = int(row['state_revision']) + 1
                    cursor = self._connection.execute(
                        '''
                        UPDATE room_mission_executions
                        SET status = 'cancelling',
                            code = 'cancel_requested',
                            state_revision = ?,
                            audit_sequence = ?,
                            cancel_requested = 1,
                            cancel_operation_id = ?,
                            cancel_target_operation_id = ?,
                            updated_at = ?
                        WHERE tool_call_id = ?
                          AND state_revision = ?
                        ''',
                        (
                            revision,
                            sequence,
                            operation_id,
                            row['active_operation_id'],
                            now,
                            tool_call_id,
                            row['state_revision'],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoomMissionLedgerBusyError(
                            'room mission execution state changed'
                        )
                    self._insert_event_locked(
                        tool_call_id=tool_call_id,
                        sequence=sequence,
                        event_kind='cancel',
                        phase=str(row['phase']),
                        source='controller',
                        status='cancelling',
                        code='cancel_requested',
                        operation_id=operation_id,
                        observed_at=now,
                    )
                    self._bump_store_revision_locked()
                    self._connection.commit()
                    result = CancellationRequest(
                        intent=CancelIntent(
                            tool_call_id=tool_call_id,
                            operation_id=operation_id,
                            state_revision=revision,
                            superseded_phase_operation_id=(
                                str(row['active_operation_id'])
                                if row['active_operation_id'] is not None
                                else None
                            ),
                            cached=False,
                        ),
                        lease=proven_lease,
                        pending_lease=proven_lease is None,
                    )
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission cancellation persistence failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def get_cancel_intent(self, lease: ExecutionLease) -> CancelIntent:
        """Read a cancellation intent without authorizing redispatch."""
        self._validate_lease(lease)
        now = self._now()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_read_locked()
                row = self._leased_execution_locked(lease, now)
                operation_id = row['cancel_operation_id']
                if (
                    str(row['status']) != 'cancelling'
                    or not bool(int(row['cancel_requested']))
                    or operation_id is None
                    or str(operation_id)
                    != _cancel_operation_id(lease.tool_call_id)
                ):
                    raise RoomMissionLedgerStateError(
                        'room mission cancellation intent is unavailable'
                    )
                intent_revision = self._cancel_intent_revision_locked(
                    lease.tool_call_id, str(operation_id)
                )
                self._commit_read_locked()
                result = CancelIntent(
                    tool_call_id=lease.tool_call_id,
                    operation_id=str(operation_id),
                    state_revision=intent_revision,
                    superseded_phase_operation_id=(
                        str(row['cancel_target_operation_id'])
                        if row['cancel_target_operation_id'] is not None
                        else None
                    ),
                    cached=True,
                )
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission cancellation read failed')
        assert result is not None
        return result

    def record_cancel_result(
        self,
        lease: ExecutionLease,
        intent: CancelIntent,
        outcome: str,
    ) -> StoredMissionExecution:
        """Record one simulated cancellation result and terminalize."""
        self._validate_lease(lease)
        if (
            type(intent) is not CancelIntent
            or intent.tool_call_id != lease.tool_call_id
            or intent.operation_id
            != _cancel_operation_id(lease.tool_call_id)
            or outcome not in PHASE_OUTCOMES
        ):
            raise RoomMissionLedgerValidationError(
                'mission cancellation result is invalid'
            )
        if intent.superseded_phase_operation_id is not None:
            _identifier(intent.superseded_phase_operation_id)
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._execution_with_owner_locked(
                    lease.tool_call_id
                )
                if row is None:
                    raise RoomMissionLedgerStateError(
                        'room mission execution is unavailable'
                    )
                expected_status, expected_code = (
                    ('cancelled', 'simulation_cancelled')
                    if outcome == 'succeeded'
                    else (
                        ('timed_out', 'simulation_cancel_timeout')
                        if outcome == 'timed_out'
                        else ('failed', 'simulation_cancel_failed')
                    )
                )
                stored_target = (
                    str(row['cancel_target_operation_id'])
                    if row['cancel_target_operation_id'] is not None
                    else None
                )
                if str(row['status']) in TERMINAL_EXECUTION_STATUSES:
                    self._execution_receipt_matches(row, lease)
                    intent_revision = (
                        self._cancel_intent_revision_locked(
                            lease.tool_call_id, intent.operation_id
                        )
                    )
                    if intent.state_revision != intent_revision:
                        raise RoomMissionLedgerConflictError(
                            'mission cancellation result conflicts'
                        )
                    if stored_target != (
                        intent.superseded_phase_operation_id
                    ):
                        raise RoomMissionLedgerConflictError(
                            'mission cancellation result conflicts'
                        )
                    terminal_event = self._connection.execute(
                        '''
                        SELECT status, code
                        FROM room_mission_events
                        WHERE tool_call_id = ?
                          AND operation_id = ?
                          AND event_kind = 'terminal'
                        ORDER BY sequence DESC
                        LIMIT 1
                        ''',
                        (lease.tool_call_id, intent.operation_id),
                    ).fetchone()
                    if (
                        terminal_event is None
                        or str(terminal_event['status'])
                        != expected_status
                        or str(terminal_event['code']) != expected_code
                    ):
                        raise RoomMissionLedgerConflictError(
                            'mission cancellation result conflicts'
                        )
                    self._connection.commit()
                    result = self._execution_from_row(row)
                else:
                    self._leased_execution_row_matches(row, lease, now)
                    intent_revision = (
                        self._cancel_intent_revision_locked(
                            lease.tool_call_id, intent.operation_id
                        )
                    )
                    if intent.state_revision != intent_revision:
                        raise RoomMissionLedgerConflictError(
                            'mission cancellation result conflicts'
                        )
                    if stored_target != (
                        intent.superseded_phase_operation_id
                    ):
                        raise RoomMissionLedgerConflictError(
                            'mission cancellation result conflicts'
                        )
                    if (
                        str(row['status']) != 'cancelling'
                        or not bool(int(row['cancel_requested']))
                        or row['cancel_operation_id']
                        != intent.operation_id
                    ):
                        raise RoomMissionLedgerStateError(
                            'mission cancellation result is not current'
                        )
                    result = self._terminal_locked(
                        row,
                        expected_status,
                        expected_code,
                        now,
                        source='simulation_adapter',
                        operation_id=intent.operation_id,
                    )
                    self._connection.commit()
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission cancellation result failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def fail_reconciliation(
        self,
        lease: ExecutionLease,
        intent: RecoveryPhaseIntent | CancelIntent,
        code: str = RECONCILIATION_FAILURE_CODE,
    ) -> StoredMissionExecution:
        """Fail one exact unresolved operation without adapter dispatch."""
        self._validate_lease(lease)
        if type(code) is not str or code != RECONCILIATION_FAILURE_CODE:
            raise RoomMissionLedgerValidationError(
                'mission reconciliation failure code is invalid'
            )
        if type(intent) is RecoveryPhaseIntent:
            valid_intent = (
                intent.tool_call_id == lease.tool_call_id
                and intent.phase in EXECUTABLE_PHASES
                and intent.operation_id
                == _operation_id(lease.tool_call_id, intent.phase)
                and type(intent.state_revision) is int
                and intent.state_revision >= 1
            )
        elif type(intent) is CancelIntent:
            valid_intent = (
                intent.tool_call_id == lease.tool_call_id
                and intent.operation_id
                == _cancel_operation_id(lease.tool_call_id)
                and type(intent.state_revision) is int
                and intent.state_revision >= 1
                and type(intent.cached) is bool
            )
            if intent.superseded_phase_operation_id is not None:
                _identifier(intent.superseded_phase_operation_id)
        else:
            valid_intent = False
        if not valid_intent:
            raise RoomMissionLedgerValidationError(
                'mission reconciliation intent is invalid'
            )
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._execution_with_owner_locked(
                    lease.tool_call_id
                )
                if row is None:
                    raise RoomMissionLedgerStateError(
                        'room mission execution is unavailable'
                    )
                terminal = (
                    str(row['status']) in TERMINAL_EXECUTION_STATUSES
                )
                if terminal:
                    self._execution_receipt_matches(row, lease)
                else:
                    self._leased_execution_row_matches(row, lease, now)
                operation_id = (
                    self._reconciliation_operation_locked(
                        row, intent, terminal=terminal
                    )
                )
                if operation_id is None:
                    error = (
                        RoomMissionLedgerConflictError
                        if terminal
                        else RoomMissionLedgerStateError
                    )
                    raise error(
                        'mission reconciliation intent is not current'
                    )
                if terminal:
                    terminal_event = self._connection.execute(
                        '''
                        SELECT source, status, code, operation_id
                        FROM room_mission_events
                        WHERE tool_call_id = ?
                          AND sequence = ?
                          AND event_kind = 'terminal'
                        ''',
                        (lease.tool_call_id, row['audit_sequence']),
                    ).fetchone()
                    if (
                        terminal_event is None
                        or str(terminal_event['source']) != 'recovery'
                        or str(terminal_event['status']) != 'failed'
                        or str(terminal_event['code']) != code
                        or str(terminal_event['operation_id'])
                        != operation_id
                    ):
                        raise RoomMissionLedgerConflictError(
                            'mission reconciliation result conflicts'
                        )
                    self._connection.commit()
                    result = self._execution_from_row(row)
                else:
                    if int(row['audit_sequence']) >= (
                        MAX_EVENTS_PER_MISSION
                    ):
                        raise RoomMissionLedgerStateError(
                            'room mission event capacity reached'
                        )
                    result = self._terminal_locked(
                        row,
                        'failed',
                        code,
                        now,
                        source='recovery',
                        operation_id=operation_id,
                    )
                    self._connection.commit()
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized(
                'room mission reconciliation failure persistence failed'
            )
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def renew_lease(self, lease: ExecutionLease) -> ExecutionLease:
        """Extend a current lease without changing its fencing epoch."""
        self._validate_lease(lease)
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._leased_execution_locked(lease, now)
                reconciling = (
                    (
                        str(row['status']) == 'reconcile_required'
                        and row['active_operation_id'] is not None
                    )
                    or (
                        str(row['status']) == 'cancelling'
                        and bool(int(row['cancel_requested']))
                    )
                )
                expires_at = (
                    now + self._lease_seconds
                    if reconciling
                    else min(
                        now + self._lease_seconds,
                        float(row['authorization_deadline']),
                    )
                )
                if expires_at <= now:
                    raise RoomMissionLedgerStateError(
                        'room mission authorization expired'
                    )
                self._connection.execute(
                    '''
                    UPDATE room_mission_executions
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE tool_call_id = ? AND lease_epoch = ?
                    ''',
                    (
                        expires_at,
                        now,
                        lease.tool_call_id,
                        lease.lease_epoch,
                    ),
                )
                self._connection.commit()
                result = ExecutionLease(
                    tool_call_id=lease.tool_call_id,
                    lease_epoch=lease.lease_epoch,
                    lease_token=lease.lease_token,
                    expires_at=expires_at,
                    recovery_required=lease.recovery_required,
                )
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission lease renewal failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def prepare_phase(
        self,
        lease: ExecutionLease,
        phase: str,
    ) -> PhaseIntent:
        """Durably record one stable operation before adapter dispatch."""
        self._validate_lease(lease)
        if phase not in EXECUTABLE_PHASES:
            raise RoomMissionLedgerValidationError(
                'mission execution phase is invalid'
            )
        operation_id = _operation_id(lease.tool_call_id, phase)
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._leased_execution_locked(lease, now)
                if (
                    bool(int(row['cancel_requested']))
                    or str(row['status']) == 'cancelling'
                ):
                    raise RoomMissionLedgerStateError(
                        'room mission cancellation is required'
                    )
                if self._terminalize_event_capacity_locked(row, now):
                    self._connection.commit()
                    raise RoomMissionLedgerStateError(
                        'room mission event capacity reached'
                    )
                existing = self._connection.execute(
                    '''
                    SELECT 1
                    FROM room_mission_events
                    WHERE tool_call_id = ?
                      AND operation_id = ?
                      AND event_kind = 'intent'
                    ''',
                    (lease.tool_call_id, operation_id),
                ).fetchone()
                if (
                    existing is not None
                    and row['active_operation_id'] == operation_id
                    and str(row['phase']) == phase
                    and str(row['status']) == 'running'
                    and not lease.recovery_required
                ):
                    raise RoomMissionLedgerStateError(
                        'room mission intent requires reconciliation'
                    )
                else:
                    if (
                        str(row['status']) == 'reconcile_required'
                        or row['active_operation_id'] is not None
                        or lease.recovery_required
                    ):
                        raise RoomMissionLedgerStateError(
                            'room mission reconciliation is required'
                        )
                    expected_phase = self._next_phase(row)
                    if (
                        existing is not None
                        or row['active_operation_id'] is not None
                        or phase != expected_phase
                    ):
                        raise RoomMissionLedgerStateError(
                            'room mission phase transition is invalid'
                        )
                    sequence = int(row['audit_sequence']) + 1
                    revision = int(row['state_revision']) + 1
                    cursor = self._connection.execute(
                        '''
                        UPDATE room_mission_executions
                        SET status = 'running',
                            phase = ?,
                            code = ?,
                            state_revision = ?,
                            audit_sequence = ?,
                            active_operation_id = ?,
                            started_at = COALESCE(started_at, ?),
                            updated_at = ?
                        WHERE tool_call_id = ?
                          AND lease_epoch = ?
                          AND state_revision = ?
                        ''',
                        (
                            phase,
                            f'{phase}_started',
                            revision,
                            sequence,
                            operation_id,
                            now,
                            now,
                            lease.tool_call_id,
                            lease.lease_epoch,
                            row['state_revision'],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoomMissionLedgerBusyError(
                            'room mission execution lease changed'
                        )
                    self._insert_event_locked(
                        tool_call_id=lease.tool_call_id,
                        sequence=sequence,
                        event_kind='intent',
                        phase=phase,
                        source='controller',
                        status='running',
                        code=f'{phase}_started',
                        operation_id=operation_id,
                        observed_at=now,
                    )
                    self._bump_store_revision_locked()
                    self._connection.commit()
                    result = PhaseIntent(
                        tool_call_id=lease.tool_call_id,
                        phase=phase,
                        operation_id=operation_id,
                        state_revision=revision,
                        cached=False,
                    )
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission phase persistence failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def get_recovery_intent(
        self,
        lease: ExecutionLease,
    ) -> RecoveryPhaseIntent:
        """Read an unresolved operation without authorizing redispatch."""
        self._validate_lease(lease)
        now = self._now()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_read_locked()
                row = self._leased_execution_locked(lease, now)
                operation_id = row['active_operation_id']
                phase = str(row['phase'])
                if (
                    str(row['status']) not in {
                        'running', 'reconcile_required', 'cancelling'
                    }
                    or operation_id is None
                    or phase not in EXECUTABLE_PHASES
                    or str(operation_id)
                    != _operation_id(lease.tool_call_id, phase)
                ):
                    raise RoomMissionLedgerStateError(
                        'room mission recovery intent is unavailable'
                    )
                self._commit_read_locked()
                result = RecoveryPhaseIntent(
                    tool_call_id=lease.tool_call_id,
                    phase=phase,
                    operation_id=str(operation_id),
                    state_revision=int(row['state_revision']),
                )
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission recovery intent read failed')
        assert result is not None
        return result

    def record_phase_result(
        self,
        lease: ExecutionLease,
        intent: PhaseIntent | RecoveryPhaseIntent,
        outcome: str,
    ) -> StoredMissionExecution:
        """Record one simulation result; terminal state is immutable."""
        self._validate_lease(lease)
        if (
            type(intent) not in {PhaseIntent, RecoveryPhaseIntent}
            or intent.tool_call_id != lease.tool_call_id
            or intent.phase not in EXECUTABLE_PHASES
            or intent.operation_id
            != _operation_id(lease.tool_call_id, intent.phase)
            or outcome not in PHASE_OUTCOMES
        ):
            raise RoomMissionLedgerValidationError(
                'mission phase result is invalid'
            )
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._connection.execute(
                    '''
                    SELECT execution.*, proposal.owner_binding_digest,
                           proposal.subject_id,
                           proposal.conversation_id,
                           proposal.conversation_session_instance_id,
                           proposal.conversation_generation
                    FROM room_mission_executions AS execution
                    JOIN room_mission_proposals AS proposal
                      ON proposal.proposal_id = execution.proposal_id
                    WHERE execution.tool_call_id = ?
                    ''',
                    (lease.tool_call_id,),
                ).fetchone()
                if row is None:
                    raise RoomMissionLedgerStateError(
                        'room mission execution is unavailable'
                    )
                prior = self._connection.execute(
                    '''
                    SELECT event_kind, status, code
                    FROM room_mission_events
                    WHERE tool_call_id = ?
                      AND operation_id = ?
                      AND event_kind IN (
                          'observation', 'terminal', 'late_discarded'
                      )
                    ORDER BY sequence ASC
                    LIMIT 1
                    ''',
                    (lease.tool_call_id, intent.operation_id),
                ).fetchone()
                terminal_replay = (
                    str(row['status']) in TERMINAL_EXECUTION_STATUSES
                )
                if terminal_replay:
                    self._execution_receipt_matches(row, lease)
                    cancel_superseded = (
                        bool(int(row['cancel_requested']))
                        and row['cancel_target_operation_id']
                        == intent.operation_id
                    )
                    if cancel_superseded and prior is None:
                        self._connection.commit()
                        result = self._execution_from_row(row)
                        return result
                    expected_code = self._outcome_code(
                        intent.phase, outcome
                    )
                    if prior is None:
                        raise RoomMissionLedgerConflictError(
                            'mission phase result conflicts'
                        )
                    if str(prior['event_kind']) == 'late_discarded':
                        expected_code = f'{expected_code}_late_discarded'
                    if str(prior['code']) != expected_code:
                        raise RoomMissionLedgerConflictError(
                            'mission phase result conflicts'
                        )
                    self._connection.commit()
                    result = self._execution_from_row(row)
                else:
                    if prior is not None:
                        if row['lease_expires_at'] is None:
                            self._execution_receipt_matches(row, lease)
                        else:
                            self._leased_execution_row_matches(
                                row, lease, now
                            )
                        expected_code = self._outcome_code(
                            intent.phase, outcome
                        )
                        if str(prior['event_kind']) == 'late_discarded':
                            expected_code = (
                                f'{expected_code}_late_discarded'
                            )
                        if str(prior['code']) != expected_code:
                            raise RoomMissionLedgerConflictError(
                                'mission phase result conflicts'
                            )
                        self._connection.commit()
                        result = self._execution_from_row(row)
                        return result
                    self._leased_execution_row_matches(row, lease, now)
                    recovering = type(intent) is RecoveryPhaseIntent
                    if (
                        str(row['status']) == 'reconcile_required'
                        and not recovering
                    ) or (
                        lease.recovery_required and not recovering
                    ):
                        raise RoomMissionLedgerStateError(
                            'mission phase result provenance is invalid'
                        )
                    if (
                        row['active_operation_id']
                        != intent.operation_id
                        or str(row['phase']) != intent.phase
                        or str(row['status']) not in {
                            'running', 'reconcile_required', 'cancelling'
                        }
                    ):
                        raise RoomMissionLedgerStateError(
                            'mission phase result is not current'
                        )
                    if bool(int(row['cancel_requested'])):
                        if self._terminalize_event_capacity_locked(
                            row, now
                        ):
                            self._connection.commit()
                            raise RoomMissionLedgerStateError(
                                'room mission event capacity reached'
                            )
                        sequence = int(row['audit_sequence']) + 1
                        revision = int(row['state_revision']) + 1
                        late_code = (
                            f'{self._outcome_code(intent.phase, outcome)}'
                            '_late_discarded'
                        )
                        cursor = self._connection.execute(
                            '''
                            UPDATE room_mission_executions
                            SET status = 'cancelling',
                                code = 'cancel_requested',
                                state_revision = ?,
                                audit_sequence = ?,
                                active_operation_id = NULL,
                                cancel_target_result_code = ?,
                                updated_at = ?
                            WHERE tool_call_id = ?
                              AND lease_epoch = ?
                              AND state_revision = ?
                            ''',
                            (
                                revision,
                                sequence,
                                late_code,
                                now,
                                lease.tool_call_id,
                                lease.lease_epoch,
                                row['state_revision'],
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RoomMissionLedgerBusyError(
                                'room mission execution lease changed'
                            )
                        self._insert_event_locked(
                            tool_call_id=lease.tool_call_id,
                            sequence=sequence,
                            event_kind='late_discarded',
                            phase=intent.phase,
                            source=(
                                'recovery'
                                if recovering
                                else 'simulation_adapter'
                            ),
                            status='cancelling',
                            code=late_code,
                            operation_id=intent.operation_id,
                            observed_at=now,
                        )
                        self._bump_store_revision_locked()
                        self._connection.commit()
                        current = self._connection.execute(
                            'SELECT * FROM room_mission_executions '
                            'WHERE tool_call_id = ?',
                            (lease.tool_call_id,),
                        ).fetchone()
                        result = self._execution_from_row(current)
                        return result
                    if (
                        recovering
                        and now >= float(row['authorization_deadline'])
                    ):
                        if self._terminalize_event_capacity_locked(
                            row, now
                        ):
                            self._connection.commit()
                            raise RoomMissionLedgerStateError(
                                'room mission event capacity reached'
                            )
                        else:
                            result = (
                                self._record_expired_recovery_result_locked(
                                    row,
                                    intent,
                                    outcome,
                                    now,
                                )
                            )
                            self._connection.commit()
                            return result
                    code = self._outcome_code(intent.phase, outcome)
                    terminal = (
                        outcome != 'succeeded'
                        or intent.phase == 'live_ready'
                    )
                    if terminal:
                        terminal_status = (
                            'succeeded'
                            if outcome == 'succeeded'
                            else outcome
                        )
                        result = self._terminal_locked(
                            row,
                            terminal_status,
                            code,
                            now,
                            source=(
                                'recovery'
                                if recovering
                                else 'simulation_adapter'
                            ),
                            operation_id=intent.operation_id,
                        )
                    else:
                        if self._terminalize_event_capacity_locked(
                            row, now
                        ):
                            self._connection.commit()
                            raise RoomMissionLedgerStateError(
                                'room mission event capacity reached'
                            )
                        sequence = int(row['audit_sequence']) + 1
                        revision = int(row['state_revision']) + 1
                        self._connection.execute(
                            '''
                            UPDATE room_mission_executions
                            SET status = 'running',
                                code = ?,
                                state_revision = ?,
                                audit_sequence = ?,
                                active_operation_id = NULL,
                                lease_owner = CASE
                                    WHEN ? THEN NULL
                                    ELSE lease_owner
                                END,
                                lease_expires_at = CASE
                                    WHEN ? THEN NULL
                                    ELSE lease_expires_at
                                END,
                                updated_at = ?
                            WHERE tool_call_id = ?
                              AND lease_epoch = ?
                              AND state_revision = ?
                            ''',
                            (
                                code,
                                revision,
                                sequence,
                                int(recovering),
                                int(recovering),
                                now,
                                lease.tool_call_id,
                                lease.lease_epoch,
                                row['state_revision'],
                            ),
                        )
                        self._insert_event_locked(
                            tool_call_id=lease.tool_call_id,
                            sequence=sequence,
                            event_kind='observation',
                            phase=intent.phase,
                            source='simulation_adapter',
                            status='running',
                            code=code,
                            operation_id=intent.operation_id,
                            observed_at=now,
                        )
                        self._bump_store_revision_locked()
                        current = self._connection.execute(
                            'SELECT * FROM room_mission_executions '
                            'WHERE tool_call_id = ?',
                            (lease.tool_call_id,),
                        ).fetchone()
                        result = self._execution_from_row(current)
                    self._connection.commit()
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission result persistence failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def _record_expired_recovery_result_locked(
        self,
        row: sqlite3.Row,
        intent: PhaseIntent | RecoveryPhaseIntent,
        outcome: str,
        now: float,
    ) -> StoredMissionExecution:
        """Audit reconciliation, then stop instead of dispatching anew."""
        sequence = int(row['audit_sequence']) + 1
        revision = int(row['state_revision']) + 1
        observation_code = self._outcome_code(intent.phase, outcome)
        self._connection.execute(
            '''
            UPDATE room_mission_executions
            SET code = ?,
                state_revision = ?,
                audit_sequence = ?,
                active_operation_id = NULL,
                updated_at = ?
            WHERE tool_call_id = ?
              AND lease_epoch = ?
              AND state_revision = ?
            ''',
            (
                observation_code,
                revision,
                sequence,
                now,
                row['tool_call_id'],
                row['lease_epoch'],
                row['state_revision'],
            ),
        )
        self._insert_event_locked(
            tool_call_id=str(row['tool_call_id']),
            sequence=sequence,
            event_kind='observation',
            phase=intent.phase,
            source='recovery',
            status='reconcile_required',
            code=observation_code,
            operation_id=intent.operation_id,
            observed_at=now,
        )
        current = self._connection.execute(
            '''
            SELECT execution.*, proposal.owner_binding_digest,
                   proposal.subject_id,
                   proposal.conversation_id,
                   proposal.conversation_session_instance_id,
                   proposal.conversation_generation
            FROM room_mission_executions AS execution
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE execution.tool_call_id = ?
            ''',
            (row['tool_call_id'],),
        ).fetchone()
        return self._terminal_locked(
            current,
            'timed_out',
            'authorization_expired',
            now,
            source='recovery',
        )

    def get_execution(
        self,
        tool_call_id: str,
        authority: DurableMissionAuthority,
    ) -> StoredMissionExecution:
        """Read one exact owner-bound execution without mutating it."""
        _identifier(tool_call_id)
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        failure = False
        row = None
        with self._lock:
            try:
                self._begin_read_locked()
                row = self._owned_execution_locked(
                    tool_call_id, authority
                )
                self._commit_read_locked()
            except RoomMissionLedgerAuthorityError:
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission execution read failed')
        assert row is not None
        return self._execution_from_row(row)

    def list_recovery_candidates(
        self,
        authority: DurableMissionAuthority,
        limit: int = 100,
    ) -> Tuple[RecoveryCandidate, ...]:
        """List nonterminal durable rows; perform no adapter work."""
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RoomMissionLedgerValidationError(
                'recovery candidate limit is invalid'
            )
        failure = False
        rows = ()
        with self._lock:
            try:
                self._begin_read_locked()
                rows = self._connection.execute(
                    '''
                    SELECT execution.tool_call_id,
                           execution.status,
                           execution.phase,
                           execution.device_id,
                           execution.active_operation_id,
                           execution.cancel_requested,
                           execution.cancel_operation_id,
                           execution.lease_epoch,
                           execution.lease_expires_at
                    FROM room_mission_executions AS execution
                    JOIN room_mission_proposals AS proposal
                      ON proposal.proposal_id = execution.proposal_id
                    WHERE proposal.owner_binding_digest = ?
                      AND execution.status IN (
                        'pending',
                        'leased',
                        'running',
                        'cancelling',
                        'reconcile_required'
                    )
                    ORDER BY execution.updated_at ASC,
                             execution.tool_call_id ASC
                    LIMIT ?
                    ''',
                    (authority.binding_digest, limit),
                ).fetchall()
                self._commit_read_locked()
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission recovery read failed')
        return tuple(
            RecoveryCandidate(
                tool_call_id=str(row['tool_call_id']),
                status=str(row['status']),
                phase=str(row['phase']),
                device_id=str(row['device_id']),
                has_unresolved_intent=(
                    row['active_operation_id'] is not None
                ),
                lease_epoch=int(row['lease_epoch']),
                lease_expires_at=(
                    float(row['lease_expires_at'])
                    if row['lease_expires_at'] is not None
                    else None
                ),
                cancel_requested=bool(int(row['cancel_requested'])),
                cancel_operation_id=(
                    str(row['cancel_operation_id'])
                    if row['cancel_operation_id'] is not None
                    else None
                ),
            )
            for row in rows
        )

    def list_events(
        self,
        tool_call_id: str,
        authority: DurableMissionAuthority,
    ) -> Tuple[MissionLedgerEvent, ...]:
        """Return ordered content-free events for an authorized owner."""
        _identifier(tool_call_id)
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        failure = False
        rows = ()
        with self._lock:
            try:
                self._begin_read_locked()
                self._owned_execution_locked(tool_call_id, authority)
                rows = self._connection.execute(
                    '''
                    SELECT tool_call_id,
                           sequence,
                           event_kind,
                           phase,
                           source,
                           status,
                           code,
                           operation_id,
                           observed_at
                    FROM room_mission_events
                    WHERE tool_call_id = ?
                    ORDER BY sequence ASC
                    ''',
                    (tool_call_id,),
                ).fetchall()
                self._commit_read_locked()
            except RoomMissionLedgerAuthorityError:
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission event read failed')
        return tuple(
            MissionLedgerEvent(
                tool_call_id=str(row['tool_call_id']),
                sequence=int(row['sequence']),
                event_kind=str(row['event_kind']),
                phase=str(row['phase']),
                source=str(row['source']),
                status=str(row['status']),
                code=str(row['code']),
                operation_id=(
                    str(row['operation_id'])
                    if row['operation_id'] is not None
                    else None
                ),
                observed_at=float(row['observed_at']),
            )
            for row in rows
        )

    def claim_feedback(
        self,
        feedback_id: str,
        authority: DurableMissionAuthority,
        worker_id: str,
        prior_lease: Optional[FeedbackLease] = None,
    ) -> FeedbackLease:
        """Claim one owner-bound pending feedback handoff."""
        _identifier(feedback_id)
        _identifier(worker_id)
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        if prior_lease is not None:
            self._validate_feedback_lease(prior_lease)
            if prior_lease.feedback_id != feedback_id:
                raise RoomMissionLedgerValidationError(
                    'room mission feedback lease is invalid'
                )
        lease_token = secrets.token_urlsafe(32)
        token_digest = _text_digest(lease_token)
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._owned_feedback_locked(feedback_id, authority)
                state = str(row['state'])
                if state in {'committed', 'orphaned'}:
                    raise RoomMissionLedgerStateError(
                        'room mission feedback is terminal'
                    )
                if state == 'leased' and prior_lease is not None:
                    self._feedback_lease_row_matches(
                        row, prior_lease, now
                    )
                    if str(row['lease_owner']) != worker_id:
                        raise RoomMissionLedgerBusyError(
                            'room mission feedback is leased'
                        )
                    self._connection.commit()
                    result = FeedbackLease(
                        feedback_id=feedback_id,
                        lease_epoch=prior_lease.lease_epoch,
                        lease_token=prior_lease.lease_token,
                        expires_at=float(row['lease_expires_at']),
                        cached=True,
                    )
                else:
                    lease_expires_at = row['lease_expires_at']
                    if (
                        state == 'leased'
                        and lease_expires_at is not None
                        and float(lease_expires_at) > now
                    ):
                        raise RoomMissionLedgerBusyError(
                            'room mission feedback is leased'
                        )
                    if prior_lease is not None:
                        raise RoomMissionLedgerBusyError(
                            'room mission feedback lease changed'
                        )
                    epoch = int(row['lease_epoch']) + 1
                    expires_at = now + self._lease_seconds
                    cursor = self._connection.execute(
                        '''
                        UPDATE room_mission_feedback
                        SET state = 'leased',
                            lease_owner = ?,
                            lease_token_digest = ?,
                            lease_epoch = ?,
                            lease_expires_at = ?,
                            updated_at = ?
                        WHERE feedback_id = ?
                          AND lease_epoch = ?
                        ''',
                        (
                            worker_id,
                            token_digest,
                            epoch,
                            expires_at,
                            now,
                            feedback_id,
                            row['lease_epoch'],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoomMissionLedgerBusyError(
                            'room mission feedback lease changed'
                        )
                    self._bump_store_revision_locked()
                    self._connection.commit()
                    result = FeedbackLease(
                        feedback_id=feedback_id,
                        lease_epoch=epoch,
                        lease_token=lease_token,
                        expires_at=expires_at,
                        cached=False,
                    )
            except (
                RoomMissionLedgerAuthorityError,
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission feedback claim failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def mark_feedback_committed(
        self,
        lease: FeedbackLease,
        response_commit_id: str,
        conversation_revision_after: int,
    ) -> StoredFeedback:
        """Commit one fenced feedback receipt with exact retry semantics."""
        self._validate_feedback_lease(lease)
        _identifier(response_commit_id)
        if (
            type(conversation_revision_after) is not int
            or conversation_revision_after < 0
        ):
            raise RoomMissionLedgerValidationError(
                'room mission feedback revision is invalid'
            )
        return self._terminalize_feedback(
            lease,
            state='committed',
            response_commit_id=response_commit_id,
            conversation_revision_after=conversation_revision_after,
            orphan_code=None,
        )

    def mark_feedback_orphaned(
        self,
        lease: FeedbackLease,
        orphan_code: str,
    ) -> StoredFeedback:
        """Stop a fenced handoff when its owner destination is gone."""
        self._validate_feedback_lease(lease)
        if orphan_code not in FEEDBACK_ORPHAN_CODES:
            raise RoomMissionLedgerValidationError(
                'room mission feedback orphan code is invalid'
            )
        return self._terminalize_feedback(
            lease,
            state='orphaned',
            response_commit_id=None,
            conversation_revision_after=None,
            orphan_code=orphan_code,
        )

    def _terminalize_feedback(
        self,
        lease: FeedbackLease,
        *,
        state: str,
        response_commit_id: Optional[str],
        conversation_revision_after: Optional[int],
        orphan_code: Optional[str],
    ) -> StoredFeedback:
        """Apply one terminal feedback transition behind a lease fence."""
        now = self._now()
        monotonic_started = time.monotonic()
        failure = False
        result = None
        with self._lock:
            try:
                self._begin_locked()
                now = self._fresh_transaction_time(
                    now, monotonic_started
                )
                self._advance_clock_locked(now)
                row = self._feedback_row_locked(lease.feedback_id)
                if row is None:
                    raise RoomMissionLedgerBusyError(
                        'room mission feedback lease changed'
                    )
                current_state = str(row['state'])
                if current_state in {'committed', 'orphaned'}:
                    self._feedback_receipt_matches(row, lease)
                    exact = (
                        current_state == state
                        and row['response_commit_id']
                        == response_commit_id
                        and row['conversation_revision_after']
                        == conversation_revision_after
                        and row['orphan_code'] == orphan_code
                    )
                    if not exact:
                        raise RoomMissionLedgerConflictError(
                            'room mission feedback result conflicts'
                        )
                    self._connection.commit()
                    result = self._feedback_from_row(row, cached=True)
                else:
                    self._feedback_lease_row_matches(row, lease, now)
                    if (
                        state == 'committed'
                        and conversation_revision_after is not None
                        and conversation_revision_after
                        <= int(row['owner_conversation_revision'])
                    ):
                        raise RoomMissionLedgerStateError(
                            'room mission feedback revision is stale'
                        )
                    if response_commit_id is not None:
                        reused = self._connection.execute(
                            '''
                            SELECT 1
                            FROM room_mission_feedback
                            WHERE response_commit_id = ?
                              AND feedback_id != ?
                            LIMIT 1
                            ''',
                            (response_commit_id, lease.feedback_id),
                        ).fetchone()
                        if reused is not None:
                            raise RoomMissionLedgerConflictError(
                                'room mission feedback commit conflicts'
                            )
                    result_digest = _feedback_result_digest(
                        feedback_id=str(row['feedback_id']),
                        tool_call_id=str(row['tool_call_id']),
                        terminal_digest=str(row['terminal_digest']),
                        state=state,
                        lease_epoch=int(row['lease_epoch']),
                        lease_token_digest=str(
                            row['lease_token_digest']
                        ),
                        response_commit_id=response_commit_id,
                        conversation_revision_after=(
                            conversation_revision_after
                        ),
                        orphan_code=orphan_code,
                    )
                    cursor = self._connection.execute(
                        '''
                        UPDATE room_mission_feedback
                        SET state = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            response_commit_id = ?,
                            conversation_revision_after = ?,
                            orphan_code = ?,
                            result_digest = ?,
                            updated_at = ?
                        WHERE feedback_id = ?
                          AND lease_epoch = ?
                          AND state = 'leased'
                        ''',
                        (
                            state,
                            response_commit_id,
                            conversation_revision_after,
                            orphan_code,
                            result_digest,
                            now,
                            lease.feedback_id,
                            lease.lease_epoch,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoomMissionLedgerBusyError(
                            'room mission feedback lease changed'
                        )
                    self._bump_store_revision_locked()
                    self._connection.commit()
                    current = self._feedback_row_locked(
                        lease.feedback_id
                    )
                    result = self._feedback_from_row(
                        current, cached=False
                    )
            except (
                RoomMissionLedgerBusyError,
                RoomMissionLedgerClockError,
                RoomMissionLedgerConflictError,
                RoomMissionLedgerStateError,
            ):
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission feedback result failed')
        SQLiteRoomMissionStore._secure_file_permissions(self)
        assert result is not None
        return result

    def get_feedback(
        self,
        feedback_id: str,
        authority: DurableMissionAuthority,
    ) -> StoredFeedback:
        """Read one exact owner-bound feedback handoff."""
        _identifier(feedback_id)
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        failure = False
        row = None
        with self._lock:
            try:
                self._begin_read_locked()
                row = self._owned_feedback_locked(feedback_id, authority)
                self._commit_read_locked()
            except RoomMissionLedgerAuthorityError:
                self._rollback_locked()
                raise
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission feedback read failed')
        assert row is not None
        return self._feedback_from_row(row, cached=False)

    def list_feedback(
        self,
        authority: DurableMissionAuthority,
        states: Tuple[str, ...] = ('pending', 'leased'),
        limit: int = 100,
    ) -> Tuple[StoredFeedback, ...]:
        """List owner-bound handoffs for restart recovery."""
        if type(authority) is not DurableMissionAuthority:
            raise RoomMissionLedgerValidationError(
                'mission authority is invalid'
            )
        valid_states = {'pending', 'leased', 'committed', 'orphaned'}
        if (
            type(states) is not tuple
            or not states
            or len(states) > len(valid_states)
            or len(set(states)) != len(states)
            or any(state not in valid_states for state in states)
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise RoomMissionLedgerValidationError(
                'room mission feedback filter is invalid'
            )
        placeholders = ', '.join('?' for _state in states)
        failure = False
        rows = ()
        with self._lock:
            try:
                self._begin_read_locked()
                rows = self._connection.execute(
                    f'''
                    SELECT feedback.*
                    FROM room_mission_feedback AS feedback
                    JOIN room_mission_executions AS execution
                      ON execution.tool_call_id = feedback.tool_call_id
                    JOIN room_mission_proposals AS proposal
                      ON proposal.proposal_id = execution.proposal_id
                    WHERE proposal.owner_binding_digest = ?
                      AND feedback.state IN ({placeholders})
                    ORDER BY feedback.updated_at ASC,
                             feedback.feedback_id ASC
                    LIMIT ?
                    ''',
                    (authority.binding_digest, *states, limit),
                ).fetchall()
                self._commit_read_locked()
            except Exception:
                self._rollback_locked()
                failure = True
        if failure:
            _raise_sanitized('room mission feedback list failed')
        return tuple(
            self._feedback_from_row(row, cached=False)
            for row in rows
        )

    def _owned_proposal_locked(
        self,
        proposal_id: str,
        authority: DurableMissionAuthority,
    ) -> sqlite3.Row:
        row = self._connection.execute(
            'SELECT * FROM room_mission_proposals WHERE proposal_id = ?',
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise RoomMissionLedgerAuthorityError(
                'mission authority required'
            )
        self._require_owner_row(row, authority)
        return row

    def _cancel_intent_revision_locked(
        self,
        tool_call_id: str,
        operation_id: str,
    ) -> int:
        """Return the immutable revision at cancellation intent commit."""
        event = self._connection.execute(
            '''
            SELECT sequence
            FROM room_mission_events
            WHERE tool_call_id = ?
              AND operation_id = ?
              AND event_kind = 'cancel'
            ''',
            (tool_call_id, operation_id),
        ).fetchone()
        if event is None:
            raise RoomMissionLedgerStateError(
                'room mission cancellation intent is invalid'
            )
        return int(event['sequence'])

    def _abort_event_locked(
        self,
        tool_call_id: str,
    ) -> Optional[sqlite3.Row]:
        """Read the sole durable unresolved-abort policy marker."""
        rows = self._connection.execute(
            '''
            SELECT sequence, code
            FROM room_mission_events
            WHERE tool_call_id = ?
              AND event_kind = 'recovery'
              AND source = 'recovery'
            ORDER BY sequence ASC
            ''',
            (tool_call_id,),
        ).fetchall()
        aborts = [
            row for row in rows
            if str(row['code']) in ABORT_EXECUTION_CODES
        ]
        if len(aborts) > 1:
            raise RoomMissionLedgerStateError(
                'room mission abort record is invalid'
            )
        return aborts[0] if aborts else None

    def _reconciliation_operation_locked(
        self,
        row: sqlite3.Row,
        intent: RecoveryPhaseIntent | CancelIntent,
        *,
        terminal: bool,
    ) -> Optional[str]:
        """Resolve an exact durable recovery typestate to its operation."""
        tool_call_id = str(row['tool_call_id'])
        if type(intent) is RecoveryPhaseIntent:
            intent_event = self._connection.execute(
                '''
                SELECT phase
                FROM room_mission_events
                WHERE tool_call_id = ?
                  AND operation_id = ?
                  AND event_kind = 'intent'
                ''',
                (tool_call_id, intent.operation_id),
            ).fetchone()
            if (
                intent_event is None
                or str(intent_event['phase']) != intent.phase
                or (
                    not terminal
                    and (
                        str(row['status']) != 'reconcile_required'
                        or bool(int(row['cancel_requested']))
                        or row['active_operation_id']
                        != intent.operation_id
                        or str(row['phase']) != intent.phase
                        or intent.state_revision
                        != int(row['state_revision'])
                    )
                )
                or (
                    terminal
                    and intent.state_revision
                    != int(row['state_revision']) - 1
                )
            ):
                return None
            return intent.operation_id
        cancel_event = self._connection.execute(
            '''
            SELECT sequence
            FROM room_mission_events
            WHERE tool_call_id = ?
              AND operation_id = ?
              AND event_kind = 'cancel'
            ''',
            (tool_call_id, intent.operation_id),
        ).fetchone()
        stored_target = (
            str(row['cancel_target_operation_id'])
            if row['cancel_target_operation_id'] is not None
            else None
        )
        if (
            cancel_event is None
            or not bool(int(row['cancel_requested']))
            or row['cancel_operation_id'] != intent.operation_id
            or int(cancel_event['sequence']) != intent.state_revision
            or stored_target != intent.superseded_phase_operation_id
            or (not terminal and str(row['status']) != 'cancelling')
        ):
            return None
        return intent.operation_id

    def _owned_feedback_locked(
        self,
        feedback_id: str,
        authority: DurableMissionAuthority,
    ) -> sqlite3.Row:
        row = self._connection.execute(
            '''
            SELECT feedback.*, proposal.owner_binding_digest
            FROM room_mission_feedback AS feedback
            JOIN room_mission_executions AS execution
              ON execution.tool_call_id = feedback.tool_call_id
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE feedback.feedback_id = ?
            ''',
            (feedback_id,),
        ).fetchone()
        if row is None:
            raise RoomMissionLedgerAuthorityError(
                'mission authority required'
            )
        self._require_owner_row(row, authority)
        return row

    def _feedback_row_locked(
        self,
        feedback_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT feedback.*,
                   proposal.conversation_revision
                       AS owner_conversation_revision
            FROM room_mission_feedback AS feedback
            JOIN room_mission_executions AS execution
              ON execution.tool_call_id = feedback.tool_call_id
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE feedback.feedback_id = ?
            ''',
            (feedback_id,),
        ).fetchone()

    def _owned_execution_locked(
        self,
        tool_call_id: str,
        authority: DurableMissionAuthority,
    ) -> sqlite3.Row:
        row = self._execution_with_owner_locked(tool_call_id)
        if row is None:
            raise RoomMissionLedgerAuthorityError(
                'mission authority required'
            )
        self._require_owner_row(row, authority)
        return row

    def _execution_with_owner_locked(
        self,
        tool_call_id: str,
    ) -> Optional[sqlite3.Row]:
        """Read one execution joined to content-free owner fields."""
        return self._connection.execute(
            '''
            SELECT execution.*, proposal.owner_binding_digest,
                   proposal.subject_id,
                   proposal.conversation_id,
                   proposal.conversation_session_instance_id,
                   proposal.conversation_generation
            FROM room_mission_executions AS execution
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE execution.tool_call_id = ?
            ''',
            (tool_call_id,),
        ).fetchone()

    @staticmethod
    def _require_owner_row(
        row: sqlite3.Row,
        authority: DurableMissionAuthority,
    ) -> None:
        stored = str(row['owner_binding_digest'])
        if not hmac.compare_digest(stored, authority.binding_digest):
            raise RoomMissionLedgerAuthorityError(
                'mission authority required'
            )

    @staticmethod
    def _confirmation_matches(
        proposal: sqlite3.Row,
        authority: DurableMissionAuthority,
        confirmation: DurableMissionConfirmation,
        now: float,
    ) -> bool:
        return (
            hmac.compare_digest(
                authority.binding_digest,
                confirmation.authority.binding_digest,
            )
            and hmac.compare_digest(
                str(proposal['owner_binding_digest']),
                authority.binding_digest,
            )
            and confirmation.decision_id == proposal['decision_id']
            and confirmation.person_subject_id == authority.subject_id
            and hmac.compare_digest(
                confirmation.arguments_digest,
                str(proposal['arguments_digest']),
            )
            and float(proposal['issued_at']) - MAX_CLOCK_SKEW_SECONDS
            <= float(confirmation.issued_at)
            <= now + MAX_CLOCK_SKEW_SECONDS
            and float(confirmation.issued_at)
            < float(confirmation.expires_at)
            and float(confirmation.expires_at)
            <= float(proposal['expires_at'])
        )

    def _leased_execution_locked(
        self,
        lease: ExecutionLease,
        now: float,
    ) -> sqlite3.Row:
        row = self._connection.execute(
            'SELECT * FROM room_mission_executions '
            'WHERE tool_call_id = ?',
            (lease.tool_call_id,),
        ).fetchone()
        if row is None:
            raise RoomMissionLedgerStateError(
                'room mission execution is unavailable'
            )
        self._leased_execution_row_matches(row, lease, now)
        return row

    @staticmethod
    def _leased_execution_row_matches(
        row: sqlite3.Row,
        lease: ExecutionLease,
        now: float,
    ) -> None:
        stored_digest = row['lease_token_digest']
        if (
            stored_digest is None
            or int(row['lease_epoch']) != lease.lease_epoch
            or not hmac.compare_digest(
                str(stored_digest), _text_digest(lease.lease_token)
            )
            or row['lease_expires_at'] is None
            or now >= float(row['lease_expires_at'])
        ):
            raise RoomMissionLedgerBusyError(
                'room mission execution lease changed'
            )

    @staticmethod
    def _validate_feedback_lease(lease: Any) -> None:
        if (
            type(lease) is not FeedbackLease
            or type(lease.feedback_id) is not str
            or not lease.feedback_id
            or type(lease.lease_epoch) is not int
            or lease.lease_epoch < 1
            or type(lease.lease_token) is not str
            or len(lease.lease_token) < 32
            or type(lease.cached) is not bool
        ):
            raise RoomMissionLedgerValidationError(
                'room mission feedback lease is invalid'
            )

    @staticmethod
    def _feedback_lease_row_matches(
        row: sqlite3.Row,
        lease: FeedbackLease,
        now: float,
    ) -> None:
        stored_digest = row['lease_token_digest']
        if (
            str(row['state']) != 'leased'
            or stored_digest is None
            or int(row['lease_epoch']) != lease.lease_epoch
            or not hmac.compare_digest(
                str(stored_digest), _text_digest(lease.lease_token)
            )
            or row['lease_expires_at'] is None
            or now >= float(row['lease_expires_at'])
        ):
            raise RoomMissionLedgerBusyError(
                'room mission feedback lease changed'
            )

    @staticmethod
    def _feedback_receipt_matches(
        row: sqlite3.Row,
        lease: FeedbackLease,
    ) -> None:
        stored_digest = row['lease_token_digest']
        result_digest = row['result_digest']
        expected_result_digest = (
            None
            if stored_digest is None
            else _feedback_result_digest(
                feedback_id=str(row['feedback_id']),
                tool_call_id=str(row['tool_call_id']),
                terminal_digest=str(row['terminal_digest']),
                state=str(row['state']),
                lease_epoch=int(row['lease_epoch']),
                lease_token_digest=str(stored_digest),
                response_commit_id=(
                    str(row['response_commit_id'])
                    if row['response_commit_id'] is not None
                    else None
                ),
                conversation_revision_after=(
                    int(row['conversation_revision_after'])
                    if row['conversation_revision_after'] is not None
                    else None
                ),
                orphan_code=(
                    str(row['orphan_code'])
                    if row['orphan_code'] is not None
                    else None
                ),
            )
        )
        if (
            stored_digest is None
            or int(row['lease_epoch']) != lease.lease_epoch
            or not hmac.compare_digest(
                str(stored_digest), _text_digest(lease.lease_token)
            )
            or result_digest is None
            or expected_result_digest is None
            or not hmac.compare_digest(
                str(result_digest), expected_result_digest
            )
        ):
            raise RoomMissionLedgerBusyError(
                'room mission feedback lease changed'
            )

    @staticmethod
    def _feedback_from_row(
        row: sqlite3.Row,
        *,
        cached: bool,
    ) -> StoredFeedback:
        return StoredFeedback(
            feedback_id=str(row['feedback_id']),
            tool_call_id=str(row['tool_call_id']),
            state=str(row['state']),
            terminal_digest=str(row['terminal_digest']),
            lease_epoch=int(row['lease_epoch']),
            lease_expires_at=(
                float(row['lease_expires_at'])
                if row['lease_expires_at'] is not None
                else None
            ),
            response_commit_id=(
                str(row['response_commit_id'])
                if row['response_commit_id'] is not None
                else None
            ),
            conversation_revision_after=(
                int(row['conversation_revision_after'])
                if row['conversation_revision_after'] is not None
                else None
            ),
            orphan_code=(
                str(row['orphan_code'])
                if row['orphan_code'] is not None
                else None
            ),
            cached=cached,
        )

    @staticmethod
    def _execution_receipt_matches(
        row: sqlite3.Row,
        lease: ExecutionLease,
    ) -> None:
        stored_digest = row['lease_token_digest']
        if (
            stored_digest is None
            or int(row['lease_epoch']) != lease.lease_epoch
            or not hmac.compare_digest(
                str(stored_digest), _text_digest(lease.lease_token)
            )
        ):
            raise RoomMissionLedgerBusyError(
                'room mission execution lease changed'
            )

    @staticmethod
    def _validate_lease(lease: Any) -> None:
        if (
            type(lease) is not ExecutionLease
            or type(lease.lease_epoch) is not int
            or lease.lease_epoch < 1
            or type(lease.lease_token) is not str
            or len(lease.lease_token) < 32
        ):
            raise RoomMissionLedgerValidationError(
                'room mission execution lease is invalid'
            )
        _identifier(lease.tool_call_id)
        _timestamp(lease.expires_at)

    @staticmethod
    def _next_phase(row: sqlite3.Row) -> str:
        phase = str(row['phase'])
        status = str(row['status'])
        if status == 'leased' and phase == 'confirmation':
            return 'preflight'
        completed_code = f'{phase}_succeeded'
        if (
            status in {'running', 'leased'}
            and str(row['code']) == completed_code
        ):
            index = EXECUTABLE_PHASES.index(phase)
            if index + 1 < len(EXECUTABLE_PHASES):
                return EXECUTABLE_PHASES[index + 1]
        return ''

    @staticmethod
    def _outcome_code(phase: str, outcome: str) -> str:
        if outcome == 'succeeded':
            return (
                'simulation_succeeded'
                if phase == 'live_ready'
                else f'{phase}_succeeded'
            )
        if outcome == 'timed_out':
            return f'{phase}_timeout'
        return f'{phase}_failed'

    def _terminal_locked(
        self,
        row: sqlite3.Row,
        status: str,
        code: str,
        now: float,
        *,
        source: str,
        operation_id: Optional[str] = None,
    ) -> StoredMissionExecution:
        if status not in TERMINAL_EXECUTION_STATUSES:
            raise RoomMissionLedgerStateError(
                'room mission terminal state is invalid'
            )
        if source not in {'controller', 'simulation_adapter', 'recovery'}:
            raise RoomMissionLedgerStateError(
                'room mission terminal source is invalid'
            )
        if str(row['status']) in TERMINAL_EXECUTION_STATUSES:
            return self._execution_from_row(row)
        sequence = int(row['audit_sequence']) + 1
        revision = int(row['state_revision']) + 1
        payload = {
            'status': status,
            'phase': 'terminal',
            'code': code,
            'tool_call_id': str(row['tool_call_id']),
            'runtime_mode': 'simulation',
            'simulated': True,
            'physical_effects': False,
            'viewer_live': False,
            'durability': self._durability,
            'lease_scope': self._lease_scope,
            'terminal_source': source,
            'superseded_phase_operation_id': (
                str(row['cancel_target_operation_id'])
                if row['cancel_target_operation_id'] is not None
                else None
            ),
            'superseded_phase_result_code': (
                str(row['cancel_target_result_code'])
                if row['cancel_target_result_code'] is not None
                else None
            ),
        }
        payload_json = _canonical_json(payload)
        terminal_digest = _text_digest(payload_json)
        self._connection.execute(
            '''
            UPDATE room_mission_executions
            SET status = ?,
                phase = 'terminal',
                code = ?,
                state_revision = ?,
                audit_sequence = ?,
                active_operation_id = NULL,
                lease_owner = NULL,
                lease_expires_at = NULL,
                terminal_at = ?,
                terminal_digest = ?,
                terminal_payload_json = ?,
                updated_at = ?
            WHERE tool_call_id = ?
            ''',
            (
                status,
                code,
                revision,
                sequence,
                now,
                terminal_digest,
                payload_json,
                now,
                row['tool_call_id'],
            ),
        )
        self._insert_event_locked(
            tool_call_id=str(row['tool_call_id']),
            sequence=sequence,
            event_kind='terminal',
            phase='terminal',
            source=source,
            status=status,
            code=code,
            operation_id=operation_id,
            observed_at=now,
        )
        token = _text_digest(str(row['tool_call_id']))
        self._connection.execute(
            '''
            INSERT INTO room_mission_feedback (
                feedback_id,
                tool_call_id,
                subject_id,
                conversation_id,
                conversation_session_instance_id,
                conversation_generation,
                feedback_request_id,
                feedback_turn_id,
                terminal_digest,
                state,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ''',
            (
                f'room-feedback-{token}',
                row['tool_call_id'],
                row['subject_id'],
                row['conversation_id'],
                row['conversation_session_instance_id'],
                row['conversation_generation'],
                f'room-feedback-request-{token}',
                f'room-feedback-turn-{token}',
                terminal_digest,
                now,
                now,
            ),
        )
        self._bump_store_revision_locked()
        current = self._connection.execute(
            'SELECT * FROM room_mission_executions '
            'WHERE tool_call_id = ?',
            (row['tool_call_id'],),
        ).fetchone()
        return self._execution_from_row(current)

    def _mark_reconcile_required_locked(
        self,
        row: sqlite3.Row,
        now: float,
        code: str,
    ) -> None:
        sequence = int(row['audit_sequence']) + 1
        revision = int(row['state_revision']) + 1
        self._connection.execute(
            '''
            UPDATE room_mission_executions
            SET status = 'reconcile_required',
                code = ?,
                state_revision = ?,
                audit_sequence = ?,
                lease_owner = NULL,
                lease_token_digest = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE tool_call_id = ?
            ''',
            (
                code,
                revision,
                sequence,
                now,
                row['tool_call_id'],
            ),
        )
        self._insert_event_locked(
            tool_call_id=str(row['tool_call_id']),
            sequence=sequence,
            event_kind='recovery',
            phase=str(row['phase']),
            source='recovery',
            status='reconcile_required',
            code=code,
            operation_id=row['active_operation_id'],
            observed_at=now,
        )
        self._bump_store_revision_locked()

    def _expire_clean_device_execution_locked(
        self,
        device_id: str,
        now: float,
    ) -> None:
        """Release only an expired device slot with no unresolved intent."""
        row = self._connection.execute(
            '''
            SELECT execution.*, proposal.owner_binding_digest,
                   proposal.subject_id,
                   proposal.conversation_id,
                   proposal.conversation_session_instance_id,
                   proposal.conversation_generation
            FROM room_mission_executions AS execution
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE execution.device_id = ?
              AND execution.status IN (
                  'pending',
                  'leased',
                  'running',
                  'cancelling',
                  'reconcile_required'
              )
            LIMIT 1
            ''',
            (device_id,),
        ).fetchone()
        if row is None or now < float(row['authorization_deadline']):
            return
        if self._terminalize_event_capacity_locked(row, now):
            return
        if bool(int(row['cancel_requested'])):
            return
        if row['active_operation_id'] is not None:
            if str(row['status']) != 'reconcile_required':
                self._mark_reconcile_required_locked(
                    row,
                    now,
                    'authorization_expired',
                )
            return
        self._terminal_locked(
            row,
            'timed_out',
            'authorization_expired',
            now,
            source='controller',
        )

    def _terminalize_event_capacity_locked(
        self,
        row: sqlite3.Row,
        now: float,
    ) -> bool:
        """Reserve the final event slot for a releasing terminal record."""
        if int(row['audit_sequence']) < MAX_EVENTS_PER_MISSION - 1:
            return False
        current = self._connection.execute(
            '''
            SELECT execution.*, proposal.owner_binding_digest,
                   proposal.subject_id,
                   proposal.conversation_id,
                   proposal.conversation_session_instance_id,
                   proposal.conversation_generation
            FROM room_mission_executions AS execution
            JOIN room_mission_proposals AS proposal
              ON proposal.proposal_id = execution.proposal_id
            WHERE execution.tool_call_id = ?
            ''',
            (row['tool_call_id'],),
        ).fetchone()
        if current is None:
            raise RoomMissionLedgerStateError(
                'room mission execution is unavailable'
            )
        self._terminal_locked(
            current,
            'failed',
            'event_capacity_reached',
            now,
            source='controller',
            operation_id=(
                current['cancel_operation_id']
                if bool(int(current['cancel_requested']))
                else current['active_operation_id']
            ),
        )
        return True

    def _execution_from_row(
        self,
        row: sqlite3.Row,
    ) -> StoredMissionExecution:
        return StoredMissionExecution(
            tool_call_id=str(row['tool_call_id']),
            status=str(row['status']),
            phase=str(row['phase']),
            code=str(row['code']),
            state_revision=int(row['state_revision']),
            active_operation_id=(
                str(row['active_operation_id'])
                if row['active_operation_id'] is not None
                else None
            ),
            lease_epoch=int(row['lease_epoch']),
            lease_expires_at=(
                float(row['lease_expires_at'])
                if row['lease_expires_at'] is not None
                else None
            ),
            terminal_digest=(
                str(row['terminal_digest'])
                if row['terminal_digest'] is not None
                else None
            ),
            cancel_requested=bool(int(row['cancel_requested'])),
            cancel_operation_id=(
                str(row['cancel_operation_id'])
                if row['cancel_operation_id'] is not None
                else None
            ),
            durability=self._durability,
            lease_scope=self._lease_scope,
        )

    def _insert_event_locked(
        self,
        *,
        tool_call_id: str,
        sequence: int,
        event_kind: str,
        phase: str,
        source: str,
        status: str,
        code: str,
        operation_id: Optional[str],
        observed_at: float,
    ) -> None:
        if (
            type(sequence) is not int
            or not 1 <= sequence <= MAX_EVENTS_PER_MISSION
        ):
            raise RoomMissionLedgerStateError(
                'room mission event capacity reached'
            )
        self._connection.execute(
            '''
            INSERT INTO room_mission_events (
                tool_call_id,
                sequence,
                event_kind,
                phase,
                source,
                status,
                code,
                operation_id,
                observed_at,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
            ''',
            (
                tool_call_id,
                sequence,
                event_kind,
                phase,
                source,
                status,
                code,
                operation_id,
                observed_at,
            ),
        )

    def _advance_clock_locked(self, now: float) -> None:
        state = self._state_row_locked()
        last = float(state['last_observed_at'])
        if now < last - MAX_CLOCK_SKEW_SECONDS:
            raise RoomMissionLedgerClockError(
                'room mission clock moved backwards'
            )
        if now > last:
            self._connection.execute(
                '''
                UPDATE room_mission_store_state
                SET last_observed_at = ?
                WHERE singleton = 1
                ''',
                (now,),
            )

    def _state_row_locked(self) -> sqlite3.Row:
        row = self._connection.execute(
            'SELECT * FROM room_mission_store_state WHERE singleton = 1'
        ).fetchone()
        if row is None:
            raise RoomMissionLedgerSchemaError(
                'room mission ledger state is missing'
            )
        return row

    def _bump_store_revision_locked(self) -> None:
        cursor = self._connection.execute(
            '''
            UPDATE room_mission_store_state
            SET revision = revision + 1
            WHERE singleton = 1
            '''
        )
        if cursor.rowcount != 1:
            raise RoomMissionLedgerSchemaError(
                'room mission ledger state is missing'
            )

    def _candidate_ids(self, prefix: str) -> Tuple[str, ...]:
        values = []
        for _index in range(3):
            token = None
            try:
                token = self._id_factory()
            except Exception:
                pass
            if (
                type(token) is not str
                or not token
                or len(token) > 64
                or not token.isascii()
                or any(
                    not (
                        character.isalnum()
                        or character in {'-', '_'}
                    )
                    for character in token
                )
            ):
                raise RoomMissionLedgerValidationError(
                    'server mission identifier generation failed'
                )
            values.append(f'{prefix}-{token}')
        return tuple(values)

    def _unused_id_locked(
        self,
        table: str,
        column: str,
        candidates: Tuple[str, ...],
    ) -> str:
        if table not in {
            'room_mission_proposals',
            'room_mission_executions',
        } or column not in {'proposal_id', 'tool_call_id'}:
            raise RoomMissionLedgerStateError(
                'server mission identifier target is invalid'
            )
        for candidate in candidates:
            row = self._connection.execute(
                f'SELECT 1 FROM {table} WHERE {column} = ?',
                (candidate,),
            ).fetchone()
            if row is None:
                return candidate
        raise RoomMissionLedgerStateError(
            'server mission identifier collision'
        )

    def _begin_locked(self) -> None:
        if self._closed:
            raise RoomMissionLedgerStateError(
                'room mission ledger is closed'
            )
        self._require_durable_identity_locked()
        self._connection.execute('BEGIN IMMEDIATE')

    def _begin_read_locked(self) -> None:
        """Start a read only while the attested DB remains live."""
        if self._closed:
            raise RoomMissionLedgerStateError(
                'room mission ledger is closed'
            )
        self._require_durable_identity_locked()
        self._connection.execute('BEGIN')

    def _commit_read_locked(self) -> None:
        """Commit a read and reject post-read identity drift."""
        self._connection.commit()
        self._require_durable_identity_locked()

    def _require_durable_identity_locked(self) -> None:
        """Fail closed when the live SQLite identity or policy drifted."""
        valid = False
        try:
            valid = (
                SQLiteRoomMissionStore
                ._durable_identity_matches_locked(self)
            )
        except Exception:
            pass
        if not valid:
            raise RoomMissionLedgerStateError(
                'room mission ledger identity is unavailable'
            )

    def _rollback_locked(self) -> None:
        """Best-effort rollback without exposing a SQLite failure."""
        try:
            self._connection.rollback()
        except Exception:
            pass

    def _now(self) -> float:
        value = None
        try:
            value = self._clock()
        except Exception:
            pass
        if (
            type(value) not in {int, float}
            or not math.isfinite(float(value))
        ):
            raise RoomMissionLedgerClockError(
                'room mission clock is invalid'
            )
        return float(value)

    def _fresh_transaction_time(
        self,
        wall_snapshot: float,
        monotonic_started: float,
    ) -> float:
        """Advance a pre-lock wall snapshot by trusted wait duration."""
        monotonic_now = time.monotonic()
        if self._clock_is_system:
            value = time.time()
        else:
            runtime = _registered_store_runtime(self)
            stale_snapshot = (
                runtime.last_snapshot_started is not None
                and monotonic_started < runtime.last_snapshot_started
            )
            if not stale_snapshot:
                if (
                    runtime.last_wall_snapshot is not None
                    and wall_snapshot
                    < (
                        runtime.last_wall_snapshot
                        - MAX_CLOCK_SKEW_SECONDS
                    )
                ):
                    raise RoomMissionLedgerClockError(
                        'room mission clock moved backwards'
                    )
                runtime.last_wall_snapshot = wall_snapshot
                runtime.last_snapshot_started = monotonic_started
            candidate = wall_snapshot + max(
                0.0, monotonic_now - monotonic_started
            )
            durable_floor = float(
                self._state_row_locked()['last_observed_at']
            )
            value = max(
                candidate,
                durable_floor,
                (
                    durable_floor
                    if stale_snapshot
                    else wall_snapshot + runtime.clock_offset
                ),
            )
            if not stale_snapshot:
                runtime.clock_offset = max(
                    runtime.clock_offset,
                    value - wall_snapshot,
                )
        if not math.isfinite(value):
            raise RoomMissionLedgerClockError(
                'room mission clock is invalid'
            )
        return float(value)

    def _prepare_database_file(self) -> Tuple[str, int, int]:
        """Validate and return the canonical file identity to attest."""
        expanded = str(Path(self.database_path).expanduser())
        self._validate_parent_directory(expanded)
        no_follow = getattr(os, 'O_NOFOLLOW', 0)
        descriptor = None
        try:
            descriptor = os.open(
                expanded,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | no_follow,
                0o600,
            )
        except FileExistsError:
            descriptor = os.open(expanded, os.O_RDWR | no_follow)
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = os.lstat(expanded)
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or stat.S_ISLNK(path_stat.st_mode)
                or descriptor_stat.st_nlink != 1
                or path_stat.st_nlink != 1
                or descriptor_stat.st_dev != path_stat.st_dev
                or descriptor_stat.st_ino != path_stat.st_ino
                or descriptor_stat.st_uid != os.geteuid()
            ):
                raise RoomMissionLedgerSchemaError(
                    'room mission database target is invalid'
                )
            os.fchmod(descriptor, 0o600)
            canonical_path = str(
                Path(expanded).resolve(strict=True)
            )
            canonical_stat = os.lstat(canonical_path)
            if (
                canonical_stat.st_dev != descriptor_stat.st_dev
                or canonical_stat.st_ino != descriptor_stat.st_ino
                or canonical_stat.st_nlink != 1
            ):
                raise RoomMissionLedgerSchemaError(
                    'room mission database target is invalid'
                )
            return (
                canonical_path,
                int(descriptor_stat.st_dev),
                int(descriptor_stat.st_ino),
            )
        finally:
            os.close(descriptor)

    def _secure_file_permissions(
        self,
        *,
        provision: bool = False,
    ) -> None:
        """Provision at open or reject runtime permission drift."""
        failure = False
        try:
            with self._lock:
                runtime = _registered_store_runtime(self)
                if provision:
                    valid = (
                        SQLiteRoomMissionStore
                        ._durable_identity_matches_locked(
                            self,
                            require_private_permissions=False,
                        )
                    )
                else:
                    valid = (
                        SQLiteRoomMissionStore
                        ._durable_identity_matches_locked(self)
                    )
                if not valid:
                    raise RoomMissionLedgerSchemaError(
                        'room mission database identity is invalid'
                    )
                if (
                    provision
                    and runtime.database_path != ':memory:'
                ):
                    expanded = runtime.attested_main_path
                    SQLiteRoomMissionStore._validate_parent_directory(
                        expanded
                    )
                    for suffix in ('', '-wal', '-shm'):
                        candidate = expanded + suffix
                        if not os.path.lexists(candidate):
                            if not suffix:
                                raise RoomMissionLedgerSchemaError(
                                    'room mission database file is invalid'
                                )
                            continue
                        try:
                            candidate_stat = os.lstat(candidate)
                        except FileNotFoundError:
                            if suffix:
                                continue
                            raise
                        if (
                            stat.S_ISLNK(candidate_stat.st_mode)
                            or not stat.S_ISREG(candidate_stat.st_mode)
                            or candidate_stat.st_nlink != 1
                            or candidate_stat.st_uid != os.geteuid()
                        ):
                            raise RoomMissionLedgerSchemaError(
                                'room mission database file is invalid'
                            )
                        try:
                            os.chmod(
                                candidate,
                                0o600,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            if suffix:
                                continue
                            raise
                if provision and not (
                    SQLiteRoomMissionStore
                    ._durable_identity_matches_locked(self)
                ):
                    raise RoomMissionLedgerSchemaError(
                        'room mission database identity is invalid'
                    )
        except Exception:
            failure = True
        if failure:
            _raise_sanitized('room mission ledger permissions failed')

    @staticmethod
    def _validate_parent_directory(database_path: str) -> None:
        """Require one private, owner-controlled final directory."""
        parent = Path(database_path).parent
        resolved = parent.resolve(strict=True)
        parent_stat = os.stat(resolved, follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or parent_stat.st_mode & 0o022
        ):
            raise RoomMissionLedgerSchemaError(
                'room mission database directory is invalid'
            )


SQLiteRoomMissionLedger = SQLiteRoomMissionStore


def _authority_binding_digest(fields: Dict[str, Any]) -> str:
    """Hash canonical non-secret authority binding fields."""
    return _json_digest(fields)


def _identifier(value: Any) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_IDENTIFIER_LENGTH
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise RoomMissionLedgerValidationError(
            'room mission identifier is invalid'
        )
    return value


def _digest(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise RoomMissionLedgerValidationError(
            'room mission digest is invalid'
        )
    return value


def _timestamp(value: Any) -> float:
    if (
        type(value) not in {int, float}
        or not math.isfinite(float(value))
    ):
        raise RoomMissionLedgerValidationError(
            'room mission time is invalid'
        )
    return float(value)


def _canonical_json(value: Dict[str, Any]) -> str:
    encoded = None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        pass
    if encoded is None or len(encoded.encode('utf-8')) > (
        MAX_EVENT_PAYLOAD_BYTES
    ):
        raise RoomMissionLedgerValidationError(
            'room mission payload is invalid'
        )
    return encoded


def _json_digest(value: Dict[str, Any]) -> str:
    return _text_digest(_canonical_json(value))


def _schema_json_digest(value: Dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )
    return _text_digest(encoded)


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _operation_id(tool_call_id: str, phase: str) -> str:
    _identifier(tool_call_id)
    if phase not in EXECUTABLE_PHASES:
        raise RoomMissionLedgerValidationError(
            'mission execution phase is invalid'
        )
    digest = _text_digest(
        f'room-mission-operation/v1|{tool_call_id}|{phase}'
    )
    return f'room-operation-{digest}'


def _cancel_operation_id(tool_call_id: str) -> str:
    _identifier(tool_call_id)
    digest = _text_digest(
        f'room-mission-cancel/v1|{tool_call_id}'
    )
    return f'room-cancel-{digest}'


def _feedback_result_digest(
    *,
    feedback_id: str,
    tool_call_id: str,
    terminal_digest: str,
    state: str,
    lease_epoch: int,
    lease_token_digest: str,
    response_commit_id: Optional[str],
    conversation_revision_after: Optional[int],
    orphan_code: Optional[str],
) -> str:
    """Bind one terminal feedback receipt to its fenced outcome."""
    return _json_digest({
        'feedback_id': feedback_id,
        'tool_call_id': tool_call_id,
        'terminal_digest': terminal_digest,
        'state': state,
        'lease_epoch': lease_epoch,
        'lease_token_digest': lease_token_digest,
        'response_commit_id': response_commit_id,
        'conversation_revision_after': conversation_revision_after,
        'orphan_code': orphan_code,
    })


def _raise_sanitized(message: str) -> None:
    error = RoomMissionLedgerError(message)
    error.__cause__ = None
    error.__context__ = None
    raise error


__all__ = [
    'ABORT_EXECUTION_CODES',
    'CancelIntent',
    'CancellationRequest',
    'DurableMissionAuthority',
    'DurableMissionConfirmation',
    'DurableMissionProposal',
    'ExecutionLease',
    'FeedbackLease',
    'FEEDBACK_ORPHAN_CODES',
    'MissionLedgerEvent',
    'MAX_AUTHORIZATION_TTL_SECONDS',
    'MAX_EVENTS_PER_MISSION',
    'PhaseIntent',
    'PROPOSAL_INVALIDATION_CODES',
    'RecoveryPhaseIntent',
    'RecoveryCandidate',
    'RECONCILIATION_FAILURE_CODE',
    'ROOM_MISSION_SCHEMA_VERSION',
    'ROOM_MISSION_WRITER_PROTOCOL_VERSION',
    'RoomMissionLedgerAuthorityError',
    'RoomMissionLedgerBusyError',
    'RoomMissionLedgerCapacityError',
    'RoomMissionLedgerClockError',
    'RoomMissionLedgerConflictError',
    'RoomMissionLedgerError',
    'RoomMissionLedgerSchemaError',
    'RoomMissionLedgerStateError',
    'RoomMissionLedgerValidationError',
    'SQLiteRoomMissionLedger',
    'SQLiteRoomMissionStore',
    'StoredMissionAuthorization',
    'StoredMissionExecution',
    'StoredMissionProposal',
    'StoredFeedback',
]
