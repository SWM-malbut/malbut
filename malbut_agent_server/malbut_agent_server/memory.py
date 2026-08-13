"""SQLite-backed, user-isolated long-term memory retrieval."""

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from malbut_agent_server.schemas import ValidationError, validate_user_id


MAX_MEMORY_LENGTH = 4000
MAX_RETRIEVAL_CANDIDATES = 5000
MAX_MUTATION_ID_LENGTH = 128
MEMORY_SCHEMA_VERSION = 3
MEMORY_WRITER_PROTOCOL_VERSION = 3
PREVIOUS_MEMORY_SCHEMA_VERSION = 2
TOKEN_PATTERN = re.compile(r'[0-9A-Za-z가-힣_]+')
KOREAN_SUFFIXES = (
    '으로',
    '에서',
    '에게',
    '였다',
    '였지',
    '인지',
    '처럼',
    '부터',
    '까지',
    '은',
    '는',
    '이',
    '가',
    '을',
    '를',
    '에',
    '로',
    '와',
    '과',
    '도',
    '의',
)
STOP_TOKENS = {
    '뭐',
    '뭐였지',
    '어디',
    '어디야',
    '알려줘',
    '우리',
    '기억',
    '해줘',
}
_UNSET = object()


class MemoryNotFoundError(ValidationError):
    """Raised without revealing whether another user owns a memory."""


class MemoryMutationConflictError(ValidationError):
    """Raised for an idempotency or compare-and-swap conflict."""


class MemoryConsentError(ValidationError):
    """Raised when a persistent memory mutation lacks confirmation."""


class MemorySchemaVersionError(RuntimeError):
    """Raised when a database requires an incompatible memory writer."""


@dataclass(frozen=True)
class MemoryRecord:
    """One persisted memory safe for JSON serialization."""

    id: str
    user_id: str
    kind: str
    content: str
    source: str
    confidence: float
    created_at: float
    expires_at: Optional[float]
    metadata: Dict[str, Any]
    revision: int = 1
    updated_at: Optional[float] = None
    evidence_conversation_id: Optional[str] = None
    evidence_turn_id: Optional[str] = None
    evidence_session_instance_id: Optional[str] = None
    evidence_generation: Optional[int] = None
    evidence_completed_at: Optional[float] = None
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Return the record without internal database details."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'kind': self.kind,
            'content': self.content,
            'source': self.source,
            'confidence': self.confidence,
            'created_at': self.created_at,
            'updated_at': (
                self.updated_at
                if self.updated_at is not None
                else self.created_at
            ),
            'expires_at': self.expires_at,
            'metadata': dict(self.metadata),
            'revision': self.revision,
            'evidence_conversation_id': self.evidence_conversation_id,
            'evidence_turn_id': self.evidence_turn_id,
            'evidence_session_instance_id': (
                self.evidence_session_instance_id
            ),
            'evidence_generation': self.evidence_generation,
            'evidence_completed_at': self.evidence_completed_at,
            'score': round(self.score, 6),
        }


@dataclass(frozen=True)
class MemoryMutationResult:
    """Content-free result of one durable confirmed mutation."""

    request_id: str
    operation: str
    memory_id: str
    record_revision: int
    user_revision: int
    global_revision: int
    audit_event_id: str
    occurred_at: float
    evidence_conversation_id: Optional[str] = None
    evidence_turn_id: Optional[str] = None
    evidence_session_instance_id: Optional[str] = None
    evidence_generation: Optional[int] = None
    evidence_completed_at: Optional[float] = None
    deleted: bool = False
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return mutation metadata without returning memory content."""
        return {
            'request_id': self.request_id,
            'operation': self.operation,
            'memory_id': self.memory_id,
            'record_revision': self.record_revision,
            'user_revision': self.user_revision,
            'global_revision': self.global_revision,
            'audit_event_id': self.audit_event_id,
            'occurred_at': self.occurred_at,
            'evidence_conversation_id': self.evidence_conversation_id,
            'evidence_turn_id': self.evidence_turn_id,
            'evidence_session_instance_id': (
                self.evidence_session_instance_id
            ),
            'evidence_generation': self.evidence_generation,
            'evidence_completed_at': self.evidence_completed_at,
            'deleted': self.deleted,
            'cached': self.cached,
        }

    def to_stored_dict(self) -> Dict[str, Any]:
        """Return the stable idempotency result without cache state."""
        value = self.to_dict()
        value.pop('cached')
        return value

    @classmethod
    def from_stored_dict(
        cls,
        value: Dict[str, Any],
    ) -> 'MemoryMutationResult':
        """Reconstruct a validated content-free idempotency result."""
        try:
            operation = str(value['operation'])
            if operation not in {'create', 'update', 'delete'}:
                raise ValueError('unsupported mutation operation')
            result = cls(
                request_id=str(value['request_id']),
                operation=operation,
                memory_id=str(value['memory_id']),
                record_revision=int(value['record_revision']),
                user_revision=int(value['user_revision']),
                global_revision=int(value['global_revision']),
                audit_event_id=str(value['audit_event_id']),
                occurred_at=float(value['occurred_at']),
                evidence_conversation_id=(
                    str(value['evidence_conversation_id'])
                    if value.get('evidence_conversation_id') is not None
                    else None
                ),
                evidence_turn_id=(
                    str(value['evidence_turn_id'])
                    if value.get('evidence_turn_id') is not None
                    else None
                ),
                evidence_session_instance_id=(
                    str(value['evidence_session_instance_id'])
                    if value.get('evidence_session_instance_id') is not None
                    else None
                ),
                evidence_generation=(
                    int(value['evidence_generation'])
                    if value.get('evidence_generation') is not None
                    else None
                ),
                evidence_completed_at=(
                    float(value['evidence_completed_at'])
                    if value.get('evidence_completed_at') is not None
                    else None
                ),
                deleted=bool(value.get('deleted', False)),
                cached=True,
            )
            provenance = (
                result.evidence_session_instance_id,
                result.evidence_generation,
                result.evidence_completed_at,
            )
            if not (
                all(item is None for item in provenance)
                or all(item is not None for item in provenance)
            ):
                raise ValueError('incomplete evidence provenance')
            if result.evidence_session_instance_id is not None and (
                not result.evidence_session_instance_id
                or result.evidence_generation is None
                or result.evidence_generation < 1
                or result.evidence_completed_at is None
                or not math.isfinite(result.evidence_completed_at)
            ):
                raise ValueError('invalid evidence provenance')
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                'stored memory mutation result is invalid'
            ) from error


@dataclass(frozen=True)
class MemoryMutationReplay:
    """Validated idempotency replay or proof the request is unused."""

    cached_result: Optional[MemoryMutationResult]
    request_fingerprint: str


@dataclass(frozen=True)
class MemoryAuditEvent:
    """Content-free evidence for one committed memory mutation."""

    event_id: str
    user_id: str
    memory_id: str
    operation: str
    request_id: Optional[str]
    record_revision_before: int
    record_revision_after: int
    user_revision: int
    global_revision: int
    occurred_at: float
    evidence_conversation_id: Optional[str] = None
    evidence_turn_id: Optional[str] = None
    evidence_session_instance_id: Optional[str] = None
    evidence_generation: Optional[int] = None
    evidence_completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return audit metadata that never contains memory content."""
        return {
            'event_id': self.event_id,
            'user_id': self.user_id,
            'memory_id': self.memory_id,
            'operation': self.operation,
            'request_id': self.request_id,
            'record_revision_before': self.record_revision_before,
            'record_revision_after': self.record_revision_after,
            'user_revision': self.user_revision,
            'global_revision': self.global_revision,
            'occurred_at': self.occurred_at,
            'evidence_conversation_id': self.evidence_conversation_id,
            'evidence_turn_id': self.evidence_turn_id,
            'evidence_session_instance_id': (
                self.evidence_session_instance_id
            ),
            'evidence_generation': self.evidence_generation,
            'evidence_completed_at': self.evidence_completed_at,
        }


def _normalize_text(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold()


def _token_variants(value: str) -> Set[str]:
    result: Set[str] = set()
    for raw_token in TOKEN_PATTERN.findall(_normalize_text(value)):
        if raw_token in STOP_TOKENS:
            continue
        result.add(raw_token)
        for suffix in KOREAN_SUFFIXES:
            if raw_token.endswith(suffix) and len(raw_token) > len(suffix) + 1:
                result.add(raw_token[:-len(suffix)])
                break
    return result


def _required_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f'{field_name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f'{field_name} must not be empty')
    if len(normalized) > MAX_MUTATION_ID_LENGTH:
        raise ValidationError(
            f'{field_name} must be at most '
            f'{MAX_MUTATION_ID_LENGTH} characters'
        )
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in normalized
    ):
        raise ValidationError(
            f'{field_name} must not contain control characters'
        )
    return normalized


class SQLiteMemoryStore:
    """Thread-safe SQLite source of truth for verified memories."""

    def __init__(
        self,
        database_path: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Open a database and migrate the memory schema in place."""
        if not database_path:
            raise ValueError('database_path must not be empty')
        self.database_path = database_path
        self._clock = clock
        if database_path != ':memory:':
            Path(database_path).expanduser().parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
        self._connection = sqlite3.connect(
            str(Path(database_path).expanduser())
            if database_path != ':memory:'
            else database_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connection.create_function(
            'memory_writer_protocol_version',
            0,
            lambda: MEMORY_WRITER_PROTOCOL_VERSION,
            deterministic=True,
        )
        try:
            self._initialize()
        except Exception:
            self._connection.close()
            raise
        self._secure_file_permissions()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute('PRAGMA busy_timeout=5000')
            self._connection.execute('PRAGMA foreign_keys=ON')
            # Schema inspection and ALTER statements must share one writer
            # lock.  Otherwise concurrent first opens can both observe the
            # version-one columns and race to add the same field.
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                self._require_supported_schema_locked()
                self._initialize_schema_locked()
                self._initialize_version_gate_locked()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _initialize_schema_locked(self) -> None:
        """Create or migrate tables while the caller holds a writer lock."""
        self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    metadata_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    evidence_conversation_id TEXT,
                    evidence_turn_id TEXT,
                    evidence_session_instance_id TEXT,
                    evidence_generation INTEGER,
                    evidence_completed_at REAL
                )
                '''
            )

        columns = {
                str(row['name'])
                for row in self._connection.execute(
                    'PRAGMA table_info(memories)'
                ).fetchall()
            }
        migrations = {
                'revision': (
                    'ALTER TABLE memories ADD COLUMN '
                    'revision INTEGER NOT NULL DEFAULT 1'
                ),
                'updated_at': (
                    'ALTER TABLE memories ADD COLUMN updated_at REAL'
                ),
                'evidence_conversation_id': (
                    'ALTER TABLE memories ADD COLUMN '
                    'evidence_conversation_id TEXT'
                ),
                'evidence_turn_id': (
                    'ALTER TABLE memories ADD COLUMN '
                    'evidence_turn_id TEXT'
                ),
                'evidence_session_instance_id': (
                    'ALTER TABLE memories ADD COLUMN '
                    'evidence_session_instance_id TEXT'
                ),
                'evidence_generation': (
                    'ALTER TABLE memories ADD COLUMN '
                    'evidence_generation INTEGER'
                ),
                'evidence_completed_at': (
                    'ALTER TABLE memories ADD COLUMN '
                    'evidence_completed_at REAL'
                ),
            }
        for column, statement in migrations.items():
            if column not in columns:
                self._connection.execute(statement)
        self._connection.execute(
                '''
                UPDATE memories
                SET updated_at = created_at
                WHERE updated_at IS NULL
                '''
            )
        self._connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS memories_user_created_idx
                ON memories (user_id, created_at DESC)
                '''
            )
        self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS memory_store_state (
                    key TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL
                )
                '''
            )
        self._connection.execute(
                '''
                INSERT OR IGNORE INTO memory_store_state (key, revision)
                VALUES (
                    'global_revision',
                    (SELECT COUNT(*) FROM memories)
                )
                '''
            )
        self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS memory_user_revisions (
                    user_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                '''
            )
        self._connection.execute(
                '''
                INSERT OR IGNORE INTO memory_user_revisions (
                    user_id,
                    revision,
                    updated_at
                )
                SELECT
                    user_id,
                    COUNT(*),
                    MAX(COALESCE(updated_at, created_at))
                FROM memories
                GROUP BY user_id
                '''
            )
        self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS memory_mutation_requests (
                    user_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    request_payload_fingerprint TEXT NOT NULL,
                    fingerprint_version INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    evidence_session_instance_id TEXT,
                    evidence_generation INTEGER,
                    evidence_completed_at REAL,
                    PRIMARY KEY (user_id, request_id)
                )
                '''
            )
        request_columns = {
                str(row['name'])
                for row in self._connection.execute(
                    'PRAGMA table_info(memory_mutation_requests)'
                ).fetchall()
            }
        request_migrations = {
                'request_payload_fingerprint': (
                    'ALTER TABLE memory_mutation_requests ADD COLUMN '
                    'request_payload_fingerprint TEXT'
                ),
                'fingerprint_version': (
                    'ALTER TABLE memory_mutation_requests ADD COLUMN '
                    'fingerprint_version INTEGER'
                ),
                'evidence_session_instance_id': (
                    'ALTER TABLE memory_mutation_requests ADD COLUMN '
                    'evidence_session_instance_id TEXT'
                ),
                'evidence_generation': (
                    'ALTER TABLE memory_mutation_requests ADD COLUMN '
                    'evidence_generation INTEGER'
                ),
                'evidence_completed_at': (
                    'ALTER TABLE memory_mutation_requests ADD COLUMN '
                    'evidence_completed_at REAL'
                ),
            }
        for column, statement in request_migrations.items():
            if column not in request_columns:
                self._connection.execute(statement)
        self._connection.execute(
                '''
                UPDATE memory_mutation_requests
                SET request_payload_fingerprint = request_fingerprint
                WHERE request_payload_fingerprint IS NULL
                '''
            )
        self._connection.execute(
                '''
                UPDATE memory_mutation_requests
                SET fingerprint_version = 1
                WHERE fingerprint_version IS NULL
                '''
            )
        self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS memory_audit_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_id TEXT,
                    record_revision_before INTEGER NOT NULL,
                    record_revision_after INTEGER NOT NULL,
                    user_revision INTEGER NOT NULL,
                    global_revision INTEGER NOT NULL,
                    occurred_at REAL NOT NULL,
                    evidence_conversation_id TEXT,
                    evidence_turn_id TEXT,
                    evidence_session_instance_id TEXT,
                    evidence_generation INTEGER,
                    evidence_completed_at REAL
                )
                '''
            )
        audit_columns = {
                str(row['name'])
                for row in self._connection.execute(
                    'PRAGMA table_info(memory_audit_events)'
                ).fetchall()
            }
        audit_migrations = {
                'evidence_session_instance_id': (
                    'ALTER TABLE memory_audit_events ADD COLUMN '
                    'evidence_session_instance_id TEXT'
                ),
                'evidence_generation': (
                    'ALTER TABLE memory_audit_events ADD COLUMN '
                    'evidence_generation INTEGER'
                ),
                'evidence_completed_at': (
                    'ALTER TABLE memory_audit_events ADD COLUMN '
                    'evidence_completed_at REAL'
                ),
            }
        for column, statement in audit_migrations.items():
            if column not in audit_columns:
                self._connection.execute(statement)
        self._connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS memory_audit_user_time_idx
                ON memory_audit_events (user_id, occurred_at DESC)
                '''
            )

    def _require_supported_schema_locked(self) -> None:
        """Reject a database created by a newer incompatible binary."""
        table = self._connection.execute(
            '''
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_schema_metadata'
            '''
        ).fetchone()
        if table is None:
            return
        row = self._connection.execute(
            '''
            SELECT schema_version,
                   min_writer_protocol,
                   max_writer_protocol
            FROM memory_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if row is None:
            raise MemorySchemaVersionError(
                'memory schema metadata is incomplete'
            )
        schema_version = int(row['schema_version'])
        minimum_writer = int(row['min_writer_protocol'])
        maximum_writer = int(row['max_writer_protocol'])
        if schema_version == PREVIOUS_MEMORY_SCHEMA_VERSION:
            if not (
                minimum_writer
                <= PREVIOUS_MEMORY_SCHEMA_VERSION
                <= maximum_writer
            ):
                raise MemorySchemaVersionError(
                    'memory writer protocol is incompatible'
                )
            # Version-two triggers require its connection-local protocol
            # value and would reject the version-three backfill DML. The
            # surrounding BEGIN IMMEDIATE excludes another writer while the
            # old gates are removed and the new gates are installed.
            self._drop_writer_gate_triggers_locked()
            return
        if schema_version != MEMORY_SCHEMA_VERSION:
            raise MemorySchemaVersionError(
                'memory database schema is incompatible'
            )
        if not (
            minimum_writer
            <= MEMORY_WRITER_PROTOCOL_VERSION
            <= maximum_writer
        ):
            raise MemorySchemaVersionError(
                'memory writer protocol is incompatible'
            )

    def _initialize_version_gate_locked(self) -> None:
        """Persist schema compatibility and gate every table mutation."""
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS memory_schema_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                min_writer_protocol INTEGER NOT NULL,
                max_writer_protocol INTEGER NOT NULL,
                migrated_at REAL NOT NULL
            )
            '''
        )
        row = self._connection.execute(
            '''
            SELECT schema_version,
                   min_writer_protocol,
                   max_writer_protocol
            FROM memory_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if row is not None:
            schema_version = int(row['schema_version'])
            minimum_writer = int(row['min_writer_protocol'])
            maximum_writer = int(row['max_writer_protocol'])
            if schema_version == PREVIOUS_MEMORY_SCHEMA_VERSION:
                self._connection.execute(
                    '''
                    UPDATE memory_schema_metadata
                    SET schema_version = ?,
                        min_writer_protocol = ?,
                        max_writer_protocol = ?,
                        migrated_at = ?
                    WHERE singleton = 1
                    ''',
                    (
                        MEMORY_SCHEMA_VERSION,
                        MEMORY_WRITER_PROTOCOL_VERSION,
                        MEMORY_WRITER_PROTOCOL_VERSION,
                        self._validated_now(),
                    ),
                )
            elif schema_version != MEMORY_SCHEMA_VERSION:
                raise MemorySchemaVersionError(
                    'memory schema metadata is incompatible'
                )
            elif not (
                minimum_writer
                <= MEMORY_WRITER_PROTOCOL_VERSION
                <= maximum_writer
            ):
                raise MemorySchemaVersionError(
                    'memory writer protocol is incompatible'
                )
        else:
            self._connection.execute(
                '''
                INSERT INTO memory_schema_metadata (
                    singleton,
                    schema_version,
                    min_writer_protocol,
                    max_writer_protocol,
                    migrated_at
                ) VALUES (1, ?, ?, ?, ?)
                ''',
                (
                    MEMORY_SCHEMA_VERSION,
                    MEMORY_WRITER_PROTOCOL_VERSION,
                    MEMORY_WRITER_PROTOCOL_VERSION,
                    self._validated_now(),
                ),
            )
        self._install_writer_gate_triggers_locked()

    def _drop_writer_gate_triggers_locked(self) -> None:
        """Remove known memory DML gates during an exclusive migration."""
        tables = (
            'memories',
            'memory_store_state',
            'memory_user_revisions',
            'memory_mutation_requests',
            'memory_audit_events',
            'memory_schema_metadata',
        )
        for table in tables:
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                trigger = (
                    f'memory_writer_gate_{table}_{operation.lower()}'
                )
                self._connection.execute(
                    f'DROP TRIGGER IF EXISTS {trigger}'
                )

    def _install_writer_gate_triggers_locked(self) -> None:
        """Block legacy or unmanaged connections from mutating state."""
        tables = (
            'memories',
            'memory_store_state',
            'memory_user_revisions',
            'memory_mutation_requests',
            'memory_audit_events',
            'memory_schema_metadata',
        )
        for table in tables:
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                trigger = (
                    f'memory_writer_gate_{table}_{operation.lower()}'
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
                            WHEN memory_writer_protocol_version()
                                 != {MEMORY_WRITER_PROTOCOL_VERSION}
                            THEN RAISE(
                                ABORT,
                                'incompatible memory writer protocol'
                            )
                        END;
                    END
                    '''
                )

    def _secure_file_permissions(self) -> None:
        if self.database_path == ':memory:':
            return
        expanded = str(Path(self.database_path).expanduser())
        for suffix in ('', '-wal', '-shm'):
            candidate = expanded + suffix
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()

    @property
    def revision(self) -> int:
        """Return the persistent global mutation revision."""
        with self._lock:
            row = self._connection.execute(
                '''
                SELECT revision
                FROM memory_store_state
                WHERE key = 'global_revision'
                '''
            ).fetchone()
            if row is None:
                raise RuntimeError('memory global revision is missing')
            return int(row['revision'])

    def user_revision(self, user_id: str) -> int:
        """Return one user's persistent mutation revision."""
        normalized_user = validate_user_id(user_id)
        with self._lock:
            row = self._connection.execute(
                '''
                SELECT revision
                FROM memory_user_revisions
                WHERE user_id = ?
                ''',
                (normalized_user,),
            ).fetchone()
            return int(row['revision']) if row is not None else 0

    def add(
        self,
        user_id: str,
        content: str,
        kind: str = 'fact',
        source: str = 'user_verified',
        confidence: float = 1.0,
        expires_at: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        memory_id: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> MemoryRecord:
        """Persist an explicit trusted internal memory.

        This compatibility method remains available for fixtures and trusted
        adapters. Product-facing changes must use the confirmed methods.
        """
        (
            normalized_user,
            normalized_content,
            normalized_kind,
            normalized_source,
            normalized_confidence,
            normalized_expiry,
            safe_metadata,
            metadata_json,
            normalized_created_at,
        ) = self._validated_record_values(
            user_id=user_id,
            content=content,
            kind=kind,
            source=source,
            confidence=confidence,
            expires_at=expires_at,
            metadata=metadata,
            created_at=created_at,
        )
        normalized_memory_id = (
            str(uuid.uuid4())
            if memory_id is None
            else _required_identifier(memory_id, 'memory_id')
        )
        record = MemoryRecord(
            id=normalized_memory_id,
            user_id=normalized_user,
            kind=normalized_kind,
            content=normalized_content,
            source=normalized_source,
            confidence=normalized_confidence,
            created_at=normalized_created_at,
            updated_at=normalized_created_at,
            expires_at=normalized_expiry,
            metadata=safe_metadata,
        )
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                self._insert_record_locked(record, metadata_json)
                global_revision, user_revision = (
                    self._bump_revisions_locked(
                        normalized_user,
                        normalized_created_at,
                    )
                )
                self._insert_audit_locked(
                    MemoryAuditEvent(
                        event_id=str(uuid.uuid4()),
                        user_id=normalized_user,
                        memory_id=record.id,
                        operation='legacy_add',
                        request_id=None,
                        record_revision_before=0,
                        record_revision_after=1,
                        user_revision=user_revision,
                        global_revision=global_revision,
                        occurred_at=normalized_created_at,
                        evidence_conversation_id=None,
                        evidence_turn_id=None,
                    )
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            self._secure_file_permissions()
        return record

    def commit_confirmed(
        self,
        user_id: str,
        request_id: str,
        content: str,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
        kind: str = 'fact',
        expires_at: Optional[float] = None,
        evidence_session_instance_id: Optional[str] = None,
        evidence_generation: Optional[int] = None,
        evidence_completed_at: Optional[float] = None,
    ) -> MemoryMutationResult:
        """Create one memory with explicit consent and durable idempotency."""
        normalized_user = validate_user_id(user_id)
        normalized_request = _required_identifier(
            request_id,
            'request_id',
        )
        evidence_conversation, evidence_turn = (
            self._validated_confirmation(
                evidence_conversation_id,
                evidence_turn_id,
                user_confirmed,
            )
        )
        (
            evidence_instance,
            evidence_generation_value,
            evidence_completed,
        ) = self._validated_evidence_provenance(
            evidence_session_instance_id,
            evidence_generation,
            evidence_completed_at,
        )
        timestamp = self._validated_now()
        (
            _user,
            normalized_content,
            normalized_kind,
            _source,
            _confidence,
            normalized_expiry,
            safe_metadata,
            metadata_json,
            _created,
        ) = self._validated_record_values(
            user_id=normalized_user,
            content=content,
            kind=kind,
            source='user_confirmed',
            confidence=1.0,
            expires_at=expires_at,
            metadata={},
            created_at=timestamp,
        )
        fingerprint_value = {
            'operation': 'create',
            'content': normalized_content,
            'kind': normalized_kind,
            'expires_at': normalized_expiry,
            'evidence_conversation_id': evidence_conversation,
            'evidence_turn_id': evidence_turn,
            'user_confirmed': True,
        }
        payload_fingerprint = self._mutation_fingerprint(fingerprint_value)
        fingerprint_value['evidence_provenance'] = (
            self._provenance_fingerprint_value(
                evidence_instance,
                evidence_generation_value,
                evidence_completed,
            )
        )
        fingerprint = self._mutation_fingerprint(fingerprint_value)
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                cached = self._cached_mutation_locked(
                    normalized_user,
                    normalized_request,
                    'create',
                    fingerprint,
                    payload_fingerprint,
                )
                if cached is not None:
                    self._connection.commit()
                    return cached
                self._require_future_expiry(
                    normalized_expiry,
                    timestamp,
                )
                memory_id = str(uuid.uuid4())
                record = MemoryRecord(
                    id=memory_id,
                    user_id=normalized_user,
                    kind=normalized_kind,
                    content=normalized_content,
                    source='user_confirmed',
                    confidence=1.0,
                    created_at=timestamp,
                    updated_at=timestamp,
                    expires_at=normalized_expiry,
                    metadata=safe_metadata,
                    revision=1,
                    evidence_conversation_id=evidence_conversation,
                    evidence_turn_id=evidence_turn,
                    evidence_session_instance_id=evidence_instance,
                    evidence_generation=evidence_generation_value,
                    evidence_completed_at=evidence_completed,
                )
                self._insert_record_locked(record, metadata_json)
                global_revision, user_revision = (
                    self._bump_revisions_locked(
                        normalized_user,
                        timestamp,
                    )
                )
                result = self._confirmed_result_locked(
                    user_id=normalized_user,
                    request_id=normalized_request,
                    operation='create',
                    memory_id=memory_id,
                    record_revision_before=0,
                    record_revision_after=1,
                    user_revision=user_revision,
                    global_revision=global_revision,
                    occurred_at=timestamp,
                    evidence_conversation_id=evidence_conversation,
                    evidence_turn_id=evidence_turn,
                    evidence_session_instance_id=evidence_instance,
                    evidence_generation=evidence_generation_value,
                    evidence_completed_at=evidence_completed,
                )
                self._store_mutation_locked(
                    normalized_user,
                    fingerprint,
                    payload_fingerprint,
                    result,
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            self._secure_file_permissions()
        return result

    def prepare_confirmed_create(
        self,
        user_id: str,
        request_id: str,
        content: str,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
        kind: str = 'fact',
        expires_at: Optional[float] = None,
    ) -> MemoryMutationReplay:
        """Check a create retry without performing a new mutation."""
        normalized_user = validate_user_id(user_id)
        normalized_request = _required_identifier(
            request_id,
            'request_id',
        )
        evidence_conversation, evidence_turn = (
            self._validated_confirmation(
                evidence_conversation_id,
                evidence_turn_id,
                user_confirmed,
            )
        )
        (
            _user,
            normalized_content,
            normalized_kind,
            _source,
            _confidence,
            normalized_expiry,
            _metadata,
            _metadata_json,
            _created,
        ) = self._validated_record_values(
            user_id=normalized_user,
            content=content,
            kind=kind,
            source='user_confirmed',
            confidence=1.0,
            expires_at=expires_at,
            metadata={},
            created_at=self._validated_now(),
        )
        fingerprint = self._mutation_fingerprint({
            'operation': 'create',
            'content': normalized_content,
            'kind': normalized_kind,
            'expires_at': normalized_expiry,
            'evidence_conversation_id': evidence_conversation,
            'evidence_turn_id': evidence_turn,
            'user_confirmed': True,
        })
        return self._prepare_mutation(
            normalized_user,
            normalized_request,
            'create',
            fingerprint,
        )

    def update_confirmed(
        self,
        user_id: str,
        memory_id: str,
        request_id: str,
        expected_revision: int,
        content: str,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
        kind: Optional[str] = None,
        expires_at: Any = _UNSET,
        evidence_session_instance_id: Optional[str] = None,
        evidence_generation: Optional[int] = None,
        evidence_completed_at: Optional[float] = None,
    ) -> MemoryMutationResult:
        """Update one owned memory with record-level compare-and-swap."""
        normalized_user = validate_user_id(user_id)
        normalized_memory_id = _required_identifier(
            memory_id,
            'memory_id',
        )
        normalized_request = _required_identifier(
            request_id,
            'request_id',
        )
        expected = self._validated_expected_revision(expected_revision)
        evidence_conversation, evidence_turn = (
            self._validated_confirmation(
                evidence_conversation_id,
                evidence_turn_id,
                user_confirmed,
            )
        )
        (
            evidence_instance,
            evidence_generation_value,
            evidence_completed,
        ) = self._validated_evidence_provenance(
            evidence_session_instance_id,
            evidence_generation,
            evidence_completed_at,
        )
        if not isinstance(content, str) or not content.strip():
            raise ValidationError('memory content must not be empty')
        normalized_content = content.strip()
        if len(normalized_content) > MAX_MEMORY_LENGTH:
            raise ValidationError('memory content is too long')
        normalized_kind = None
        if kind is not None:
            normalized_kind = self._validated_label(kind, 'kind')
        normalized_expiry = expires_at
        if expires_at is not _UNSET:
            normalized_expiry = self._validated_expiry(expires_at)
        timestamp = self._validated_now()
        fingerprint_value = {
            'operation': 'update',
            'memory_id': normalized_memory_id,
            'expected_revision': expected,
            'content': normalized_content,
            'kind': normalized_kind,
            'expires_at': (
                '__unchanged__'
                if normalized_expiry is _UNSET
                else normalized_expiry
            ),
            'evidence_conversation_id': evidence_conversation,
            'evidence_turn_id': evidence_turn,
            'user_confirmed': True,
        }
        payload_fingerprint = self._mutation_fingerprint(fingerprint_value)
        fingerprint_value['evidence_provenance'] = (
            self._provenance_fingerprint_value(
                evidence_instance,
                evidence_generation_value,
                evidence_completed,
            )
        )
        fingerprint = self._mutation_fingerprint(fingerprint_value)
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                cached = self._cached_mutation_locked(
                    normalized_user,
                    normalized_request,
                    'update',
                    fingerprint,
                    payload_fingerprint,
                )
                if cached is not None:
                    self._connection.commit()
                    return cached
                if normalized_expiry is not _UNSET:
                    self._require_future_expiry(
                        normalized_expiry,
                        timestamp,
                    )
                row = self._owned_row_locked(
                    normalized_user,
                    normalized_memory_id,
                )
                before = int(row['revision'])
                if before != expected:
                    raise MemoryMutationConflictError(
                        'memory revision does not match expected_revision'
                    )
                after = before + 1
                effective_kind = (
                    normalized_kind
                    if normalized_kind is not None
                    else str(row['kind'])
                )
                effective_expiry = (
                    row['expires_at']
                    if normalized_expiry is _UNSET
                    else normalized_expiry
                )
                if (
                    row['expires_at'] is not None
                    and float(row['expires_at']) <= timestamp
                ):
                    raise MemoryMutationConflictError(
                        'expired memory cannot be updated; create a new memory'
                    )
                self._connection.execute(
                    '''
                    UPDATE memories
                    SET content = ?,
                        kind = ?,
                        source = 'user_confirmed',
                        confidence = 1.0,
                        expires_at = ?,
                        revision = ?,
                        updated_at = ?,
                        evidence_conversation_id = ?,
                        evidence_turn_id = ?,
                        evidence_session_instance_id = ?,
                        evidence_generation = ?,
                        evidence_completed_at = ?
                    WHERE user_id = ? AND id = ? AND revision = ?
                    ''',
                    (
                        normalized_content,
                        effective_kind,
                        effective_expiry,
                        after,
                        timestamp,
                        evidence_conversation,
                        evidence_turn,
                        evidence_instance,
                        evidence_generation_value,
                        evidence_completed,
                        normalized_user,
                        normalized_memory_id,
                        expected,
                    ),
                )
                global_revision, user_revision = (
                    self._bump_revisions_locked(
                        normalized_user,
                        timestamp,
                    )
                )
                result = self._confirmed_result_locked(
                    user_id=normalized_user,
                    request_id=normalized_request,
                    operation='update',
                    memory_id=normalized_memory_id,
                    record_revision_before=before,
                    record_revision_after=after,
                    user_revision=user_revision,
                    global_revision=global_revision,
                    occurred_at=timestamp,
                    evidence_conversation_id=evidence_conversation,
                    evidence_turn_id=evidence_turn,
                    evidence_session_instance_id=evidence_instance,
                    evidence_generation=evidence_generation_value,
                    evidence_completed_at=evidence_completed,
                )
                self._store_mutation_locked(
                    normalized_user,
                    fingerprint,
                    payload_fingerprint,
                    result,
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            self._secure_file_permissions()
        return result

    def prepare_confirmed_update(
        self,
        user_id: str,
        memory_id: str,
        request_id: str,
        expected_revision: int,
        content: str,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
        kind: Optional[str] = None,
        expires_at: Any = _UNSET,
    ) -> MemoryMutationReplay:
        """Check an update retry without performing a new mutation."""
        normalized_user = validate_user_id(user_id)
        normalized_memory = _required_identifier(memory_id, 'memory_id')
        normalized_request = _required_identifier(
            request_id,
            'request_id',
        )
        expected = self._validated_expected_revision(expected_revision)
        evidence_conversation, evidence_turn = (
            self._validated_confirmation(
                evidence_conversation_id,
                evidence_turn_id,
                user_confirmed,
            )
        )
        if not isinstance(content, str) or not content.strip():
            raise ValidationError('memory content must not be empty')
        normalized_content = content.strip()
        if len(normalized_content) > MAX_MEMORY_LENGTH:
            raise ValidationError('memory content is too long')
        normalized_kind = (
            self._validated_label(kind, 'kind')
            if kind is not None
            else None
        )
        normalized_expiry = expires_at
        if expires_at is not _UNSET:
            normalized_expiry = self._validated_expiry(expires_at)
        fingerprint = self._mutation_fingerprint({
            'operation': 'update',
            'memory_id': normalized_memory,
            'expected_revision': expected,
            'content': normalized_content,
            'kind': normalized_kind,
            'expires_at': (
                '__unchanged__'
                if normalized_expiry is _UNSET
                else normalized_expiry
            ),
            'evidence_conversation_id': evidence_conversation,
            'evidence_turn_id': evidence_turn,
            'user_confirmed': True,
        })
        return self._prepare_mutation(
            normalized_user,
            normalized_request,
            'update',
            fingerprint,
        )

    def delete_confirmed(
        self,
        user_id: str,
        memory_id: str,
        request_id: str,
        expected_revision: int,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
        evidence_session_instance_id: Optional[str] = None,
        evidence_generation: Optional[int] = None,
        evidence_completed_at: Optional[float] = None,
    ) -> MemoryMutationResult:
        """Delete one owned memory with consent, CAS, and idempotency."""
        normalized_user = validate_user_id(user_id)
        normalized_memory_id = _required_identifier(
            memory_id,
            'memory_id',
        )
        normalized_request = _required_identifier(
            request_id,
            'request_id',
        )
        expected = self._validated_expected_revision(expected_revision)
        evidence_conversation, evidence_turn = (
            self._validated_confirmation(
                evidence_conversation_id,
                evidence_turn_id,
                user_confirmed,
            )
        )
        (
            evidence_instance,
            evidence_generation_value,
            evidence_completed,
        ) = self._validated_evidence_provenance(
            evidence_session_instance_id,
            evidence_generation,
            evidence_completed_at,
        )
        timestamp = self._validated_now()
        fingerprint_value = {
            'operation': 'delete',
            'memory_id': normalized_memory_id,
            'expected_revision': expected,
            'evidence_conversation_id': evidence_conversation,
            'evidence_turn_id': evidence_turn,
            'user_confirmed': True,
        }
        payload_fingerprint = self._mutation_fingerprint(fingerprint_value)
        fingerprint_value['evidence_provenance'] = (
            self._provenance_fingerprint_value(
                evidence_instance,
                evidence_generation_value,
                evidence_completed,
            )
        )
        fingerprint = self._mutation_fingerprint(fingerprint_value)
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                cached = self._cached_mutation_locked(
                    normalized_user,
                    normalized_request,
                    'delete',
                    fingerprint,
                    payload_fingerprint,
                )
                if cached is not None:
                    self._connection.commit()
                    return cached
                row = self._owned_row_locked(
                    normalized_user,
                    normalized_memory_id,
                )
                before = int(row['revision'])
                if before != expected:
                    raise MemoryMutationConflictError(
                        'memory revision does not match expected_revision'
                    )
                after = before + 1
                cursor = self._connection.execute(
                    '''
                    DELETE FROM memories
                    WHERE user_id = ? AND id = ? AND revision = ?
                    ''',
                    (
                        normalized_user,
                        normalized_memory_id,
                        expected,
                    ),
                )
                if cursor.rowcount != 1:
                    raise MemoryMutationConflictError(
                        'memory changed before deletion'
                    )
                global_revision, user_revision = (
                    self._bump_revisions_locked(
                        normalized_user,
                        timestamp,
                    )
                )
                result = self._confirmed_result_locked(
                    user_id=normalized_user,
                    request_id=normalized_request,
                    operation='delete',
                    memory_id=normalized_memory_id,
                    record_revision_before=before,
                    record_revision_after=after,
                    user_revision=user_revision,
                    global_revision=global_revision,
                    occurred_at=timestamp,
                    evidence_conversation_id=evidence_conversation,
                    evidence_turn_id=evidence_turn,
                    evidence_session_instance_id=evidence_instance,
                    evidence_generation=evidence_generation_value,
                    evidence_completed_at=evidence_completed,
                    deleted=True,
                )
                self._store_mutation_locked(
                    normalized_user,
                    fingerprint,
                    payload_fingerprint,
                    result,
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            self._secure_file_permissions()
        return result

    def prepare_confirmed_delete(
        self,
        user_id: str,
        memory_id: str,
        request_id: str,
        expected_revision: int,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        user_confirmed: bool,
    ) -> MemoryMutationReplay:
        """Check a delete retry without performing a new mutation."""
        normalized_user = validate_user_id(user_id)
        normalized_memory = _required_identifier(memory_id, 'memory_id')
        normalized_request = _required_identifier(
            request_id,
            'request_id',
        )
        expected = self._validated_expected_revision(expected_revision)
        evidence_conversation, evidence_turn = (
            self._validated_confirmation(
                evidence_conversation_id,
                evidence_turn_id,
                user_confirmed,
            )
        )
        fingerprint = self._mutation_fingerprint({
            'operation': 'delete',
            'memory_id': normalized_memory,
            'expected_revision': expected,
            'evidence_conversation_id': evidence_conversation,
            'evidence_turn_id': evidence_turn,
            'user_confirmed': True,
        })
        return self._prepare_mutation(
            normalized_user,
            normalized_request,
            'delete',
            fingerprint,
        )

    def get_for_user(
        self,
        user_id: str,
        memory_id: str,
        now: Optional[float] = None,
        include_expired: bool = False,
    ) -> Optional[MemoryRecord]:
        """Return one owned memory without revealing other user records."""
        normalized_user = validate_user_id(user_id)
        normalized_memory_id = _required_identifier(
            memory_id,
            'memory_id',
        )
        current_time = self._validated_now(now)
        with self._lock:
            row = self._connection.execute(
                '''
                SELECT *
                FROM memories
                WHERE user_id = ? AND id = ?
                  AND (
                    ? = 1
                    OR expires_at IS NULL
                    OR expires_at > ?
                  )
                ''',
                (
                    normalized_user,
                    normalized_memory_id,
                    int(include_expired),
                    current_time,
                ),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def search(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        now: Optional[float] = None,
    ) -> List[MemoryRecord]:
        """Rank active memories by lexical overlap, confidence, and recency."""
        records, _revision = self.search_with_revision(
            user_id,
            query,
            limit=limit,
            now=now,
        )
        return records

    def search_with_revision(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        now: Optional[float] = None,
    ) -> Tuple[List[MemoryRecord], int]:
        """Return active matches and an atomic persistent global revision."""
        return self._search_snapshot(
            user_id,
            query,
            limit=limit,
            now=now,
            owner_revision=False,
        )

    def search_with_owner_revision(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        now: Optional[float] = None,
    ) -> Tuple[List[MemoryRecord], int]:
        """Return active matches and the same user's atomic revision."""
        return self._search_snapshot(
            user_id,
            query,
            limit=limit,
            now=now,
            owner_revision=True,
        )

    def owner_snapshot_is_current(
        self,
        user_id: str,
        expected_revision: int,
        records: Sequence[MemoryRecord],
        now: Optional[float] = None,
    ) -> bool:
        """Check an inference snapshot for owner changes and expiry.

        The check uses one SQLite read transaction so a cross-process
        mutation cannot be observed half-way through validation.  Expiry is
        checked independently because time passing does not mutate a row or
        increment its revision.
        """
        normalized_user = validate_user_id(user_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValidationError(
                'expected owner revision must be a non-negative integer'
            )
        normalized_records = list(records)
        for record in normalized_records:
            if not isinstance(record, MemoryRecord):
                raise ValidationError(
                    'memory snapshot records must be MemoryRecord values'
                )
            if record.user_id != normalized_user:
                raise ValidationError(
                    'memory snapshot record owner does not match user_id'
                )
        current_time = self._validated_now(now)
        with self._lock:
            try:
                self._connection.execute('BEGIN')
                revision_row = self._connection.execute(
                    '''
                    SELECT revision
                    FROM memory_user_revisions
                    WHERE user_id = ?
                    ''',
                    (normalized_user,),
                ).fetchone()
                current_revision = (
                    int(revision_row['revision'])
                    if revision_row is not None
                    else 0
                )
                if current_revision != expected_revision:
                    self._connection.commit()
                    return False
                if normalized_records:
                    placeholders = ','.join(
                        '?' for _record in normalized_records
                    )
                    rows = self._connection.execute(
                        f'''
                        SELECT id, revision, expires_at
                        FROM memories
                        WHERE user_id = ?
                          AND id IN ({placeholders})
                        ''',
                        (
                            normalized_user,
                            *(
                                record.id
                                for record in normalized_records
                            ),
                        ),
                    ).fetchall()
                else:
                    rows = []
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        current_records = {
            str(row['id']): row
            for row in rows
        }
        if len(current_records) != len(normalized_records):
            return False
        for record in normalized_records:
            row = current_records.get(record.id)
            if row is None or int(row['revision']) != record.revision:
                return False
            expires_at = row['expires_at']
            if expires_at is not None and float(expires_at) <= current_time:
                return False
        return True

    def _search_snapshot(
        self,
        user_id: str,
        query: str,
        *,
        limit: int,
        now: Optional[float],
        owner_revision: bool,
    ) -> Tuple[List[MemoryRecord], int]:
        """Search and read either global or owner revision atomically."""
        normalized_user = validate_user_id(user_id)
        if not isinstance(query, str) or not query.strip():
            raise ValidationError('memory query must not be empty')
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError('memory search limit must be an integer')
        if limit < 1 or limit > 10:
            raise ValidationError(
                'memory search limit must be between 1 and 10'
            )
        current_time = self._validated_now(now)
        with self._lock:
            try:
                self._connection.execute('BEGIN')
                rows = self._connection.execute(
                    '''
                    SELECT *
                    FROM memories
                    WHERE user_id = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY created_at DESC
                    LIMIT ?
                    ''',
                    (
                        normalized_user,
                        current_time,
                        MAX_RETRIEVAL_CANDIDATES,
                    ),
                ).fetchall()
                if owner_revision:
                    revision_row = self._connection.execute(
                        '''
                        SELECT revision
                        FROM memory_user_revisions
                        WHERE user_id = ?
                        ''',
                        (normalized_user,),
                    ).fetchone()
                else:
                    revision_row = self._connection.execute(
                        '''
                        SELECT revision
                        FROM memory_store_state
                        WHERE key = 'global_revision'
                        '''
                    ).fetchone()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        if revision_row is None and not owner_revision:
            raise RuntimeError('memory global revision is missing')
        revision = (
            int(revision_row['revision'])
            if revision_row is not None
            else 0
        )

        query_normalized = _normalize_text(query)
        query_tokens = _token_variants(query)
        scored: List[MemoryRecord] = []
        for row in rows:
            content = str(row['content'])
            content_normalized = _normalize_text(content)
            content_tokens = _token_variants(content)
            overlap = query_tokens & content_tokens
            score = float(len(overlap) * 4)
            if query_normalized in content_normalized:
                score += 8
            for query_token in query_tokens:
                if len(query_token) < 2:
                    continue
                if any(
                    query_token in content_token
                    or content_token in query_token
                    for content_token in content_tokens
                    if len(content_token) >= 2
                ):
                    score += 1
            if score <= 0:
                continue
            age_days = max(
                0.0,
                (current_time - float(row['created_at'])) / 86400.0,
            )
            recency = math.exp(-age_days / 90.0)
            confidence = float(row['confidence'])
            score += confidence + recency
            scored.append(
                replace(
                    self._record_from_row(row),
                    score=score,
                )
            )
        scored.sort(
            key=lambda item: (item.score, item.created_at),
            reverse=True,
        )
        return scored[:limit], revision

    def purge_expired(self, now: Optional[float] = None) -> int:
        """Delete expired records with content-free audit events."""
        current_time = self._validated_now(now)
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                rows = self._connection.execute(
                    '''
                    SELECT id, user_id, revision
                    FROM memories
                    WHERE expires_at IS NOT NULL
                      AND expires_at <= ?
                    ORDER BY user_id, id
                    ''',
                    (current_time,),
                ).fetchall()
                self._connection.execute(
                    '''
                    DELETE FROM memories
                    WHERE expires_at IS NOT NULL
                      AND expires_at <= ?
                    ''',
                    (current_time,),
                )
                for row in rows:
                    global_revision, user_revision = (
                        self._bump_revisions_locked(
                            str(row['user_id']),
                            current_time,
                        )
                    )
                    before = int(row['revision'])
                    self._insert_audit_locked(
                        MemoryAuditEvent(
                            event_id=str(uuid.uuid4()),
                            user_id=str(row['user_id']),
                            memory_id=str(row['id']),
                            operation='expire_purge',
                            request_id=None,
                            record_revision_before=before,
                            record_revision_after=before + 1,
                            user_revision=user_revision,
                            global_revision=global_revision,
                            occurred_at=current_time,
                            evidence_conversation_id=None,
                            evidence_turn_id=None,
                        )
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            self._secure_file_permissions()
        return len(rows)

    def delete(
        self,
        user_id: str,
        memory_id: str,
    ) -> bool:
        """Delete one memory in a trusted internal owner scope."""
        normalized_user = validate_user_id(user_id)
        normalized_memory_id = _required_identifier(
            memory_id,
            'memory_id',
        )
        timestamp = self._validated_now()
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                row = self._connection.execute(
                    '''
                    SELECT revision
                    FROM memories
                    WHERE id = ? AND user_id = ?
                    ''',
                    (normalized_memory_id, normalized_user),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return False
                cursor = self._connection.execute(
                    '''
                    DELETE FROM memories
                    WHERE id = ? AND user_id = ?
                    ''',
                    (normalized_memory_id, normalized_user),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError('scoped memory deletion failed')
                global_revision, user_revision = (
                    self._bump_revisions_locked(
                        normalized_user,
                        timestamp,
                    )
                )
                before = int(row['revision'])
                self._insert_audit_locked(
                    MemoryAuditEvent(
                        event_id=str(uuid.uuid4()),
                        user_id=normalized_user,
                        memory_id=normalized_memory_id,
                        operation='legacy_delete',
                        request_id=None,
                        record_revision_before=before,
                        record_revision_after=before + 1,
                        user_revision=user_revision,
                        global_revision=global_revision,
                        occurred_at=timestamp,
                        evidence_conversation_id=None,
                        evidence_turn_id=None,
                    )
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            self._secure_file_permissions()
        return True

    def list_for_user(
        self,
        user_id: str,
        now: Optional[float] = None,
    ) -> Sequence[MemoryRecord]:
        """List active memories for diagnostics without cross-user access."""
        normalized_user = validate_user_id(user_id)
        current_time = self._validated_now(now)
        with self._lock:
            rows = self._connection.execute(
                '''
                SELECT *
                FROM memories
                WHERE user_id = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
                ''',
                (normalized_user, current_time),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def list_audit_events(
        self,
        user_id: str,
        limit: int = 100,
    ) -> Sequence[MemoryAuditEvent]:
        """List content-free audit metadata in one user scope."""
        normalized_user = validate_user_id(user_id)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError('audit limit must be an integer')
        if limit < 1 or limit > 500:
            raise ValidationError('audit limit must be between 1 and 500')
        with self._lock:
            rows = self._connection.execute(
                '''
                SELECT *
                FROM memory_audit_events
                WHERE user_id = ?
                ORDER BY global_revision DESC, event_id DESC
                LIMIT ?
                ''',
                (normalized_user, limit),
            ).fetchall()
        return [self._audit_from_row(row) for row in rows]

    def _validated_record_values(
        self,
        user_id: str,
        content: str,
        kind: str,
        source: str,
        confidence: float,
        expires_at: Optional[float],
        metadata: Optional[Dict[str, Any]],
        created_at: Optional[float],
    ) -> Tuple[
        str,
        str,
        str,
        str,
        float,
        Optional[float],
        Dict[str, Any],
        str,
        float,
    ]:
        normalized_user = validate_user_id(user_id)
        if not isinstance(content, str) or not content.strip():
            raise ValidationError('memory content must not be empty')
        normalized_content = content.strip()
        if len(normalized_content) > MAX_MEMORY_LENGTH:
            raise ValidationError('memory content is too long')
        normalized_kind = self._validated_label(kind, 'kind')
        normalized_source = self._validated_label(source, 'source')
        if isinstance(confidence, bool) or not isinstance(
            confidence,
            (int, float),
        ):
            raise ValidationError('memory confidence must be a number')
        normalized_confidence = float(confidence)
        if (
            not math.isfinite(normalized_confidence)
            or normalized_confidence < 0
            or normalized_confidence > 1
        ):
            raise ValidationError(
                'memory confidence must be between 0 and 1'
            )
        normalized_expiry = self._validated_expiry(expires_at)
        safe_metadata = {} if metadata is None else metadata
        if not isinstance(safe_metadata, dict):
            raise ValidationError('memory metadata must be an object')
        try:
            metadata_json = json.dumps(
                safe_metadata,
                ensure_ascii=False,
                separators=(',', ':'),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValidationError(
                'memory metadata must contain finite JSON values'
            ) from error
        if len(metadata_json) > 8000:
            raise ValidationError('memory metadata is too large')
        normalized_created_at = self._validated_now(created_at)
        return (
            normalized_user,
            normalized_content,
            normalized_kind,
            normalized_source,
            normalized_confidence,
            normalized_expiry,
            dict(safe_metadata),
            metadata_json,
            normalized_created_at,
        )

    @staticmethod
    def _validated_label(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f'memory {field_name} is invalid')
        normalized = value.strip()
        if len(normalized) > 64:
            raise ValidationError(f'memory {field_name} is invalid')
        return normalized

    @staticmethod
    def _validated_expiry(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(
                'memory expires_at must be a number or null'
            )
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValidationError('memory expires_at must be finite')
        return normalized

    def _validated_now(self, value: Optional[float] = None) -> float:
        raw = self._clock() if value is None else value
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValidationError('memory timestamp must be a number')
        normalized = float(raw)
        if not math.isfinite(normalized):
            raise ValidationError('memory timestamp must be finite')
        return normalized

    @staticmethod
    def _require_future_expiry(
        expires_at: Optional[float],
        now: float,
    ) -> None:
        if expires_at is not None and expires_at <= now:
            raise ValidationError(
                'confirmed memory expires_at must be in the future'
            )

    @staticmethod
    def _validated_expected_revision(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError('expected_revision must be an integer')
        if value < 1:
            raise ValidationError('expected_revision must be positive')
        return value

    @staticmethod
    def _validated_confirmation(
        conversation_id: Any,
        turn_id: Any,
        user_confirmed: Any,
    ) -> Tuple[str, str]:
        if user_confirmed is not True:
            raise MemoryConsentError(
                'memory mutation requires explicit user confirmation'
            )
        return (
            _required_identifier(
                conversation_id,
                'evidence_conversation_id',
            ),
            _required_identifier(turn_id, 'evidence_turn_id'),
        )

    @staticmethod
    def _validated_evidence_provenance(
        session_instance_id: Any,
        generation: Any,
        completed_at: Any,
    ) -> Tuple[Optional[str], Optional[int], Optional[float]]:
        """Validate an all-known or explicitly unknown evidence origin."""
        values = (session_instance_id, generation, completed_at)
        if all(value is None for value in values):
            return None, None, None
        if any(value is None for value in values):
            raise ValidationError(
                'evidence provenance must be complete or entirely unknown'
            )
        normalized_instance = _required_identifier(
            session_instance_id,
            'evidence_session_instance_id',
        )
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValidationError(
                'evidence_generation must be a positive integer'
            )
        if generation < 1:
            raise ValidationError(
                'evidence_generation must be a positive integer'
            )
        if isinstance(completed_at, bool) or not isinstance(
            completed_at,
            (int, float),
        ):
            raise ValidationError('evidence_completed_at must be finite')
        normalized_completed_at = float(completed_at)
        if not math.isfinite(normalized_completed_at):
            raise ValidationError('evidence_completed_at must be finite')
        return normalized_instance, generation, normalized_completed_at

    @staticmethod
    def _provenance_fingerprint_value(
        session_instance_id: Optional[str],
        generation: Optional[int],
        completed_at: Optional[float],
    ) -> Dict[str, Any]:
        """Return a canonical explicit known/unknown provenance value."""
        if session_instance_id is None:
            return {'status': 'unknown'}
        return {
            'status': 'validated',
            'session_instance_id': session_instance_id,
            'generation': generation,
            'completed_at': completed_at,
        }

    @staticmethod
    def _mutation_fingerprint(value: Dict[str, Any]) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def _insert_record_locked(
        self,
        record: MemoryRecord,
        metadata_json: str,
    ) -> None:
        self._connection.execute(
            '''
            INSERT INTO memories (
                id,
                user_id,
                kind,
                content,
                source,
                confidence,
                created_at,
                expires_at,
                metadata_json,
                revision,
                updated_at,
                evidence_conversation_id,
                evidence_turn_id,
                evidence_session_instance_id,
                evidence_generation,
                evidence_completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                record.id,
                record.user_id,
                record.kind,
                record.content,
                record.source,
                record.confidence,
                record.created_at,
                record.expires_at,
                metadata_json,
                record.revision,
                record.updated_at,
                record.evidence_conversation_id,
                record.evidence_turn_id,
                record.evidence_session_instance_id,
                record.evidence_generation,
                record.evidence_completed_at,
            ),
        )

    def _bump_revisions_locked(
        self,
        user_id: str,
        occurred_at: float,
    ) -> Tuple[int, int]:
        self._connection.execute(
            '''
            INSERT OR IGNORE INTO memory_user_revisions (
                user_id,
                revision,
                updated_at
            ) VALUES (?, 0, ?)
            ''',
            (user_id, occurred_at),
        )
        self._connection.execute(
            '''
            UPDATE memory_user_revisions
            SET revision = revision + 1,
                updated_at = ?
            WHERE user_id = ?
            ''',
            (occurred_at, user_id),
        )
        self._connection.execute(
            '''
            UPDATE memory_store_state
            SET revision = revision + 1
            WHERE key = 'global_revision'
            '''
        )
        user_row = self._connection.execute(
            '''
            SELECT revision
            FROM memory_user_revisions
            WHERE user_id = ?
            ''',
            (user_id,),
        ).fetchone()
        global_row = self._connection.execute(
            '''
            SELECT revision
            FROM memory_store_state
            WHERE key = 'global_revision'
            '''
        ).fetchone()
        if user_row is None or global_row is None:
            raise RuntimeError('memory revision update failed')
        return int(global_row['revision']), int(user_row['revision'])

    def _owned_row_locked(
        self,
        user_id: str,
        memory_id: str,
    ) -> sqlite3.Row:
        row = self._connection.execute(
            '''
            SELECT *
            FROM memories
            WHERE user_id = ? AND id = ?
            ''',
            (user_id, memory_id),
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError('memory was not found')
        return row

    def _cached_mutation_locked(
        self,
        user_id: str,
        request_id: str,
        operation: str,
        fingerprint: str,
        payload_fingerprint: str,
    ) -> Optional[MemoryMutationResult]:
        row = self._connection.execute(
            '''
            SELECT operation,
                   request_fingerprint,
                   request_payload_fingerprint,
                   fingerprint_version,
                   response_json,
                   evidence_session_instance_id,
                   evidence_generation,
                   evidence_completed_at
            FROM memory_mutation_requests
            WHERE user_id = ? AND request_id = ?
            ''',
            (user_id, request_id),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row['operation']) != operation
            or str(row['request_payload_fingerprint'])
            != payload_fingerprint
            or (
                int(row['fingerprint_version']) >= 2
                and str(row['request_fingerprint']) != fingerprint
            )
        ):
            raise MemoryMutationConflictError(
                'request_id was already used with different input'
            )
        try:
            value = json.loads(str(row['response_json']))
        except json.JSONDecodeError as error:
            raise RuntimeError(
                'stored memory mutation response is invalid'
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError('stored memory mutation response is invalid')
        result = MemoryMutationResult.from_stored_dict(value)
        if int(row['fingerprint_version']) >= 2 and (
            result.evidence_session_instance_id
            != row['evidence_session_instance_id']
            or result.evidence_generation != row['evidence_generation']
            or result.evidence_completed_at != row['evidence_completed_at']
        ):
            raise RuntimeError(
                'stored memory mutation provenance is inconsistent'
            )
        return result

    def _cached_payload_mutation_locked(
        self,
        user_id: str,
        request_id: str,
        operation: str,
        payload_fingerprint: str,
    ) -> Optional[MemoryMutationResult]:
        """Replay only an exact caller payload before evidence lookup."""
        row = self._connection.execute(
            '''
            SELECT operation,
                   request_payload_fingerprint,
                   response_json,
                   fingerprint_version,
                   evidence_session_instance_id,
                   evidence_generation,
                   evidence_completed_at
            FROM memory_mutation_requests
            WHERE user_id = ? AND request_id = ?
            ''',
            (user_id, request_id),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row['operation']) != operation
            or str(row['request_payload_fingerprint'])
            != payload_fingerprint
        ):
            raise MemoryMutationConflictError(
                'request_id was already used with different input'
            )
        try:
            value = json.loads(str(row['response_json']))
        except json.JSONDecodeError as error:
            raise RuntimeError(
                'stored memory mutation response is invalid'
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError('stored memory mutation response is invalid')
        result = MemoryMutationResult.from_stored_dict(value)
        if int(row['fingerprint_version']) >= 2 and (
            result.evidence_session_instance_id
            != row['evidence_session_instance_id']
            or result.evidence_generation != row['evidence_generation']
            or result.evidence_completed_at != row['evidence_completed_at']
        ):
            raise RuntimeError(
                'stored memory mutation provenance is inconsistent'
            )
        return result

    def _prepare_mutation(
        self,
        user_id: str,
        request_id: str,
        operation: str,
        fingerprint: str,
    ) -> MemoryMutationReplay:
        """Atomically validate an exact retry or reserve no state."""
        with self._lock:
            try:
                self._connection.execute('BEGIN')
                cached = self._cached_payload_mutation_locked(
                    user_id,
                    request_id,
                    operation,
                    fingerprint,
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return MemoryMutationReplay(
            cached_result=cached,
            request_fingerprint=fingerprint,
        )

    def _confirmed_result_locked(
        self,
        user_id: str,
        request_id: str,
        operation: str,
        memory_id: str,
        record_revision_before: int,
        record_revision_after: int,
        user_revision: int,
        global_revision: int,
        occurred_at: float,
        evidence_conversation_id: str,
        evidence_turn_id: str,
        evidence_session_instance_id: Optional[str],
        evidence_generation: Optional[int],
        evidence_completed_at: Optional[float],
        deleted: bool = False,
    ) -> MemoryMutationResult:
        event_id = str(uuid.uuid4())
        event = MemoryAuditEvent(
            event_id=event_id,
            user_id=user_id,
            memory_id=memory_id,
            operation=operation,
            request_id=request_id,
            record_revision_before=record_revision_before,
            record_revision_after=record_revision_after,
            user_revision=user_revision,
            global_revision=global_revision,
            occurred_at=occurred_at,
            evidence_conversation_id=evidence_conversation_id,
            evidence_turn_id=evidence_turn_id,
            evidence_session_instance_id=evidence_session_instance_id,
            evidence_generation=evidence_generation,
            evidence_completed_at=evidence_completed_at,
        )
        self._insert_audit_locked(event)
        return MemoryMutationResult(
            request_id=request_id,
            operation=operation,
            memory_id=memory_id,
            record_revision=record_revision_after,
            user_revision=user_revision,
            global_revision=global_revision,
            audit_event_id=event_id,
            occurred_at=occurred_at,
            evidence_conversation_id=evidence_conversation_id,
            evidence_turn_id=evidence_turn_id,
            evidence_session_instance_id=evidence_session_instance_id,
            evidence_generation=evidence_generation,
            evidence_completed_at=evidence_completed_at,
            deleted=deleted,
        )

    def _store_mutation_locked(
        self,
        user_id: str,
        fingerprint: str,
        payload_fingerprint: str,
        result: MemoryMutationResult,
    ) -> None:
        self._connection.execute(
            '''
            INSERT INTO memory_mutation_requests (
                user_id,
                request_id,
                operation,
                request_fingerprint,
                request_payload_fingerprint,
                fingerprint_version,
                response_json,
                created_at,
                evidence_session_instance_id,
                evidence_generation,
                evidence_completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                user_id,
                result.request_id,
                result.operation,
                fingerprint,
                payload_fingerprint,
                2,
                json.dumps(
                    result.to_stored_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ),
                result.occurred_at,
                result.evidence_session_instance_id,
                result.evidence_generation,
                result.evidence_completed_at,
            ),
        )

    def _insert_audit_locked(self, event: MemoryAuditEvent) -> None:
        self._connection.execute(
            '''
            INSERT INTO memory_audit_events (
                event_id,
                user_id,
                memory_id,
                operation,
                request_id,
                record_revision_before,
                record_revision_after,
                user_revision,
                global_revision,
                occurred_at,
                evidence_conversation_id,
                evidence_turn_id,
                evidence_session_instance_id,
                evidence_generation,
                evidence_completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                event.event_id,
                event.user_id,
                event.memory_id,
                event.operation,
                event.request_id,
                event.record_revision_before,
                event.record_revision_after,
                event.user_revision,
                event.global_revision,
                event.occurred_at,
                event.evidence_conversation_id,
                event.evidence_turn_id,
                event.evidence_session_instance_id,
                event.evidence_generation,
                event.evidence_completed_at,
            ),
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> MemoryRecord:
        metadata = json.loads(str(row['metadata_json']))
        if not isinstance(metadata, dict):
            raise RuntimeError('stored memory metadata is invalid')
        return MemoryRecord(
            id=str(row['id']),
            user_id=str(row['user_id']),
            kind=str(row['kind']),
            content=str(row['content']),
            source=str(row['source']),
            confidence=float(row['confidence']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
            expires_at=(
                float(row['expires_at'])
                if row['expires_at'] is not None
                else None
            ),
            metadata=metadata,
            revision=int(row['revision']),
            evidence_conversation_id=(
                str(row['evidence_conversation_id'])
                if row['evidence_conversation_id'] is not None
                else None
            ),
            evidence_turn_id=(
                str(row['evidence_turn_id'])
                if row['evidence_turn_id'] is not None
                else None
            ),
            evidence_session_instance_id=(
                str(row['evidence_session_instance_id'])
                if row['evidence_session_instance_id'] is not None
                else None
            ),
            evidence_generation=(
                int(row['evidence_generation'])
                if row['evidence_generation'] is not None
                else None
            ),
            evidence_completed_at=(
                float(row['evidence_completed_at'])
                if row['evidence_completed_at'] is not None
                else None
            ),
        )

    @staticmethod
    def _audit_from_row(row: sqlite3.Row) -> MemoryAuditEvent:
        return MemoryAuditEvent(
            event_id=str(row['event_id']),
            user_id=str(row['user_id']),
            memory_id=str(row['memory_id']),
            operation=str(row['operation']),
            request_id=(
                str(row['request_id'])
                if row['request_id'] is not None
                else None
            ),
            record_revision_before=int(
                row['record_revision_before']
            ),
            record_revision_after=int(row['record_revision_after']),
            user_revision=int(row['user_revision']),
            global_revision=int(row['global_revision']),
            occurred_at=float(row['occurred_at']),
            evidence_conversation_id=(
                str(row['evidence_conversation_id'])
                if row['evidence_conversation_id'] is not None
                else None
            ),
            evidence_turn_id=(
                str(row['evidence_turn_id'])
                if row['evidence_turn_id'] is not None
                else None
            ),
            evidence_session_instance_id=(
                str(row['evidence_session_instance_id'])
                if row['evidence_session_instance_id'] is not None
                else None
            ),
            evidence_generation=(
                int(row['evidence_generation'])
                if row['evidence_generation'] is not None
                else None
            ),
            evidence_completed_at=(
                float(row['evidence_completed_at'])
                if row['evidence_completed_at'] is not None
                else None
            ),
        )
