"""SQLite action ledger with fenced exactly-once-attempt dispatch."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from malbut_agent_server.domain.robot_action import (
    ActionBinding,
    ActionState,
    DispatchAuthorization,
    RobotAction,
)
from malbut_agent_server.ports.action_repository import (
    ActionClaim,
    DispatchIntent,
)
from malbut_agent_server.text_confirmation import (
    APPROVED,
    ConfirmationRecord,
)


ACTION_SCHEMA_VERSION = 1
DEFAULT_DISPATCH_WINDOW_SECONDS = 30.0
MAX_DISPATCH_WINDOW_SECONDS = 120.0
MAX_LEASE_SECONDS = 300.0


class ActionPersistenceError(RuntimeError):
    """Raised when durable action state cannot be trusted."""


class ActionConflictError(ActionPersistenceError):
    """Raised when an identity or expected action revision changed."""


class ActionClaimLostError(ActionConflictError):
    """Raised when a worker no longer owns the exact fenced claim."""


_SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS action_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);

CREATE TABLE IF NOT EXISTS robot_actions (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    action_id TEXT NOT NULL PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    confirmation_request_id TEXT NOT NULL UNIQUE,
    proposal_fingerprint TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    conversation_revision INTEGER NOT NULL
        CHECK (conversation_revision >= 1),
    decision_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    target_room_name TEXT NOT NULL,
    target_room_category TEXT NOT NULL,
    confirmation_state_evidence_id TEXT NOT NULL,
    confirmation_state_observed_at REAL NOT NULL,
    confirmation_safety_policy_revision TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'PENDING_PREFLIGHT', 'CLAIMED', 'DISPATCH_INTENT', 'STARTED',
        'SUCCEEDED', 'FAILED', 'CANCELED', 'BLOCKED', 'UNKNOWN'
    )),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    dispatch_expires_at REAL NOT NULL CHECK (
        dispatch_expires_at > created_at
    ),
    result_code TEXT,
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    claim_worker_id TEXT,
    claim_token_digest TEXT,
    claim_fence INTEGER NOT NULL DEFAULT 0 CHECK (claim_fence >= 0),
    lease_expires_at REAL,
    dispatch_state_evidence_id TEXT,
    dispatch_state_observed_at REAL,
    dispatch_safety_policy_revision TEXT,
    dispatch_target_binding_digest TEXT,
    dispatch_authorized_at REAL,
    CHECK (
        (claim_worker_id IS NULL AND claim_token_digest IS NULL
         AND lease_expires_at IS NULL)
        OR
        (claim_worker_id IS NOT NULL AND claim_token_digest IS NOT NULL
         AND lease_expires_at IS NOT NULL AND claim_fence >= 1)
    ),
    CHECK (
        (dispatch_state_evidence_id IS NULL
         AND dispatch_state_observed_at IS NULL
         AND dispatch_safety_policy_revision IS NULL
         AND dispatch_target_binding_digest IS NULL
         AND dispatch_authorized_at IS NULL)
        OR
        (dispatch_state_evidence_id IS NOT NULL
         AND dispatch_state_observed_at IS NOT NULL
         AND dispatch_safety_policy_revision IS NOT NULL
         AND dispatch_target_binding_digest IS NOT NULL
         AND dispatch_authorized_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS robot_actions_claim_candidates_idx
ON robot_actions (state, dispatch_expires_at, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS execution_outbox (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    intent_id TEXT NOT NULL PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('DISPATCH_INTENT', 'STARTED', 'TERMINAL', 'UNKNOWN')
    ),
    worker_id TEXT NOT NULL,
    claim_token_digest TEXT NOT NULL,
    claim_fence INTEGER NOT NULL CHECK (claim_fence >= 1),
    state_evidence_id TEXT NOT NULL,
    state_observed_at REAL NOT NULL,
    safety_policy_revision TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    authorized_at REAL NOT NULL,
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    result_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (action_id) REFERENCES robot_actions (action_id)
        ON DELETE RESTRICT
);
'''


def _timestamp(value: Any, name: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f'{name} must be a number')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'{name} is invalid')
    return result


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f'{name} must be a string')
    result = value.strip()
    if not result or len(result) > 256:
        raise ValueError(f'{name} is invalid')
    if any(ord(character) < 32 or ord(character) == 127
           for character in result):
        raise ValueError(f'{name} contains control characters')
    return result


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
    except (OverflowError, TypeError, ValueError):
        raise ActionPersistenceError('action value is not JSON-safe') from None


def initialize_action_schema(connection: sqlite3.Connection) -> None:
    """Initialize the additive action schema without committing its owner."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('connection must be sqlite3.Connection')
    for statement in _SCHEMA_SQL.split(';'):
        if statement.strip():
            connection.execute(statement)
    row = connection.execute(
        'SELECT schema_version FROM action_schema_metadata '
        'WHERE singleton = 1'
    ).fetchone()
    if row is None:
        connection.execute(
            'INSERT INTO action_schema_metadata '
            '(singleton, schema_version) VALUES (1, ?)',
            (ACTION_SCHEMA_VERSION,),
        )
    elif int(row[0]) != ACTION_SCHEMA_VERSION:
        raise ActionPersistenceError('action schema version is unsupported')


def _require_action_schema(connection: sqlite3.Connection) -> None:
    """Validate preinitialized schema without DDL or transaction mutation."""
    try:
        row = connection.execute(
            'SELECT schema_version FROM action_schema_metadata '
            'WHERE singleton = 1'
        ).fetchone()
        tables = {
            item[0]
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('robot_actions', 'execution_outbox')"
            ).fetchall()
        }
    except sqlite3.DatabaseError as error:
        raise ActionPersistenceError(
            'action schema is not initialized'
        ) from error
    if (
        row is None
        or int(row[0]) != ACTION_SCHEMA_VERSION
        or tables != {'robot_actions', 'execution_outbox'}
    ):
        raise ActionPersistenceError('action schema is not initialized')


def _row_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return {
        description[0]: value
        for description, value in zip(cursor.description, row)
    }


def _authorization_from_row(
    row: dict[str, Any],
) -> Optional[DispatchAuthorization]:
    if row['dispatch_state_evidence_id'] is None:
        return None
    return DispatchAuthorization(
        state_evidence_id=row['dispatch_state_evidence_id'],
        state_observed_at=float(row['dispatch_state_observed_at']),
        safety_policy_revision=row['dispatch_safety_policy_revision'],
        target_binding_digest=row['dispatch_target_binding_digest'],
        authorized_at=float(row['dispatch_authorized_at']),
    )


def _outbox_matches_action_row(
    outbox: sqlite3.Row,
    action: sqlite3.Row,
) -> bool:
    """Verify the duplicated send authority is byte-for-byte consistent."""
    try:
        return (
            outbox['action_id'] == action['action_id']
            and outbox['operation_id'] == action['operation_id']
            and outbox['worker_id'] == action['claim_worker_id']
            and outbox['claim_token_digest']
            == action['claim_token_digest']
            and int(outbox['claim_fence']) == int(action['claim_fence'])
            and outbox['state_evidence_id']
            == action['dispatch_state_evidence_id']
            and float(outbox['state_observed_at'])
            == float(action['dispatch_state_observed_at'])
            and outbox['safety_policy_revision']
            == action['dispatch_safety_policy_revision']
            and outbox['target_binding_digest']
            == action['dispatch_target_binding_digest']
            and outbox['target_binding_digest']
            == action['target_binding_digest']
            and float(outbox['authorized_at'])
            == float(action['dispatch_authorized_at'])
            and int(outbox['simulation']) == 1
            and int(outbox['physical_authorized']) == 0
            and int(action['simulation']) == 1
            and int(action['physical_authorized']) == 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def _action_from_row(row: dict[str, Any]) -> RobotAction:
    try:
        arguments = json.loads(row['arguments_json'])
        binding = ActionBinding(
            confirmation_request_id=row['confirmation_request_id'],
            proposal_fingerprint=row['proposal_fingerprint'],
            arguments_digest=row['arguments_digest'],
            target_binding_digest=row['target_binding_digest'],
            user_id=row['user_id'],
            conversation_id=row['conversation_id'],
            session_instance_id=row['session_instance_id'],
            generation=int(row['generation']),
            conversation_revision=int(row['conversation_revision']),
            decision_id=row['decision_id'],
            tool_name=row['tool_name'],
            arguments=arguments,
            target_room_name=row['target_room_name'],
            target_room_category=row['target_room_category'],
            confirmation_state_evidence_id=(
                row['confirmation_state_evidence_id']
            ),
            confirmation_state_observed_at=float(
                row['confirmation_state_observed_at']
            ),
            confirmation_safety_policy_revision=(
                row['confirmation_safety_policy_revision']
            ),
        )
        return RobotAction(
            action_id=row['action_id'],
            operation_id=row['operation_id'],
            binding=binding,
            state=ActionState(row['state']),
            revision=int(row['revision']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
            dispatch_expires_at=float(row['dispatch_expires_at']),
            result_code=row['result_code'],
            dispatch_authorization=_authorization_from_row(row),
            simulation=bool(row['simulation']),
            physical_authorized=bool(row['physical_authorized']),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ActionPersistenceError(
            'stored robot action cannot be trusted'
        ) from error


def find_action_for_confirmation(
    connection: sqlite3.Connection,
    confirmation_request_id: str,
) -> Optional[RobotAction]:
    """Find an action using an existing connection and transaction view."""
    confirmation_id = _identifier(
        confirmation_request_id,
        'confirmation_request_id',
    )
    cursor = connection.execute(
        'SELECT * FROM robot_actions WHERE confirmation_request_id = ?',
        (confirmation_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _action_from_row(_row_dict(cursor, row))


def _require_exact_approved_confirmation(
    connection: sqlite3.Connection,
    record: ConfirmationRecord,
) -> None:
    if type(record) is not ConfirmationRecord:
        raise TypeError('record must be an exact ConfirmationRecord')
    if (
        record.disposition != APPROVED
        or record.requested_disposition != 'approve'
        or record.result_code != 'confirmation_approved'
        or record.resolved_at is None
        or record.execution_authorized is not False
        or record.consume_once is not False
    ):
        raise ActionConflictError('only an exact approval may create action')
    if record.resolved_at < record.issued_at:
        raise ActionConflictError('approval resolution predates proposal')
    cursor = connection.execute(
        'SELECT * FROM confirmation_intents '
        'WHERE confirmation_request_id = ?',
        (record.confirmation_request_id,),
    )
    stored = cursor.fetchone()
    if stored is None:
        raise ActionConflictError('approved confirmation is not durable')
    row = _row_dict(cursor, stored)
    expected_json = _canonical_json(record.to_private_dict())
    exact = (
        row['state'] == 'resolved'
        and row['disposition'] == APPROVED
        and row['requested_disposition'] == 'approve'
        and row['result_code'] == 'confirmation_approved'
        and row['confirmation_request_id']
        == record.confirmation_request_id
        and row['user_id'] == record.user_id
        and row['conversation_id'] == record.conversation_id
        and row['session_instance_id'] == record.session_instance_id
        and int(row['generation']) == record.generation
        and int(row['revision']) == record.revision
        and row['decision_id'] == record.decision_id
        and row['tool_name'] == record.tool_name
        and row['arguments_digest'] == record.arguments_digest
        and row['target_binding_digest']
        == record.target_binding_digest
        and row['proposal_fingerprint'] == record.proposal_fingerprint
        and row['response_id'] == record.response_id
        and row['response_turn_id'] == record.response_turn_id
        and row['response_fingerprint'] == record.response_fingerprint
        and float(row['resolved_at']) == float(record.resolved_at)
        and row['record_json'] == expected_json
        and row['authority_kind'] == 'none'
        and int(row['execution_authorized']) == 0
        and int(row['consume_once']) == 0
        and row['tool_call_id'] is None
        and row['mission_id'] is None
    )
    if not exact:
        raise ActionConflictError(
            'approved confirmation does not match durable source'
        )


def _new_server_id(prefix: str, forbidden: set[str]) -> str:
    while True:
        candidate = f'{prefix}-{uuid.uuid4()}'
        if candidate not in forbidden:
            return candidate


def _action_matches_confirmation(
    action: RobotAction,
    record: ConfirmationRecord,
) -> bool:
    binding = action.binding
    return (
        binding.confirmation_request_id == record.confirmation_request_id
        and binding.proposal_fingerprint == record.proposal_fingerprint
        and binding.arguments_digest == record.arguments_digest
        and binding.target_binding_digest == record.target_binding_digest
        and binding.user_id == record.user_id
        and binding.conversation_id == record.conversation_id
        and binding.session_instance_id == record.session_instance_id
        and binding.generation == record.generation
        and binding.conversation_revision == record.revision
        and binding.decision_id == record.decision_id
        and binding.tool_name == record.tool_name
        and binding.arguments_dict() == record.arguments_dict()
        and binding.target_room_name == record.target_room_name
        and binding.target_room_category == record.target_room_category
        and binding.confirmation_state_evidence_id
        == record.state_evidence_id
        and binding.confirmation_state_observed_at
        == float(record.state_observed_at)
        and binding.confirmation_safety_policy_revision
        == record.safety_policy_revision
        and action.simulation is True
        and action.physical_authorized is False
    )


def insert_action_for_approved_confirmation(
    connection: sqlite3.Connection,
    record: ConfirmationRecord,
    *,
    now: float,
    dispatch_window: float = DEFAULT_DISPATCH_WINDOW_SECONDS,
) -> RobotAction:
    """
    Insert one action in the caller's approval transaction.

    This helper never begins, commits, or rolls back the transaction. A replay
    returns the same server-owned action bound to the unique confirmation.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('connection must be sqlite3.Connection')
    if not connection.in_transaction:
        raise ActionPersistenceError(
            'action insertion requires an existing transaction'
        )
    created_at = _timestamp(now, 'now')
    window = _timestamp(dispatch_window, 'dispatch_window')
    if window < 1.0 or window > MAX_DISPATCH_WINDOW_SECONDS:
        raise ValueError('dispatch_window must be from 1 to 120 seconds')
    _require_action_schema(connection)
    _require_exact_approved_confirmation(connection, record)
    if created_at < float(record.resolved_at):
        raise ActionConflictError('action creation predates approval')
    existing = find_action_for_confirmation(
        connection,
        record.confirmation_request_id,
    )
    if existing is not None:
        if not _action_matches_confirmation(existing, record):
            raise ActionConflictError(
                'confirmation is already bound to another action'
            )
        return existing
    forbidden = {
        record.confirmation_request_id,
        record.decision_id,
        record.request_id,
        record.response_id,
    }
    action_id = _new_server_id('action', forbidden)
    operation_id = _new_server_id(
        'operation',
        forbidden | {action_id},
    )
    binding = ActionBinding(
        confirmation_request_id=record.confirmation_request_id,
        proposal_fingerprint=record.proposal_fingerprint,
        arguments_digest=record.arguments_digest,
        target_binding_digest=record.target_binding_digest,
        user_id=record.user_id,
        conversation_id=record.conversation_id,
        session_instance_id=record.session_instance_id,
        generation=record.generation,
        conversation_revision=record.revision,
        decision_id=record.decision_id,
        tool_name=record.tool_name,
        arguments=record.arguments_dict(),
        target_room_name=record.target_room_name,
        target_room_category=record.target_room_category,
        confirmation_state_evidence_id=record.state_evidence_id,
        confirmation_state_observed_at=record.state_observed_at,
        confirmation_safety_policy_revision=(
            record.safety_policy_revision
        ),
    )
    action = RobotAction(
        action_id=action_id,
        operation_id=operation_id,
        binding=binding,
        state=ActionState.PENDING_PREFLIGHT,
        revision=1,
        created_at=created_at,
        updated_at=created_at,
        dispatch_expires_at=float(record.resolved_at) + window,
    )
    try:
        connection.execute(
            '''
            INSERT INTO robot_actions (
                schema_version, action_id, operation_id,
                confirmation_request_id, proposal_fingerprint,
                arguments_digest, arguments_json, target_binding_digest,
                user_id, conversation_id, session_instance_id, generation,
                conversation_revision, decision_id, tool_name,
                target_room_name, target_room_category,
                confirmation_state_evidence_id,
                confirmation_state_observed_at,
                confirmation_safety_policy_revision,
                state, revision, created_at, updated_at,
                dispatch_expires_at, result_code, simulation,
                physical_authorized
            ) VALUES (
                1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, NULL, 1, 0
            )
            ''',
            (
                action.action_id,
                action.operation_id,
                binding.confirmation_request_id,
                binding.proposal_fingerprint,
                binding.arguments_digest,
                _canonical_json(binding.arguments_dict()),
                binding.target_binding_digest,
                binding.user_id,
                binding.conversation_id,
                binding.session_instance_id,
                binding.generation,
                binding.conversation_revision,
                binding.decision_id,
                binding.tool_name,
                binding.target_room_name,
                binding.target_room_category,
                binding.confirmation_state_evidence_id,
                binding.confirmation_state_observed_at,
                binding.confirmation_safety_policy_revision,
                action.state.value,
                action.revision,
                action.created_at,
                action.updated_at,
                action.dispatch_expires_at,
            ),
        )
    except sqlite3.IntegrityError as error:
        replay = find_action_for_confirmation(
            connection,
            record.confirmation_request_id,
        )
        if replay is not None:
            if _action_matches_confirmation(replay, record):
                return replay
            raise ActionConflictError(
                'confirmation action replay binding changed'
            ) from error
        raise ActionConflictError('action identity conflicts') from error
    return action


class SQLiteActionRepository:
    """Separate SQLite connection for fenced simulation action workers."""

    def __init__(self, database_path: str) -> None:
        """Open a separate connection and initialize schema before serving."""
        if type(database_path) is not str or not database_path.strip():
            raise ValueError('database_path is invalid')
        self.database_path = database_path
        if database_path != ':memory:':
            Path(database_path).expanduser().parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
            resolved = str(Path(database_path).expanduser())
        else:
            resolved = database_path
        self._connection = sqlite3.connect(
            resolved,
            check_same_thread=False,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        with self._lock:
            if database_path != ':memory:':
                self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA foreign_keys=ON')
            self._connection.execute('PRAGMA busy_timeout=5000')
            initialize_action_schema(self._connection)
            self._connection.commit()
            self._secure_file_permissions()

    def _secure_file_permissions(self) -> None:
        if self.database_path == ':memory:':
            return
        expanded = str(Path(self.database_path).expanduser())
        for suffix in ('', '-wal', '-shm'):
            candidate = expanded + suffix
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def close(self) -> None:
        """Close the repository connection idempotently."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _begin(self) -> None:
        if self._closed:
            raise ActionPersistenceError('action repository is closed')
        self._connection.execute('BEGIN IMMEDIATE')

    def _select_action(self, action_id: str) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            'SELECT * FROM robot_actions WHERE action_id = ?',
            (action_id,),
        ).fetchone()

    def get(self, action_id: str) -> Optional[RobotAction]:
        """Return one validated action snapshot."""
        normalized = _identifier(action_id, 'action_id')
        with self._lock:
            row = self._select_action(normalized)
            return None if row is None else _action_from_row(dict(row))

    def find_by_confirmation(
        self,
        confirmation_request_id: str,
    ) -> Optional[RobotAction]:
        """Return the unique action copied from a confirmation."""
        normalized = _identifier(
            confirmation_request_id,
            'confirmation_request_id',
        )
        with self._lock:
            row = self._connection.execute(
                'SELECT * FROM robot_actions '
                'WHERE confirmation_request_id = ?',
                (normalized,),
            ).fetchone()
            return None if row is None else _action_from_row(dict(row))

    def _expire_unclaimed(self, now: float) -> None:
        rows = self._connection.execute(
            '''
            SELECT action_id, revision, created_at, updated_at
            FROM robot_actions
            WHERE state IN ('PENDING_PREFLIGHT', 'CLAIMED')
              AND dispatch_expires_at <= ?
            ''',
            (now,),
        ).fetchall()
        for row in rows:
            safe_now = max(
                now,
                float(row['created_at']),
                float(row['updated_at']),
            )
            self._connection.execute(
                '''
                UPDATE robot_actions
                SET state = 'BLOCKED', result_code = 'action_expired',
                    revision = revision + 1, updated_at = ?,
                    claim_worker_id = NULL, claim_token_digest = NULL,
                    lease_expires_at = NULL
                WHERE action_id = ? AND revision = ?
                  AND state IN ('PENDING_PREFLIGHT', 'CLAIMED')
                ''',
                (safe_now, row['action_id'], row['revision']),
            )

    def _block_clock_rollback(self, now: float) -> None:
        rows = self._connection.execute(
            '''
            SELECT action_id, revision, created_at, updated_at
            FROM robot_actions
            WHERE state IN ('PENDING_PREFLIGHT', 'CLAIMED')
              AND (? < created_at OR ? < updated_at)
            ''',
            (now, now),
        ).fetchall()
        for row in rows:
            safe_now = max(
                float(row['created_at']),
                float(row['updated_at']),
            )
            self._connection.execute(
                '''
                UPDATE robot_actions
                SET state = 'BLOCKED',
                    result_code = 'action_clock_rollback',
                    revision = revision + 1, updated_at = ?,
                    claim_worker_id = NULL, claim_token_digest = NULL,
                    lease_expires_at = NULL
                WHERE action_id = ? AND revision = ?
                  AND state IN ('PENDING_PREFLIGHT', 'CLAIMED')
                ''',
                (safe_now, row['action_id'], row['revision']),
            )

    def claim_next(
        self,
        worker_id: str,
        *,
        now: float,
        lease_for: float,
    ) -> Optional[ActionClaim]:
        """Claim pending work or replace only an expired preflight lease."""
        worker = _identifier(worker_id, 'worker_id')
        claimed_at = _timestamp(now, 'now')
        lease_seconds = _timestamp(lease_for, 'lease_for')
        if lease_seconds <= 0 or lease_seconds > MAX_LEASE_SECONDS:
            raise ValueError('lease_for must be from 0 to 300 seconds')
        with self._lock:
            self._begin()
            try:
                self._block_clock_rollback(claimed_at)
                self._expire_unclaimed(claimed_at)
                row = self._connection.execute(
                    '''
                    SELECT * FROM robot_actions
                    WHERE dispatch_expires_at > ? AND (
                        state = 'PENDING_PREFLIGHT'
                        OR (state = 'CLAIMED' AND lease_expires_at <= ?)
                    )
                    ORDER BY created_at, action_id
                    LIMIT 1
                    ''',
                    (claimed_at, claimed_at),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                token = secrets.token_urlsafe(32)
                digest = _token_digest(token)
                fence = int(row['claim_fence']) + 1
                lease_expires_at = claimed_at + lease_seconds
                cursor = self._connection.execute(
                    '''
                    UPDATE robot_actions
                    SET state = 'CLAIMED', revision = revision + 1,
                        updated_at = ?, claim_worker_id = ?,
                        claim_token_digest = ?, claim_fence = ?,
                        lease_expires_at = ?
                    WHERE action_id = ? AND revision = ?
                      AND dispatch_expires_at > ? AND (
                        state = 'PENDING_PREFLIGHT'
                        OR (state = 'CLAIMED' AND lease_expires_at <= ?)
                      )
                    ''',
                    (
                        claimed_at,
                        worker,
                        digest,
                        fence,
                        lease_expires_at,
                        row['action_id'],
                        row['revision'],
                        claimed_at,
                        claimed_at,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ActionConflictError('action claim CAS failed')
                claimed_row = self._select_action(row['action_id'])
                action = _action_from_row(dict(claimed_row))
                self._connection.commit()
                return ActionClaim(
                    action=action,
                    worker_id=worker,
                    claim_token=token,
                    fence=fence,
                    lease_expires_at=lease_expires_at,
                )
            except Exception:
                self._connection.rollback()
                raise

    def _require_claim(
        self,
        claim: ActionClaim,
        now: float,
        *,
        require_dispatch_fresh: bool = True,
    ) -> sqlite3.Row:
        if type(claim) is not ActionClaim:
            raise TypeError('claim must be an ActionClaim')
        row = self._select_action(claim.action.action_id)
        if row is None:
            raise ActionClaimLostError('claimed action no longer exists')
        if now < float(row['updated_at']):
            raise ActionClaimLostError('worker clock moved backward')
        matches = (
            row['state'] == 'CLAIMED'
            and int(row['revision']) == claim.action.revision
            and row['claim_worker_id'] == claim.worker_id
            and row['claim_token_digest']
            == _token_digest(claim.claim_token)
            and int(row['claim_fence']) == claim.fence
            and float(row['lease_expires_at']) > now
            and (
                not require_dispatch_fresh
                or float(row['dispatch_expires_at']) > now
            )
        )
        if not matches:
            raise ActionClaimLostError('action claim is stale or expired')
        return row

    def block(
        self,
        claim: ActionClaim,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        """Persist a definite failed preflight without external I/O."""
        code = _identifier(result_code, 'result_code')
        blocked_at = _timestamp(now, 'now')
        with self._lock:
            self._begin()
            try:
                row = self._require_claim(
                    claim,
                    blocked_at,
                    require_dispatch_fresh=False,
                )
                cursor = self._connection.execute(
                    '''
                    UPDATE robot_actions
                    SET state = 'BLOCKED', result_code = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE action_id = ? AND revision = ?
                      AND state = 'CLAIMED' AND claim_worker_id = ?
                      AND claim_token_digest = ? AND claim_fence = ?
                    ''',
                    (
                        code,
                        blocked_at,
                        row['action_id'],
                        row['revision'],
                        claim.worker_id,
                        _token_digest(claim.claim_token),
                        claim.fence,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ActionClaimLostError('block CAS failed')
                action = _action_from_row(dict(
                    self._select_action(row['action_id'])
                ))
                self._connection.commit()
                return action
            except Exception:
                self._connection.rollback()
                raise

    def record_dispatch_intent(
        self,
        claim: ActionClaim,
        authorization: DispatchAuthorization,
        *,
        now: float,
    ) -> DispatchIntent:
        """Persist fresh authority and intent before external I/O."""
        if type(authorization) is not DispatchAuthorization:
            raise TypeError(
                'authorization must be a DispatchAuthorization'
            )
        intent_at = _timestamp(now, 'now')
        with self._lock:
            self._begin()
            try:
                row = self._require_claim(claim, intent_at)
                if (
                    authorization.target_binding_digest
                    != row['target_binding_digest']
                    or authorization.state_observed_at
                    < float(row['created_at'])
                    or authorization.safety_policy_revision
                    != row['confirmation_safety_policy_revision']
                    or authorization.authorized_at
                    < float(row['created_at'])
                    or authorization.authorized_at > intent_at
                ):
                    raise ActionConflictError(
                        'dispatch authorization does not match action'
                    )
                intent_id = _new_server_id(
                    'intent',
                    {
                        row['action_id'],
                        row['operation_id'],
                        row['confirmation_request_id'],
                    },
                )
                token_digest = _token_digest(claim.claim_token)
                self._connection.execute(
                    '''
                    INSERT INTO execution_outbox (
                        schema_version, intent_id, action_id, operation_id,
                        state, worker_id, claim_token_digest, claim_fence,
                        state_evidence_id, state_observed_at,
                        safety_policy_revision, target_binding_digest,
                        authorized_at, simulation, physical_authorized,
                        result_code, created_at, updated_at
                    ) VALUES (
                        1, ?, ?, ?, 'DISPATCH_INTENT', ?, ?, ?, ?, ?, ?, ?,
                        ?, 1, 0, NULL, ?, ?
                    )
                    ''',
                    (
                        intent_id,
                        row['action_id'],
                        row['operation_id'],
                        claim.worker_id,
                        token_digest,
                        claim.fence,
                        authorization.state_evidence_id,
                        authorization.state_observed_at,
                        authorization.safety_policy_revision,
                        authorization.target_binding_digest,
                        authorization.authorized_at,
                        intent_at,
                        intent_at,
                    ),
                )
                cursor = self._connection.execute(
                    '''
                    UPDATE robot_actions
                    SET state = 'DISPATCH_INTENT', revision = revision + 1,
                        updated_at = ?, dispatch_state_evidence_id = ?,
                        dispatch_state_observed_at = ?,
                        dispatch_safety_policy_revision = ?,
                        dispatch_target_binding_digest = ?,
                        dispatch_authorized_at = ?
                    WHERE action_id = ? AND revision = ?
                      AND state = 'CLAIMED' AND claim_worker_id = ?
                      AND claim_token_digest = ? AND claim_fence = ?
                      AND lease_expires_at > ?
                      AND dispatch_expires_at > ?
                    ''',
                    (
                        intent_at,
                        authorization.state_evidence_id,
                        authorization.state_observed_at,
                        authorization.safety_policy_revision,
                        authorization.target_binding_digest,
                        authorization.authorized_at,
                        row['action_id'],
                        row['revision'],
                        claim.worker_id,
                        token_digest,
                        claim.fence,
                        intent_at,
                        intent_at,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ActionClaimLostError('dispatch intent CAS failed')
                action = _action_from_row(dict(
                    self._select_action(row['action_id'])
                ))
                self._connection.commit()
                return DispatchIntent(
                    action=action,
                    intent_id=intent_id,
                    worker_id=claim.worker_id,
                    claim_token=claim.claim_token,
                    fence=claim.fence,
                )
            except Exception:
                self._connection.rollback()
                raise

    def _require_intent(
        self,
        intent: DispatchIntent,
        now: float,
        expected_states: set[str],
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if type(intent) is not DispatchIntent:
            raise TypeError('intent must be a DispatchIntent')
        row = self._select_action(intent.action.action_id)
        outbox = self._connection.execute(
            'SELECT * FROM execution_outbox WHERE intent_id = ?',
            (intent.intent_id,),
        ).fetchone()
        if row is None or outbox is None:
            raise ActionClaimLostError('dispatch intent is missing')
        current_action = _action_from_row(dict(row))
        token_digest = _token_digest(intent.claim_token)
        if now < float(row['updated_at']):
            raise ActionClaimLostError('worker clock moved backward')
        matches = (
            row['state'] in expected_states
            and int(row['revision']) == intent.action.revision
            and current_action.action_id == intent.action.action_id
            and current_action.operation_id == intent.action.operation_id
            and current_action.state is intent.action.state
            and outbox['intent_id'] == intent.intent_id
            and row['claim_worker_id'] == intent.worker_id
            and row['claim_token_digest'] == token_digest
            and int(row['claim_fence']) == intent.fence
            and float(row['lease_expires_at']) > now
            and outbox['worker_id'] == intent.worker_id
            and outbox['claim_token_digest'] == token_digest
            and int(outbox['claim_fence']) == intent.fence
            and _outbox_matches_action_row(outbox, row)
            and (
                (row['state'] == 'DISPATCH_INTENT'
                 and outbox['state'] == 'DISPATCH_INTENT')
                or
                (row['state'] == 'STARTED'
                 and outbox['state'] == 'STARTED')
            )
        )
        if not matches:
            raise ActionClaimLostError('dispatch intent ownership changed')
        return row, outbox

    def mark_started(
        self,
        intent: DispatchIntent,
        *,
        now: float,
    ) -> DispatchIntent:
        """Record a known accepted operation without creating a new send."""
        started_at = _timestamp(now, 'now')
        with self._lock:
            self._begin()
            try:
                row, _outbox = self._require_intent(
                    intent,
                    started_at,
                    {'DISPATCH_INTENT'},
                )
                cursor = self._connection.execute(
                    '''
                    UPDATE robot_actions
                    SET state = 'STARTED', revision = revision + 1,
                        updated_at = ?
                    WHERE action_id = ? AND revision = ?
                      AND state = 'DISPATCH_INTENT'
                    ''',
                    (started_at, row['action_id'], row['revision']),
                )
                outbox_cursor = self._connection.execute(
                    '''
                    UPDATE execution_outbox
                    SET state = 'STARTED', updated_at = ?
                    WHERE intent_id = ? AND state = 'DISPATCH_INTENT'
                    ''',
                    (started_at, intent.intent_id),
                )
                if cursor.rowcount != 1 or outbox_cursor.rowcount != 1:
                    raise ActionClaimLostError('start CAS failed')
                action = _action_from_row(dict(
                    self._select_action(row['action_id'])
                ))
                self._connection.commit()
                return DispatchIntent(
                    action=action,
                    intent_id=intent.intent_id,
                    worker_id=intent.worker_id,
                    claim_token=intent.claim_token,
                    fence=intent.fence,
                )
            except Exception:
                self._connection.rollback()
                raise

    def finish(
        self,
        intent: DispatchIntent,
        state: ActionState,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        """Persist a known terminal result under the original fence."""
        if type(state) is not ActionState:
            try:
                state = ActionState(state)
            except (TypeError, ValueError):
                raise ValueError('terminal action state is invalid') from None
        if state not in {
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.CANCELED,
            ActionState.UNKNOWN,
        }:
            raise ValueError('finish state is unsupported')
        code = _identifier(result_code, 'result_code')
        finished_at = _timestamp(now, 'now')
        with self._lock:
            self._begin()
            try:
                row, _outbox = self._require_intent(
                    intent,
                    finished_at,
                    {'DISPATCH_INTENT', 'STARTED'},
                )
                cursor = self._connection.execute(
                    '''
                    UPDATE robot_actions
                    SET state = ?, result_code = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE action_id = ? AND revision = ?
                      AND state IN ('DISPATCH_INTENT', 'STARTED')
                    ''',
                    (
                        state.value,
                        code,
                        finished_at,
                        row['action_id'],
                        row['revision'],
                    ),
                )
                outbox_state = (
                    'UNKNOWN'
                    if state is ActionState.UNKNOWN
                    else 'TERMINAL'
                )
                outbox_cursor = self._connection.execute(
                    '''
                    UPDATE execution_outbox
                    SET state = ?, result_code = ?, updated_at = ?
                    WHERE intent_id = ?
                      AND state IN ('DISPATCH_INTENT', 'STARTED')
                    ''',
                    (
                        outbox_state,
                        code,
                        finished_at,
                        intent.intent_id,
                    ),
                )
                if cursor.rowcount != 1 or outbox_cursor.rowcount != 1:
                    raise ActionClaimLostError('terminal CAS failed')
                action = _action_from_row(dict(
                    self._select_action(row['action_id'])
                ))
                self._connection.commit()
                return action
            except Exception:
                self._connection.rollback()
                raise

    def recover_uncertain_after_restart(self, *, now: float) -> int:
        """Mark previously sent work UNKNOWN and never make it claimable."""
        recovered_at = _timestamp(now, 'now')
        with self._lock:
            self._begin()
            try:
                rows = self._connection.execute(
                    '''
                    SELECT action_id, revision, state, updated_at
                    FROM robot_actions
                    WHERE state IN ('DISPATCH_INTENT', 'STARTED')
                      AND lease_expires_at <= ?
                    ''',
                    (recovered_at,),
                ).fetchall()
                count = 0
                for row in rows:
                    safe_now = max(recovered_at, float(row['updated_at']))
                    outbox = self._connection.execute(
                        'SELECT * FROM execution_outbox '
                        'WHERE action_id = ?',
                        (row['action_id'],),
                    ).fetchone()
                    full_row = self._select_action(row['action_id'])
                    if (
                        outbox is None
                        or full_row is None
                        or _action_from_row(dict(full_row)).state.value
                        != row['state']
                        or not _outbox_matches_action_row(
                            outbox,
                            full_row,
                        )
                        or outbox['state'] != row['state']
                    ):
                        raise ActionPersistenceError(
                            'sent action and outbox binding diverged'
                        )
                    cursor = self._connection.execute(
                        '''
                        UPDATE robot_actions
                        SET state = 'UNKNOWN',
                            result_code =
                                'dispatch_outcome_unknown_after_restart',
                            revision = revision + 1, updated_at = ?
                        WHERE action_id = ? AND revision = ?
                          AND state IN ('DISPATCH_INTENT', 'STARTED')
                          AND lease_expires_at <= ?
                        ''',
                        (
                            safe_now,
                            row['action_id'],
                            row['revision'],
                            recovered_at,
                        ),
                    )
                    if cursor.rowcount == 1:
                        outbox_cursor = self._connection.execute(
                            '''
                            UPDATE execution_outbox
                            SET state = 'UNKNOWN',
                                result_code =
                                  'dispatch_outcome_unknown_after_restart',
                                updated_at = ?
                            WHERE action_id = ?
                              AND state IN ('DISPATCH_INTENT', 'STARTED')
                            ''',
                            (safe_now, row['action_id']),
                        )
                        if outbox_cursor.rowcount != 1:
                            raise ActionPersistenceError(
                                'restart recovery outbox CAS failed'
                            )
                        count += 1
                    else:
                        raise ActionConflictError(
                            'restart recovery action CAS failed'
                        )
                self._connection.commit()
                return count
            except Exception:
                self._connection.rollback()
                raise
