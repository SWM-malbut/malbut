"""SQLite-backed, user-isolated short-term conversation sessions."""

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from malbut_agent_server.schemas import (
    MAX_UTTERANCE_LENGTH,
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)
from malbut_agent_server.summarization import (
    ExtractiveConversationSummarizer,
    SummaryResult,
    SummarySourceTurn,
)

if TYPE_CHECKING:
    from malbut_agent_server.text_confirmation import (
        ConfirmationDraft,
        ConfirmationRecord,
        ConfirmationResolution,
    )


MAX_RESPONSE_JSON_LENGTH = 65536
DEFAULT_SUMMARY_MAX_CHARS = 2000
SUMMARY_UPDATE_BATCH_SIZE = 128
CONFIRMATION_STORAGE_SCHEMA_VERSION = 1
MAX_CONFIRMATION_JSON_LENGTH = 32768
TEXT_TURN_CLAIM_SCHEMA_VERSION = 1
TEXT_TURN_CLAIM_OUTCOMES = frozenset({
    'confirmation_unrecognized',
    'confirmation_not_pending',
    'confirmation_resolved',
    'confirmation_invalidated',
})


CONFIRMATION_SCHEMA_METADATA_SQL = '''
CREATE TABLE confirmation_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
)
'''


CONFIRMATION_INTENTS_SQL = '''
CREATE TABLE confirmation_intents (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    turn_id TEXT NOT NULL,
    agent_request_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK (expires_at > issued_at),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'resolved', 'invalidated')
    ),
    disposition TEXT NOT NULL CHECK (
        disposition IN (
            'pending', 'approved', 'denied', 'canceled',
            'expired', 'invalidated'
        )
    ),
    requested_disposition TEXT CHECK (
        requested_disposition IS NULL OR requested_disposition IN (
            'approve', 'deny', 'cancel'
        )
    ),
    result_code TEXT NOT NULL,
    response_id TEXT,
    response_turn_id TEXT,
    response_fingerprint TEXT,
    resolved_at REAL,
    record_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    authority_kind TEXT NOT NULL DEFAULT 'none'
        CHECK (authority_kind = 'none'),
    execution_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (execution_authorized = 0),
    consume_once INTEGER NOT NULL DEFAULT 0
        CHECK (consume_once = 0),
    tool_call_id TEXT CHECK (tool_call_id IS NULL),
    mission_id TEXT CHECK (mission_id IS NULL),
    UNIQUE (user_id, agent_request_id),
    UNIQUE (user_id, decision_id),
    UNIQUE (user_id, proposal_fingerprint),
    FOREIGN KEY (user_id, conversation_id)
        REFERENCES conversation_sessions (user_id, conversation_id)
        ON DELETE CASCADE,
    CHECK (
        (state = 'pending'
         AND disposition = 'pending'
         AND requested_disposition IS NULL
         AND result_code = 'confirmation_pending'
         AND response_id IS NULL
         AND response_turn_id IS NULL
         AND response_fingerprint IS NULL
         AND resolved_at IS NULL)
        OR
        (state = 'resolved'
         AND disposition IN (
             'approved', 'denied', 'canceled', 'expired'
         )
         AND result_code != 'confirmation_pending'
         AND resolved_at IS NOT NULL
         AND (
             (response_id IS NULL
              AND response_turn_id IS NULL
              AND response_fingerprint IS NULL
              AND requested_disposition IS NULL
              AND disposition = 'expired')
             OR
             (response_id IS NOT NULL
              AND response_turn_id IS NOT NULL
              AND response_fingerprint IS NOT NULL
              AND requested_disposition IS NOT NULL)
         ))
        OR
        (state = 'invalidated'
         AND disposition = 'invalidated'
         AND requested_disposition IS NULL
         AND result_code != 'confirmation_pending'
         AND response_id IS NULL
         AND response_turn_id IS NULL
         AND response_fingerprint IS NULL
         AND resolved_at IS NOT NULL)
    )
)
'''


CONFIRMATION_RESPONSE_OWNER_INDEX_SQL = '''
CREATE UNIQUE INDEX confirmation_response_owner_idx
ON confirmation_intents (user_id, response_id)
WHERE response_id IS NOT NULL
'''


CONFIRMATION_ONE_PENDING_INDEX_SQL = '''
CREATE UNIQUE INDEX confirmation_one_pending_session_idx
ON confirmation_intents (
    user_id, session_instance_id, generation
)
WHERE state = 'pending'
'''


TEXT_TURN_REQUEST_CLAIMS_SQL = '''
CREATE TABLE text_turn_request_claims (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    turn_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN (
            'confirmation_unrecognized',
            'confirmation_not_pending',
            'confirmation_resolved',
            'confirmation_invalidated'
        )
    ),
    confirmation_request_id TEXT,
    created_at REAL NOT NULL,
    authority_kind TEXT NOT NULL DEFAULT 'none'
        CHECK (authority_kind = 'none'),
    execution_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (execution_authorized = 0),
    consume_once INTEGER NOT NULL DEFAULT 0
        CHECK (consume_once = 0),
    tool_call_id TEXT CHECK (tool_call_id IS NULL),
    mission_id TEXT CHECK (mission_id IS NULL),
    PRIMARY KEY (user_id, request_id),
    UNIQUE (
        user_id, conversation_id, session_instance_id, generation, turn_id
    ),
    FOREIGN KEY (user_id, conversation_id)
        REFERENCES conversation_sessions (user_id, conversation_id)
        ON DELETE CASCADE,
    FOREIGN KEY (confirmation_request_id)
        REFERENCES confirmation_intents (confirmation_request_id)
        ON DELETE CASCADE,
    CHECK (
        (outcome = 'confirmation_not_pending'
         AND confirmation_request_id IS NULL)
        OR
        (outcome != 'confirmation_not_pending'
         AND confirmation_request_id IS NOT NULL)
    )
)
'''


_CONFIRMATION_SCHEMA_OBJECTS = {
    'confirmation_schema_metadata': (
        'table',
        CONFIRMATION_SCHEMA_METADATA_SQL,
    ),
    'confirmation_intents': (
        'table',
        CONFIRMATION_INTENTS_SQL,
    ),
    'confirmation_response_owner_idx': (
        'index',
        CONFIRMATION_RESPONSE_OWNER_INDEX_SQL,
    ),
    'confirmation_one_pending_session_idx': (
        'index',
        CONFIRMATION_ONE_PENDING_INDEX_SQL,
    ),
    'text_turn_request_claims': (
        'table',
        TEXT_TURN_REQUEST_CLAIMS_SQL,
    ),
}


class ConversationNotFoundError(ValidationError):
    """Raised when a user-scoped conversation does not exist."""


class ConversationStateError(ValidationError):
    """Raised when a closed or expired conversation receives a turn."""


class ConversationConflictError(ValidationError):
    """Raised when a request or turn identifier is reused differently."""


class ConversationChangedError(ValidationError):
    """Raised when a session changes while model inference is running."""


class ConfirmationSchemaError(RuntimeError):
    """Raised when confirmation persistence cannot be trusted."""


class ConfirmationIntentNotFoundError(ValidationError):
    """Raised when no owner-scoped confirmation can be selected."""


class ConfirmationIntentConflictError(ValidationError):
    """Raised when a confirmation identity is reused differently."""


class ConfirmationIntentAlreadyTerminalError(ValidationError):
    """Raised when another response already made the intent terminal."""


@dataclass(frozen=True)
class ConversationSession:
    """One user-scoped conversation lifecycle record."""

    conversation_id: str
    user_id: str
    session_instance_id: str
    status: str
    generation: int
    revision: int
    created_at: float
    updated_at: float
    expires_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe session summary."""
        return {
            'conversation_id': self.conversation_id,
            'user_id': self.user_id,
            'session_instance_id': self.session_instance_id,
            'status': self.status,
            'generation': self.generation,
            'revision': self.revision,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'expires_at': self.expires_at,
        }


@dataclass(frozen=True)
class ConversationTurn:
    """One committed user and assistant exchange."""

    conversation_id: str
    user_id: str
    session_instance_id: str
    turn_id: str
    request_id: str
    request_fingerprint: str
    generation: int
    ordinal: int
    user_content: str
    assistant_content: str
    response: Dict[str, Any]
    created_at: float
    completed_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe turn without raw provider proposals."""
        return {
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'turn_id': self.turn_id,
            'request_id': self.request_id,
            'generation': self.generation,
            'ordinal': self.ordinal,
            'user': self.user_content,
            'assistant': self.assistant_content,
            'decision': dict(
                self.response.get('decision', {})
            ),
            'created_at': self.created_at,
            'completed_at': self.completed_at,
        }

    def to_messages(self) -> List[Dict[str, Any]]:
        """Return ordered user and assistant message records."""
        return [
            {
                'sequence': self.ordinal * 2 - 1,
                'turn_id': self.turn_id,
                'role': 'user',
                'content': self.user_content,
                'created_at': self.created_at,
            },
            {
                'sequence': self.ordinal * 2,
                'turn_id': self.turn_id,
                'role': 'assistant',
                'content': self.assistant_content,
                'created_at': self.completed_at,
            },
        ]


@dataclass(frozen=True)
class ConversationSummary:
    """Bounded derived context for turns older than the raw window."""

    summary_id: str
    user_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    summary_revision: int
    content: str
    source_start_ordinal: int
    source_end_ordinal: int
    source_turn_count: int
    source_digest: str
    summarizer: str
    fallback_used: bool
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return summary content with complete source provenance."""
        return {
            'summary_id': self.summary_id,
            'user_id': self.user_id,
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'generation': self.generation,
            'summary_revision': self.summary_revision,
            'content': self.content,
            'source_start_ordinal': self.source_start_ordinal,
            'source_end_ordinal': self.source_end_ordinal,
            'source_turn_count': self.source_turn_count,
            'source_digest': self.source_digest,
            'summarizer': self.summarizer,
            'fallback_used': self.fallback_used,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass(frozen=True)
class BeginTurnToken:
    """Immutable compare-and-swap token for one in-flight turn."""

    user_id: str
    conversation_id: str
    session_instance_id: str
    turn_id: str
    request_id: str
    request_fingerprint: str
    generation: int
    revision: int
    ordinal: int


@dataclass(frozen=True)
class BeginTurnResult:
    """Either a new in-flight token or a durable cached response."""

    session: ConversationSession
    history: Tuple[ConversationTurn, ...]
    summary: Optional[ConversationSummary]
    token: Optional[BeginTurnToken]
    cached_response: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ConversationSnapshot:
    """One generation-consistent session, history, and summary view."""

    session: ConversationSession
    turns: Tuple[ConversationTurn, ...]
    summary: Optional[ConversationSummary]


@dataclass(frozen=True)
class TextTurnRequestClaim:
    """Durable, content-free claim in the unified text request namespace."""

    user_id: str
    request_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    revision: int
    turn_id: str
    request_fingerprint: str
    outcome: str
    confirmation_request_id: Optional[str]
    created_at: float


class SQLiteConversationStore:
    """Thread-safe source of truth for short-term conversation history."""

    def __init__(
        self,
        database_path: str,
        ttl_seconds: int = 1800,
        history_limit: int = 10,
        max_sessions_per_user: int = 100,
        max_turns_per_session: int = 1000,
        summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
        summarizer: Optional[
            ExtractiveConversationSummarizer
        ] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Open a database and initialize the conversation schema."""
        if not database_path:
            raise ValueError('database_path must not be empty')
        self.ttl_seconds = self._bounded_integer(
            ttl_seconds,
            'ttl_seconds',
            60,
            2592000,
        )
        self.history_limit = self._bounded_integer(
            history_limit,
            'history_limit',
            10,
            50,
        )
        self.max_sessions_per_user = self._bounded_integer(
            max_sessions_per_user,
            'max_sessions_per_user',
            1,
            1000,
        )
        self.max_turns_per_session = self._bounded_integer(
            max_turns_per_session,
            'max_turns_per_session',
            10,
            10000,
        )
        self.summary_max_chars = self._bounded_integer(
            summary_max_chars,
            'summary_max_chars',
            256,
            8000,
        )
        self._summarizer = (
            summarizer
            if summarizer is not None
            else ExtractiveConversationSummarizer()
        )
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
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()
        self._secure_file_permissions()

    def _initialize(self) -> None:
        with self._lock:
            if self.database_path != ':memory:':
                self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA foreign_keys=ON')
            self._connection.execute('PRAGMA busy_timeout=5000')
            self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (user_id, conversation_id),
                    CHECK (status IN ('active', 'closed', 'expired'))
                )
                '''
            )
            self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    PRIMARY KEY (
                        user_id,
                        conversation_id,
                        generation,
                        turn_id
                    ),
                    UNIQUE (user_id, request_id),
                    UNIQUE (
                        user_id,
                        conversation_id,
                        generation,
                        ordinal
                    ),
                    FOREIGN KEY (user_id, conversation_id)
                        REFERENCES conversation_sessions (
                            user_id,
                            conversation_id
                        )
                        ON DELETE CASCADE,
                    CHECK (status IN ('pending', 'completed'))
                )
                '''
            )
            self._ensure_session_instance_columns()
            self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    summary_id TEXT NOT NULL,
                    summary_revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    source_start_ordinal INTEGER NOT NULL,
                    source_end_ordinal INTEGER NOT NULL,
                    source_turn_count INTEGER NOT NULL,
                    source_digest TEXT NOT NULL,
                    summarizer TEXT NOT NULL,
                    fallback_used INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (
                        user_id,
                        conversation_id,
                        session_instance_id,
                        generation
                    ),
                    FOREIGN KEY (user_id, conversation_id)
                        REFERENCES conversation_sessions (
                            user_id,
                            conversation_id
                        )
                        ON DELETE CASCADE
                )
                '''
            )
            self._connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS conversation_turns_order_idx
                ON conversation_turns (
                    user_id,
                    conversation_id,
                    generation,
                    ordinal DESC
                )
                '''
            )
            self._connection.execute(
                '''
                CREATE UNIQUE INDEX IF NOT EXISTS
                    conversation_one_pending_idx
                ON conversation_turns (user_id, conversation_id)
                WHERE status = 'pending'
                '''
            )
            self._initialize_confirmation_schema()
            self._connection.commit()

    def _initialize_confirmation_schema(self) -> None:
        """Create or strictly validate the additive confirmation schema."""
        object_names = tuple(_CONFIRMATION_SCHEMA_OBJECTS)
        placeholders = ', '.join('?' for _name in object_names)
        rows = self._connection.execute(
            f'''
            SELECT type, name, sql
            FROM sqlite_master
            WHERE name IN ({placeholders})
            ''',
            object_names,
        ).fetchall()
        found = {str(row['name']): row for row in rows}
        expected_names = set(_CONFIRMATION_SCHEMA_OBJECTS)
        if not found:
            self._connection.execute(CONFIRMATION_SCHEMA_METADATA_SQL)
            self._connection.execute(CONFIRMATION_INTENTS_SQL)
            self._connection.execute(
                CONFIRMATION_RESPONSE_OWNER_INDEX_SQL
            )
            self._connection.execute(
                CONFIRMATION_ONE_PENDING_INDEX_SQL
            )
            self._connection.execute(TEXT_TURN_REQUEST_CLAIMS_SQL)
            self._connection.execute(
                '''
                INSERT INTO confirmation_schema_metadata (
                    singleton, schema_version
                ) VALUES (1, ?)
                ''',
                (CONFIRMATION_STORAGE_SCHEMA_VERSION,),
            )
            return
        if set(found) != expected_names:
            raise ConfirmationSchemaError(
                'confirmation persistence schema is incomplete'
            )
        for name, (object_type, expected_sql) in (
            _CONFIRMATION_SCHEMA_OBJECTS.items()
        ):
            row = found[name]
            if row['type'] != object_type:
                raise ConfirmationSchemaError(
                    'confirmation persistence schema object type '
                    f'is invalid: {name}'
                )
            if self._normalized_schema_sql(row['sql']) != (
                self._normalized_schema_sql(expected_sql)
            ):
                raise ConfirmationSchemaError(
                    'confirmation persistence schema does not match '
                    f'version {CONFIRMATION_STORAGE_SCHEMA_VERSION}: '
                    f'{name}'
                )
        metadata_rows = self._connection.execute(
            '''
            SELECT singleton, schema_version
            FROM confirmation_schema_metadata
            '''
        ).fetchall()
        if (
            len(metadata_rows) != 1
            or int(metadata_rows[0]['singleton']) != 1
            or int(metadata_rows[0]['schema_version'])
            != CONFIRMATION_STORAGE_SCHEMA_VERSION
        ):
            raise ConfirmationSchemaError(
                'confirmation persistence schema version is invalid'
            )

    @staticmethod
    def _normalized_schema_sql(value: Optional[str]) -> str:
        if value is None:
            return ''
        return ' '.join(value.strip().rstrip(';').split()).lower()

    def _ensure_session_instance_columns(self) -> None:
        """Add opaque session identities to databases from version 0.2."""
        session_columns = {
            row['name']
            for row in self._connection.execute(
                'PRAGMA table_info(conversation_sessions)'
            ).fetchall()
        }
        if 'session_instance_id' not in session_columns:
            self._connection.execute(
                '''
                ALTER TABLE conversation_sessions
                ADD COLUMN session_instance_id TEXT
                '''
            )
        turn_columns = {
            row['name']
            for row in self._connection.execute(
                'PRAGMA table_info(conversation_turns)'
            ).fetchall()
        }
        if 'session_instance_id' not in turn_columns:
            self._connection.execute(
                '''
                ALTER TABLE conversation_turns
                ADD COLUMN session_instance_id TEXT
                '''
            )
        rows = self._connection.execute(
            '''
            SELECT user_id, conversation_id, session_instance_id
            FROM conversation_sessions
            '''
        ).fetchall()
        for row in rows:
            instance_id = row['session_instance_id']
            if not instance_id:
                instance_id = str(uuid.uuid4())
                self._connection.execute(
                    '''
                    UPDATE conversation_sessions
                    SET session_instance_id = ?
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (
                        instance_id,
                        row['user_id'],
                        row['conversation_id'],
                    ),
                )
            self._connection.execute(
                '''
                UPDATE conversation_turns
                SET session_instance_id = ?
                WHERE user_id = ? AND conversation_id = ?
                  AND (
                      session_instance_id IS NULL
                      OR session_instance_id = ''
                  )
                ''',
                (
                    instance_id,
                    row['user_id'],
                    row['conversation_id'],
                ),
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

    def create(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
    ) -> ConversationSession:
        """Create or idempotently return one active session."""
        normalized_user = validate_user_id(user_id)
        normalized_id = (
            validate_conversation_id(conversation_id)
            if conversation_id is not None
            else str(uuid.uuid4())
        )
        now = self._now()
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is not None:
                    session = self._session_from_row(row)
                    if session.status == 'active':
                        self._connection.commit()
                        return session
                    raise ConversationConflictError(
                        'conversation_id belongs to a closed or '
                        'expired session'
                    )
                count_row = self._connection.execute(
                    '''
                    SELECT COUNT(*) AS session_count
                    FROM conversation_sessions
                    WHERE user_id = ?
                    ''',
                    (normalized_user,),
                ).fetchone()
                if (
                    int(count_row['session_count'])
                    >= self.max_sessions_per_user
                ):
                    raise ConversationStateError(
                        'conversation session limit reached; '
                        'delete an old session'
                    )
                self._connection.execute(
                    '''
                    INSERT INTO conversation_sessions (
                        user_id,
                        conversation_id,
                        session_instance_id,
                        status,
                        generation,
                        revision,
                        created_at,
                        updated_at,
                        expires_at
                    )
                    VALUES (?, ?, ?, 'active', 1, 0, ?, ?, ?)
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        str(uuid.uuid4()),
                        now,
                        now,
                        now + self.ttl_seconds,
                    ),
                )
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                self._connection.commit()
                return self._session_from_row(row)
            except Exception:
                self._connection.rollback()
                raise

    def get(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Return one session after lazily expiring due sessions."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        now = self._now()
        with self._lock:
            self._expire_and_commit(now)
            row = self._select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            return self._session_from_row(row)

    def get_summary(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[ConversationSummary]:
        """Return the summary for the current session generation."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._expire_and_commit(self._now())
            row = self._select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            session = self._session_from_row(row)
            summary_row = self._select_summary_locked(session)
            return (
                self._summary_from_row(summary_row)
                if summary_row is not None
                else None
            )

    def snapshot(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> ConversationSnapshot:
        """Read session, turns, and summary in one transaction."""
        normalized_limit = self._bounded_integer(
            limit,
            'turn limit',
            1,
            500,
        )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'conversation was not found'
                    )
                session = self._session_from_row(row)
                turns = self._history_locked(
                    normalized_user,
                    normalized_id,
                    session.session_instance_id,
                    session.generation,
                    normalized_limit,
                )
                summary_row = self._select_summary_locked(session)
                summary = (
                    self._summary_from_row(summary_row)
                    if summary_row is not None
                    else None
                )
                self._connection.commit()
                return ConversationSnapshot(
                    session=session,
                    turns=tuple(turns),
                    summary=summary,
                )
            except Exception:
                self._connection.rollback()
                raise

    def begin_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        request_id: str,
        request_fingerprint: str,
        user_content: str,
    ) -> BeginTurnResult:
        """Reserve one ordered turn or return its durable response."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_turn = validate_turn_id(turn_id)
        normalized_request = self._required_text(
            request_id,
            'request_id',
            128,
        )
        normalized_fingerprint = self._required_text(
            request_fingerprint,
            'request_fingerprint',
            128,
        )
        normalized_content = self._required_text(
            user_content,
            'user_content',
            MAX_UTTERANCE_LENGTH,
        )
        now = self._now()
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                session = self._require_active(row)
                response_claim = self._select_text_turn_claim_locked(
                    normalized_user,
                    normalized_request,
                )
                if response_claim is not None:
                    raise ConversationConflictError(
                        'request_id is already owned by a confirmation '
                        'response'
                    )
                existing = self._existing_request_locked(
                    normalized_user,
                    normalized_request,
                )
                if existing is not None:
                    result = self._reuse_existing_locked(
                        existing,
                        normalized_id,
                        normalized_turn,
                        normalized_fingerprint,
                        normalized_content,
                        session,
                    )
                    self._connection.commit()
                    return result
                turn_row = self._connection.execute(
                    '''
                    SELECT *
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND turn_id = ?
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                        normalized_turn,
                    ),
                ).fetchone()
                if turn_row is not None:
                    raise ConversationConflictError(
                        'turn_id was already used in this conversation'
                    )
                claimed_turn = self._connection.execute(
                    '''
                    SELECT 1
                    FROM text_turn_request_claims
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND turn_id = ?
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                        normalized_turn,
                    ),
                ).fetchone()
                if claimed_turn is not None:
                    raise ConversationConflictError(
                        'turn_id is already owned by a confirmation response'
                    )
                pending = self._connection.execute(
                    '''
                    SELECT turn_id
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND status = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                    ),
                ).fetchone()
                if pending is not None:
                    raise ConversationConflictError(
                        'another turn is already in progress'
                    )
                count_row = self._connection.execute(
                    '''
                    SELECT COUNT(*) AS turn_count
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND status = 'completed'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                    ),
                ).fetchone()
                turn_count = int(count_row['turn_count'])
                if turn_count >= self.max_turns_per_session:
                    raise ConversationStateError(
                        'conversation turn limit reached; '
                        'reset or create a new session'
                    )
                summary = self._summary_for_window_locked(
                    session,
                    turn_count,
                    now,
                )
                history = self._history_locked(
                    normalized_user,
                    normalized_id,
                    session.session_instance_id,
                    session.generation,
                    self.history_limit,
                )
                ordinal = turn_count + 1
                self._connection.execute(
                    '''
                    INSERT INTO conversation_turns (
                        user_id,
                        conversation_id,
                        session_instance_id,
                        turn_id,
                        request_id,
                        request_fingerprint,
                        generation,
                        ordinal,
                        status,
                        user_content,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        normalized_turn,
                        normalized_request,
                        normalized_fingerprint,
                        session.generation,
                        ordinal,
                        normalized_content,
                        now,
                    ),
                )
                token = BeginTurnToken(
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=(
                        session.session_instance_id
                    ),
                    turn_id=normalized_turn,
                    request_id=normalized_request,
                    request_fingerprint=normalized_fingerprint,
                    generation=session.generation,
                    revision=session.revision,
                    ordinal=ordinal,
                )
                self._connection.commit()
                return BeginTurnResult(
                    session=session,
                    history=tuple(history),
                    summary=summary,
                    token=token,
                    cached_response=None,
                )
            except Exception:
                self._connection.rollback()
                raise

    def complete_turn(
        self,
        token: BeginTurnToken,
        assistant_content: str,
        response: Dict[str, Any],
        confirmation_draft: Optional['ConfirmationDraft'] = None,
    ) -> Tuple[ConversationSession, ConversationTurn]:
        """Atomically commit one response and optional confirmation."""
        normalized_assistant = self._assistant_text(
            assistant_content
        )
        response_json = self._response_json(response)
        confirmation_record = self._pending_confirmation_record(
            confirmation_draft
        )
        now = self._now()
        changed_error: Optional[ConversationChangedError] = None
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    token.user_id,
                    token.conversation_id,
                )
                if row is None:
                    changed_error = ConversationChangedError(
                        'conversation was deleted during model inference'
                    )
                else:
                    session = self._session_from_row(row)
                    if (
                        session.status != 'active'
                        or (
                            session.session_instance_id
                            != token.session_instance_id
                        )
                        or session.generation != token.generation
                        or session.revision != token.revision
                    ):
                        changed_error = ConversationChangedError(
                            'conversation changed during model inference; '
                            'retry with a new request_id and turn_id'
                        )
                if changed_error is None:
                    cursor = self._connection.execute(
                        '''
                        UPDATE conversation_turns
                        SET status = 'completed',
                            assistant_content = ?,
                            response_json = ?,
                            completed_at = ?
                        WHERE user_id = ?
                          AND conversation_id = ?
                          AND session_instance_id = ?
                          AND turn_id = ?
                          AND request_id = ?
                          AND request_fingerprint = ?
                          AND generation = ?
                          AND ordinal = ?
                          AND status = 'pending'
                        ''',
                        (
                            normalized_assistant,
                            response_json,
                            now,
                            token.user_id,
                            token.conversation_id,
                            token.session_instance_id,
                            token.turn_id,
                            token.request_id,
                            token.request_fingerprint,
                            token.generation,
                            token.ordinal,
                        ),
                    )
                    if cursor.rowcount != 1:
                        changed_error = ConversationChangedError(
                            'pending conversation turn no longer exists'
                        )
                if changed_error is not None:
                    self._delete_pending_locked(token)
                    self._connection.commit()
                    raise changed_error
                self._advance_summary_locked(token, now)
                self._invalidate_pending_confirmations_locked(
                    token.user_id,
                    token.conversation_id,
                    'confirmation_conversation_changed',
                    now,
                )
                self._connection.execute(
                    '''
                    UPDATE conversation_sessions
                    SET revision = revision + 1,
                        updated_at = ?,
                        expires_at = ?
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                    ''',
                    (
                        now,
                        now + self.ttl_seconds,
                        token.user_id,
                        token.conversation_id,
                        token.session_instance_id,
                    ),
                )
                if confirmation_record is not None:
                    self._insert_confirmation_locked(
                        confirmation_record,
                        token,
                        response,
                        now,
                    )
                session_row = self._select_session_locked(
                    token.user_id,
                    token.conversation_id,
                )
                turn_row = self._select_turn_locked(token)
                self._connection.commit()
                return (
                    self._session_from_row(session_row),
                    self._turn_from_row(turn_row),
                )
            except Exception:
                self._connection.rollback()
                raise

    def fail_turn(self, token: BeginTurnToken) -> None:
        """Discard one pending turn after provider or safety failure."""
        with self._lock:
            self._delete_pending_locked(token)
            self._connection.commit()

    def pending_confirmation(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional['ConfirmationRecord']:
        """Return the exact current pending intent, if one exists."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._begin()
            try:
                now = self._now()
                self._expire_due_locked(now)
                session = self._require_active(
                    self._select_session_locked(
                        normalized_user,
                        normalized_id,
                    )
                )
                row = self._connection.execute(
                    '''
                    SELECT *
                    FROM confirmation_intents
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND state = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                    ),
                ).fetchone()
                if row is not None and (
                    int(row['revision']) != session.revision
                ):
                    self._invalidate_confirmation_row_locked(
                        row,
                        'confirmation_conversation_changed',
                        now,
                    )
                    row = None
                record = (
                    self._confirmation_record_from_row(row)
                    if row is not None
                    else None
                )
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def confirmation_for_request(
        self,
        user_id: str,
        agent_request_id: str,
    ) -> 'ConfirmationRecord':
        """Return one durable intent selected by owner and agent request."""
        normalized_user = validate_user_id(user_id)
        normalized_request = self._required_text(
            agent_request_id,
            'agent_request_id',
            128,
        )
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._connection.execute(
                    '''
                    SELECT *
                    FROM confirmation_intents
                    WHERE user_id = ? AND agent_request_id = ?
                    ''',
                    (normalized_user, normalized_request),
                ).fetchone()
                if row is None:
                    raise ConfirmationIntentNotFoundError(
                        'confirmation intent was not found'
                    )
                record = self._confirmation_record_from_row(row)
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def confirmation_for_response(
        self,
        user_id: str,
        response_id: str,
    ) -> Optional['ConfirmationRecord']:
        """Return an owner-scoped response only in its current context."""
        normalized_user = validate_user_id(user_id)
        normalized_response = self._required_text(
            response_id,
            'response_id',
            128,
        )
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._connection.execute(
                    '''
                    SELECT *
                    FROM confirmation_intents
                    WHERE user_id = ? AND response_id = ?
                    ''',
                    (normalized_user, normalized_response),
                ).fetchone()
                record = None
                if row is not None:
                    session_row = self._select_session_locked(
                        normalized_user,
                        row['conversation_id'],
                    )
                    if session_row is not None:
                        session = self._session_from_row(session_row)
                        if (
                            session.status == 'active'
                            and row['session_instance_id']
                            == session.session_instance_id
                            and int(row['generation'])
                            == session.generation
                            and int(row['revision'])
                            == session.revision
                        ):
                            record = self._confirmation_record_from_row(
                                row
                            )
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def text_turn_request_claim(
        self,
        user_id: str,
        request_id: str,
        request_fingerprint: str,
    ) -> Optional[Tuple[
        TextTurnRequestClaim,
        Optional['ConfirmationRecord'],
    ]]:
        """Replay one exact confirmation-side text request claim."""
        normalized_user = validate_user_id(user_id)
        normalized_request = self._required_text(
            request_id,
            'request_id',
            128,
        )
        normalized_fingerprint = self._required_digest(
            request_fingerprint,
            'request_fingerprint',
        )
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                claim = self._select_text_turn_claim_locked(
                    normalized_user,
                    normalized_request,
                )
                if claim is None:
                    self._connection.commit()
                    return None
                if claim.request_fingerprint != normalized_fingerprint:
                    raise ConfirmationIntentConflictError(
                        'text turn request_id was reused with different '
                        'payload'
                    )
                session = self._require_active(
                    self._select_session_locked(
                        normalized_user,
                        claim.conversation_id,
                    )
                )
                self._require_text_turn_claim_context(claim, session)
                record = self._linked_confirmation_locked(claim)
                self._connection.commit()
                return claim, record
            except Exception:
                self._connection.rollback()
                raise

    def has_agent_request(self, user_id: str, request_id: str) -> bool:
        """Return whether the user already owns an Agent request ID."""
        normalized_user = validate_user_id(user_id)
        normalized_request = self._required_text(
            request_id,
            'request_id',
            128,
        )
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                exists = self._existing_request_locked(
                    normalized_user,
                    normalized_request,
                ) is not None
                self._connection.commit()
                return exists
            except Exception:
                self._connection.rollback()
                raise

    def claim_text_turn_response(
        self,
        user_id: str,
        conversation_id: str,
        *,
        request_id: str,
        turn_id: str,
        request_fingerprint: str,
        outcome: str,
        confirmation_request_id: Optional[str] = None,
        now: float,
    ) -> Tuple[
        TextTurnRequestClaim,
        Optional['ConfirmationRecord'],
    ]:
        """Atomically claim an ambiguous or no-pending text response."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_request = self._required_text(
            request_id,
            'request_id',
            128,
        )
        normalized_turn = validate_turn_id(turn_id)
        normalized_fingerprint = self._required_digest(
            request_fingerprint,
            'request_fingerprint',
        )
        if outcome not in {
            'confirmation_unrecognized',
            'confirmation_not_pending',
        }:
            raise ValidationError(
                'text response claim outcome is unsupported'
            )
        normalized_confirmation = None
        if confirmation_request_id is not None:
            normalized_confirmation = self._required_text(
                confirmation_request_id,
                'confirmation_request_id',
                128,
            )
        claimed_at = self._finite_timestamp(now, 'now')
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(claimed_at)
                session = self._require_active(
                    self._select_session_locked(
                        normalized_user,
                        normalized_id,
                    )
                )
                existing = self._select_text_turn_claim_locked(
                    normalized_user,
                    normalized_request,
                )
                if existing is not None:
                    self._require_text_turn_claim_match(
                        existing,
                        conversation_id=normalized_id,
                        session=session,
                        turn_id=normalized_turn,
                        request_fingerprint=normalized_fingerprint,
                        outcome=outcome,
                        confirmation_request_id=normalized_confirmation,
                    )
                    record = self._linked_confirmation_locked(existing)
                    self._connection.commit()
                    return existing, record
                self._require_text_turn_namespace_available_locked(
                    normalized_user,
                    normalized_id,
                    session,
                    normalized_request,
                    normalized_turn,
                )
                pending_row = self._current_pending_confirmation_locked(
                    session
                )
                if outcome == 'confirmation_unrecognized':
                    if (
                        pending_row is None
                        or normalized_confirmation is None
                        or pending_row['confirmation_request_id']
                        != normalized_confirmation
                    ):
                        raise ConfirmationIntentConflictError(
                            'ambiguous response no longer matches the '
                            'pending confirmation'
                        )
                    record = self._confirmation_record_from_row(
                        pending_row
                    )
                else:
                    if normalized_confirmation is not None:
                        raise ConfirmationIntentConflictError(
                            'no-pending response cannot name a confirmation'
                        )
                    if pending_row is not None:
                        raise ConfirmationIntentConflictError(
                            'a confirmation is currently pending'
                        )
                    record = None
                claim = TextTurnRequestClaim(
                    user_id=normalized_user,
                    request_id=normalized_request,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    revision=session.revision,
                    turn_id=normalized_turn,
                    request_fingerprint=normalized_fingerprint,
                    outcome=outcome,
                    confirmation_request_id=normalized_confirmation,
                    created_at=claimed_at,
                )
                self._insert_text_turn_claim_locked(claim)
                self._connection.commit()
                return claim, record
            except Exception:
                self._connection.rollback()
                raise

    def invalidate_confirmation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        result_code: str,
        now: float,
        expected_target_binding_digest: Optional[str] = None,
        response_id: Optional[str] = None,
        response_turn_id: Optional[str] = None,
        text_turn_request_fingerprint: Optional[str] = None,
    ) -> 'ConfirmationRecord':
        """CAS-invalidate the current ticket after target-state change."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_code = self._required_text(
            result_code,
            'result_code',
            128,
        )
        if normalized_code == 'confirmation_pending':
            raise ValidationError(
                'invalidation result_code must be terminal'
            )
        invalidated_at = self._finite_timestamp(now, 'now')
        expected_target = None
        if expected_target_binding_digest is not None:
            expected_target = self._required_text(
                expected_target_binding_digest,
                'expected_target_binding_digest',
                128,
            )
        claim_values = (
            response_id,
            response_turn_id,
            text_turn_request_fingerprint,
        )
        if any(value is not None for value in claim_values) and not all(
            value is not None for value in claim_values
        ):
            raise ValidationError(
                'target-change response claim must be complete'
            )
        normalized_response_id = None
        normalized_response_turn = None
        normalized_text_fingerprint = None
        if response_id is not None:
            normalized_response_id = self._required_text(
                response_id,
                'response_id',
                128,
            )
            normalized_response_turn = validate_turn_id(
                response_turn_id
            )
            normalized_text_fingerprint = self._required_digest(
                text_turn_request_fingerprint,
                'text_turn_request_fingerprint',
            )
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(invalidated_at)
                session = self._require_active(
                    self._select_session_locked(
                        normalized_user,
                        normalized_id,
                    )
                )
                if normalized_response_id is not None:
                    existing_claim = self._select_text_turn_claim_locked(
                        normalized_user,
                        normalized_response_id,
                    )
                    if existing_claim is not None:
                        self._require_text_turn_claim_match(
                            existing_claim,
                            conversation_id=normalized_id,
                            session=session,
                            turn_id=normalized_response_turn,
                            request_fingerprint=(
                                normalized_text_fingerprint
                            ),
                            outcome='confirmation_invalidated',
                            confirmation_request_id=(
                                existing_claim.confirmation_request_id
                            ),
                        )
                        replay = self._linked_confirmation_locked(
                            existing_claim
                        )
                        if (
                            replay is None
                            or replay.disposition != 'invalidated'
                            or replay.result_code != normalized_code
                        ):
                            raise ConfirmationIntentConflictError(
                                'target-change response claim does not '
                                'match its terminal confirmation'
                            )
                        self._connection.commit()
                        return replay
                    self._require_text_turn_namespace_available_locked(
                        normalized_user,
                        normalized_id,
                        session,
                        normalized_response_id,
                        normalized_response_turn,
                    )
                row = self._connection.execute(
                    '''
                    SELECT *
                    FROM confirmation_intents
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND state = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                    ),
                ).fetchone()
                if row is None:
                    terminal = self._connection.execute(
                        '''
                        SELECT 1
                        FROM confirmation_intents
                        WHERE user_id = ?
                          AND conversation_id = ?
                          AND session_instance_id = ?
                          AND generation = ?
                        LIMIT 1
                        ''',
                        (
                            normalized_user,
                            normalized_id,
                            session.session_instance_id,
                            session.generation,
                        ),
                    ).fetchone()
                    if terminal is not None:
                        raise ConfirmationIntentAlreadyTerminalError(
                            'confirmation intent is already terminal'
                        )
                    raise ConfirmationIntentNotFoundError(
                        'pending confirmation intent was not found'
                    )
                target_digest = (
                    row['target_binding_digest']
                    if expected_target is None
                    else expected_target
                )
                self._require_confirmation_context_locked(
                    row,
                    session,
                    target_digest,
                )
                if invalidated_at < float(row['issued_at']):
                    raise ConfirmationIntentConflictError(
                        'confirmation invalidation predates proposal'
                    )
                record = self._confirmation_record_from_row(row)
                invalidated = record.invalidate(
                    normalized_code,
                    resolved_at=invalidated_at,
                )
                cursor = self._write_confirmation_record_locked(
                    row,
                    invalidated,
                    invalidated_at,
                    expected_state='pending',
                )
                if cursor.rowcount != 1:
                    raise ConfirmationIntentConflictError(
                        'confirmation invalidation compare-and-swap failed'
                    )
                if normalized_response_id is not None:
                    self._insert_text_turn_claim_locked(
                        TextTurnRequestClaim(
                            user_id=normalized_user,
                            request_id=normalized_response_id,
                            conversation_id=normalized_id,
                            session_instance_id=(
                                session.session_instance_id
                            ),
                            generation=session.generation,
                            revision=session.revision,
                            turn_id=normalized_response_turn,
                            request_fingerprint=(
                                normalized_text_fingerprint
                            ),
                            outcome='confirmation_invalidated',
                            confirmation_request_id=(
                                invalidated.confirmation_request_id
                            ),
                            created_at=invalidated_at,
                        )
                    )
                self._connection.commit()
                return invalidated
            except Exception:
                self._connection.rollback()
                raise

    def resolve_confirmation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        response_id: str,
        response_fingerprint: str,
        disposition: str,
        now: float,
        current_target_binding_digest: str,
        response_turn_id: Optional[str] = None,
        text_turn_request_fingerprint: Optional[str] = None,
    ) -> 'ConfirmationRecord':
        """Strict-CAS one verified response into a terminal record."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_response_id = self._required_text(
            response_id,
            'response_id',
            128,
        )
        normalized_response_fingerprint = self._required_text(
            response_fingerprint,
            'response_fingerprint',
            128,
        )
        if response_turn_id is None:
            legacy_digest = hashlib.sha256(
                normalized_response_id.encode('utf-8')
            ).hexdigest()[:32]
            normalized_response_turn = (
                f'legacy-confirmation-response-{legacy_digest}'
            )
        else:
            normalized_response_turn = validate_turn_id(
                response_turn_id
            )
        normalized_text_fingerprint = None
        if text_turn_request_fingerprint is not None:
            if response_turn_id is None:
                raise ValidationError(
                    'text response claim requires response_turn_id'
                )
            normalized_text_fingerprint = self._required_digest(
                text_turn_request_fingerprint,
                'text_turn_request_fingerprint',
            )
        if disposition not in {'approve', 'deny', 'cancel'}:
            raise ValidationError(
                'confirmation disposition is unsupported'
            )
        resolved_at = self._finite_timestamp(now, 'now')
        target_digest = self._required_text(
            current_target_binding_digest,
            'current_target_binding_digest',
            128,
        )
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(resolved_at)
                session = self._require_active(
                    self._select_session_locked(
                        normalized_user,
                        normalized_id,
                    )
                )
                existing_claim = self._select_text_turn_claim_locked(
                    normalized_user,
                    normalized_response_id,
                )
                if existing_claim is not None:
                    if normalized_text_fingerprint is None:
                        raise ConfirmationIntentConflictError(
                            'response_id is owned by a text turn claim'
                        )
                    self._require_text_turn_claim_match(
                        existing_claim,
                        conversation_id=normalized_id,
                        session=session,
                        turn_id=normalized_response_turn,
                        request_fingerprint=normalized_text_fingerprint,
                        outcome='confirmation_resolved',
                        confirmation_request_id=(
                            existing_claim.confirmation_request_id
                        ),
                    )
                    replay = self._linked_confirmation_locked(
                        existing_claim
                    )
                    if (
                        replay is None
                        or replay.response_id != normalized_response_id
                        or replay.response_turn_id
                        != normalized_response_turn
                        or replay.response_fingerprint
                        != normalized_response_fingerprint
                        or replay.requested_disposition != disposition
                    ):
                        raise ConfirmationIntentConflictError(
                            'confirmation response claim does not match '
                            'its terminal record'
                        )
                    self._require_confirmation_context_locked(
                        self._confirmation_row_for_record_locked(replay),
                        session,
                        target_digest,
                    )
                    self._connection.commit()
                    return replay
                self._require_text_turn_namespace_available_locked(
                    normalized_user,
                    normalized_id,
                    session,
                    normalized_response_id,
                    normalized_response_turn,
                )
                response_owner = self._connection.execute(
                    '''
                    SELECT *
                    FROM confirmation_intents
                    WHERE user_id = ? AND response_id = ?
                    ''',
                    (normalized_user, normalized_response_id),
                ).fetchone()
                if response_owner is not None:
                    self._require_confirmation_context_locked(
                        response_owner,
                        session,
                        target_digest,
                    )
                    replay = self._confirmation_record_from_row(
                        response_owner
                    )
                    if (
                        replay.response_fingerprint
                        != normalized_response_fingerprint
                        or replay.response_turn_id
                        != normalized_response_turn
                        or replay.requested_disposition != disposition
                    ):
                        raise ConfirmationIntentConflictError(
                            'confirmation response_id was reused with '
                            'different payload'
                        )
                    if normalized_text_fingerprint is not None:
                        self._insert_text_turn_claim_locked(
                            TextTurnRequestClaim(
                                user_id=normalized_user,
                                request_id=normalized_response_id,
                                conversation_id=normalized_id,
                                session_instance_id=(
                                    session.session_instance_id
                                ),
                                generation=session.generation,
                                revision=session.revision,
                                turn_id=normalized_response_turn,
                                request_fingerprint=(
                                    normalized_text_fingerprint
                                ),
                                outcome='confirmation_resolved',
                                confirmation_request_id=(
                                    replay.confirmation_request_id
                                ),
                                created_at=resolved_at,
                            )
                        )
                    self._connection.commit()
                    return replay
                row = self._connection.execute(
                    '''
                    SELECT *
                    FROM confirmation_intents
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND state = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                    ),
                ).fetchone()
                if row is None:
                    terminal = self._connection.execute(
                        '''
                        SELECT *
                        FROM confirmation_intents
                        WHERE user_id = ?
                          AND conversation_id = ?
                          AND session_instance_id = ?
                          AND generation = ?
                        ORDER BY revision DESC, updated_at DESC
                        LIMIT 1
                        ''',
                        (
                            normalized_user,
                            normalized_id,
                            session.session_instance_id,
                            session.generation,
                        ),
                    ).fetchone()
                    if terminal is not None:
                        raise ConfirmationIntentAlreadyTerminalError(
                            'confirmation intent is already terminal'
                        )
                    raise ConfirmationIntentNotFoundError(
                        'pending confirmation intent was not found'
                    )
                self._require_confirmation_context_locked(
                    row,
                    session,
                    target_digest,
                )
                record = self._confirmation_record_from_row(row)
                resolution = self._verified_confirmation_resolution(
                    record,
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    response_id=normalized_response_id,
                    response_turn_id=normalized_response_turn,
                    response_fingerprint=(
                        normalized_response_fingerprint
                    ),
                    disposition=disposition,
                )
                terminal_record = record.resolve(
                    resolution,
                    resolved_at=resolved_at,
                )
                if terminal_record.disposition == 'pending':
                    raise ConfirmationIntentConflictError(
                        'confirmation response did not resolve intent'
                    )
                cursor = self._write_confirmation_record_locked(
                    row,
                    terminal_record,
                    resolved_at,
                    expected_state='pending',
                )
                if cursor.rowcount != 1:
                    raise ConfirmationIntentConflictError(
                        'confirmation compare-and-swap failed'
                    )
                if normalized_text_fingerprint is not None:
                    self._insert_text_turn_claim_locked(
                        TextTurnRequestClaim(
                            user_id=normalized_user,
                            request_id=normalized_response_id,
                            conversation_id=normalized_id,
                            session_instance_id=(
                                session.session_instance_id
                            ),
                            generation=session.generation,
                            revision=session.revision,
                            turn_id=normalized_response_turn,
                            request_fingerprint=(
                                normalized_text_fingerprint
                            ),
                            outcome='confirmation_resolved',
                            confirmation_request_id=(
                                terminal_record.confirmation_request_id
                            ),
                            created_at=resolved_at,
                        )
                    )
                self._connection.commit()
                return terminal_record
            except Exception:
                self._connection.rollback()
                raise

    def list_turns(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> List[ConversationTurn]:
        """Return completed turns in chronological order."""
        normalized_limit = self._bounded_integer(
            limit,
            'turn limit',
            1,
            500,
        )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._expire_and_commit(self._now())
            row = self._select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            session = self._session_from_row(row)
            return self._history_locked(
                normalized_user,
                normalized_id,
                session.session_instance_id,
                session.generation,
                normalized_limit,
            )

    def reset(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Delete all turns and start a new active generation."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        now = self._now()
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                session = self._require_active(row)
                self._invalidate_pending_confirmations_locked(
                    normalized_user,
                    normalized_id,
                    'confirmation_conversation_changed',
                    now,
                )
                self._connection.execute(
                    '''
                    DELETE FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND status = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                    ),
                )
                self._connection.execute(
                    '''
                    DELETE FROM conversation_summaries
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (normalized_user, normalized_id),
                )
                self._connection.execute(
                    '''
                    UPDATE conversation_sessions
                    SET generation = generation + 1,
                        revision = revision + 1,
                        updated_at = ?,
                        expires_at = ?
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (
                        now,
                        now + self.ttl_seconds,
                        normalized_user,
                        normalized_id,
                    ),
                )
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                self._connection.commit()
                return self._session_from_row(row)
            except Exception:
                self._connection.rollback()
                raise

    def close_session(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Close an active session without deleting committed history."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        now = self._now()
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'conversation was not found'
                    )
                session = self._session_from_row(row)
                if session.status == 'expired':
                    raise ConversationStateError(
                        'conversation has expired'
                    )
                self._invalidate_pending_confirmations_locked(
                    normalized_user,
                    normalized_id,
                    'confirmation_session_closed',
                    now,
                )
                self._connection.execute(
                    '''
                    DELETE FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND status = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                    ),
                )
                if session.status == 'active':
                    self._connection.execute(
                        '''
                        UPDATE conversation_sessions
                        SET status = 'closed',
                            revision = revision + 1,
                            updated_at = ?
                        WHERE user_id = ? AND conversation_id = ?
                        ''',
                        (now, normalized_user, normalized_id),
                    )
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                self._connection.commit()
                return self._session_from_row(row)
            except Exception:
                self._connection.rollback()
                raise

    def delete(
        self,
        user_id: str,
        conversation_id: str,
    ) -> bool:
        """Delete one session and all of its turns."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            cursor = self._connection.execute(
                '''
                DELETE FROM conversation_sessions
                WHERE user_id = ? AND conversation_id = ?
                ''',
                (normalized_user, normalized_id),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def purge_expired(self) -> int:
        """Mark every due active session as expired."""
        with self._lock:
            self._begin()
            try:
                count = self._expire_due_locked(self._now())
                self._connection.commit()
                return count
            except Exception:
                self._connection.rollback()
                raise

    @staticmethod
    def _pending_confirmation_record(
        draft: Optional['ConfirmationDraft'],
    ) -> Optional['ConfirmationRecord']:
        if draft is None:
            return None
        from malbut_agent_server.text_confirmation import (
            ConfirmationDraft,
            ConfirmationRecord,
        )

        if not isinstance(draft, ConfirmationDraft):
            raise TypeError(
                'confirmation_draft must be a ConfirmationDraft'
            )
        return ConfirmationRecord.pending(draft)

    @staticmethod
    def _verified_confirmation_resolution(
        record: 'ConfirmationRecord',
        *,
        user_id: str,
        conversation_id: str,
        session_instance_id: str,
        generation: int,
        response_id: str,
        response_turn_id: str,
        response_fingerprint: str,
        disposition: str,
    ) -> 'ConfirmationResolution':
        from malbut_agent_server.text_confirmation import (
            ConfirmationResolution,
        )

        return ConfirmationResolution.from_verified_response(
            record,
            caller_user_id=user_id,
            caller_conversation_id=conversation_id,
            caller_session_instance_id=session_instance_id,
            caller_generation=generation,
            response_id=response_id,
            response_turn_id=response_turn_id,
            response_fingerprint=response_fingerprint,
            requested_disposition=disposition,
        )

    def _insert_confirmation_locked(
        self,
        record: 'ConfirmationRecord',
        token: BeginTurnToken,
        response: Dict[str, Any],
        now: float,
    ) -> None:
        if record.disposition != 'pending':
            raise ConfirmationIntentConflictError(
                'new confirmation intent must be pending'
            )
        expected = (
            record.user_id == token.user_id
            and record.conversation_id == token.conversation_id
            and record.session_instance_id
            == token.session_instance_id
            and record.generation == token.generation
            and record.revision == token.revision + 1
            and record.ordinal == token.ordinal
            and record.turn_id == token.turn_id
            and record.request_id == token.request_id
        )
        if not expected:
            raise ConversationChangedError(
                'confirmation draft does not match completed turn'
            )
        if float(record.issued_at) > now:
            raise ConfirmationIntentConflictError(
                'confirmation intent was issued in the future'
            )
        if float(record.expires_at) <= now:
            raise ConfirmationIntentConflictError(
                'confirmation intent has expired'
            )
        self._validate_confirmation_response(record, response)
        values = self._confirmation_storage_values(record, now)
        try:
            self._connection.execute(
                '''
                INSERT INTO confirmation_intents (
                    schema_version,
                    confirmation_request_id,
                    user_id,
                    conversation_id,
                    session_instance_id,
                    generation,
                    revision,
                    ordinal,
                    turn_id,
                    agent_request_id,
                    decision_id,
                    tool_name,
                    arguments_digest,
                    target_binding_digest,
                    proposal_fingerprint,
                    issued_at,
                    expires_at,
                    state,
                    disposition,
                    requested_disposition,
                    result_code,
                    response_id,
                    response_turn_id,
                    response_fingerprint,
                    resolved_at,
                    record_json,
                    created_at,
                    updated_at,
                    authority_kind,
                    execution_authorized,
                    consume_once,
                    tool_call_id,
                    mission_id
                ) VALUES (
                    :schema_version,
                    :confirmation_request_id,
                    :user_id,
                    :conversation_id,
                    :session_instance_id,
                    :generation,
                    :revision,
                    :ordinal,
                    :turn_id,
                    :agent_request_id,
                    :decision_id,
                    :tool_name,
                    :arguments_digest,
                    :target_binding_digest,
                    :proposal_fingerprint,
                    :issued_at,
                    :expires_at,
                    :state,
                    :disposition,
                    :requested_disposition,
                    :result_code,
                    :response_id,
                    :response_turn_id,
                    :response_fingerprint,
                    :resolved_at,
                    :record_json,
                    :created_at,
                    :updated_at,
                    'none', 0, 0, NULL, NULL
                )
                ''',
                values,
            )
        except sqlite3.IntegrityError as error:
            raise ConfirmationIntentConflictError(
                'confirmation intent conflicts with durable state'
            ) from error

    def _select_text_turn_claim_locked(
        self,
        user_id: str,
        request_id: str,
    ) -> Optional[TextTurnRequestClaim]:
        row = self._connection.execute(
            '''
            SELECT *
            FROM text_turn_request_claims
            WHERE user_id = ? AND request_id = ?
            ''',
            (user_id, request_id),
        ).fetchone()
        return (
            self._text_turn_claim_from_row(row)
            if row is not None
            else None
        )

    def _text_turn_claim_from_row(
        self,
        row: sqlite3.Row,
    ) -> TextTurnRequestClaim:
        try:
            if int(row['schema_version']) != TEXT_TURN_CLAIM_SCHEMA_VERSION:
                raise ValueError('unsupported claim schema')
            claim = TextTurnRequestClaim(
                user_id=validate_user_id(row['user_id']),
                request_id=self._required_text(
                    row['request_id'],
                    'request_id',
                    128,
                ),
                conversation_id=validate_conversation_id(
                    row['conversation_id']
                ),
                session_instance_id=self._required_text(
                    row['session_instance_id'],
                    'session_instance_id',
                    128,
                ),
                generation=self._bounded_integer(
                    row['generation'],
                    'generation',
                    1,
                    2 ** 63 - 1,
                ),
                revision=self._bounded_integer(
                    row['revision'],
                    'revision',
                    0,
                    2 ** 63 - 1,
                ),
                turn_id=validate_turn_id(row['turn_id']),
                request_fingerprint=self._required_digest(
                    row['request_fingerprint'],
                    'request_fingerprint',
                ),
                outcome=self._required_text(
                    row['outcome'],
                    'outcome',
                    64,
                ),
                confirmation_request_id=(
                    None
                    if row['confirmation_request_id'] is None
                    else self._required_text(
                        row['confirmation_request_id'],
                        'confirmation_request_id',
                        128,
                    )
                ),
                created_at=self._finite_timestamp(
                    row['created_at'],
                    'created_at',
                ),
            )
        except Exception as error:
            raise ConfirmationSchemaError(
                'stored text turn claim cannot be trusted'
            ) from error
        if (
            claim.outcome not in TEXT_TURN_CLAIM_OUTCOMES
            or (
                claim.outcome == 'confirmation_not_pending'
                and claim.confirmation_request_id is not None
            )
            or (
                claim.outcome != 'confirmation_not_pending'
                and claim.confirmation_request_id is None
            )
            or row['authority_kind'] != 'none'
            or int(row['execution_authorized']) != 0
            or int(row['consume_once']) != 0
            or row['tool_call_id'] is not None
            or row['mission_id'] is not None
        ):
            raise ConfirmationSchemaError(
                'stored text turn claim unexpectedly carries authority'
            )
        return claim

    def _insert_text_turn_claim_locked(
        self,
        claim: TextTurnRequestClaim,
    ) -> None:
        if claim.outcome not in TEXT_TURN_CLAIM_OUTCOMES:
            raise ValidationError('text turn claim outcome is unsupported')
        try:
            self._connection.execute(
                '''
                INSERT INTO text_turn_request_claims (
                    schema_version,
                    user_id,
                    request_id,
                    conversation_id,
                    session_instance_id,
                    generation,
                    revision,
                    turn_id,
                    request_fingerprint,
                    outcome,
                    confirmation_request_id,
                    created_at,
                    authority_kind,
                    execution_authorized,
                    consume_once,
                    tool_call_id,
                    mission_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'none', 0, 0, NULL, NULL
                )
                ''',
                (
                    TEXT_TURN_CLAIM_SCHEMA_VERSION,
                    claim.user_id,
                    claim.request_id,
                    claim.conversation_id,
                    claim.session_instance_id,
                    claim.generation,
                    claim.revision,
                    claim.turn_id,
                    claim.request_fingerprint,
                    claim.outcome,
                    claim.confirmation_request_id,
                    claim.created_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConfirmationIntentConflictError(
                'text turn request claim conflicts with durable state'
            ) from error

    def _require_text_turn_namespace_available_locked(
        self,
        user_id: str,
        conversation_id: str,
        session: ConversationSession,
        request_id: str,
        turn_id: str,
    ) -> None:
        existing_claim = self._select_text_turn_claim_locked(
            user_id,
            request_id,
        )
        if existing_claim is not None:
            raise ConfirmationIntentConflictError(
                'text response request_id is already claimed'
            )
        if self._existing_request_locked(user_id, request_id) is not None:
            raise ConfirmationIntentConflictError(
                'request_id is already owned by an agent turn'
            )
        turn_owner = self._connection.execute(
            '''
            SELECT 1
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND turn_id = ?
            LIMIT 1
            ''',
            (
                user_id,
                conversation_id,
                session.session_instance_id,
                session.generation,
                turn_id,
            ),
        ).fetchone()
        if turn_owner is not None:
            raise ConfirmationIntentConflictError(
                'turn_id is already owned by an agent turn'
            )

    @staticmethod
    def _require_text_turn_claim_context(
        claim: TextTurnRequestClaim,
        session: ConversationSession,
    ) -> None:
        if (
            claim.user_id != session.user_id
            or claim.conversation_id != session.conversation_id
            or claim.session_instance_id != session.session_instance_id
            or claim.generation != session.generation
            or claim.revision > session.revision
        ):
            raise ConversationChangedError(
                'text turn response context changed'
            )

    def _require_text_turn_claim_match(
        self,
        claim: TextTurnRequestClaim,
        *,
        conversation_id: str,
        session: ConversationSession,
        turn_id: str,
        request_fingerprint: str,
        outcome: str,
        confirmation_request_id: Optional[str],
    ) -> None:
        self._require_text_turn_claim_context(claim, session)
        if (
            claim.conversation_id != conversation_id
            or claim.turn_id != turn_id
            or claim.request_fingerprint != request_fingerprint
            or claim.outcome != outcome
            or claim.confirmation_request_id != confirmation_request_id
        ):
            raise ConfirmationIntentConflictError(
                'text turn request claim was reused with different payload'
            )

    def _linked_confirmation_locked(
        self,
        claim: TextTurnRequestClaim,
    ) -> Optional['ConfirmationRecord']:
        if claim.confirmation_request_id is None:
            return None
        row = self._connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE confirmation_request_id = ?
            ''',
            (claim.confirmation_request_id,),
        ).fetchone()
        if row is None:
            raise ConfirmationSchemaError(
                'text turn claim lost its confirmation record'
            )
        record = self._confirmation_record_from_row(row)
        if (
            record.user_id != claim.user_id
            or record.conversation_id != claim.conversation_id
            or record.session_instance_id != claim.session_instance_id
            or record.generation != claim.generation
            or record.revision != claim.revision
        ):
            raise ConfirmationSchemaError(
                'text turn claim confirmation binding is invalid'
            )
        return record

    def _confirmation_row_for_record_locked(
        self,
        record: 'ConfirmationRecord',
    ) -> sqlite3.Row:
        row = self._connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE confirmation_request_id = ?
            ''',
            (record.confirmation_request_id,),
        ).fetchone()
        if row is None or self._confirmation_record_from_row(row) != record:
            raise ConfirmationSchemaError(
                'confirmation record is not durably bound'
            )
        return row

    def _current_pending_confirmation_locked(
        self,
        session: ConversationSession,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND revision = ?
              AND state = 'pending'
            ''',
            (
                session.user_id,
                session.conversation_id,
                session.session_instance_id,
                session.generation,
                session.revision,
            ),
        ).fetchone()

    @staticmethod
    def _validate_confirmation_response(
        record: 'ConfirmationRecord',
        response: Dict[str, Any],
    ) -> None:
        try:
            if response['schema_version'] != 3:
                raise ValueError(
                    'confirmation source response lacks provenance'
                )
            safety_binding = response['safety_binding']
            if (
                type(safety_binding) is not dict
                or frozenset(safety_binding) != frozenset({
                    'state_evidence_id',
                    'state_observed_at',
                    'safety_policy_revision',
                })
            ):
                raise ValueError('confirmation safety binding is invalid')
            if (
                type(safety_binding['state_evidence_id']) is not str
                or type(safety_binding['state_observed_at'])
                not in {int, float}
                or type(safety_binding['safety_policy_revision']) is not str
                or not math.isfinite(
                    float(safety_binding['state_observed_at'])
                )
            ):
                raise ValueError('confirmation safety binding is invalid')
            public = response['public']
            conversation = public['conversation']
            decision = public['decision']
            safety = public['safety']
            execution = public['execution']
            arguments = decision['arguments']
            arguments_json = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            )
            arguments_digest = hashlib.sha256(
                arguments_json.encode('utf-8')
            ).hexdigest()
            session_instance = conversation.get(
                'session_instance_id',
                record.session_instance_id,
            )
            matches = (
                isinstance(public, dict)
                and public['request_id'] == record.request_id
                and conversation['conversation_id']
                == record.conversation_id
                and session_instance == record.session_instance_id
                and conversation['turn_id'] == record.turn_id
                and conversation['generation'] == record.generation
                and conversation['revision'] == record.revision
                and conversation['ordinal'] == record.ordinal
                and decision['type'] == 'tool_call'
                and decision['tool_name'] == record.tool_name
                and arguments_digest == record.arguments_digest
                and safety['allowed'] is True
                and execution['decision_id'] == record.decision_id
                and float(execution['issued_at'])
                == float(record.issued_at)
                and math.isfinite(float(execution['expires_at']))
                and float(execution['expires_at'])
                > float(record.issued_at)
                and safety_binding['state_evidence_id']
                == record.state_evidence_id
                and float(safety_binding['state_observed_at'])
                == float(record.state_observed_at)
                and safety_binding['safety_policy_revision']
                == record.safety_policy_revision
                and execution['proposal_authorized'] is True
                and execution['state_trusted'] is True
                and execution['authorized'] is False
                and execution['consume_once'] is False
                and execution['tool_call_id'] is None
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            raise ConfirmationIntentConflictError(
                'confirmation source response is invalid'
            ) from error
        if not matches:
            raise ConfirmationIntentConflictError(
                'confirmation draft does not match source response'
            )

    @staticmethod
    def _confirmation_state(record: 'ConfirmationRecord') -> str:
        if record.disposition == 'pending':
            return 'pending'
        if record.disposition == 'invalidated':
            return 'invalidated'
        return 'resolved'

    def _confirmation_storage_values(
        self,
        record: 'ConfirmationRecord',
        updated_at: float,
        *,
        created_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        private_value = record.to_private_dict()
        if not isinstance(private_value, dict):
            raise ConfirmationSchemaError(
                'confirmation private record must be an object'
            )
        try:
            record_json = json.dumps(
                private_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ConfirmationSchemaError(
                'confirmation record is not JSON-safe'
            ) from error
        if len(record_json) > MAX_CONFIRMATION_JSON_LENGTH:
            raise ValidationError('confirmation record is too large')
        return {
            'schema_version': CONFIRMATION_STORAGE_SCHEMA_VERSION,
            'confirmation_request_id': record.confirmation_request_id,
            'user_id': record.user_id,
            'conversation_id': record.conversation_id,
            'session_instance_id': record.session_instance_id,
            'generation': record.generation,
            'revision': record.revision,
            'ordinal': record.ordinal,
            'turn_id': record.turn_id,
            'agent_request_id': record.request_id,
            'decision_id': record.decision_id,
            'tool_name': record.tool_name,
            'arguments_digest': record.arguments_digest,
            'target_binding_digest': record.target_binding_digest,
            'proposal_fingerprint': record.proposal_fingerprint,
            'issued_at': record.issued_at,
            'expires_at': record.expires_at,
            'state': self._confirmation_state(record),
            'disposition': record.disposition,
            'requested_disposition': record.requested_disposition,
            'result_code': record.result_code,
            'response_id': record.response_id,
            'response_turn_id': record.response_turn_id,
            'response_fingerprint': record.response_fingerprint,
            'resolved_at': record.resolved_at,
            'record_json': record_json,
            'created_at': (
                updated_at if created_at is None else created_at
            ),
            'updated_at': updated_at,
        }

    def _confirmation_record_from_row(
        self,
        row: sqlite3.Row,
    ) -> 'ConfirmationRecord':
        from malbut_agent_server.text_confirmation import (
            ConfirmationRecord,
        )

        stored_json = row['record_json']
        if (
            not isinstance(stored_json, str)
            or len(stored_json) > MAX_CONFIRMATION_JSON_LENGTH
        ):
            raise ConfirmationSchemaError(
                'stored confirmation record is invalid'
            )
        try:
            private_value = json.loads(stored_json)
            record = ConfirmationRecord.from_private_dict(
                private_value
            )
        except Exception as error:
            raise ConfirmationSchemaError(
                'stored confirmation record cannot be trusted'
            ) from error
        expected = self._confirmation_storage_values(
            record,
            float(row['updated_at']),
            created_at=float(row['created_at']),
        )
        indexed_fields = (
            'confirmation_request_id',
            'user_id',
            'conversation_id',
            'session_instance_id',
            'generation',
            'revision',
            'ordinal',
            'turn_id',
            'agent_request_id',
            'decision_id',
            'tool_name',
            'arguments_digest',
            'target_binding_digest',
            'proposal_fingerprint',
            'issued_at',
            'expires_at',
            'state',
            'disposition',
            'requested_disposition',
            'result_code',
            'response_id',
            'response_turn_id',
            'response_fingerprint',
            'resolved_at',
            'record_json',
        )
        if any(row[name] != expected[name] for name in indexed_fields):
            raise ConfirmationSchemaError(
                'stored confirmation indexes do not match record'
            )
        if (
            row['authority_kind'] != 'none'
            or int(row['execution_authorized']) != 0
            or int(row['consume_once']) != 0
            or row['tool_call_id'] is not None
            or row['mission_id'] is not None
            or record.execution_authorized is not False
            or record.consume_once is not False
        ):
            raise ConfirmationSchemaError(
                'stored confirmation unexpectedly carries authority'
            )
        return record

    def _write_confirmation_record_locked(
        self,
        old_row: sqlite3.Row,
        record: 'ConfirmationRecord',
        updated_at: float,
        *,
        expected_state: str,
    ) -> sqlite3.Cursor:
        values = self._confirmation_storage_values(
            record,
            updated_at,
            created_at=float(old_row['created_at']),
        )
        values.update({
            'expected_state': expected_state,
            'old_record_json': old_row['record_json'],
            'old_updated_at': old_row['updated_at'],
        })
        try:
            return self._connection.execute(
                '''
                UPDATE confirmation_intents
                SET state = :state,
                    disposition = :disposition,
                    requested_disposition = :requested_disposition,
                    result_code = :result_code,
                    response_id = :response_id,
                    response_turn_id = :response_turn_id,
                    response_fingerprint = :response_fingerprint,
                    resolved_at = :resolved_at,
                    record_json = :record_json,
                    updated_at = :updated_at
                WHERE confirmation_request_id =
                          :confirmation_request_id
                  AND user_id = :user_id
                  AND conversation_id = :conversation_id
                  AND session_instance_id = :session_instance_id
                  AND generation = :generation
                  AND revision = :revision
                  AND proposal_fingerprint = :proposal_fingerprint
                  AND target_binding_digest = :target_binding_digest
                  AND state = :expected_state
                  AND record_json = :old_record_json
                  AND updated_at = :old_updated_at
                ''',
                values,
            )
        except sqlite3.IntegrityError as error:
            raise ConfirmationIntentConflictError(
                'confirmation terminal state conflicts with durable state'
            ) from error

    def _invalidate_confirmation_row_locked(
        self,
        row: sqlite3.Row,
        result_code: str,
        now: float,
    ) -> None:
        record = self._confirmation_record_from_row(row)
        invalidated = record.invalidate(
            result_code,
            resolved_at=now,
        )
        cursor = self._write_confirmation_record_locked(
            row,
            invalidated,
            now,
            expected_state='pending',
        )
        if cursor.rowcount != 1:
            raise ConfirmationIntentConflictError(
                'confirmation invalidation compare-and-swap failed'
            )

    def _invalidate_pending_confirmations_locked(
        self,
        user_id: str,
        conversation_id: str,
        result_code: str,
        now: float,
    ) -> None:
        rows = self._connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE user_id = ?
              AND conversation_id = ?
              AND state = 'pending'
            ''',
            (user_id, conversation_id),
        ).fetchall()
        for row in rows:
            self._invalidate_confirmation_row_locked(
                row,
                result_code,
                now,
            )

    def _expire_due_confirmations_locked(self, now: float) -> None:
        rows = self._connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE state = 'pending' AND expires_at <= ?
            ORDER BY expires_at, confirmation_request_id
            ''',
            (now,),
        ).fetchall()
        for row in rows:
            record = self._confirmation_record_from_row(row)
            expired = record.expire(resolved_at=now)
            cursor = self._write_confirmation_record_locked(
                row,
                expired,
                now,
                expected_state='pending',
            )
            if cursor.rowcount != 1:
                raise ConfirmationIntentConflictError(
                    'confirmation expiry compare-and-swap failed'
                )

    @staticmethod
    def _require_confirmation_context_locked(
        row: sqlite3.Row,
        session: ConversationSession,
        target_binding_digest: str,
    ) -> None:
        if (
            row['user_id'] != session.user_id
            or row['conversation_id'] != session.conversation_id
            or row['session_instance_id']
            != session.session_instance_id
            or int(row['generation']) != session.generation
            or int(row['revision']) != session.revision
        ):
            raise ConversationChangedError(
                'confirmation conversation context changed'
            )
        if row['target_binding_digest'] != target_binding_digest:
            raise ConfirmationIntentConflictError(
                'confirmation target binding changed'
            )

    @staticmethod
    def _finite_timestamp(value: Any, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            raise ValidationError(f'{name} must be a timestamp')
        result = float(value)
        if not math.isfinite(result):
            raise ValidationError(f'{name} must be finite')
        return result

    def _reuse_existing_locked(
        self,
        row: sqlite3.Row,
        conversation_id: str,
        turn_id: str,
        request_fingerprint: str,
        user_content: str,
        session: ConversationSession,
    ) -> BeginTurnResult:
        same_request = (
            row['conversation_id'] == conversation_id
            and row['turn_id'] == turn_id
            and row['request_fingerprint'] == request_fingerprint
            and row['user_content'] == user_content
        )
        if not same_request:
            raise ConversationConflictError(
                'request_id was already used with different input'
            )
        if int(row['generation']) != session.generation:
            raise ConversationConflictError(
                'request belongs to a previous conversation generation'
            )
        if row['session_instance_id'] != session.session_instance_id:
            raise ConversationConflictError(
                'request belongs to a previous conversation instance'
            )
        if row['status'] != 'completed':
            raise ConversationConflictError(
                'the same turn is already in progress'
            )
        response = self._load_response(row['response_json'])
        return BeginTurnResult(
            session=session,
            history=(),
            summary=None,
            token=None,
            cached_response=response,
        )

    def _advance_summary_locked(
        self,
        token: BeginTurnToken,
        now: float,
    ) -> Optional[ConversationSummary]:
        """Atomically extend the summary beyond the recent raw window."""
        source_end = token.ordinal - self.history_limit
        if source_end < 1:
            return None
        session_row = self._select_session_locked(
            token.user_id,
            token.conversation_id,
        )
        session = self._session_from_row(session_row)
        existing_row = self._select_summary_locked(session)
        existing = (
            self._summary_from_row(existing_row)
            if existing_row is not None
            else None
        )
        previous_end = (
            existing.source_end_ordinal
            if existing is not None
            else 0
        )
        if previous_end >= source_end:
            return existing
        source_turns = self._summary_source_turns_locked(
            token,
            previous_end,
            source_end,
        )
        expected_ordinals = list(
            range(previous_end + 1, source_end + 1)
        )
        actual_ordinals = [
            turn.ordinal
            for turn in source_turns
        ]
        previous_state = (
            existing_row['state_json']
            if existing_row is not None
            else ''
        )
        fallback_used = False
        digest_previous = (
            existing.source_digest
            if existing is not None
            else ''
        )
        digest_turns = source_turns
        try:
            if actual_ordinals != expected_ordinals:
                raise RuntimeError(
                    'summary source turns are not contiguous'
                )
            result = self._summarize_in_batches(
                self._summarizer,
                previous_state,
                source_turns,
                source_end,
            )
            if result.fallback_used:
                raise RuntimeError(
                    'configured summarizer requested recovery'
                )
            content = result.content
            state_json = result.state_json
            summarizer_name = result.algorithm
        except Exception:
            fallback_used = True
            full_turns = self._summary_source_turns_locked(
                token,
                0,
                source_end,
            )
            full_ordinals = [
                turn.ordinal
                for turn in full_turns
            ]
            if full_ordinals != list(range(1, source_end + 1)):
                full_turns = []
            recovery = ExtractiveConversationSummarizer()
            result = self._summarize_in_batches(
                recovery,
                '',
                full_turns,
                source_end,
            )
            content = result.content
            state_json = result.state_json
            summarizer_name = result.algorithm
            digest_previous = ''
            digest_turns = full_turns

        source_digest = self._extend_source_digest(
            digest_previous,
            digest_turns,
        )
        summary_id = (
            existing.summary_id
            if existing is not None
            else str(uuid.uuid4())
        )
        summary_revision = (
            existing.summary_revision + 1
            if existing is not None
            else 1
        )
        created_at = (
            existing.created_at
            if existing is not None
            else now
        )
        self._connection.execute(
            '''
            INSERT INTO conversation_summaries (
                user_id,
                conversation_id,
                session_instance_id,
                generation,
                summary_id,
                summary_revision,
                content,
                state_json,
                source_start_ordinal,
                source_end_ordinal,
                source_turn_count,
                source_digest,
                summarizer,
                fallback_used,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT (
                user_id,
                conversation_id,
                session_instance_id,
                generation
            )
            DO UPDATE SET
                summary_id = excluded.summary_id,
                summary_revision = excluded.summary_revision,
                content = excluded.content,
                state_json = excluded.state_json,
                source_start_ordinal =
                    excluded.source_start_ordinal,
                source_end_ordinal = excluded.source_end_ordinal,
                source_turn_count = excluded.source_turn_count,
                source_digest = excluded.source_digest,
                summarizer = excluded.summarizer,
                fallback_used = excluded.fallback_used,
                updated_at = excluded.updated_at
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                summary_id,
                summary_revision,
                content,
                state_json,
                1,
                source_end,
                source_end,
                source_digest,
                summarizer_name,
                int(fallback_used),
                created_at,
                now,
            ),
        )
        row = self._select_summary_locked(session)
        return self._summary_from_row(row)

    def _summary_source_turns_locked(
        self,
        token: BeginTurnToken,
        start_exclusive: int,
        end_inclusive: int,
    ) -> List[SummarySourceTurn]:
        rows = self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND status = 'completed'
              AND ordinal > ?
              AND ordinal <= ?
            ORDER BY ordinal ASC
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                start_exclusive,
                end_inclusive,
            ),
        ).fetchall()
        return [
            SummarySourceTurn(
                ordinal=int(row['ordinal']),
                turn_id=row['turn_id'],
                user_content=row['user_content'],
                assistant_content=row['assistant_content'],
            )
            for row in rows
        ]

    def _summarize_in_batches(
        self,
        summarizer: Any,
        previous_state: str,
        turns: List[SummarySourceTurn],
        source_end: int,
    ) -> SummaryResult:
        """Feed every source turn through a bounded rolling state."""
        batches = [
            turns[index:index + SUMMARY_UPDATE_BATCH_SIZE]
            for index in range(
                0,
                len(turns),
                SUMMARY_UPDATE_BATCH_SIZE,
            )
        ] or [[]]
        state = previous_state
        result: Optional[SummaryResult] = None
        for batch in batches:
            candidate = summarizer.update(
                previous_state_json=state,
                new_turns=batch,
                source_start_ordinal=1,
                source_end_ordinal=source_end,
                source_turn_count=source_end,
                max_chars=self.summary_max_chars,
            )
            if (
                not isinstance(candidate, SummaryResult)
                or not isinstance(candidate.content, str)
                or not isinstance(candidate.state_json, str)
                or not isinstance(candidate.algorithm, str)
                or len(candidate.content) > self.summary_max_chars
                or len(candidate.state_json) > 262144
            ):
                raise RuntimeError(
                    'summarizer returned an invalid bounded result'
                )
            state = candidate.state_json
            result = candidate
        if result is None:
            raise RuntimeError('summarizer returned no result')
        return result

    def _summary_for_window_locked(
        self,
        session: ConversationSession,
        completed_turn_count: int,
        now: float,
    ) -> Optional[ConversationSummary]:
        """Align persisted summary coverage with the configured raw N."""
        desired_end = max(
            0,
            completed_turn_count - self.history_limit,
        )
        row = self._select_summary_locked(session)
        existing = (
            self._summary_from_row(row)
            if row is not None
            else None
        )
        coverage_invalid = (
            existing is not None
            and (
                existing.source_start_ordinal != 1
                or existing.source_end_ordinal > desired_end
                or (
                    existing.source_turn_count
                    != existing.source_end_ordinal
                )
            )
        )
        if coverage_invalid:
            self._connection.execute(
                '''
                DELETE FROM conversation_summaries
                WHERE user_id = ?
                  AND conversation_id = ?
                  AND session_instance_id = ?
                  AND generation = ?
                ''',
                (
                    session.user_id,
                    session.conversation_id,
                    session.session_instance_id,
                    session.generation,
                ),
            )
            existing = None
        if desired_end == 0:
            return None
        if (
            existing is not None
            and existing.source_end_ordinal == desired_end
        ):
            return existing
        synthetic = BeginTurnToken(
            user_id=session.user_id,
            conversation_id=session.conversation_id,
            session_instance_id=session.session_instance_id,
            turn_id='',
            request_id='',
            request_fingerprint='',
            generation=session.generation,
            revision=session.revision,
            ordinal=completed_turn_count,
        )
        return self._advance_summary_locked(synthetic, now)

    @staticmethod
    def _extend_source_digest(
        previous_digest: str,
        turns: List[SummarySourceTurn],
    ) -> str:
        """Extend a deterministic per-turn digest chain."""
        try:
            digest_bytes = bytes.fromhex(previous_digest)
            if len(digest_bytes) != 32:
                raise ValueError
        except (TypeError, ValueError):
            digest_bytes = hashlib.sha256(b'').digest()
        for turn in turns:
            canonical = json.dumps(
                {
                    'ordinal': turn.ordinal,
                    'turn_id': turn.turn_id,
                    'user': turn.user_content,
                    'assistant': turn.assistant_content,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')
            digest_bytes = hashlib.sha256(
                digest_bytes + canonical
            ).digest()
        return digest_bytes.hex()

    def _history_locked(
        self,
        user_id: str,
        conversation_id: str,
        session_instance_id: str,
        generation: int,
        limit: int,
    ) -> List[ConversationTurn]:
        rows = self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND status = 'completed'
            ORDER BY ordinal DESC
            LIMIT ?
            ''',
            (
                user_id,
                conversation_id,
                session_instance_id,
                generation,
                limit,
            ),
        ).fetchall()
        return [
            self._turn_from_row(row)
            for row in reversed(rows)
        ]

    def _expire_and_commit(self, now: float) -> None:
        self._begin()
        try:
            self._expire_due_locked(now)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _expire_due_locked(self, now: float) -> int:
        due_rows = self._connection.execute(
            '''
            SELECT user_id, conversation_id, session_instance_id
            FROM conversation_sessions
            WHERE status = 'active' AND expires_at <= ?
            ''',
            (now,),
        ).fetchall()
        for row in due_rows:
            self._invalidate_pending_confirmations_locked(
                row['user_id'],
                row['conversation_id'],
                'confirmation_session_expired',
                now,
            )
            self._connection.execute(
                '''
                DELETE FROM conversation_turns
                WHERE user_id = ?
                  AND conversation_id = ?
                  AND session_instance_id = ?
                  AND status = 'pending'
                ''',
                (
                    row['user_id'],
                    row['conversation_id'],
                    row['session_instance_id'],
                ),
            )
            self._connection.execute(
                '''
                DELETE FROM conversation_summaries
                WHERE user_id = ?
                  AND conversation_id = ?
                  AND session_instance_id = ?
                ''',
                (
                    row['user_id'],
                    row['conversation_id'],
                    row['session_instance_id'],
                ),
            )
        cursor = self._connection.execute(
            '''
            UPDATE conversation_sessions
            SET status = 'expired',
                generation = generation + 1,
                revision = revision + 1,
                updated_at = ?
            WHERE status = 'active' AND expires_at <= ?
            ''',
            (now, now),
        )
        self._expire_due_confirmations_locked(now)
        return cursor.rowcount

    def _existing_request_locked(
        self,
        user_id: str,
        request_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ? AND request_id = ?
            ''',
            (user_id, request_id),
        ).fetchone()

    def _select_session_locked(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_sessions
            WHERE user_id = ? AND conversation_id = ?
            ''',
            (user_id, conversation_id),
        ).fetchone()

    def _select_summary_locked(
        self,
        session: ConversationSession,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_summaries
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
            ''',
            (
                session.user_id,
                session.conversation_id,
                session.session_instance_id,
                session.generation,
            ),
        ).fetchone()

    def _select_turn_locked(
        self,
        token: BeginTurnToken,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND turn_id = ?
              AND status = 'completed'
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                token.turn_id,
            ),
        ).fetchone()

    def _delete_pending_locked(
        self,
        token: BeginTurnToken,
    ) -> None:
        self._connection.execute(
            '''
            DELETE FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND turn_id = ?
              AND request_id = ?
              AND status = 'pending'
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                token.turn_id,
                token.request_id,
            ),
        )

    @staticmethod
    def _require_active(
        row: Optional[sqlite3.Row],
    ) -> ConversationSession:
        if row is None:
            raise ConversationNotFoundError(
                'conversation was not found'
            )
        session = SQLiteConversationStore._session_from_row(row)
        if session.status != 'active':
            raise ConversationStateError(
                f'conversation is {session.status}'
            )
        return session

    @staticmethod
    def _session_from_row(
        row: sqlite3.Row,
    ) -> ConversationSession:
        return ConversationSession(
            conversation_id=row['conversation_id'],
            user_id=row['user_id'],
            session_instance_id=row['session_instance_id'],
            status=row['status'],
            generation=int(row['generation']),
            revision=int(row['revision']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
            expires_at=float(row['expires_at']),
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> ConversationTurn:
        if row is None or row['status'] != 'completed':
            raise RuntimeError('conversation turn is not completed')
        response = SQLiteConversationStore._load_response(
            row['response_json']
        )
        return ConversationTurn(
            conversation_id=row['conversation_id'],
            user_id=row['user_id'],
            session_instance_id=row['session_instance_id'],
            turn_id=row['turn_id'],
            request_id=row['request_id'],
            request_fingerprint=row['request_fingerprint'],
            generation=int(row['generation']),
            ordinal=int(row['ordinal']),
            user_content=row['user_content'],
            assistant_content=row['assistant_content'],
            response=response,
            created_at=float(row['created_at']),
            completed_at=float(row['completed_at']),
        )

    @staticmethod
    def _summary_from_row(
        row: sqlite3.Row,
    ) -> ConversationSummary:
        if row is None:
            raise RuntimeError('conversation summary is missing')
        return ConversationSummary(
            summary_id=row['summary_id'],
            user_id=row['user_id'],
            conversation_id=row['conversation_id'],
            session_instance_id=row['session_instance_id'],
            generation=int(row['generation']),
            summary_revision=int(row['summary_revision']),
            content=row['content'],
            source_start_ordinal=int(
                row['source_start_ordinal']
            ),
            source_end_ordinal=int(row['source_end_ordinal']),
            source_turn_count=int(row['source_turn_count']),
            source_digest=row['source_digest'],
            summarizer=row['summarizer'],
            fallback_used=bool(row['fallback_used']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
        )

    @staticmethod
    def _load_response(value: Any) -> Dict[str, Any]:
        if not isinstance(value, str):
            raise RuntimeError(
                'stored conversation response is missing'
            )
        try:
            response = json.loads(value)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                'stored conversation response is invalid'
            ) from error
        if not isinstance(response, dict):
            raise RuntimeError(
                'stored conversation response must be an object'
            )
        return response

    @staticmethod
    def _response_json(value: Any) -> str:
        if not isinstance(value, dict):
            raise ValidationError('response must be an object')
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        if len(rendered) > MAX_RESPONSE_JSON_LENGTH:
            raise ValidationError('response is too large')
        return rendered

    @staticmethod
    def _required_text(
        value: Any,
        name: str,
        maximum: int,
    ) -> str:
        if not isinstance(value, str):
            raise ValidationError(f'{name} must be a string')
        result = value.strip()
        if not result:
            raise ValidationError(f'{name} must not be empty')
        if len(result) > maximum:
            raise ValidationError(
                f'{name} must be at most {maximum} characters'
            )
        return result

    @staticmethod
    def _required_digest(value: Any, name: str) -> str:
        result = SQLiteConversationStore._required_text(
            value,
            name,
            64,
        )
        if len(result) != 64 or any(
            character not in '0123456789abcdef'
            for character in result
        ):
            raise ValidationError(
                f'{name} must be a lowercase SHA-256 digest'
            )
        return result

    @staticmethod
    def _assistant_text(value: Any) -> str:
        if not isinstance(value, str):
            raise ValidationError(
                'assistant_content must be a string'
            )
        if len(value) > MAX_UTTERANCE_LENGTH:
            raise ValidationError(
                'assistant_content is too long'
            )
        return value

    @staticmethod
    def _bounded_integer(
        value: Any,
        name: str,
        minimum: int,
        maximum: int,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f'{name} must be an integer')
        if value < minimum or value > maximum:
            raise ValueError(
                f'{name} must be between {minimum} and {maximum}'
            )
        return value

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise RuntimeError('conversation clock is not finite')
        return value

    def _begin(self) -> None:
        self._connection.execute('BEGIN IMMEDIATE')
