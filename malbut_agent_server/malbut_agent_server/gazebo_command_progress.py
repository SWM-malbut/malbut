"""
Server-owned durable progress for explicit Gazebo command execution.

This module keeps the caller outside the execution cursor.  A caller can
present only one confirmation identifier and one opaque intent previously
issued by this service.  Outbox, operation, fence, owner, request-chain, and
gateway evidence values remain private in the Agent SQLite database.

Every gateway result that advances authoritative state is appended as an
immutable step before the materialized head moves.  Intent receipts make a
lost HTTP response or process restart exact-replay the same public result.
No constructor starts a worker and every call performs at most one foreground
gateway exchange.
"""

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
from typing import Any, Dict, Optional, Tuple
import weakref

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.execution_ledger import (
    SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL,
)
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboPreparedExecutionAuthority,
    GazeboSimulationExecutionPolicy,
    _POLICY_CURRENT_BOOTTIME_NS,
    _POLICY_CURRENT_HOST_BOOT_ID,
    _validate_outbox_row_locked,
    resolve_prepared_gazebo_execution_locked,
    validate_gazebo_execution_outbox_schema_locked,
)
from malbut_agent_server.gazebo_monitor_room_command_runner import (
    GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS,
    GazeboMonitorRoomCommandRunner,
    GazeboMonitorRoomCommandRunnerError,
    GazeboMonitorRoomCommandStep,
    _monotonic as _RUNNER_MONOTONIC,
)
from malbut_agent_server.schemas import validate_user_id


GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION = 1
GAZEBO_COMMAND_PROGRESS_MAX_STEPS = 65536
GAZEBO_COMMAND_PROGRESS_ACTIVATION_SENTINEL = hashlib.sha256(
    b'malbut-gazebo-command-progress-activation-v1'
).hexdigest()

_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_INTENT_ID = re.compile(r'^gazebo-intent-[A-Za-z0-9_-]{32,96}$')
_ERROR_CODE = re.compile(r'^gazebo_command_progress_[a-z0-9_]{1,64}$')
_RESOLVED_TERMINAL_STATES = frozenset(
    {'succeeded', 'failed', 'canceled'}
)
_UNKNOWN_TERMINAL_STATES = frozenset(
    {'delivery_unknown', 'cancel_unknown'}
)
_SERVICE_SEAL_LOCK = threading.RLock()
_SERVICE_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)

_RUNNER_ATTEST = GazeboMonitorRoomCommandRunner._attest_configuration
_RUNNER_INVOKE = GazeboMonitorRoomCommandRunner._invoke_locked
_RUNNER_NEXT_CANCEL = GazeboMonitorRoomCommandRunner._next_cancel_command
_POLICY_ROBOT_ID = GazeboSimulationExecutionPolicy.robot_id.fget
_POLICY_EXPECTED_BOOT_ID = (
    GazeboSimulationExecutionPolicy.expected_host_boot_id.fget
)


class GazeboCommandProgressError(RuntimeError):
    """Content-free failure at the durable progress boundary."""

    def __init__(
        self,
        code: str = 'gazebo_command_progress_unavailable',
    ) -> None:
        normalized = (
            code
            if type(code) is str and _ERROR_CODE.fullmatch(code)
            else 'gazebo_command_progress_unavailable'
        )
        super().__init__('Gazebo command progress is unavailable')
        self.code = normalized

    def __getattribute__(self, name: str) -> Any:
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


def _error(code: str) -> GazeboCommandProgressError:
    return GazeboCommandProgressError(code)


def _identifier(value: Any) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _error('gazebo_command_progress_request_invalid')
    return value


def _intent_id(value: Any) -> str:
    if type(value) is not str or _INTENT_ID.fullmatch(value) is None:
        raise _error('gazebo_command_progress_intent_invalid')
    return value


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _error('gazebo_command_progress_record_invalid')
    return value


def _canonical_json(value: Any) -> str:
    invalid = False
    rendered = ''
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        invalid = True
    if invalid or not rendered or len(rendered) > 65536:
        raise _error('gazebo_command_progress_record_invalid')
    return rendered


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('ascii')).hexdigest()


def _principal_digest(user_id: str) -> str:
    return _hash(
        {
            'contract': 'gazebo-command-progress-principal-v1',
            'user_id': validate_user_id(user_id),
        }
    )


def _run_request_id(anchor_digest: str, flow: str, generation: int) -> str:
    return 'gazebo-run-' + _hash(
        {
            'contract': 'gazebo-command-progress-run-v1',
            'anchor_digest': _digest(anchor_digest),
            'flow': flow,
            'generation': generation,
        }
    )


def _new_intent_id_locked(connection: sqlite3.Connection) -> str:
    for _attempt in range(8):
        value = f'gazebo-intent-{secrets.token_urlsafe(32)}'
        if _INTENT_ID.fullmatch(value) is None:
            continue
        collision = connection.execute(
            '''
            SELECT 1 FROM monitor_room_gazebo_command_intents
            WHERE intent_id = ?
            UNION ALL
            SELECT 1 FROM monitor_room_gazebo_command_executions
            WHERE next_drive_intent_id = ? OR next_cancel_intent_id = ?
            LIMIT 1
            ''',
            (value, value, value),
        ).fetchone()
        if collision is None:
            return value
    raise _error('gazebo_command_progress_intent_unavailable')


@dataclass(frozen=True, repr=False)
class GazeboCommandProgressSnapshot:
    """Redacted progress plus the next server-issued request capabilities."""

    state: str
    drive_steps: int
    cancel_steps: int
    total_steps: int
    terminal: bool
    next_intent_id: Optional[str] = field(default=None, repr=False)
    cancel_intent_id: Optional[str] = field(default=None, repr=False)
    schema_version: int = GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION
            or self.state not in {
                'ready', 'driving', 'cancel_required', 'canceling',
                'succeeded', 'failed', 'canceled',
            }
            or any(
                type(value) is not int
                or not 0 <= value <= GAZEBO_COMMAND_PROGRESS_MAX_STEPS
                for value in (
                    self.drive_steps,
                    self.cancel_steps,
                    self.total_steps,
                )
            )
            or self.total_steps != self.drive_steps + self.cancel_steps
            or type(self.terminal) is not bool
            or (self.state in _RESOLVED_TERMINAL_STATES) != self.terminal
            or self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            raise _error('gazebo_command_progress_result_invalid')
        for value in (self.next_intent_id, self.cancel_intent_id):
            if value is not None:
                _intent_id(value)
        if self.terminal:
            if (
                self.next_intent_id is not None
                or self.cancel_intent_id is not None
            ):
                raise _error('gazebo_command_progress_result_invalid')
        if (
            self.state in {'ready', 'driving'}
            and (
                self.next_intent_id is None
                or self.cancel_intent_id is None
            )
        ):
            raise _error('gazebo_command_progress_result_invalid')
        if (
            self.state == 'cancel_required'
            and (
                self.next_intent_id is not None
                or self.cancel_intent_id is None
            )
        ):
            raise _error('gazebo_command_progress_result_invalid')
        if self.state == 'canceling' and self.next_intent_id is not None:
            raise _error('gazebo_command_progress_result_invalid')

    def to_public_dict(self) -> Dict[str, Any]:
        """Return no durable operation, owner, cursor, or evidence values."""
        return {
            'schema_version': self.schema_version,
            'state': self.state,
            'drive_steps': self.drive_steps,
            'cancel_steps': self.cancel_steps,
            'total_steps': self.total_steps,
            'terminal': self.terminal,
            'next_intent_id': self.next_intent_id,
            'cancel_intent_id': self.cancel_intent_id,
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

    @classmethod
    def _from_public_dict(
        cls,
        value: Any,
    ) -> 'GazeboCommandProgressSnapshot':
        if type(value) is not dict or set(value) != {
            'schema_version', 'state', 'drive_steps',
            'cancel_steps', 'total_steps', 'terminal',
            'next_intent_id', 'cancel_intent_id',
            'runtime_mode', 'simulation', 'physical_authorized',
            'physical_effects', 'viewer_live',
            'camera_coverage_validated', 'coverage_achieved',
        }:
            raise _error('gazebo_command_progress_result_invalid')
        result = cls(
            schema_version=value['schema_version'],
            state=value['state'],
            drive_steps=value['drive_steps'],
            cancel_steps=value['cancel_steps'],
            total_steps=value['total_steps'],
            terminal=value['terminal'],
            next_intent_id=value['next_intent_id'],
            cancel_intent_id=value['cancel_intent_id'],
        )
        if result.to_public_dict() != value:
            raise _error('gazebo_command_progress_result_invalid')
        return result

    def __repr__(self) -> str:
        return (
            'GazeboCommandProgressSnapshot('
            f'state={self.state!r}, '
            f'drive_steps={self.drive_steps!r}, '
            f'cancel_steps={self.cancel_steps!r}, '
            f'total_steps={self.total_steps!r}, '
            f'terminal={self.terminal!r}, '
            'next_intent_id=<redacted>, cancel_intent_id=<redacted>, '
            "runtime_mode='gazebo', simulation=True, "
            'physical_authorized=False, physical_effects=False, '
            'viewer_live=False, camera_coverage_validated=False, '
            'coverage_achieved=False)'
        )


@dataclass(frozen=True, repr=False)
class GazeboCommandTerminalAnchor:
    """Private immutable anchor for later same-transaction result delivery."""

    confirmation_request_id: str = field(repr=False)
    principal_digest: str = field(repr=False)
    terminal_step_id: str = field(repr=False)
    terminal_state: str
    terminal_code: str
    terminal_chain_digest: str = field(repr=False)
    terminal_evidence_digest: str = field(repr=False)
    terminal_gateway_fingerprint: str = field(repr=False)
    record_digest: str = field(repr=False)
    schema_version: int = GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _identifier(self.confirmation_request_id)
        _identifier(self.terminal_step_id)
        for value in (
            self.principal_digest,
            self.terminal_chain_digest,
            self.terminal_evidence_digest,
            self.terminal_gateway_fingerprint,
            self.record_digest,
        ):
            _digest(value)
        if (
            self.terminal_state not in _RESOLVED_TERMINAL_STATES
            or type(self.terminal_code) is not str
            or not self.terminal_code
            or self.schema_version != GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION
            or self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            raise _error('gazebo_command_progress_terminal_invalid')

    def __repr__(self) -> str:
        return (
            'GazeboCommandTerminalAnchor('
            f'terminal_state={self.terminal_state!r}, '
            f'terminal_code={self.terminal_code!r}, '
            "runtime_mode='gazebo', simulation=True, "
            'physical_authorized=False, physical_effects=False, '
            'viewer_live=False, camera_coverage_validated=False, '
            'coverage_achieved=False)'
        )


_METADATA_TABLE_SQL = '''
CREATE TABLE monitor_room_gazebo_command_progress_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    activation_epoch TEXT NOT NULL,
    preactivation_count INTEGER NOT NULL,
    preactivation_digest TEXT NOT NULL,
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
        AND typeof(preactivation_count) = 'integer'
        AND preactivation_count >= 0
        AND length(preactivation_digest) = 64
        AND preactivation_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''

_PREACTIVATION_TABLE_SQL = '''
CREATE TABLE monitor_room_gazebo_command_preactivation (
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    source_fingerprint TEXT NOT NULL UNIQUE,
    CHECK (
        length(source_fingerprint) = 64
        AND source_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
)
'''

_EXECUTIONS_TABLE_SQL = f'''
CREATE TABLE monitor_room_gazebo_command_executions (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    principal_digest TEXT NOT NULL,
    outbox_id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL UNIQUE,
    claim_fence INTEGER NOT NULL,
    owner_binding_digest TEXT NOT NULL,
    prepare_fingerprint TEXT NOT NULL,
    acknowledgement_fingerprint TEXT NOT NULL,
    host_boot_id TEXT NOT NULL,
    prepared_boottime_ns INTEGER NOT NULL,
    deadline_boottime_ns INTEGER NOT NULL,
    anchor_digest TEXT NOT NULL UNIQUE,
    drive_run_request_id TEXT NOT NULL UNIQUE,
    cancel_run_generation INTEGER NOT NULL,
    cancel_run_request_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    drive_steps INTEGER NOT NULL,
    cancel_steps INTEGER NOT NULL,
    total_steps INTEGER NOT NULL,
    drive_last_step_id TEXT,
    cancel_last_step_id TEXT,
    terminal_step_id TEXT,
    terminal_state TEXT,
    terminal_code TEXT,
    terminal_chain_digest TEXT,
    terminal_evidence_digest TEXT,
    terminal_gateway_fingerprint TEXT,
    next_drive_intent_id TEXT UNIQUE,
    next_cancel_intent_id TEXT UNIQUE,
    runtime_mode TEXT NOT NULL CHECK (runtime_mode = 'gazebo'),
    simulation INTEGER NOT NULL CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL CHECK (physical_effects = 0),
    viewer_live INTEGER NOT NULL CHECK (viewer_live = 0),
    camera_coverage_validated INTEGER NOT NULL
        CHECK (camera_coverage_validated = 0),
    coverage_achieved INTEGER NOT NULL CHECK (coverage_achieved = 0),
    record_digest TEXT NOT NULL,
    CHECK (
        length(principal_digest) = 64
        AND principal_digest NOT GLOB '*[^0-9a-f]*'
        AND outbox_id GLOB 'gazebo-execution-outbox-*'
        AND operation_id GLOB 'gazebo-operation-*'
        AND typeof(claim_fence) = 'integer'
        AND claim_fence BETWEEN 1 AND 8
        AND length(owner_binding_digest) = 64
        AND owner_binding_digest NOT GLOB '*[^0-9a-f]*'
        AND length(prepare_fingerprint) = 64
        AND prepare_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(acknowledgement_fingerprint) = 64
        AND acknowledgement_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND typeof(prepared_boottime_ns) = 'integer'
        AND prepared_boottime_ns >= 0
        AND typeof(deadline_boottime_ns) = 'integer'
        AND deadline_boottime_ns > prepared_boottime_ns
        AND length(anchor_digest) = 64
        AND anchor_digest NOT GLOB '*[^0-9a-f]*'
        AND drive_run_request_id GLOB 'gazebo-run-*'
        AND cancel_run_request_id GLOB 'gazebo-run-*'
        AND typeof(cancel_run_generation) = 'integer'
        AND cancel_run_generation BETWEEN 0 AND {GAZEBO_COMMAND_PROGRESS_MAX_STEPS}
        AND state IN (
            'ready', 'driving', 'cancel_required', 'canceling', 'terminal'
        )
        AND typeof(drive_steps) = 'integer'
        AND drive_steps BETWEEN 0 AND {GAZEBO_COMMAND_PROGRESS_MAX_STEPS}
        AND typeof(cancel_steps) = 'integer'
        AND cancel_steps BETWEEN 0 AND {GAZEBO_COMMAND_PROGRESS_MAX_STEPS}
        AND typeof(total_steps) = 'integer'
        AND total_steps = drive_steps + cancel_steps
        AND total_steps BETWEEN 0 AND {GAZEBO_COMMAND_PROGRESS_MAX_STEPS}
        AND length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        (state = 'terminal'
         AND terminal_step_id IS NOT NULL
         AND terminal_state IN ('succeeded', 'failed', 'canceled')
         AND terminal_code IS NOT NULL
         AND terminal_chain_digest IS NOT NULL
         AND terminal_evidence_digest IS NOT NULL
         AND terminal_gateway_fingerprint IS NOT NULL
         AND next_drive_intent_id IS NULL
         AND next_cancel_intent_id IS NULL)
        OR
        (state != 'terminal'
         AND terminal_step_id IS NULL
         AND terminal_state IS NULL
         AND terminal_code IS NULL
         AND terminal_chain_digest IS NULL
         AND terminal_evidence_digest IS NULL
         AND terminal_gateway_fingerprint IS NULL)
    ),
    CHECK (
        (state IN ('ready', 'driving')
         AND next_drive_intent_id IS NOT NULL
         AND next_cancel_intent_id IS NOT NULL)
        OR
        (state = 'cancel_required'
         AND next_drive_intent_id IS NULL
         AND next_cancel_intent_id IS NOT NULL)
        OR
        (state = 'canceling'
         AND next_drive_intent_id IS NULL)
        OR state = 'terminal'
    )
)
'''

_INTENTS_TABLE_SQL = '''
CREATE TABLE monitor_room_gazebo_command_intents (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    intent_id TEXT NOT NULL PRIMARY KEY,
    confirmation_request_id TEXT NOT NULL,
    action TEXT NOT NULL,
    intent_sequence INTEGER NOT NULL,
    run_request_id TEXT NOT NULL,
    prior_step_id TEXT,
    prior_chain_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    result_step_id TEXT,
    response_json TEXT,
    response_digest TEXT,
    record_digest TEXT NOT NULL,
    FOREIGN KEY (confirmation_request_id)
        REFERENCES monitor_room_gazebo_command_executions (
            confirmation_request_id
        ) ON DELETE RESTRICT,
    UNIQUE (confirmation_request_id, action, intent_sequence),
    CHECK (
        intent_id GLOB 'gazebo-intent-*'
        AND action IN ('advance', 'cancel')
        AND typeof(intent_sequence) = 'integer'
        AND intent_sequence BETWEEN 1 AND 65536
        AND run_request_id GLOB 'gazebo-run-*'
        AND length(prior_chain_digest) = 64
        AND prior_chain_digest NOT GLOB '*[^0-9a-f]*'
        AND status IN ('pending', 'completed', 'abandoned')
        AND length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        (status = 'pending'
         AND result_step_id IS NULL
         AND response_json IS NULL
         AND response_digest IS NULL)
        OR
        (status IN ('completed', 'abandoned')
         AND response_json IS NOT NULL
         AND response_digest IS NOT NULL)
    )
)
'''

_STEPS_TABLE_SQL = '''
CREATE TABLE monitor_room_gazebo_command_steps (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    step_id TEXT NOT NULL PRIMARY KEY,
    confirmation_request_id TEXT NOT NULL,
    intent_id TEXT NOT NULL UNIQUE,
    progress_sequence INTEGER NOT NULL,
    flow TEXT NOT NULL,
    run_request_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    gateway_response_fingerprint TEXT NOT NULL,
    chain_digest TEXT NOT NULL,
    step_json TEXT NOT NULL,
    record_digest TEXT NOT NULL,
    FOREIGN KEY (confirmation_request_id)
        REFERENCES monitor_room_gazebo_command_executions (
            confirmation_request_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (intent_id)
        REFERENCES monitor_room_gazebo_command_intents (intent_id)
        ON DELETE RESTRICT,
    UNIQUE (confirmation_request_id, progress_sequence),
    UNIQUE (confirmation_request_id, flow, run_request_id, step_index),
    CHECK (
        step_id GLOB 'gazebo-progress-step-*'
        AND typeof(progress_sequence) = 'integer'
        AND progress_sequence BETWEEN 1 AND 65536
        AND flow IN ('drive', 'cancel')
        AND run_request_id GLOB 'gazebo-run-*'
        AND typeof(step_index) = 'integer'
        AND step_index BETWEEN 0 AND 32767
        AND request_id GLOB 'gazebo-command-*'
        AND length(gateway_response_fingerprint) = 64
        AND gateway_response_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(chain_digest) = 64
        AND chain_digest NOT GLOB '*[^0-9a-f]*'
        AND length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''

_PREACTIVATION_NO_INSERT_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_preactivation_no_insert
BEFORE INSERT ON monitor_room_gazebo_command_preactivation
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command preactivation is immutable');
END
'''

_PREACTIVATION_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_preactivation_no_update
BEFORE UPDATE ON monitor_room_gazebo_command_preactivation
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command preactivation is immutable');
END
'''

_PREACTIVATION_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_preactivation_no_delete
BEFORE DELETE ON monitor_room_gazebo_command_preactivation
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command preactivation is immutable');
END
'''

_METADATA_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_metadata_no_update
BEFORE UPDATE ON monitor_room_gazebo_command_progress_metadata
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command metadata is immutable');
END
'''

_METADATA_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_metadata_no_delete
BEFORE DELETE ON monitor_room_gazebo_command_progress_metadata
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command metadata is immutable');
END
'''

_METADATA_NO_INSERT_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_metadata_no_insert
BEFORE INSERT ON monitor_room_gazebo_command_progress_metadata
WHEN EXISTS (
    SELECT 1 FROM monitor_room_gazebo_command_progress_metadata
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command metadata is immutable');
END
'''

_EXECUTION_IDENTITY_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_execution_identity_no_update
BEFORE UPDATE ON monitor_room_gazebo_command_executions
WHEN NEW.schema_version IS NOT OLD.schema_version
  OR NEW.confirmation_request_id IS NOT OLD.confirmation_request_id
  OR NEW.principal_digest IS NOT OLD.principal_digest
  OR NEW.outbox_id IS NOT OLD.outbox_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.claim_fence IS NOT OLD.claim_fence
  OR NEW.owner_binding_digest IS NOT OLD.owner_binding_digest
  OR NEW.prepare_fingerprint IS NOT OLD.prepare_fingerprint
  OR NEW.acknowledgement_fingerprint IS NOT OLD.acknowledgement_fingerprint
  OR NEW.host_boot_id IS NOT OLD.host_boot_id
  OR NEW.prepared_boottime_ns IS NOT OLD.prepared_boottime_ns
  OR NEW.deadline_boottime_ns IS NOT OLD.deadline_boottime_ns
  OR NEW.anchor_digest IS NOT OLD.anchor_digest
  OR NEW.drive_run_request_id IS NOT OLD.drive_run_request_id
  OR NEW.runtime_mode IS NOT OLD.runtime_mode
  OR NEW.simulation IS NOT OLD.simulation
  OR NEW.physical_authorized IS NOT OLD.physical_authorized
  OR NEW.physical_effects IS NOT OLD.physical_effects
  OR NEW.viewer_live IS NOT OLD.viewer_live
  OR NEW.camera_coverage_validated IS NOT OLD.camera_coverage_validated
  OR NEW.coverage_achieved IS NOT OLD.coverage_achieved
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command execution identity is immutable');
END
'''

_EXECUTION_TERMINAL_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_terminal_no_update
BEFORE UPDATE ON monitor_room_gazebo_command_executions
WHEN OLD.state = 'terminal'
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command terminal is immutable');
END
'''

_EXECUTION_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_execution_no_delete
BEFORE DELETE ON monitor_room_gazebo_command_executions
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command execution is append-only');
END
'''

_INTENT_INSERT_GUARD_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_intent_insert_guard
BEFORE INSERT ON monitor_room_gazebo_command_intents
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM monitor_room_gazebo_command_executions AS execution
        WHERE execution.confirmation_request_id = NEW.confirmation_request_id
          AND execution.state != 'terminal'
          AND (
              (NEW.action = 'advance'
               AND execution.next_drive_intent_id = NEW.intent_id
               AND execution.state IN ('ready', 'driving'))
              OR
              (NEW.action = 'cancel'
               AND execution.next_cancel_intent_id = NEW.intent_id)
          )
    ) THEN RAISE(ABORT, 'Gazebo command intent source is invalid') END;
END
'''

_INTENT_TRANSITION_GUARD_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_intent_transition_guard
BEFORE UPDATE ON monitor_room_gazebo_command_intents
WHEN NOT (
    OLD.status = 'pending'
    AND NEW.status IN ('completed', 'abandoned')
    AND NEW.schema_version IS OLD.schema_version
    AND NEW.intent_id IS OLD.intent_id
    AND NEW.confirmation_request_id IS OLD.confirmation_request_id
    AND NEW.action IS OLD.action
    AND NEW.intent_sequence IS OLD.intent_sequence
    AND NEW.run_request_id IS OLD.run_request_id
    AND NEW.prior_step_id IS OLD.prior_step_id
    AND NEW.prior_chain_digest IS OLD.prior_chain_digest
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command intent transition is invalid');
END
'''

_INTENT_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_intent_no_delete
BEFORE DELETE ON monitor_room_gazebo_command_intents
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command intents are append-only');
END
'''

_STEP_INSERT_GUARD_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_step_insert_guard
BEFORE INSERT ON monitor_room_gazebo_command_steps
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM monitor_room_gazebo_command_executions AS execution
        JOIN monitor_room_gazebo_command_intents AS intent
          ON intent.intent_id = NEW.intent_id
         AND intent.confirmation_request_id = execution.confirmation_request_id
        WHERE execution.confirmation_request_id = NEW.confirmation_request_id
          AND execution.state != 'terminal'
          AND intent.status = 'pending'
          AND NEW.progress_sequence = execution.total_steps + 1
          AND ((intent.action = 'advance' AND NEW.flow = 'drive')
               OR (intent.action = 'cancel' AND NEW.flow = 'cancel'))
    ) THEN RAISE(ABORT, 'Gazebo command step source is invalid') END;
END
'''

_STEP_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_step_no_update
BEFORE UPDATE ON monitor_room_gazebo_command_steps
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command steps are immutable');
END
'''

_STEP_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_command_step_no_delete
BEFORE DELETE ON monitor_room_gazebo_command_steps
BEGIN
    SELECT RAISE(ABORT, 'Gazebo command steps are append-only');
END
'''


def _expected_schema_objects() -> Dict[str, Tuple[str, str]]:
    return {
        'monitor_room_gazebo_command_progress_metadata': (
            'table', _METADATA_TABLE_SQL
        ),
        'monitor_room_gazebo_command_preactivation': (
            'table', _PREACTIVATION_TABLE_SQL
        ),
        'monitor_room_gazebo_command_executions': (
            'table', _EXECUTIONS_TABLE_SQL
        ),
        'monitor_room_gazebo_command_intents': (
            'table', _INTENTS_TABLE_SQL
        ),
        'monitor_room_gazebo_command_steps': ('table', _STEPS_TABLE_SQL),
        'monitor_room_gazebo_command_preactivation_no_insert': (
            'trigger', _PREACTIVATION_NO_INSERT_SQL
        ),
        'monitor_room_gazebo_command_preactivation_no_update': (
            'trigger', _PREACTIVATION_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_command_preactivation_no_delete': (
            'trigger', _PREACTIVATION_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_command_metadata_no_update': (
            'trigger', _METADATA_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_command_metadata_no_delete': (
            'trigger', _METADATA_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_command_metadata_no_insert': (
            'trigger', _METADATA_NO_INSERT_SQL
        ),
        'monitor_room_gazebo_command_execution_identity_no_update': (
            'trigger', _EXECUTION_IDENTITY_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_command_terminal_no_update': (
            'trigger', _EXECUTION_TERMINAL_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_command_execution_no_delete': (
            'trigger', _EXECUTION_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_command_intent_insert_guard': (
            'trigger', _INTENT_INSERT_GUARD_SQL
        ),
        'monitor_room_gazebo_command_intent_transition_guard': (
            'trigger', _INTENT_TRANSITION_GUARD_SQL
        ),
        'monitor_room_gazebo_command_intent_no_delete': (
            'trigger', _INTENT_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_command_step_insert_guard': (
            'trigger', _STEP_INSERT_GUARD_SQL
        ),
        'monitor_room_gazebo_command_step_no_update': (
            'trigger', _STEP_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_command_step_no_delete': (
            'trigger', _STEP_NO_DELETE_SQL
        ),
    }


def _activation_anchor_value(preactivation_digest: str) -> int:
    return int(_digest(preactivation_digest)[:15], 16) + 1


def _source_fingerprint(row: sqlite3.Row) -> str:
    return _hash(
        {
            'contract': 'gazebo-command-progress-preactivation-v1',
            'confirmation_request_id': str(
                row['confirmation_request_id']
            ),
            'outbox_fingerprint': str(row['outbox_fingerprint']),
            'operation_id': str(row['operation_id']),
            'state': str(row['state']),
            'acknowledgement_fingerprint': (
                None
                if row['acknowledgement_fingerprint'] is None
                else str(row['acknowledgement_fingerprint'])
            ),
        }
    )


def _install_activation_anchor_locked(
    connection: sqlite3.Connection,
    preactivation_digest: str,
) -> None:
    simulation = connection.execute(
        '''
        SELECT activation_epoch, activated_at
        FROM monitor_room_simulation_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    if simulation is None:
        raise _error('gazebo_command_progress_schema_invalid')
    connection.execute(
        'DROP TRIGGER monitor_room_simulation_preactivation_no_insert'
    )
    try:
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_preactivation_proposals (
                proposal_fingerprint, activation_epoch,
                snapshot_rowid, snapshotted_at
            ) VALUES (?, ?, ?, ?)
            ''',
            (
                GAZEBO_COMMAND_PROGRESS_ACTIVATION_SENTINEL,
                simulation['activation_epoch'],
                _activation_anchor_value(preactivation_digest),
                simulation['activated_at'],
            ),
        )
    finally:
        connection.execute(
            SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL
        )


def prepare_gazebo_command_progress_schema_locked(
    connection: sqlite3.Connection,
) -> None:
    """Activate once and permanently deny every existing outbox source."""
    if not connection.in_transaction:
        raise _error('gazebo_command_progress_schema_invalid')
    validate_gazebo_execution_outbox_schema_locked(connection)
    expected = _expected_schema_objects()
    placeholders = ','.join('?' for _name in expected)
    objects = connection.execute(
        f'''
        SELECT name, type, sql FROM sqlite_master
        WHERE name IN ({placeholders})
        ''',
        tuple(expected),
    ).fetchall()
    sentinel = connection.execute(
        '''
        SELECT snapshot_rowid
        FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (GAZEBO_COMMAND_PROGRESS_ACTIVATION_SENTINEL,),
    ).fetchone()
    if objects:
        if {str(row['name']) for row in objects} != set(expected):
            raise _error('gazebo_command_progress_schema_invalid')
        if sentinel is None:
            raise _error('gazebo_command_progress_schema_invalid')
        validate_gazebo_command_progress_schema_locked(connection)
        return
    if sentinel is not None:
        raise _error('gazebo_command_progress_schema_removed')
    for sql in (
        _METADATA_TABLE_SQL,
        _PREACTIVATION_TABLE_SQL,
        _EXECUTIONS_TABLE_SQL,
        _INTENTS_TABLE_SQL,
        _STEPS_TABLE_SQL,
    ):
        connection.execute(sql)
    existing = connection.execute(
        '''
        SELECT event.confirmation_request_id,
               event.outbox_fingerprint,
               event.operation_id,
               event.state,
               ack.acknowledgement_fingerprint
        FROM monitor_room_gazebo_execution_outbox AS event
        LEFT JOIN monitor_room_gazebo_execution_acknowledgements AS ack
          ON ack.outbox_id = event.outbox_id
        ORDER BY event.confirmation_request_id
        '''
    ).fetchall()
    sources = [
        (
            str(row['confirmation_request_id']),
            _source_fingerprint(row),
        )
        for row in existing
    ]
    for confirmation_request_id, fingerprint in sources:
        connection.execute(
            '''
            INSERT INTO monitor_room_gazebo_command_preactivation (
                confirmation_request_id, source_fingerprint
            ) VALUES (?, ?)
            ''',
            (confirmation_request_id, fingerprint),
        )
    activation_epoch = secrets.token_hex(32)
    preactivation_digest = _hash(
        {
            'contract': 'gazebo-command-progress-activation-v1',
            'schema_version': GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION,
            'activation_epoch': activation_epoch,
            'sources': [list(source) for source in sources],
        }
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_gazebo_command_progress_metadata (
            singleton, schema_version, activation_epoch,
            preactivation_count, preactivation_digest
        ) VALUES (1, 1, ?, ?, ?)
        ''',
        (activation_epoch, len(sources), preactivation_digest),
    )
    for sql in (
        _PREACTIVATION_NO_INSERT_SQL,
        _PREACTIVATION_NO_UPDATE_SQL,
        _PREACTIVATION_NO_DELETE_SQL,
        _METADATA_NO_UPDATE_SQL,
        _METADATA_NO_DELETE_SQL,
        _METADATA_NO_INSERT_SQL,
        _EXECUTION_IDENTITY_NO_UPDATE_SQL,
        _EXECUTION_TERMINAL_NO_UPDATE_SQL,
        _EXECUTION_NO_DELETE_SQL,
        _INTENT_INSERT_GUARD_SQL,
        _INTENT_TRANSITION_GUARD_SQL,
        _INTENT_NO_DELETE_SQL,
        _STEP_INSERT_GUARD_SQL,
        _STEP_NO_UPDATE_SQL,
        _STEP_NO_DELETE_SQL,
    ):
        connection.execute(sql)
    _install_activation_anchor_locked(connection, preactivation_digest)
    validate_gazebo_command_progress_schema_locked(connection)


def validate_gazebo_command_progress_schema_locked(
    connection: sqlite3.Connection,
) -> None:
    """Validate exact owned DDL and the external activation sentinel."""
    expected = _expected_schema_objects()
    rows = connection.execute(
        '''
        SELECT name, type, sql FROM sqlite_master
        WHERE name GLOB 'monitor_room_gazebo_command_*'
        ORDER BY name
        '''
    ).fetchall()
    actual = {str(row['name']): row for row in rows}
    if set(actual) != set(expected):
        raise _error('gazebo_command_progress_schema_invalid')
    for name, (kind, sql) in expected.items():
        row = actual[name]
        if row['type'] != kind or str(row['sql']).strip() != sql.strip():
            raise _error('gazebo_command_progress_schema_invalid')
    metadata_rows = connection.execute(
        '''
        SELECT *, typeof(singleton), typeof(schema_version),
               typeof(activation_epoch), typeof(preactivation_count),
               typeof(preactivation_digest)
        FROM monitor_room_gazebo_command_progress_metadata
        '''
    ).fetchall()
    if len(metadata_rows) != 1:
        raise _error('gazebo_command_progress_schema_invalid')
    metadata = metadata_rows[0]
    if (
        tuple(metadata)[-5:]
        != ('integer', 'integer', 'text', 'integer', 'text')
        or metadata['singleton'] != 1
        or metadata['schema_version'] != 1
        or type(metadata['activation_epoch']) is not str
        or _DIGEST.fullmatch(metadata['activation_epoch']) is None
        or type(metadata['preactivation_count']) is not int
        or metadata['preactivation_count'] < 0
        or type(metadata['preactivation_digest']) is not str
        or _DIGEST.fullmatch(metadata['preactivation_digest']) is None
    ):
        raise _error('gazebo_command_progress_schema_invalid')
    snapshot = connection.execute(
        '''
        SELECT confirmation_request_id, source_fingerprint
        FROM monitor_room_gazebo_command_preactivation
        ORDER BY confirmation_request_id
        '''
    ).fetchall()
    if len(snapshot) != metadata['preactivation_count']:
        raise _error('gazebo_command_progress_schema_invalid')
    expected_digest = _hash(
        {
            'contract': 'gazebo-command-progress-activation-v1',
            'schema_version': GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION,
            'activation_epoch': metadata['activation_epoch'],
            'sources': [
                [row['confirmation_request_id'], row['source_fingerprint']]
                for row in snapshot
            ],
        }
    )
    simulation = connection.execute(
        '''
        SELECT activation_epoch, activated_at
        FROM monitor_room_simulation_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    sentinel = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (GAZEBO_COMMAND_PROGRESS_ACTIVATION_SENTINEL,),
    ).fetchone()
    if (
        expected_digest != metadata['preactivation_digest']
        or simulation is None
        or sentinel is None
        or sentinel['activation_epoch'] != simulation['activation_epoch']
        or sentinel['snapshotted_at'] != simulation['activated_at']
        or sentinel['snapshot_rowid']
        != _activation_anchor_value(expected_digest)
    ):
        raise _error('gazebo_command_progress_schema_invalid')


_EXECUTION_RECORD_FIELDS = (
    'schema_version', 'confirmation_request_id', 'principal_digest',
    'outbox_id', 'operation_id', 'claim_fence', 'owner_binding_digest',
    'prepare_fingerprint', 'acknowledgement_fingerprint', 'host_boot_id',
    'prepared_boottime_ns', 'deadline_boottime_ns', 'anchor_digest',
    'drive_run_request_id', 'cancel_run_generation',
    'cancel_run_request_id', 'state', 'drive_steps', 'cancel_steps',
    'total_steps', 'drive_last_step_id', 'cancel_last_step_id',
    'terminal_step_id', 'terminal_state', 'terminal_code',
    'terminal_chain_digest', 'terminal_evidence_digest',
    'terminal_gateway_fingerprint', 'next_drive_intent_id',
    'next_cancel_intent_id', 'runtime_mode', 'simulation',
    'physical_authorized', 'physical_effects', 'viewer_live',
    'camera_coverage_validated', 'coverage_achieved',
)


def _execution_record_digest(values: Dict[str, Any]) -> str:
    return _hash(
        {
            'contract': 'gazebo-command-progress-execution-v1',
            **{name: values[name] for name in _EXECUTION_RECORD_FIELDS},
        }
    )


def _execution_values(row: sqlite3.Row) -> Dict[str, Any]:
    return {name: row[name] for name in _EXECUTION_RECORD_FIELDS}


def _anchor_digest_from_values(values: Dict[str, Any]) -> str:
    return _hash(
        {
            'contract': 'gazebo-command-progress-anchor-v1',
            'schema_version': values['schema_version'],
            'confirmation_request_id': values['confirmation_request_id'],
            'principal_digest': values['principal_digest'],
            'outbox_id': values['outbox_id'],
            'operation_id': values['operation_id'],
            'claim_fence': values['claim_fence'],
            'owner_binding_digest': values['owner_binding_digest'],
            'prepare_fingerprint': values['prepare_fingerprint'],
            'acknowledgement_fingerprint': (
                values['acknowledgement_fingerprint']
            ),
            'host_boot_id': values['host_boot_id'],
            'prepared_boottime_ns': values['prepared_boottime_ns'],
            'deadline_boottime_ns': values['deadline_boottime_ns'],
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }
    )


_STEP_FIELDS = (
    'outbox_id', 'operation_id', 'run_request_id',
    'authorization_digest', 'flow', 'request_id', 'command',
    'step_index', 'previous_request_id',
    'previous_response_fingerprint', 'state', 'current_sample_index',
    'navigation_samples_total', 'navigation_samples_reached', 'terminal',
    'robot_blocked', 'terminal_code', 'evidence_digest',
    'gateway_response_fingerprint', 'previous_chain_digest',
    'schema_version',
)


def _step_payload(step: Any) -> Dict[str, Any]:
    if type(step) is not GazeboMonitorRoomCommandStep:
        raise _error('gazebo_command_progress_step_invalid')
    try:
        GazeboMonitorRoomCommandStep._attest(step)
        values = {name: getattr(step, name) for name in _STEP_FIELDS}
        chain_digest = GazeboMonitorRoomCommandStep.chain_digest.fget(step)
        GazeboMonitorRoomCommandStep._attest(step)
    except Exception:
        raise _error('gazebo_command_progress_step_invalid') from None
    return {
        **values,
        'chain_digest': chain_digest,
        'runtime_mode': step.runtime_mode,
        'simulation': step.simulation,
        'physical_authorized': step.physical_authorized,
        'physical_effects': step.physical_effects,
        'viewer_live': step.viewer_live,
        'camera_coverage_validated': step.camera_coverage_validated,
        'coverage_achieved': step.coverage_achieved,
    }


def _step_from_json(value: Any) -> GazeboMonitorRoomCommandStep:
    if type(value) is not str or not value or len(value) > 65536:
        raise _error('gazebo_command_progress_step_invalid')
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise _error('gazebo_command_progress_step_invalid') from None
    expected = set(_STEP_FIELDS) | {
        'chain_digest', 'runtime_mode', 'simulation',
        'physical_authorized', 'physical_effects', 'viewer_live',
        'camera_coverage_validated', 'coverage_achieved',
    }
    if type(payload) is not dict or set(payload) != expected:
        raise _error('gazebo_command_progress_step_invalid')
    if _canonical_json(payload) != value:
        raise _error('gazebo_command_progress_step_invalid')
    try:
        step = GazeboMonitorRoomCommandStep(
            **{name: payload[name] for name in _STEP_FIELDS}
        )
        GazeboMonitorRoomCommandStep._attest(step)
        if (
            _step_payload(step) != payload
            or step.chain_digest != payload['chain_digest']
        ):
            raise _error('gazebo_command_progress_step_invalid')
    except GazeboCommandProgressError:
        raise
    except Exception:
        raise _error('gazebo_command_progress_step_invalid') from None
    return step


def _step_record_digest(
    *,
    step_id: str,
    confirmation_request_id: str,
    intent_id: str,
    progress_sequence: int,
    step: GazeboMonitorRoomCommandStep,
    step_json: str,
) -> str:
    return _hash(
        {
            'contract': 'gazebo-command-progress-step-v1',
            'schema_version': GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION,
            'step_id': step_id,
            'confirmation_request_id': confirmation_request_id,
            'intent_id': intent_id,
            'progress_sequence': progress_sequence,
            'flow': step.flow,
            'run_request_id': step.run_request_id,
            'step_index': step.step_index,
            'request_id': step.request_id,
            'gateway_response_fingerprint': (
                step.gateway_response_fingerprint
            ),
            'chain_digest': step.chain_digest,
            'step_json': step_json,
        }
    )


def _intent_record_digest(values: Dict[str, Any]) -> str:
    return _hash(
        {
            'contract': 'gazebo-command-progress-intent-v1',
            'schema_version': values['schema_version'],
            'intent_id': values['intent_id'],
            'confirmation_request_id': values['confirmation_request_id'],
            'action': values['action'],
            'intent_sequence': values['intent_sequence'],
            'run_request_id': values['run_request_id'],
            'prior_step_id': values['prior_step_id'],
            'prior_chain_digest': values['prior_chain_digest'],
            'status': values['status'],
            'result_step_id': values['result_step_id'],
            'response_json': values['response_json'],
            'response_digest': values['response_digest'],
        }
    )


def _validate_intent_row(row: sqlite3.Row) -> None:
    values = {
        name: row[name]
        for name in (
            'schema_version', 'intent_id', 'confirmation_request_id',
            'action', 'intent_sequence', 'run_request_id',
            'prior_step_id', 'prior_chain_digest', 'status',
            'result_step_id', 'response_json', 'response_digest',
        )
    }
    if (
        row['record_digest'] != _intent_record_digest(values)
        or values['schema_version'] != 1
        or values['action'] not in {'advance', 'cancel'}
        or values['status'] not in {'pending', 'completed', 'abandoned'}
    ):
        raise _error('gazebo_command_progress_record_invalid')
    _intent_id(values['intent_id'])
    _identifier(values['confirmation_request_id'])
    _identifier(values['run_request_id'])
    _digest(values['prior_chain_digest'])
    if values['status'] == 'pending':
        if any(
            values[name] is not None
            for name in ('result_step_id', 'response_json', 'response_digest')
        ):
            raise _error('gazebo_command_progress_record_invalid')
    else:
        if (
            type(values['response_json']) is not str
            or values['response_digest']
            != hashlib.sha256(
                values['response_json'].encode('ascii')
            ).hexdigest()
        ):
            raise _error('gazebo_command_progress_record_invalid')
        _replay_snapshot(row)


def _replay_snapshot(row: sqlite3.Row) -> GazeboCommandProgressSnapshot:
    try:
        payload = json.loads(row['response_json'])
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise _error('gazebo_command_progress_record_invalid') from None
    if _canonical_json(payload) != row['response_json']:
        raise _error('gazebo_command_progress_record_invalid')
    return GazeboCommandProgressSnapshot._from_public_dict(payload)


def _validate_step_row(row: sqlite3.Row) -> GazeboMonitorRoomCommandStep:
    step = _step_from_json(row['step_json'])
    expected_digest = _step_record_digest(
        step_id=row['step_id'],
        confirmation_request_id=row['confirmation_request_id'],
        intent_id=row['intent_id'],
        progress_sequence=row['progress_sequence'],
        step=step,
        step_json=row['step_json'],
    )
    if (
        row['schema_version'] != 1
        or row['record_digest'] != expected_digest
        or row['flow'] != step.flow
        or row['run_request_id'] != step.run_request_id
        or row['step_index'] != step.step_index
        or row['request_id'] != step.request_id
        or row['gateway_response_fingerprint']
        != step.gateway_response_fingerprint
        or row['chain_digest'] != step.chain_digest
    ):
        raise _error('gazebo_command_progress_step_invalid')
    return step


def _load_execution_locked(
    connection: sqlite3.Connection,
    confirmation_request_id: str,
) -> Optional[sqlite3.Row]:
    row = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_command_executions
        WHERE confirmation_request_id = ?
        ''',
        (confirmation_request_id,),
    ).fetchone()
    if row is None:
        return None
    values = _execution_values(row)
    if (
        row['record_digest'] != _execution_record_digest(values)
        or row['anchor_digest'] != _anchor_digest_from_values(values)
        or row['schema_version'] != 1
        or row['runtime_mode'] != 'gazebo'
        or row['simulation'] != 1
        or row['physical_authorized'] != 0
        or row['physical_effects'] != 0
        or row['viewer_live'] != 0
        or row['camera_coverage_validated'] != 0
        or row['coverage_achieved'] != 0
    ):
        raise _error('gazebo_command_progress_record_invalid')
    for name in (
        'principal_digest', 'owner_binding_digest', 'prepare_fingerprint',
        'acknowledgement_fingerprint', 'anchor_digest',
    ):
        _digest(row[name])
    steps = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_command_steps
        WHERE confirmation_request_id = ?
        ORDER BY progress_sequence
        ''',
        (confirmation_request_id,),
    ).fetchall()
    if (
        len(steps) != row['total_steps']
        or row['total_steps']
        != row['drive_steps'] + row['cancel_steps']
    ):
        raise _error('gazebo_command_progress_record_invalid')
    previous_by_run: Dict[Tuple[str, str], GazeboMonitorRoomCommandStep] = {}
    drive_count = 0
    cancel_count = 0
    drive_last = None
    cancel_last = None
    validated_steps: Dict[str, GazeboMonitorRoomCommandStep] = {}
    for sequence, step_row in enumerate(steps, start=1):
        if step_row['progress_sequence'] != sequence:
            raise _error('gazebo_command_progress_step_invalid')
        step = _validate_step_row(step_row)
        key = (step.flow, step.run_request_id)
        previous = previous_by_run.get(key)
        if previous is None:
            if step.step_index != 0:
                raise _error('gazebo_command_progress_step_invalid')
        elif (
            step.step_index != previous.step_index + 1
            or step.previous_request_id != previous.request_id
            or step.previous_response_fingerprint
            != previous.gateway_response_fingerprint
            or step.previous_chain_digest != previous.chain_digest
        ):
            raise _error('gazebo_command_progress_step_invalid')
        previous_by_run[key] = step
        validated_steps[step_row['step_id']] = step
        if step.flow == 'drive':
            drive_count += 1
            drive_last = step_row['step_id']
        else:
            cancel_count += 1
            cancel_last = step_row['step_id']
    if (
        drive_count != row['drive_steps']
        or cancel_count != row['cancel_steps']
        or drive_last != row['drive_last_step_id']
        or cancel_last != row['cancel_last_step_id']
    ):
        raise _error('gazebo_command_progress_record_invalid')
    if row['state'] == 'terminal':
        terminal = validated_steps.get(row['terminal_step_id'])
        if (
            terminal is None
            or terminal.state != row['terminal_state']
            or terminal.terminal_code != row['terminal_code']
            or terminal.chain_digest != row['terminal_chain_digest']
            or terminal.evidence_digest != row['terminal_evidence_digest']
            or terminal.gateway_response_fingerprint
            != row['terminal_gateway_fingerprint']
        ):
            raise _error('gazebo_command_progress_terminal_invalid')
    return row


def _load_step_locked(
    connection: sqlite3.Connection,
    step_id: Optional[str],
) -> Optional[GazeboMonitorRoomCommandStep]:
    if step_id is None:
        return None
    row = connection.execute(
        '''SELECT * FROM monitor_room_gazebo_command_steps WHERE step_id = ?''',
        (step_id,),
    ).fetchone()
    if row is None:
        raise _error('gazebo_command_progress_step_invalid')
    return _validate_step_row(row)


def _authority_from_execution(
    row: sqlite3.Row,
    execution_scope: str,
) -> GazeboPreparedExecutionAuthority:
    try:
        authority = GazeboPreparedExecutionAuthority(
            confirmation_request_id=row['confirmation_request_id'],
            outbox_id=row['outbox_id'],
            operation_id=row['operation_id'],
            claim_fence=row['claim_fence'],
            owner_binding_digest=row['owner_binding_digest'],
            prepare_fingerprint=row['prepare_fingerprint'],
            acknowledgement_fingerprint=(
                row['acknowledgement_fingerprint']
            ),
            host_boot_id=row['host_boot_id'],
            prepared_boottime_ns=row['prepared_boottime_ns'],
            deadline_boottime_ns=row['deadline_boottime_ns'],
            execution_scope=execution_scope,
        )
        GazeboPreparedExecutionAuthority.binding_digest.fget(authority)
        return authority
    except Exception:
        raise _error('gazebo_command_progress_authority_invalid') from None


def _authority_matches_execution(
    authority: GazeboPreparedExecutionAuthority,
    row: sqlite3.Row,
    execution_scope: str,
) -> bool:
    try:
        expected = _authority_from_execution(row, execution_scope)
        return (
            type(authority) is GazeboPreparedExecutionAuthority
            and authority.execution_scope == execution_scope
            and GazeboPreparedExecutionAuthority.binding_digest.fget(
                authority
            )
            == GazeboPreparedExecutionAuthority.binding_digest.fget(
                expected
            )
        )
    except Exception:
        return False


def _validate_anchored_authority_locked(
    connection: sqlite3.Connection,
    *,
    policy: GazeboSimulationExecutionPolicy,
    execution: sqlite3.Row,
    execution_scope: str = 'cancel',
) -> GazeboPreparedExecutionAuthority:
    """Revalidate deletion-safe ACK authority for cancellation only."""
    try:
        validate_gazebo_execution_outbox_schema_locked(connection)
        current_boot_id = _POLICY_CURRENT_HOST_BOOT_ID(policy)
        current_boottime_ns = _POLICY_CURRENT_BOOTTIME_NS(policy)
        robot_id = _POLICY_ROBOT_ID(policy)
        expected_boot_id = _POLICY_EXPECTED_BOOT_ID(policy)
        outbox = connection.execute(
            '''
            SELECT * FROM monitor_room_gazebo_execution_outbox
            WHERE outbox_id = ?
            ''',
            (execution['outbox_id'],),
        ).fetchone()
        _validate_outbox_row_locked(connection, outbox)
        source = connection.execute(
            '''
            SELECT owner_binding_digest
            FROM monitor_room_simulation_ledger
            WHERE confirmation_request_id = ?
            ''',
            (execution['confirmation_request_id'],),
        ).fetchone()
        acknowledgement = connection.execute(
            '''
            SELECT *
            FROM monitor_room_gazebo_execution_acknowledgements
            WHERE outbox_id = ?
            ''',
            (execution['outbox_id'],),
        ).fetchone()
        invalid = (
            outbox is None
            or source is None
            or acknowledgement is None
            or outbox['state'] != 'prepared'
            or outbox['confirmation_request_id']
            != execution['confirmation_request_id']
            or outbox['outbox_id'] != execution['outbox_id']
            or outbox['operation_id'] != execution['operation_id']
            or outbox['claim_fence'] != execution['claim_fence']
            or outbox['robot_id'] != robot_id
            or outbox['host_boot_id'] != expected_boot_id
            or outbox['host_boot_id'] != current_boot_id
            or outbox['prepared_boottime_ns']
            != execution['prepared_boottime_ns']
            or outbox['deadline_boottime_ns']
            != execution['deadline_boottime_ns']
            or outbox['prepare_fingerprint']
            != execution['prepare_fingerprint']
            or current_boottime_ns < outbox['prepared_boottime_ns']
            or source['owner_binding_digest']
            != execution['owner_binding_digest']
            or acknowledgement['outbox_id'] != execution['outbox_id']
            or acknowledgement['operation_id']
            != execution['operation_id']
            or acknowledgement['claim_fence']
            != execution['claim_fence']
            or acknowledgement['prepare_fingerprint']
            != execution['prepare_fingerprint']
            or acknowledgement['acknowledgement_fingerprint']
            != execution['acknowledgement_fingerprint']
            or acknowledgement['prepared_boottime_ns']
            != execution['prepared_boottime_ns']
        )
    except Exception:
        invalid = True
    if invalid:
        raise _error('gazebo_command_progress_authority_invalid')
    if execution_scope not in {'drive', 'cancel'}:
        raise _error('gazebo_command_progress_authority_invalid')
    return _authority_from_execution(execution, execution_scope)


def _live_drive_authority_locked(
    connection: sqlite3.Connection,
    *,
    policy: GazeboSimulationExecutionPolicy,
    confirmation_request_id: str,
    user_id: str,
) -> GazeboPreparedExecutionAuthority:
    """Require the exact still-active conversation generation for drive."""
    try:
        authority = resolve_prepared_gazebo_execution_locked(
            connection,
            policy=policy,
            confirmation_request_id=confirmation_request_id,
            expected_user_id=user_id,
            execution_scope='drive',
        )
        owner = connection.execute(
            '''
            SELECT confirmation.user_id,
                   confirmation.conversation_id,
                   confirmation.session_instance_id,
                   confirmation.generation,
                   confirmation.state AS confirmation_state,
                   confirmation.disposition,
                   session.session_instance_id AS current_instance_id,
                   session.generation AS current_generation,
                   session.status AS current_status
            FROM confirmation_intents AS confirmation
            JOIN conversation_sessions AS session
              ON session.user_id = confirmation.user_id
             AND session.conversation_id = confirmation.conversation_id
            WHERE confirmation.confirmation_request_id = ?
            ''',
            (confirmation_request_id,),
        ).fetchone()
        invalid = (
            owner is None
            or owner['user_id'] != user_id
            or owner['confirmation_state'] != 'resolved'
            or owner['disposition'] != 'approve'
            or owner['current_status'] != 'active'
            or owner['session_instance_id']
            != owner['current_instance_id']
            or owner['generation'] != owner['current_generation']
        )
    except Exception:
        invalid = True
    if invalid:
        raise _error('gazebo_command_progress_conversation_inactive')
    return authority


def _snapshot_from_execution(
    row: sqlite3.Row,
    *,
    action: str,
) -> GazeboCommandProgressSnapshot:
    if action not in {'status', 'advance', 'cancel'}:
        raise _error('gazebo_command_progress_result_invalid')
    return GazeboCommandProgressSnapshot(
        state=(
            row['terminal_state']
            if row['state'] == 'terminal'
            else row['state']
        ),
        drive_steps=row['drive_steps'],
        cancel_steps=row['cancel_steps'],
        total_steps=row['total_steps'],
        terminal=row['state'] == 'terminal',
        next_intent_id=row['next_drive_intent_id'],
        cancel_intent_id=row['next_cancel_intent_id'],
    )


def _update_execution_locked(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    **updates: Any,
) -> sqlite3.Row:
    mutable = {
        'cancel_run_generation', 'cancel_run_request_id', 'state',
        'drive_steps', 'cancel_steps', 'total_steps',
        'drive_last_step_id', 'cancel_last_step_id', 'terminal_step_id',
        'terminal_state', 'terminal_code', 'terminal_chain_digest',
        'terminal_evidence_digest', 'terminal_gateway_fingerprint',
        'next_drive_intent_id', 'next_cancel_intent_id',
    }
    if not updates or not set(updates) <= mutable:
        raise _error('gazebo_command_progress_transition_invalid')
    if row['state'] == 'terminal':
        raise _error('gazebo_command_progress_terminal_immutable')
    values = _execution_values(row)
    values.update(updates)
    digest = _execution_record_digest(values)
    assignments = ', '.join(f'{name} = ?' for name in updates)
    cursor = connection.execute(
        f'''
        UPDATE monitor_room_gazebo_command_executions
        SET {assignments}, record_digest = ?
        WHERE confirmation_request_id = ? AND record_digest = ?
        ''',
        (
            *updates.values(),
            digest,
            row['confirmation_request_id'],
            row['record_digest'],
        ),
    )
    if cursor.rowcount != 1:
        raise _error('gazebo_command_progress_conflict')
    updated = _load_execution_locked(
        connection, row['confirmation_request_id']
    )
    if updated is None:
        raise _error('gazebo_command_progress_record_invalid')
    return updated


def _insert_execution_locked(
    connection: sqlite3.Connection,
    *,
    authority: GazeboPreparedExecutionAuthority,
    principal_digest: str,
) -> sqlite3.Row:
    confirmation_request_id = authority.confirmation_request_id
    if connection.execute(
        '''
        SELECT 1 FROM monitor_room_gazebo_command_preactivation
        WHERE confirmation_request_id = ?
        ''',
        (confirmation_request_id,),
    ).fetchone() is not None:
        raise _error('gazebo_command_progress_preactivation_denied')
    values: Dict[str, Any] = {
        'schema_version': 1,
        'confirmation_request_id': confirmation_request_id,
        'principal_digest': _digest(principal_digest),
        'outbox_id': authority.outbox_id,
        'operation_id': authority.operation_id,
        'claim_fence': authority.claim_fence,
        'owner_binding_digest': authority.owner_binding_digest,
        'prepare_fingerprint': authority.prepare_fingerprint,
        'acknowledgement_fingerprint': (
            authority.acknowledgement_fingerprint
        ),
        'host_boot_id': authority.host_boot_id,
        'prepared_boottime_ns': authority.prepared_boottime_ns,
        'deadline_boottime_ns': authority.deadline_boottime_ns,
        'anchor_digest': '',
        'drive_run_request_id': '',
        'cancel_run_generation': 0,
        'cancel_run_request_id': '',
        'state': 'ready',
        'drive_steps': 0,
        'cancel_steps': 0,
        'total_steps': 0,
        'drive_last_step_id': None,
        'cancel_last_step_id': None,
        'terminal_step_id': None,
        'terminal_state': None,
        'terminal_code': None,
        'terminal_chain_digest': None,
        'terminal_evidence_digest': None,
        'terminal_gateway_fingerprint': None,
        'next_drive_intent_id': None,
        'next_cancel_intent_id': None,
        'runtime_mode': 'gazebo',
        'simulation': 1,
        'physical_authorized': 0,
        'physical_effects': 0,
        'viewer_live': 0,
        'camera_coverage_validated': 0,
        'coverage_achieved': 0,
    }
    values['anchor_digest'] = _anchor_digest_from_values(values)
    values['drive_run_request_id'] = _run_request_id(
        values['anchor_digest'], 'drive', 0
    )
    values['cancel_run_request_id'] = _run_request_id(
        values['anchor_digest'], 'cancel', 0
    )
    values['next_drive_intent_id'] = _new_intent_id_locked(connection)
    values['next_cancel_intent_id'] = _new_intent_id_locked(connection)
    record_digest = _execution_record_digest(values)
    columns = (*_EXECUTION_RECORD_FIELDS, 'record_digest')
    connection.execute(
        f'''
        INSERT INTO monitor_room_gazebo_command_executions (
            {', '.join(columns)}
        ) VALUES ({', '.join('?' for _name in columns)})
        ''',
        tuple(values[name] for name in _EXECUTION_RECORD_FIELDS)
        + (record_digest,),
    )
    row = _load_execution_locked(connection, confirmation_request_id)
    if row is None:
        raise _error('gazebo_command_progress_record_invalid')
    return row


def _intent_values(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        name: row[name]
        for name in (
            'schema_version', 'intent_id', 'confirmation_request_id',
            'action', 'intent_sequence', 'run_request_id',
            'prior_step_id', 'prior_chain_digest', 'status',
            'result_step_id', 'response_json', 'response_digest',
        )
    }


def _complete_intent_locked(
    connection: sqlite3.Connection,
    intent: sqlite3.Row,
    *,
    status: str,
    snapshot: GazeboCommandProgressSnapshot,
    result_step_id: Optional[str] = None,
) -> sqlite3.Row:
    if intent['status'] != 'pending' or status not in {
        'completed', 'abandoned'
    }:
        raise _error('gazebo_command_progress_intent_invalid')
    response_json = _canonical_json(snapshot.to_public_dict())
    response_digest = hashlib.sha256(
        response_json.encode('ascii')
    ).hexdigest()
    values = _intent_values(intent)
    values.update(
        status=status,
        result_step_id=result_step_id,
        response_json=response_json,
        response_digest=response_digest,
    )
    record_digest = _intent_record_digest(values)
    cursor = connection.execute(
        '''
        UPDATE monitor_room_gazebo_command_intents
        SET status = ?, result_step_id = ?, response_json = ?,
            response_digest = ?, record_digest = ?
        WHERE intent_id = ? AND status = 'pending'
          AND record_digest = ?
        ''',
        (
            status, result_step_id, response_json, response_digest,
            record_digest, intent['intent_id'], intent['record_digest'],
        ),
    )
    if cursor.rowcount != 1:
        raise _error('gazebo_command_progress_conflict')
    updated = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_command_intents
        WHERE intent_id = ?
        ''',
        (intent['intent_id'],),
    ).fetchone()
    _validate_intent_row(updated)
    return updated


@dataclass(frozen=True, repr=False)
class _ReservedInvocation:
    action: str
    confirmation_request_id: str = field(repr=False)
    intent_id: str = field(repr=False)
    run_request_id: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class _InvocationPlan:
    reservation: _ReservedInvocation = field(repr=False)
    authority: GazeboPreparedExecutionAuthority = field(repr=False)
    previous: Optional[GazeboMonitorRoomCommandStep] = field(
        repr=False
    )
    command: str


def _load_intent_locked(
    connection: sqlite3.Connection,
    intent_id: str,
) -> Optional[sqlite3.Row]:
    row = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_command_intents
        WHERE intent_id = ?
        ''',
        (intent_id,),
    ).fetchone()
    if row is not None:
        _validate_intent_row(row)
    return row


def _insert_pending_intent_locked(
    connection: sqlite3.Connection,
    *,
    execution: sqlite3.Row,
    action: str,
    intent_id: str,
    authority: GazeboPreparedExecutionAuthority,
) -> sqlite3.Row:
    if action == 'advance':
        run_request_id = execution['drive_run_request_id']
        prior_step_id = execution['drive_last_step_id']
    else:
        run_request_id = execution['cancel_run_request_id']
        prior_step_id = execution['cancel_last_step_id']
        previous = _load_step_locked(connection, prior_step_id)
        if (
            previous is not None
            and previous.run_request_id != run_request_id
        ):
            prior_step_id = None
    previous = _load_step_locked(connection, prior_step_id)
    prior_chain_digest = (
        GazeboPreparedExecutionAuthority.binding_digest.fget(authority)
        if previous is None
        else previous.chain_digest
    )
    sequence = connection.execute(
        '''
        SELECT COALESCE(MAX(intent_sequence), 0) + 1
        FROM monitor_room_gazebo_command_intents
        WHERE confirmation_request_id = ?
        ''',
        (execution['confirmation_request_id'],),
    ).fetchone()[0]
    if type(sequence) is not int or not 1 <= sequence <= 65536:
        raise _error('gazebo_command_progress_step_limit')
    values: Dict[str, Any] = {
        'schema_version': 1,
        'intent_id': intent_id,
        'confirmation_request_id': execution['confirmation_request_id'],
        'action': action,
        'intent_sequence': sequence,
        'run_request_id': run_request_id,
        'prior_step_id': prior_step_id,
        'prior_chain_digest': prior_chain_digest,
        'status': 'pending',
        'result_step_id': None,
        'response_json': None,
        'response_digest': None,
    }
    values['record_digest'] = _intent_record_digest(values)
    connection.execute(
        '''
        INSERT INTO monitor_room_gazebo_command_intents (
            schema_version, intent_id, confirmation_request_id,
            action, intent_sequence, run_request_id, prior_step_id,
            prior_chain_digest, status, result_step_id, response_json,
            response_digest, record_digest
        ) VALUES (
            :schema_version, :intent_id, :confirmation_request_id,
            :action, :intent_sequence, :run_request_id, :prior_step_id,
            :prior_chain_digest, :status, :result_step_id, :response_json,
            :response_digest, :record_digest
        )
        ''',
        values,
    )
    row = _load_intent_locked(connection, intent_id)
    if row is None:
        raise _error('gazebo_command_progress_intent_invalid')
    return row


def _abandon_pending_advances_locked(
    connection: sqlite3.Connection,
    execution: sqlite3.Row,
) -> None:
    pending = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_command_intents
        WHERE confirmation_request_id = ?
          AND action = 'advance' AND status = 'pending'
        ORDER BY intent_sequence
        ''',
        (execution['confirmation_request_id'],),
    ).fetchall()
    for intent in pending:
        _validate_intent_row(intent)
        _complete_intent_locked(
            connection,
            intent,
            status='abandoned',
            snapshot=_snapshot_from_execution(
                execution, action='advance'
            ),
        )


def _abandon_all_pending_locked(
    connection: sqlite3.Connection,
    execution: sqlite3.Row,
    *,
    exclude_intent_id: str,
) -> None:
    pending = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_command_intents
        WHERE confirmation_request_id = ? AND status = 'pending'
          AND intent_id != ?
        ORDER BY intent_sequence
        ''',
        (execution['confirmation_request_id'], exclude_intent_id),
    ).fetchall()
    for intent in pending:
        _validate_intent_row(intent)
        _complete_intent_locked(
            connection,
            intent,
            status='abandoned',
            snapshot=_snapshot_from_execution(
                execution, action=intent['action']
            ),
        )


def _validate_invocation_previous(
    *,
    intent: sqlite3.Row,
    authority: GazeboPreparedExecutionAuthority,
    previous: Optional[GazeboMonitorRoomCommandStep],
) -> None:
    flow = 'drive' if intent['action'] == 'advance' else 'cancel'
    authorization = (
        GazeboPreparedExecutionAuthority.binding_digest.fget(authority)
    )
    invalid = (
        authority.execution_scope != flow
        or intent['run_request_id'] is None
        or (
            previous is None
            and (
                intent['prior_step_id'] is not None
                or intent['prior_chain_digest'] != authorization
            )
        )
        or (
            previous is not None
            and (
                previous.outbox_id != authority.outbox_id
                or previous.operation_id != authority.operation_id
                or previous.flow != flow
                or previous.run_request_id != intent['run_request_id']
                or previous.authorization_digest != authorization
                or previous.chain_digest
                != intent['prior_chain_digest']
                or previous.terminal
            )
        )
    )
    if invalid:
        raise _error('gazebo_command_progress_intent_invalid')


def _insert_step_locked(
    connection: sqlite3.Connection,
    *,
    execution: sqlite3.Row,
    intent: sqlite3.Row,
    step: GazeboMonitorRoomCommandStep,
) -> str:
    if execution['total_steps'] >= GAZEBO_COMMAND_PROGRESS_MAX_STEPS:
        raise _error('gazebo_command_progress_step_limit')
    payload = _step_payload(step)
    step_json = _canonical_json(payload)
    step_id = 'gazebo-progress-step-' + _hash(
        {
            'contract': 'gazebo-command-progress-step-id-v1',
            'confirmation_request_id': execution[
                'confirmation_request_id'
            ],
            'intent_id': intent['intent_id'],
            'request_id': step.request_id,
            'gateway_response_fingerprint': (
                step.gateway_response_fingerprint
            ),
        }
    )
    progress_sequence = execution['total_steps'] + 1
    record_digest = _step_record_digest(
        step_id=step_id,
        confirmation_request_id=execution['confirmation_request_id'],
        intent_id=intent['intent_id'],
        progress_sequence=progress_sequence,
        step=step,
        step_json=step_json,
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_gazebo_command_steps (
            schema_version, step_id, confirmation_request_id, intent_id,
            progress_sequence, flow, run_request_id, step_index,
            request_id, gateway_response_fingerprint, chain_digest,
            step_json, record_digest
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            step_id, execution['confirmation_request_id'],
            intent['intent_id'], progress_sequence, step.flow,
            step.run_request_id, step.step_index, step.request_id,
            step.gateway_response_fingerprint, step.chain_digest,
            step_json, record_digest,
        ),
    )
    stored = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_command_steps
        WHERE step_id = ?
        ''',
        (step_id,),
    ).fetchone()
    _validate_step_row(stored)
    return step_id


class GazeboCommandProgressService:
    """Authenticated, cursor-free durable command progress coordinator."""

    def __init__(
        self,
        runner: GazeboMonitorRoomCommandRunner,
        *,
        timeout_seconds: float = (
            GAZEBO_COMMAND_RUNNER_DEFAULT_TIMEOUT_SECONDS
        ),
    ) -> None:
        if type(runner) is not GazeboMonitorRoomCommandRunner:
            raise _error('gazebo_command_progress_configuration_invalid')
        try:
            _RUNNER_ATTEST(runner)
            timeout = float(timeout_seconds)
            if (
                isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout)
                or not 0.05 <= timeout <= 30.0
            ):
                raise ValueError
            store = runner._store
            user_id = validate_user_id(runner._user_id)
            connection = store._connection
            store_lock = store._lock
            policy = store._gazebo_execution_policy
            if (
                type(store) is not SQLiteConversationStore
                or type(connection) is not sqlite3.Connection
                or type(policy) is not GazeboSimulationExecutionPolicy
                or not hasattr(store_lock, '__enter__')
            ):
                raise TypeError
        except Exception:
            raise _error(
                'gazebo_command_progress_configuration_invalid'
            ) from None
        drive_lock = threading.Lock()
        cancel_lock = threading.Lock()
        self._runner = runner
        self._store = store
        self._connection = connection
        self._store_lock = store_lock
        self._policy = policy
        self._user_id = user_id
        self._principal_digest = _principal_digest(user_id)
        self._timeout_seconds = timeout
        self._drive_lock = drive_lock
        self._cancel_lock = cancel_lock
        self._configuration_seal = (
            runner, store, connection, store_lock, policy, user_id,
            self._principal_digest, timeout, drive_lock, cancel_lock,
        )
        with _SERVICE_SEAL_LOCK:
            _SERVICE_SEALS[self] = self._configuration_seal
        try:
            GazeboCommandProgressService._activate_schema(self)
        except Exception:
            with _SERVICE_SEAL_LOCK:
                _SERVICE_SEALS.pop(self, None)
            raise

    def _attest_configuration(self) -> None:
        seal = getattr(self, '_configuration_seal', None)
        external = None
        try:
            with _SERVICE_SEAL_LOCK:
                external = _SERVICE_SEALS.get(self)
        except Exception:
            external = None
        if (
            type(self) is not GazeboCommandProgressService
            or type(seal) is not tuple
            or len(seal) != 10
            or external is None
            or external != seal
            or set(getattr(self, '__dict__', {})) != {
                '_runner', '_store', '_connection', '_store_lock',
                '_policy', '_user_id', '_principal_digest',
                '_timeout_seconds', '_drive_lock', '_cancel_lock',
                '_configuration_seal',
            }
            or self._runner is not seal[0]
            or self._store is not seal[1]
            or self._connection is not seal[2]
            or self._store_lock is not seal[3]
            or self._policy is not seal[4]
            or self._user_id != seal[5]
            or self._principal_digest != seal[6]
            or self._timeout_seconds != seal[7]
            or self._drive_lock is not seal[8]
            or self._cancel_lock is not seal[9]
            or getattr(self._store, '_connection', None) is not seal[2]
            or getattr(self._store, '_lock', None) is not seal[3]
            or getattr(
                self._store, '_gazebo_execution_policy', None
            ) is not seal[4]
        ):
            raise _error('gazebo_command_progress_configuration_changed')
        try:
            _RUNNER_ATTEST(self._runner)
        except Exception:
            raise _error(
                'gazebo_command_progress_configuration_changed'
            ) from None

    def _activate_schema(self) -> None:
        GazeboCommandProgressService._attest_configuration(self)
        SQLiteConversationStore.attest_command_boundary_durability(
            self._store
        )
        with self._store_lock:
            SQLiteConversationStore.attest_command_boundary_durability(
                self._store
            )
            SQLiteConversationStore._begin(self._store)
            try:
                prepare_gazebo_command_progress_schema_locked(
                    self._connection
                )
                SQLiteConversationStore.attest_command_boundary_durability(
                    self._store
                )
                self._connection.commit()
            except Exception:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise
            SQLiteConversationStore.attest_command_boundary_durability(
                self._store
            )

    def _transaction(self, callback: Any) -> Any:
        GazeboCommandProgressService._attest_configuration(self)
        SQLiteConversationStore.attest_command_boundary_durability(
            self._store
        )
        with self._store_lock:
            GazeboCommandProgressService._attest_configuration(self)
            SQLiteConversationStore.attest_command_boundary_durability(
                self._store
            )
            SQLiteConversationStore._begin(self._store)
            committed = False
            try:
                validate_gazebo_command_progress_schema_locked(
                    self._connection
                )
                result = callback(self._connection)
                SQLiteConversationStore.attest_command_boundary_durability(
                    self._store
                )
                self._connection.commit()
                committed = True
                SQLiteConversationStore.attest_command_boundary_durability(
                    self._store
                )
                return result
            except Exception:
                if not committed and self._connection.in_transaction:
                    self._connection.rollback()
                raise

    def _require_execution_principal(
        self,
        execution: Optional[sqlite3.Row],
    ) -> sqlite3.Row:
        if (
            execution is None
            or execution['principal_digest'] != self._principal_digest
        ):
            raise _error('gazebo_command_progress_not_found')
        return execution

    def _status_locked(
        self,
        connection: sqlite3.Connection,
        confirmation_request_id: str,
    ) -> GazeboCommandProgressSnapshot:
        execution = _load_execution_locked(
            connection, confirmation_request_id
        )
        if execution is None:
            if connection.execute(
                '''
                SELECT 1 FROM monitor_room_gazebo_command_preactivation
                WHERE confirmation_request_id = ?
                ''',
                (confirmation_request_id,),
            ).fetchone() is not None:
                raise _error(
                    'gazebo_command_progress_preactivation_denied'
                )
            authority = _live_drive_authority_locked(
                connection,
                policy=self._policy,
                confirmation_request_id=confirmation_request_id,
                user_id=self._user_id,
            )
            execution = _insert_execution_locked(
                connection,
                authority=authority,
                principal_digest=self._principal_digest,
            )
        execution = GazeboCommandProgressService._require_execution_principal(
            self, execution
        )
        if execution['state'] != 'terminal':
            _validate_anchored_authority_locked(
                connection,
                policy=self._policy,
                execution=execution,
            )
        return _snapshot_from_execution(execution, action='status')

    def status(
        self,
        confirmation_request_id: str,
    ) -> GazeboCommandProgressSnapshot:
        """Read progress; first use creates exactly one fixed intent pair."""
        normalized = _identifier(confirmation_request_id)
        try:
            return GazeboCommandProgressService._transaction(
                self,
                lambda connection: GazeboCommandProgressService
                ._status_locked(self, connection, normalized),
            )
        except GazeboCommandProgressError:
            raise
        except Exception:
            raise _error('gazebo_command_progress_unavailable') from None

    def _reserve_locked(
        self,
        connection: sqlite3.Connection,
        *,
        confirmation_request_id: str,
        intent_id: str,
        action: str,
    ) -> Any:
        execution = GazeboCommandProgressService._require_execution_principal(
            self,
            _load_execution_locked(connection, confirmation_request_id),
        )
        existing = _load_intent_locked(connection, intent_id)
        if existing is not None:
            if (
                existing['confirmation_request_id']
                != confirmation_request_id
                or existing['action'] != action
            ):
                raise _error('gazebo_command_progress_intent_invalid')
            if existing['status'] != 'pending':
                return _replay_snapshot(existing)
            expected_token = (
                execution['next_drive_intent_id']
                if action == 'advance'
                else execution['next_cancel_intent_id']
            )
            if (
                expected_token != intent_id
                or execution['state'] == 'terminal'
            ):
                raise _error('gazebo_command_progress_record_invalid')
            return _ReservedInvocation(
                action=action,
                confirmation_request_id=confirmation_request_id,
                intent_id=intent_id,
                run_request_id=existing['run_request_id'],
            )
        expected_token = (
            execution['next_drive_intent_id']
            if action == 'advance'
            else execution['next_cancel_intent_id']
        )
        if expected_token != intent_id or execution['state'] == 'terminal':
            raise _error('gazebo_command_progress_intent_invalid')
        if action == 'advance':
            if execution['state'] not in {'ready', 'driving'}:
                raise _error('gazebo_command_progress_drive_forbidden')
            authority = _live_drive_authority_locked(
                connection,
                policy=self._policy,
                confirmation_request_id=confirmation_request_id,
                user_id=self._user_id,
            )
            if not _authority_matches_execution(
                authority, execution, 'drive'
            ):
                raise _error('gazebo_command_progress_authority_invalid')
        else:
            if execution['state'] not in {
                'ready', 'driving', 'cancel_required', 'canceling'
            }:
                raise _error('gazebo_command_progress_cancel_forbidden')
            authority = _validate_anchored_authority_locked(
                connection,
                policy=self._policy,
                execution=execution,
            )
        pending = _insert_pending_intent_locked(
            connection,
            execution=execution,
            action=action,
            intent_id=intent_id,
            authority=authority,
        )
        if action == 'advance':
            execution = _update_execution_locked(
                connection, execution, state='driving'
            )
        else:
            execution = _update_execution_locked(
                connection,
                execution,
                state='canceling',
                next_drive_intent_id=None,
            )
            _abandon_pending_advances_locked(connection, execution)
        return _ReservedInvocation(
            action=action,
            confirmation_request_id=confirmation_request_id,
            intent_id=intent_id,
            run_request_id=pending['run_request_id'],
        )

    def _prepare_invocation_locked(
        self,
        connection: sqlite3.Connection,
        reservation: _ReservedInvocation,
    ) -> Any:
        intent = _load_intent_locked(connection, reservation.intent_id)
        if intent is None:
            raise _error('gazebo_command_progress_intent_invalid')
        if intent['status'] != 'pending':
            return _replay_snapshot(intent)
        execution = GazeboCommandProgressService._require_execution_principal(
            self,
            _load_execution_locked(
                connection, reservation.confirmation_request_id
            ),
        )
        if reservation.action == 'advance':
            if (
                execution['state'] not in {'ready', 'driving'}
                or execution['next_drive_intent_id']
                != reservation.intent_id
            ):
                completed = _complete_intent_locked(
                    connection,
                    intent,
                    status='abandoned',
                    snapshot=_snapshot_from_execution(
                        execution, action='advance'
                    ),
                )
                return _replay_snapshot(completed)
            try:
                authority = _live_drive_authority_locked(
                    connection,
                    policy=self._policy,
                    confirmation_request_id=(
                        reservation.confirmation_request_id
                    ),
                    user_id=self._user_id,
                )
                if not _authority_matches_execution(
                    authority, execution, 'drive'
                ):
                    raise _error(
                        'gazebo_command_progress_authority_invalid'
                    )
            except GazeboCommandProgressError:
                execution = _update_execution_locked(
                    connection,
                    execution,
                    state='cancel_required',
                    next_drive_intent_id=None,
                )
                completed = _complete_intent_locked(
                    connection,
                    intent,
                    status='abandoned',
                    snapshot=_snapshot_from_execution(
                        execution, action='advance'
                    ),
                )
                return _replay_snapshot(completed)
        else:
            if (
                execution['state'] == 'terminal'
                or execution['next_cancel_intent_id']
                != reservation.intent_id
            ):
                completed = _complete_intent_locked(
                    connection,
                    intent,
                    status='abandoned',
                    snapshot=_snapshot_from_execution(
                        execution, action='cancel'
                    ),
                )
                return _replay_snapshot(completed)
            authority = _validate_anchored_authority_locked(
                connection,
                policy=self._policy,
                execution=execution,
            )
        previous = _load_step_locked(connection, intent['prior_step_id'])
        _validate_invocation_previous(
            intent=intent,
            authority=authority,
            previous=previous,
        )
        command = (
            'drive'
            if reservation.action == 'advance'
            else _RUNNER_NEXT_CANCEL(previous)
        )
        return _InvocationPlan(
            reservation=reservation,
            authority=authority,
            previous=previous,
            command=command,
        )

    def _commit_step_locked(
        self,
        connection: sqlite3.Connection,
        *,
        plan: _InvocationPlan,
        step: GazeboMonitorRoomCommandStep,
    ) -> GazeboCommandProgressSnapshot:
        reservation = plan.reservation
        intent = _load_intent_locked(connection, reservation.intent_id)
        if intent is None:
            raise _error('gazebo_command_progress_intent_invalid')
        if intent['status'] != 'pending':
            return _replay_snapshot(intent)
        execution = GazeboCommandProgressService._require_execution_principal(
            self,
            _load_execution_locked(
                connection, reservation.confirmation_request_id
            ),
        )
        expected_token = (
            execution['next_drive_intent_id']
            if reservation.action == 'advance'
            else execution['next_cancel_intent_id']
        )
        expected_state = (
            {'ready', 'driving'}
            if reservation.action == 'advance'
            else {'cancel_required', 'canceling'}
        )
        if (
            execution['state'] not in expected_state
            or expected_token != reservation.intent_id
        ):
            completed = _complete_intent_locked(
                connection,
                intent,
                status='abandoned',
                snapshot=_snapshot_from_execution(
                    execution, action=reservation.action
                ),
            )
            return _replay_snapshot(completed)
        flow = 'drive' if reservation.action == 'advance' else 'cancel'
        authority = _validate_anchored_authority_locked(
            connection,
            policy=self._policy,
            execution=execution,
            execution_scope=flow,
        )
        previous = _load_step_locked(connection, intent['prior_step_id'])
        _validate_invocation_previous(
            intent=intent,
            authority=authority,
            previous=previous,
        )
        try:
            GazeboMonitorRoomCommandStep._attest(step)
            authorization = (
                GazeboPreparedExecutionAuthority.binding_digest.fget(
                    authority
                )
            )
            invalid_step = (
                type(step) is not GazeboMonitorRoomCommandStep
                or step.outbox_id != execution['outbox_id']
                or step.operation_id != execution['operation_id']
                or step.run_request_id != intent['run_request_id']
                or step.authorization_digest != authorization
                or step.flow != flow
                or (
                    reservation.action == 'advance'
                    and step.command != 'drive'
                )
                or (
                    reservation.action == 'cancel'
                    and step.command != plan.command
                )
                or (
                    previous is None
                    and (
                        step.step_index != 0
                        or step.previous_request_id is not None
                        or step.previous_chain_digest != authorization
                    )
                )
                or (
                    previous is not None
                    and (
                        step.step_index != previous.step_index + 1
                        or step.previous_request_id
                        != previous.request_id
                        or step.previous_chain_digest
                        != previous.chain_digest
                    )
                )
            )
        except Exception:
            invalid_step = True
        if invalid_step:
            raise _error('gazebo_command_progress_step_invalid')
        step_id = _insert_step_locked(
            connection,
            execution=execution,
            intent=intent,
            step=step,
        )
        updates: Dict[str, Any] = {
            'drive_steps': execution['drive_steps']
            + (1 if flow == 'drive' else 0),
            'cancel_steps': execution['cancel_steps']
            + (1 if flow == 'cancel' else 0),
            'total_steps': execution['total_steps'] + 1,
            'drive_last_step_id': (
                step_id
                if flow == 'drive'
                else execution['drive_last_step_id']
            ),
            'cancel_last_step_id': (
                step_id
                if flow == 'cancel'
                else execution['cancel_last_step_id']
            ),
        }
        if step.state in _RESOLVED_TERMINAL_STATES:
            if (
                not step.terminal
                or type(step.terminal_code) is not str
                or not step.terminal_code
            ):
                raise _error('gazebo_command_progress_step_invalid')
            updates.update(
                state='terminal',
                terminal_step_id=step_id,
                terminal_state=step.state,
                terminal_code=step.terminal_code,
                terminal_chain_digest=step.chain_digest,
                terminal_evidence_digest=step.evidence_digest,
                terminal_gateway_fingerprint=(
                    step.gateway_response_fingerprint
                ),
                next_drive_intent_id=None,
                next_cancel_intent_id=None,
            )
        elif step.state in _UNKNOWN_TERMINAL_STATES:
            if not step.terminal:
                raise _error('gazebo_command_progress_step_invalid')
            generation = execution['cancel_run_generation'] + 1
            if generation > GAZEBO_COMMAND_PROGRESS_MAX_STEPS:
                raise _error('gazebo_command_progress_step_limit')
            updates.update(
                state='cancel_required',
                cancel_run_generation=generation,
                cancel_run_request_id=_run_request_id(
                    execution['anchor_digest'], 'cancel', generation
                ),
                next_drive_intent_id=None,
                next_cancel_intent_id=_new_intent_id_locked(connection),
            )
        elif step.terminal:
            raise _error('gazebo_command_progress_step_invalid')
        elif flow == 'drive':
            updates.update(
                state='driving',
                next_drive_intent_id=_new_intent_id_locked(connection),
            )
        else:
            updates.update(
                state='canceling',
                next_drive_intent_id=None,
                next_cancel_intent_id=_new_intent_id_locked(connection),
            )
        execution = _update_execution_locked(
            connection, execution, **updates
        )
        snapshot = _snapshot_from_execution(
            execution, action=reservation.action
        )
        completed = _complete_intent_locked(
            connection,
            intent,
            status='completed',
            snapshot=snapshot,
            result_step_id=step_id,
        )
        if execution['state'] == 'terminal':
            _abandon_all_pending_locked(
                connection,
                execution,
                exclude_intent_id=reservation.intent_id,
            )
        return _replay_snapshot(completed)

    def _execute_action(
        self,
        *,
        confirmation_request_id: str,
        intent_id: str,
        action: str,
    ) -> GazeboCommandProgressSnapshot:
        lock = self._drive_lock if action == 'advance' else self._cancel_lock
        with lock:
            reserved = GazeboCommandProgressService._transaction(
                self,
                lambda connection: GazeboCommandProgressService
                ._reserve_locked(
                    self,
                    connection,
                    confirmation_request_id=confirmation_request_id,
                    intent_id=intent_id,
                    action=action,
                ),
            )
            if type(reserved) is GazeboCommandProgressSnapshot:
                return reserved
            plan = GazeboCommandProgressService._transaction(
                self,
                lambda connection: GazeboCommandProgressService
                ._prepare_invocation_locked(
                    self, connection, reserved
                ),
            )
            if type(plan) is GazeboCommandProgressSnapshot:
                return plan
            GazeboCommandProgressService._attest_configuration(self)
            SQLiteConversationStore.attest_command_boundary_durability(
                self._store
            )
            deadline = _RUNNER_MONOTONIC() + self._timeout_seconds
            try:
                step = _RUNNER_INVOKE(
                    self._runner,
                    authority=plan.authority,
                    run_request_id=plan.reservation.run_request_id,
                    flow=(
                        'drive' if action == 'advance' else 'cancel'
                    ),
                    command=plan.command,
                    previous=plan.previous,
                    deadline=deadline,
                )
            except GazeboMonitorRoomCommandRunnerError:
                raise _error(
                    'gazebo_command_progress_gateway_unavailable'
                ) from None
            except Exception:
                raise _error(
                    'gazebo_command_progress_gateway_unavailable'
                ) from None
            return GazeboCommandProgressService._transaction(
                self,
                lambda connection: GazeboCommandProgressService
                ._commit_step_locked(
                    self, connection, plan=plan, step=step
                ),
            )

    def advance(
        self,
        confirmation_request_id: str,
        next_intent_id: str,
    ) -> GazeboCommandProgressSnapshot:
        """Perform at most one deterministic foreground drive exchange."""
        confirmation = _identifier(confirmation_request_id)
        intent = _intent_id(next_intent_id)
        try:
            return GazeboCommandProgressService._execute_action(
                self,
                confirmation_request_id=confirmation,
                intent_id=intent,
                action='advance',
            )
        except GazeboCommandProgressError:
            raise
        except Exception:
            raise _error('gazebo_command_progress_unavailable') from None

    def cancel(
        self,
        confirmation_request_id: str,
        cancel_intent_id: str,
    ) -> GazeboCommandProgressSnapshot:
        """Prioritize and advance one anchored cancellation exchange."""
        confirmation = _identifier(confirmation_request_id)
        intent = _intent_id(cancel_intent_id)
        try:
            return GazeboCommandProgressService._execute_action(
                self,
                confirmation_request_id=confirmation,
                intent_id=intent,
                action='cancel',
            )
        except GazeboCommandProgressError:
            raise
        except Exception:
            raise _error('gazebo_command_progress_unavailable') from None

    def get_terminal_anchor(
        self,
        confirmation_request_id: str,
    ) -> GazeboCommandTerminalAnchor:
        """Return the private immutable terminal row for trusted-result use."""
        confirmation = _identifier(confirmation_request_id)

        def load(connection: sqlite3.Connection) -> Any:
            execution = (
                GazeboCommandProgressService._require_execution_principal(
                    self,
                    _load_execution_locked(connection, confirmation),
                )
            )
            if execution['state'] != 'terminal':
                raise _error('gazebo_command_progress_terminal_pending')
            return GazeboCommandTerminalAnchor(
                confirmation_request_id=confirmation,
                principal_digest=execution['principal_digest'],
                terminal_step_id=execution['terminal_step_id'],
                terminal_state=execution['terminal_state'],
                terminal_code=execution['terminal_code'],
                terminal_chain_digest=(
                    execution['terminal_chain_digest']
                ),
                terminal_evidence_digest=(
                    execution['terminal_evidence_digest']
                ),
                terminal_gateway_fingerprint=(
                    execution['terminal_gateway_fingerprint']
                ),
                record_digest=execution['record_digest'],
            )

        try:
            return GazeboCommandProgressService._transaction(self, load)
        except GazeboCommandProgressError:
            raise
        except Exception:
            raise _error('gazebo_command_progress_unavailable') from None


__all__ = [
    'GAZEBO_COMMAND_PROGRESS_MAX_STEPS',
    'GAZEBO_COMMAND_PROGRESS_SCHEMA_VERSION',
    'GazeboCommandProgressError',
    'GazeboCommandProgressService',
    'GazeboCommandProgressSnapshot',
    'GazeboCommandTerminalAnchor',
    'prepare_gazebo_command_progress_schema_locked',
    'validate_gazebo_command_progress_schema_locked',
]
