"""Trusted, structured conversation results derived from terminal receipts."""

import hashlib
import json
import math
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from malbut_agent_server.execution_ledger import (
    SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL,
    _record_from_row,
)
from malbut_agent_server.monitor_room_coverage import (
    DEFAULT_COVERAGE_PROFILE,
    PLANNER_REVISION,
)
from malbut_agent_server.schemas import (
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)


TRUSTED_RESULT_SCHEMA_VERSION = 1
TRUSTED_RESULT_SOURCE = 'monitor_room_simulation'
TRUSTED_RESULT_ACTIVATION_SENTINEL = hashlib.sha256(
    b'malbut-monitor-room-trusted-result-activation-v1'
).hexdigest()

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


TRUSTED_RESULT_METADATA_TABLE_SQL = '''
CREATE TABLE monitor_room_trusted_result_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    activated_at REAL NOT NULL,
    activation_epoch TEXT NOT NULL,
    terminal_rowid_cutoff INTEGER NOT NULL,
    CHECK (
        typeof(activated_at) IN ('integer', 'real')
        AND activated_at >= 0
        AND activated_at <= 1.7976931348623157e308
    ),
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(terminal_rowid_cutoff) = 'integer'
        AND terminal_rowid_cutoff >= 0
    )
)
'''


TRUSTED_RESULTS_TABLE_SQL = f'''
CREATE TABLE conversation_trusted_tool_results (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    trusted_result_id TEXT NOT NULL PRIMARY KEY,
    trusted_result_fingerprint TEXT NOT NULL UNIQUE,
    terminal_rowid INTEGER NOT NULL UNIQUE,
    confirmation_request_id TEXT NOT NULL UNIQUE,
    receipt_digest TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    source_revision INTEGER NOT NULL,
    source_ordinal INTEGER NOT NULL,
    source_turn_id TEXT NOT NULL,
    record_kind TEXT NOT NULL CHECK (
        record_kind IN ('planned', 'planning_failed')
    ),
    state TEXT NOT NULL CHECK (state IN ('succeeded', 'failed')),
    result_code TEXT NOT NULL,
    planner_revision TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    plan_digest TEXT,
    result_digest TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    component_count INTEGER NOT NULL,
    completed_at REAL NOT NULL,
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL DEFAULT 0
        CHECK (physical_effects = 0),
    viewer_live INTEGER NOT NULL DEFAULT 0 CHECK (viewer_live = 0),
    nav2_validated INTEGER NOT NULL DEFAULT 0 CHECK (nav2_validated = 0),
    camera_coverage_validated INTEGER NOT NULL DEFAULT 0
        CHECK (camera_coverage_validated = 0),
    coverage_achieved INTEGER NOT NULL DEFAULT 0
        CHECK (coverage_achieved = 0),
    execution_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (execution_authorized = 0),
    FOREIGN KEY (confirmation_request_id)
        REFERENCES monitor_room_simulation_ledger (
            confirmation_request_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (confirmation_request_id)
        REFERENCES confirmation_intents (
            confirmation_request_id
        ) ON DELETE CASCADE,
    FOREIGN KEY (user_id, conversation_id)
        REFERENCES conversation_sessions (user_id, conversation_id)
        ON DELETE CASCADE,
    FOREIGN KEY (
        user_id, conversation_id, generation, source_turn_id
    ) REFERENCES conversation_turns (
        user_id, conversation_id, generation, turn_id
    ) ON DELETE CASCADE,
    CHECK (typeof(terminal_rowid) = 'integer' AND terminal_rowid > 0),
    CHECK (typeof(generation) = 'integer' AND generation >= 1),
    CHECK (typeof(source_revision) = 'integer' AND source_revision >= 1),
    CHECK (typeof(source_ordinal) = 'integer' AND source_ordinal >= 1),
    CHECK (
        length(trusted_result_fingerprint) = 64
        AND trusted_result_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(receipt_digest) = 64
        AND receipt_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(profile_digest) = 64
        AND profile_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        plan_digest IS NULL OR (
            length(plan_digest) = 64
            AND plan_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        length(result_digest) = 64
        AND result_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(completed_at) IN ('integer', 'real')
        AND completed_at >= 0
        AND completed_at <= 1.7976931348623157e308
    ),
    CHECK (
        (record_kind = 'planned'
         AND state = 'succeeded'
         AND result_code = 'semantic_sample_plan_created'
         AND planner_revision = '{PLANNER_REVISION}'
         AND profile_digest = '{DEFAULT_COVERAGE_PROFILE.digest}'
         AND plan_digest IS NOT NULL
         AND typeof(sample_count) = 'integer'
         AND sample_count BETWEEN 1 AND 4096
         AND typeof(component_count) = 'integer'
         AND component_count BETWEEN 1 AND 128)
        OR
        (record_kind = 'planning_failed'
         AND state = 'failed'
         AND result_code IN (
             'semantic_sample_planning_failed',
             'semantic_sample_result_invalid'
         )
         AND planner_revision = '{PLANNER_REVISION}'
         AND profile_digest = '{DEFAULT_COVERAGE_PROFILE.digest}'
         AND plan_digest IS NULL
         AND typeof(sample_count) = 'integer'
         AND sample_count = 0
         AND typeof(component_count) = 'integer'
         AND component_count = 0)
    )
)
'''


TRUSTED_RESULTS_OWNER_INDEX_SQL = '''
CREATE INDEX conversation_trusted_tool_results_owner_idx
ON conversation_trusted_tool_results (
    user_id,
    conversation_id,
    session_instance_id,
    generation,
    source_ordinal,
    trusted_result_id
)
'''


TRUSTED_RESULT_INSERT_GUARD_SQL = '''
CREATE TRIGGER conversation_trusted_tool_result_insert_guard
BEFORE INSERT ON conversation_trusted_tool_results
WHEN NOT EXISTS (
    SELECT 1
    FROM monitor_room_simulation_ledger AS terminal
    JOIN confirmation_intents AS confirmation
      ON confirmation.confirmation_request_id =
         terminal.confirmation_request_id
    JOIN conversation_sessions AS session
      ON session.user_id = confirmation.user_id
     AND session.conversation_id = confirmation.conversation_id
    JOIN conversation_turns AS source_turn
      ON source_turn.user_id = confirmation.user_id
     AND source_turn.conversation_id = confirmation.conversation_id
     AND source_turn.generation = confirmation.generation
     AND source_turn.turn_id = confirmation.turn_id
    WHERE terminal.rowid = NEW.terminal_rowid
      AND terminal.schema_version = 4
      AND terminal.record_kind IN ('planned', 'planning_failed')
      AND terminal.confirmation_request_id = NEW.confirmation_request_id
      AND terminal.receipt_digest = NEW.receipt_digest
      AND terminal.record_kind = NEW.record_kind
      AND terminal.state = NEW.state
      AND terminal.result_code = NEW.result_code
      AND terminal.planner_revision = NEW.planner_revision
      AND terminal.profile_digest = NEW.profile_digest
      AND terminal.plan_digest IS NEW.plan_digest
      AND terminal.result_digest = NEW.result_digest
      AND terminal.sample_count = NEW.sample_count
      AND terminal.component_count = NEW.component_count
      AND terminal.completed_at = NEW.completed_at
      AND terminal.simulation = NEW.simulation
      AND terminal.physical_authorized = NEW.physical_authorized
      AND terminal.physical_effects = NEW.physical_effects
      AND terminal.viewer_live = NEW.viewer_live
      AND terminal.nav2_validated = NEW.nav2_validated
      AND terminal.camera_coverage_validated =
          NEW.camera_coverage_validated
      AND terminal.coverage_achieved = NEW.coverage_achieved
      AND confirmation.user_id = NEW.user_id
      AND confirmation.conversation_id = NEW.conversation_id
      AND confirmation.session_instance_id = NEW.session_instance_id
      AND confirmation.generation = NEW.generation
      AND confirmation.revision = NEW.source_revision
      AND confirmation.ordinal = NEW.source_ordinal
      AND confirmation.turn_id = NEW.source_turn_id
      AND session.session_instance_id = NEW.session_instance_id
      AND session.generation = NEW.generation
      AND session.revision = NEW.source_revision
      AND session.status = 'active'
      AND source_turn.session_instance_id = NEW.session_instance_id
      AND source_turn.ordinal = NEW.source_ordinal
      AND source_turn.status = 'completed'
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result source is invalid');
END
'''


TRUSTED_RESULT_NO_UPDATE_SQL = '''
CREATE TRIGGER conversation_trusted_tool_result_no_update
BEFORE UPDATE ON conversation_trusted_tool_results
BEGIN
    SELECT RAISE(ABORT, 'trusted result is immutable');
END
'''


TRUSTED_RESULT_NO_REPLACE_SQL = '''
CREATE TRIGGER conversation_trusted_tool_result_no_replace
BEFORE INSERT ON conversation_trusted_tool_results
WHEN EXISTS (
    SELECT 1 FROM conversation_trusted_tool_results
    WHERE trusted_result_id = NEW.trusted_result_id
       OR trusted_result_fingerprint = NEW.trusted_result_fingerprint
       OR terminal_rowid = NEW.terminal_rowid
       OR confirmation_request_id = NEW.confirmation_request_id
       OR receipt_digest = NEW.receipt_digest
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result identity is immutable');
END
'''


TRUSTED_RESULT_METADATA_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_trusted_result_metadata_no_update
BEFORE UPDATE ON monitor_room_trusted_result_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'trusted result metadata is immutable');
END
'''


TRUSTED_RESULT_METADATA_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_trusted_result_metadata_no_delete
BEFORE DELETE ON monitor_room_trusted_result_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'trusted result metadata is immutable');
END
'''


TRUSTED_RESULT_METADATA_NO_REPLACE_SQL = '''
CREATE TRIGGER monitor_room_trusted_result_metadata_no_replace
BEFORE INSERT ON monitor_room_trusted_result_schema_metadata
WHEN EXISTS (
    SELECT 1 FROM monitor_room_trusted_result_schema_metadata
    WHERE singleton = NEW.singleton
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result metadata is immutable');
END
'''


class TrustedResultSchemaError(RuntimeError):
    """Raised when the trusted-result schema or rows are incompatible."""


@dataclass(frozen=True)
class TrustedToolResult:
    """One server-derived, non-authorizing structured tool result."""

    trusted_result_id: str
    trusted_result_fingerprint: str = field(repr=False)
    user_id: str = field(repr=False)
    conversation_id: str = field(repr=False)
    session_instance_id: str = field(repr=False)
    generation: int = field(repr=False)
    source_revision: int = field(repr=False)
    source_turn_id: str = field(repr=False)
    source_ordinal: int = field(repr=False)
    record_kind: str
    state: str
    result_code: str
    planner_revision: str
    profile_digest: str = field(repr=False)
    plan_digest: Optional[str] = field(repr=False)
    result_digest: str = field(repr=False)
    sample_count: int
    component_count: int
    completed_at: float
    schema_version: int = TRUSTED_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject construction outside the closed trusted-result contract."""
        try:
            normalized_user = validate_user_id(self.user_id)
            normalized_conversation = validate_conversation_id(
                self.conversation_id
            )
            normalized_turn = validate_turn_id(self.source_turn_id)
        except ValidationError:
            raise ValidationError('trusted tool result is invalid') from None
        if (
            type(self.schema_version) is not int
            or self.schema_version != TRUSTED_RESULT_SCHEMA_VERSION
            or not isinstance(self.trusted_result_id, str)
            or not self.trusted_result_id.startswith(
                'trusted-tool-result-'
            )
            or not _SAFE_IDENTIFIER.fullmatch(self.trusted_result_id)
            or not isinstance(self.trusted_result_fingerprint, str)
            or not _HEX_DIGEST.fullmatch(self.trusted_result_fingerprint)
            or not isinstance(self.session_instance_id, str)
            or not 1 <= len(self.session_instance_id) <= 128
            or not _SAFE_IDENTIFIER.fullmatch(self.session_instance_id)
            or normalized_user != self.user_id
            or normalized_conversation != self.conversation_id
            or normalized_turn != self.source_turn_id
            or type(self.generation) is not int
            or self.generation < 1
            or type(self.source_ordinal) is not int
            or self.source_ordinal < 1
            or type(self.source_revision) is not int
            or self.source_revision < 1
            or self.planner_revision != PLANNER_REVISION
            or self.profile_digest != DEFAULT_COVERAGE_PROFILE.digest
            or not isinstance(self.result_digest, str)
            or not _HEX_DIGEST.fullmatch(self.result_digest)
            or (
                self.plan_digest is not None
                and (
                    not isinstance(self.plan_digest, str)
                    or not _HEX_DIGEST.fullmatch(self.plan_digest)
                )
            )
            or isinstance(self.completed_at, bool)
            or not isinstance(self.completed_at, (int, float))
            or not math.isfinite(float(self.completed_at))
            or self.completed_at < 0
        ):
            raise ValidationError('trusted tool result is invalid')
        planned = (
            self.record_kind == 'planned'
            and self.state == 'succeeded'
            and self.result_code == 'semantic_sample_plan_created'
            and self.plan_digest is not None
            and type(self.sample_count) is int
            and 1 <= self.sample_count <= 4096
            and type(self.component_count) is int
            and 1 <= self.component_count <= 128
        )
        failed = (
            self.record_kind == 'planning_failed'
            and self.state == 'failed'
            and self.result_code in {
                'semantic_sample_planning_failed',
                'semantic_sample_result_invalid',
            }
            and self.plan_digest is None
            and type(self.sample_count) is int
            and self.sample_count == 0
            and type(self.component_count) is int
            and self.component_count == 0
        )
        if not (planned or failed):
            raise ValidationError('trusted tool result is invalid')
        if self.completed_at == 0:
            object.__setattr__(self, 'completed_at', 0.0)

    def to_prompt_dict(self) -> Dict[str, Any]:
        """Return the closed, identifier-free trusted prompt projection."""
        return {
            'schema_version': self.schema_version,
            'source': TRUSTED_RESULT_SOURCE,
            'tool_name': 'monitor_room',
            'record_kind': self.record_kind,
            'state': self.state,
            'code': self.result_code,
            'completed_at': self.completed_at,
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'nav2_validated': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
            'execution_authorized': False,
            'coverage_plan': {
                'planner_revision': self.planner_revision,
                'sample_count': self.sample_count,
                'component_count': self.component_count,
            },
        }


def _canonical_hash(value: Dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: Any, name: str) -> float:
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise TrustedResultSchemaError(f'{name} is invalid') from None
    if not math.isfinite(normalized) or normalized < 0:
        raise TrustedResultSchemaError(f'{name} is invalid')
    return 0.0 if normalized == 0 else normalized


def _owner_binding_digest(row: sqlite3.Row) -> str:
    return _canonical_hash(
        {
            'user_id': row['user_id'],
            'conversation_id': row['conversation_id'],
            'session_instance_id': row['session_instance_id'],
            'generation': int(row['generation']),
            'revision': int(row['revision']),
            'ordinal': int(row['ordinal']),
        }
    )


def _fingerprint_body(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'schema_version': TRUSTED_RESULT_SCHEMA_VERSION,
        'source': TRUSTED_RESULT_SOURCE,
        'tool_name': 'monitor_room',
        'confirmation_request_id': values['confirmation_request_id'],
        'receipt_digest': values['receipt_digest'],
        'user_id': values['user_id'],
        'conversation_id': values['conversation_id'],
        'session_instance_id': values['session_instance_id'],
        'generation': values['generation'],
        'source_revision': values['source_revision'],
        'source_ordinal': values['source_ordinal'],
        'source_turn_id': values['source_turn_id'],
        'record_kind': values['record_kind'],
        'state': values['state'],
        'code': values['result_code'],
        'planner_revision': values['planner_revision'],
        'profile_digest': values['profile_digest'],
        'plan_digest': values['plan_digest'],
        'result_digest': values['result_digest'],
        'sample_count': values['sample_count'],
        'component_count': values['component_count'],
        'completed_at': values['completed_at'],
        'simulation': True,
        'physical_authorized': False,
        'physical_effects': False,
        'viewer_live': False,
        'nav2_validated': False,
        'camera_coverage_validated': False,
        'coverage_achieved': False,
        'execution_authorized': False,
    }


def _trusted_result_fingerprint(values: Dict[str, Any]) -> str:
    return _canonical_hash(_fingerprint_body(values))


def _trusted_result_id(fingerprint: str) -> str:
    digest = hashlib.sha256(
        (
            'monitor-room-trusted-result-id-v1\0' + fingerprint
        ).encode('utf-8')
    ).hexdigest()[:40]
    return f'trusted-tool-result-{digest}'


def _expected_objects() -> Dict[str, Tuple[str, str]]:
    return {
        'monitor_room_trusted_result_schema_metadata': (
            'table', TRUSTED_RESULT_METADATA_TABLE_SQL
        ),
        'conversation_trusted_tool_results': (
            'table', TRUSTED_RESULTS_TABLE_SQL
        ),
        'conversation_trusted_tool_results_owner_idx': (
            'index', TRUSTED_RESULTS_OWNER_INDEX_SQL
        ),
        'conversation_trusted_tool_result_insert_guard': (
            'trigger', TRUSTED_RESULT_INSERT_GUARD_SQL
        ),
        'conversation_trusted_tool_result_no_update': (
            'trigger', TRUSTED_RESULT_NO_UPDATE_SQL
        ),
        'conversation_trusted_tool_result_no_replace': (
            'trigger', TRUSTED_RESULT_NO_REPLACE_SQL
        ),
        'monitor_room_trusted_result_metadata_no_update': (
            'trigger', TRUSTED_RESULT_METADATA_NO_UPDATE_SQL
        ),
        'monitor_room_trusted_result_metadata_no_delete': (
            'trigger', TRUSTED_RESULT_METADATA_NO_DELETE_SQL
        ),
        'monitor_room_trusted_result_metadata_no_replace': (
            'trigger', TRUSTED_RESULT_METADATA_NO_REPLACE_SQL
        ),
    }


def prepare_trusted_result_schema_locked(
    connection: sqlite3.Connection,
    *,
    activated_at: float,
) -> None:
    """Create or exactly validate the independent trusted-result schema."""
    if not connection.in_transaction:
        raise TrustedResultSchemaError(
            'trusted result schema requires a write transaction'
        )
    expected = _expected_objects()
    placeholders = ','.join('?' for _ in expected)
    rows = connection.execute(
        f'''
        SELECT name, type FROM sqlite_master
        WHERE name IN ({placeholders})
        ''',
        tuple(expected),
    ).fetchall()
    sentinel = connection.execute(
        '''
        SELECT proposal_fingerprint
        FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (TRUSTED_RESULT_ACTIVATION_SENTINEL,),
    ).fetchone()
    if not rows:
        if sentinel is not None:
            raise TrustedResultSchemaError(
                'trusted result schema was removed after activation'
            )
        normalized_time = _timestamp(activated_at, 'activated_at')
        cutoff_row = connection.execute(
            '''
            SELECT COALESCE(MAX(rowid), 0) AS terminal_rowid_cutoff
            FROM monitor_room_simulation_ledger
            '''
        ).fetchone()
        cutoff = int(cutoff_row['terminal_rowid_cutoff'])
        connection.execute(TRUSTED_RESULT_METADATA_TABLE_SQL)
        connection.execute(TRUSTED_RESULTS_TABLE_SQL)
        connection.execute(TRUSTED_RESULTS_OWNER_INDEX_SQL)
        connection.execute(TRUSTED_RESULT_INSERT_GUARD_SQL)
        connection.execute(TRUSTED_RESULT_NO_UPDATE_SQL)
        connection.execute(TRUSTED_RESULT_NO_REPLACE_SQL)
        connection.execute(TRUSTED_RESULT_METADATA_NO_UPDATE_SQL)
        connection.execute(TRUSTED_RESULT_METADATA_NO_DELETE_SQL)
        connection.execute(TRUSTED_RESULT_METADATA_NO_REPLACE_SQL)
        connection.execute(
            '''
            INSERT INTO monitor_room_trusted_result_schema_metadata (
                singleton, schema_version, activated_at,
                activation_epoch, terminal_rowid_cutoff
            ) VALUES (1, 1, ?, ?, ?)
            ''',
            (normalized_time, secrets.token_hex(32), cutoff),
        )
    elif {str(row['name']) for row in rows} != set(expected):
        raise TrustedResultSchemaError(
            'trusted result schema is incomplete'
        )
    elif sentinel is None:
        result_count = connection.execute(
            'SELECT COUNT(*) FROM conversation_trusted_tool_results'
        ).fetchone()[0]
        eligible_terminal_count = connection.execute(
            '''
            SELECT COUNT(*) FROM monitor_room_simulation_ledger
            WHERE schema_version = 4
              AND record_kind IN ('planned', 'planning_failed')
            '''
        ).fetchone()[0]
        if result_count != 0 or eligible_terminal_count != 0:
            raise TrustedResultSchemaError(
                'trusted result activation anchor is missing'
            )
    if sentinel is None:
        simulation_metadata = connection.execute(
            '''
            SELECT activation_epoch, activated_at
            FROM monitor_room_simulation_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if simulation_metadata is None:
            raise TrustedResultSchemaError(
                'simulation activation metadata is missing'
            )
        trusted_metadata = connection.execute(
            '''
            SELECT terminal_rowid_cutoff
            FROM monitor_room_trusted_result_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if trusted_metadata is None:
            raise TrustedResultSchemaError(
                'trusted result activation metadata is missing'
            )
        connection.execute(
            'DROP TRIGGER monitor_room_simulation_preactivation_no_insert'
        )
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_preactivation_proposals (
                proposal_fingerprint, activation_epoch,
                snapshot_rowid, snapshotted_at
            ) VALUES (?, ?, ?, ?)
            ''',
            (
                TRUSTED_RESULT_ACTIVATION_SENTINEL,
                simulation_metadata['activation_epoch'],
                int(trusted_metadata['terminal_rowid_cutoff']) + 1,
                simulation_metadata['activated_at'],
            ),
        )
        connection.execute(
            SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL
        )
    validate_trusted_result_schema_locked(connection)


def validate_trusted_result_schema_locked(
    connection: sqlite3.Connection,
) -> None:
    """Fail closed on DDL, ownership, shape, or digest drift."""
    expected = _expected_objects()
    sentinel = connection.execute(
        '''
        SELECT sentinel.*, metadata.activation_epoch AS current_epoch,
               metadata.activated_at AS current_activated_at
        FROM monitor_room_simulation_preactivation_proposals AS sentinel
        CROSS JOIN monitor_room_simulation_schema_metadata AS metadata
        WHERE sentinel.proposal_fingerprint = ?
          AND metadata.singleton = 1
        ''',
        (TRUSTED_RESULT_ACTIVATION_SENTINEL,),
    ).fetchone()
    trusted_metadata = connection.execute(
        '''
        SELECT terminal_rowid_cutoff
        FROM monitor_room_trusted_result_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    if (
        sentinel is None
        or trusted_metadata is None
        or sentinel['activation_epoch'] != sentinel['current_epoch']
        or sentinel['snapshotted_at']
        != sentinel['current_activated_at']
        or type(sentinel['snapshot_rowid']) is not int
        or sentinel['snapshot_rowid']
        != int(trusted_metadata['terminal_rowid_cutoff']) + 1
    ):
        raise TrustedResultSchemaError(
            'trusted result activation anchor is incompatible'
        )
    for name, (object_type, exact_sql) in expected.items():
        row = connection.execute(
            'SELECT type, sql FROM sqlite_master WHERE name = ?',
            (name,),
        ).fetchone()
        if (
            row is None
            or row['type'] != object_type
            or str(row['sql']).strip() != exact_sql.strip()
        ):
            raise TrustedResultSchemaError(
                'trusted result schema is incompatible'
            )
    custom = {
        (str(row['type']), str(row['name']), str(row['tbl_name']))
        for row in connection.execute(
            '''
            SELECT type, name, tbl_name FROM sqlite_master
            WHERE type IN ('index', 'trigger')
              AND tbl_name IN (
                  'monitor_room_trusted_result_schema_metadata',
                  'conversation_trusted_tool_results'
              )
              AND sql IS NOT NULL
            '''
        ).fetchall()
    }
    expected_custom = {
        (object_type, name, (
            'monitor_room_trusted_result_schema_metadata'
            if name.startswith('monitor_room_trusted_result_metadata_')
            else 'conversation_trusted_tool_results'
        ))
        for name, (object_type, _sql) in expected.items()
        if object_type in {'index', 'trigger'}
    }
    if custom != expected_custom:
        raise TrustedResultSchemaError(
            'trusted result schema has unexpected objects'
        )
    metadata = connection.execute(
        '''
        SELECT *, typeof(singleton) AS singleton_type,
               typeof(schema_version) AS schema_version_type,
               typeof(activated_at) AS activated_at_type,
               typeof(activation_epoch) AS activation_epoch_type,
               typeof(terminal_rowid_cutoff) AS cutoff_type
        FROM monitor_room_trusted_result_schema_metadata
        '''
    ).fetchall()
    if (
        len(metadata) != 1
        or metadata[0]['singleton'] != 1
        or metadata[0]['schema_version'] != 1
        or metadata[0]['singleton_type'] != 'integer'
        or metadata[0]['schema_version_type'] != 'integer'
        or metadata[0]['activated_at_type'] not in ('integer', 'real')
        or metadata[0]['activation_epoch_type'] != 'text'
        or metadata[0]['cutoff_type'] != 'integer'
        or not math.isfinite(float(metadata[0]['activated_at']))
        or float(metadata[0]['activated_at']) < 0
        or not _HEX_DIGEST.fullmatch(metadata[0]['activation_epoch'])
        or int(metadata[0]['terminal_rowid_cutoff']) < 0
    ):
        raise TrustedResultSchemaError(
            'trusted result metadata is incompatible'
        )
    expected_foreign_keys = {
        (
            'monitor_room_simulation_ledger',
            'confirmation_request_id',
            'confirmation_request_id',
            'RESTRICT',
        ),
        (
            'confirmation_intents',
            'confirmation_request_id',
            'confirmation_request_id',
            'CASCADE',
        ),
        ('conversation_sessions', 'user_id', 'user_id', 'CASCADE'),
        (
            'conversation_sessions',
            'conversation_id',
            'conversation_id',
            'CASCADE',
        ),
        ('conversation_turns', 'user_id', 'user_id', 'CASCADE'),
        (
            'conversation_turns',
            'conversation_id',
            'conversation_id',
            'CASCADE',
        ),
        ('conversation_turns', 'generation', 'generation', 'CASCADE'),
        ('conversation_turns', 'source_turn_id', 'turn_id', 'CASCADE'),
    }
    actual_foreign_keys = {
        (
            str(row['table']),
            str(row['from']),
            str(row['to']),
            str(row['on_delete']).upper(),
        )
        for row in connection.execute(
            'PRAGMA foreign_key_list(conversation_trusted_tool_results)'
        ).fetchall()
    }
    if actual_foreign_keys != expected_foreign_keys:
        raise TrustedResultSchemaError(
            'trusted result ownership is incompatible'
        )
    for row in connection.execute(
        'SELECT * FROM conversation_trusted_tool_results'
    ).fetchall():
        _trusted_result_from_row(connection, row)
    cutoff = int(metadata[0]['terminal_rowid_cutoff'])
    unexpected = connection.execute(
        '''
        SELECT 1
        FROM conversation_trusted_tool_results AS result
        JOIN monitor_room_simulation_ledger AS terminal
          ON terminal.rowid = result.terminal_rowid
        WHERE terminal.rowid <= ?
           OR terminal.schema_version != 4
           OR terminal.record_kind NOT IN ('planned', 'planning_failed')
        LIMIT 1
        ''',
        (cutoff,),
    ).fetchone()
    if unexpected is not None:
        raise TrustedResultSchemaError(
            'trusted result predates schema activation'
        )
    missing = connection.execute(
        '''
        SELECT 1
        FROM monitor_room_simulation_ledger AS terminal
        JOIN confirmation_intents AS confirmation
          ON confirmation.confirmation_request_id =
             terminal.confirmation_request_id
        JOIN conversation_sessions AS session
          ON session.user_id = confirmation.user_id
         AND session.conversation_id = confirmation.conversation_id
        LEFT JOIN conversation_trusted_tool_results AS result
          ON result.terminal_rowid = terminal.rowid
        WHERE terminal.rowid > ?
          AND terminal.schema_version = 4
          AND terminal.record_kind IN ('planned', 'planning_failed')
          AND result.terminal_rowid IS NULL
        LIMIT 1
        ''',
        (cutoff,),
    ).fetchone()
    if missing is not None:
        raise TrustedResultSchemaError(
            'trusted result is missing for a terminal receipt'
        )


def _trusted_result_values(
    terminal: sqlite3.Row,
    confirmation: sqlite3.Row,
) -> Dict[str, Any]:
    values = {
        'terminal_rowid': int(terminal['terminal_rowid']),
        'confirmation_request_id': terminal['confirmation_request_id'],
        'receipt_digest': terminal['receipt_digest'],
        'user_id': confirmation['user_id'],
        'conversation_id': confirmation['conversation_id'],
        'session_instance_id': confirmation['session_instance_id'],
        'generation': int(confirmation['generation']),
        'source_revision': int(confirmation['revision']),
        'source_ordinal': int(confirmation['ordinal']),
        'source_turn_id': confirmation['turn_id'],
        'record_kind': terminal['record_kind'],
        'state': terminal['state'],
        'result_code': terminal['result_code'],
        'planner_revision': terminal['planner_revision'],
        'profile_digest': terminal['profile_digest'],
        'plan_digest': terminal['plan_digest'],
        'result_digest': terminal['result_digest'],
        'sample_count': terminal['sample_count'],
        'component_count': terminal['component_count'],
        'completed_at': _timestamp(terminal['completed_at'], 'completed_at'),
    }
    fingerprint = _trusted_result_fingerprint(values)
    values['trusted_result_fingerprint'] = fingerprint
    values['trusted_result_id'] = _trusted_result_id(fingerprint)
    return values


def _trusted_result_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> TrustedToolResult:
    terminal = connection.execute(
        '''
        SELECT rowid AS terminal_rowid, *
        FROM monitor_room_simulation_ledger
        WHERE rowid = ? AND confirmation_request_id = ?
        ''',
        (row['terminal_rowid'], row['confirmation_request_id']),
    ).fetchone()
    confirmation = connection.execute(
        '''
        SELECT * FROM confirmation_intents
        WHERE confirmation_request_id = ?
        ''',
        (row['confirmation_request_id'],),
    ).fetchone()
    if terminal is None or confirmation is None:
        raise TrustedResultSchemaError(
            'trusted result source is missing'
        )
    _record_from_row(terminal)
    expected = _trusted_result_values(terminal, confirmation)
    exact_fields = (
        'trusted_result_id',
        'trusted_result_fingerprint',
        'terminal_rowid',
        'confirmation_request_id',
        'receipt_digest',
        'user_id',
        'conversation_id',
        'session_instance_id',
        'generation',
        'source_revision',
        'source_ordinal',
        'source_turn_id',
        'record_kind',
        'state',
        'result_code',
        'planner_revision',
        'profile_digest',
        'plan_digest',
        'result_digest',
        'sample_count',
        'component_count',
        'completed_at',
    )
    if (
        any(row[name] != expected[name] for name in exact_fields)
        or terminal['owner_binding_digest']
        != _owner_binding_digest(confirmation)
        or any(
            type(row[name]) is not int or row[name] != expected_value
            for name, expected_value in (
                ('schema_version', 1),
                ('simulation', 1),
                ('physical_authorized', 0),
                ('physical_effects', 0),
                ('viewer_live', 0),
                ('nav2_validated', 0),
                ('camera_coverage_validated', 0),
                ('coverage_achieved', 0),
                ('execution_authorized', 0),
            )
        )
    ):
        raise TrustedResultSchemaError('trusted result is incompatible')
    source_turn = connection.execute(
        '''
        SELECT session_instance_id, ordinal, status
        FROM conversation_turns
        WHERE user_id = ? AND conversation_id = ?
          AND generation = ? AND turn_id = ?
        ''',
        (
            row['user_id'],
            row['conversation_id'],
            row['generation'],
            row['source_turn_id'],
        ),
    ).fetchone()
    if (
        source_turn is None
        or source_turn['session_instance_id'] != row['session_instance_id']
        or source_turn['ordinal'] != row['source_ordinal']
        or source_turn['status'] != 'completed'
    ):
        raise TrustedResultSchemaError(
            'trusted result source turn is incompatible'
        )
    try:
        return TrustedToolResult(
            trusted_result_id=row['trusted_result_id'],
            trusted_result_fingerprint=(
                row['trusted_result_fingerprint']
            ),
            user_id=row['user_id'],
            conversation_id=row['conversation_id'],
            session_instance_id=row['session_instance_id'],
            generation=row['generation'],
            source_revision=row['source_revision'],
            source_turn_id=row['source_turn_id'],
            source_ordinal=row['source_ordinal'],
            record_kind=row['record_kind'],
            state=row['state'],
            result_code=row['result_code'],
            planner_revision=row['planner_revision'],
            profile_digest=row['profile_digest'],
            plan_digest=row['plan_digest'],
            result_digest=row['result_digest'],
            sample_count=row['sample_count'],
            component_count=row['component_count'],
            completed_at=row['completed_at'],
        )
    except (TypeError, ValidationError):
        raise TrustedResultSchemaError(
            'trusted result is incompatible'
        ) from None


def record_or_verify_trusted_result_locked(
    connection: sqlite3.Connection,
    *,
    confirmation_request_id: str,
    replayed: bool,
) -> Optional[TrustedToolResult]:
    """Insert one fresh result or verify exact replay in this transaction."""
    if not connection.in_transaction:
        raise TrustedResultSchemaError(
            'trusted result recording requires a write transaction'
        )
    terminal = connection.execute(
        '''
        SELECT rowid AS terminal_rowid, *
        FROM monitor_room_simulation_ledger
        WHERE confirmation_request_id = ?
        ''',
        (confirmation_request_id,),
    ).fetchone()
    if terminal is None:
        raise TrustedResultSchemaError('terminal receipt is missing')
    _record_from_row(terminal, replayed=replayed)
    metadata = connection.execute(
        '''
        SELECT terminal_rowid_cutoff
        FROM monitor_room_trusted_result_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    if metadata is None:
        raise TrustedResultSchemaError(
            'trusted result metadata is missing'
        )
    existing = connection.execute(
        '''
        SELECT * FROM conversation_trusted_tool_results
        WHERE confirmation_request_id = ?
        ''',
        (confirmation_request_id,),
    ).fetchone()
    eligible = (
        terminal['schema_version'] == 4
        and terminal['record_kind'] in ('planned', 'planning_failed')
    )
    post_activation = (
        int(terminal['terminal_rowid'])
        > int(metadata['terminal_rowid_cutoff'])
    )
    if not eligible or not post_activation:
        if existing is not None:
            raise TrustedResultSchemaError(
                'ineligible receipt has a trusted result'
            )
        return None
    if existing is not None:
        return _trusted_result_from_row(connection, existing)
    if replayed:
        owner = connection.execute(
            '''
            SELECT session.user_id
            FROM confirmation_intents AS confirmation
            JOIN conversation_sessions AS session
              ON session.user_id = confirmation.user_id
             AND session.conversation_id = confirmation.conversation_id
            WHERE confirmation.confirmation_request_id = ?
            ''',
            (confirmation_request_id,),
        ).fetchone()
        if owner is None:
            return None
        raise TrustedResultSchemaError(
            'trusted result is missing for exact replay'
        )
    confirmation = connection.execute(
        '''
        SELECT * FROM confirmation_intents
        WHERE confirmation_request_id = ?
        ''',
        (confirmation_request_id,),
    ).fetchone()
    if confirmation is None:
        raise TrustedResultSchemaError(
            'trusted result owner is missing'
        )
    if terminal['owner_binding_digest'] != _owner_binding_digest(
        confirmation
    ):
        raise TrustedResultSchemaError(
            'trusted result owner is incompatible'
        )
    values = _trusted_result_values(terminal, confirmation)
    connection.execute(
        '''
        INSERT INTO conversation_trusted_tool_results (
            schema_version, trusted_result_id,
            trusted_result_fingerprint, terminal_rowid,
            confirmation_request_id, receipt_digest,
            user_id, conversation_id, session_instance_id,
            generation, source_revision, source_ordinal,
            source_turn_id, record_kind, state, result_code,
            planner_revision, profile_digest, plan_digest,
            result_digest, sample_count, component_count,
            completed_at, simulation, physical_authorized,
            physical_effects, viewer_live, nav2_validated,
            camera_coverage_validated, coverage_achieved,
            execution_authorized
        ) VALUES (
            1, :trusted_result_id, :trusted_result_fingerprint,
            :terminal_rowid, :confirmation_request_id, :receipt_digest,
            :user_id, :conversation_id, :session_instance_id,
            :generation, :source_revision, :source_ordinal,
            :source_turn_id, :record_kind, :state, :result_code,
            :planner_revision, :profile_digest, :plan_digest,
            :result_digest, :sample_count, :component_count,
            :completed_at, 1, 0, 0, 0, 0, 0, 0, 0
        )
        ''',
        values,
    )
    cursor = connection.execute(
        '''
        UPDATE conversation_sessions
        SET revision = revision + 1,
            updated_at = ?
        WHERE user_id = ? AND conversation_id = ?
          AND session_instance_id = ?
          AND generation = ?
          AND revision = ?
          AND status = 'active'
        ''',
        (
            values['completed_at'],
            values['user_id'],
            values['conversation_id'],
            values['session_instance_id'],
            values['generation'],
            values['source_revision'],
        ),
    )
    if cursor.rowcount != 1:
        raise TrustedResultSchemaError(
            'trusted result conversation changed'
        )
    stored = connection.execute(
        '''
        SELECT * FROM conversation_trusted_tool_results
        WHERE trusted_result_id = ?
        ''',
        (values['trusted_result_id'],),
    ).fetchone()
    return _trusted_result_from_row(connection, stored)


def list_trusted_results_locked(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    session_instance_id: str,
    generation: int,
    limit: int,
) -> Tuple[TrustedToolResult, ...]:
    """Return bounded current-generation results in stable order."""
    rows = connection.execute(
        '''
        SELECT * FROM (
            SELECT * FROM conversation_trusted_tool_results
            WHERE user_id = ? AND conversation_id = ?
              AND session_instance_id = ? AND generation = ?
            ORDER BY source_ordinal DESC, trusted_result_id DESC
            LIMIT ?
        )
        ORDER BY source_ordinal ASC, trusted_result_id ASC
        ''',
        (
            user_id,
            conversation_id,
            session_instance_id,
            generation,
            limit,
        ),
    ).fetchall()
    return tuple(
        _trusted_result_from_row(connection, row) for row in rows
    )


__all__ = [
    'TRUSTED_RESULT_SCHEMA_VERSION',
    'TRUSTED_RESULT_SOURCE',
    'TrustedResultSchemaError',
    'TrustedToolResult',
    'list_trusted_results_locked',
    'prepare_trusted_result_schema_locked',
    'record_or_verify_trusted_result_locked',
    'validate_trusted_result_schema_locked',
]
