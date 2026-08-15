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
from typing import Any, Callable, Dict, List, Optional, Tuple

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


MAX_RESPONSE_JSON_LENGTH = 65536
DEFAULT_SUMMARY_MAX_CHARS = 2000
SUMMARY_UPDATE_BATCH_SIZE = 128
TRUSTED_TOOL_RESULT_SCHEMA_VERSION = 1
MAX_TRUSTED_TOOL_RESULTS_PER_GENERATION = 1000
_TRUSTED_TOOL_RESULT_STATUSES = frozenset({
    'succeeded', 'failed', 'cancelled', 'timed_out',
})
_TRUSTED_TOOL_RESULT_SOURCES = frozenset({
    'controller', 'simulation_adapter', 'recovery',
})
_TRUSTED_TOOL_RESULT_CODES = frozenset({
    'simulation_succeeded',
    'preflight_failed',
    'preflight_timeout',
    'navigating_failed',
    'navigating_timeout',
    'coverage_failed',
    'coverage_timeout',
    'live_ready_failed',
    'live_ready_timeout',
    'simulation_cancelled',
    'simulation_cancel_failed',
    'simulation_cancel_timeout',
    'authority_revoked',
    'state_unavailable',
    'state_stale',
    'privacy_blocked',
    'emergency_stop',
    'map_changed',
    'device_unavailable',
    'recovery_unavailable',
    'authorization_expired',
    'event_capacity_reached',
})
_TRUSTED_TOOL_RESULT_STATUS_CODES = {
    'succeeded': frozenset({'simulation_succeeded'}),
    'failed': frozenset({
        'preflight_failed',
        'navigating_failed',
        'coverage_failed',
        'live_ready_failed',
        'simulation_cancel_failed',
        'authority_revoked',
        'state_unavailable',
        'state_stale',
        'privacy_blocked',
        'emergency_stop',
        'map_changed',
        'device_unavailable',
        'recovery_unavailable',
        'event_capacity_reached',
    }),
    'cancelled': frozenset({'simulation_cancelled'}),
    'timed_out': frozenset({
        'preflight_timeout',
        'navigating_timeout',
        'coverage_timeout',
        'live_ready_timeout',
        'simulation_cancel_timeout',
        'authorization_expired',
    }),
}
_TRUSTED_TOOL_RESULT_CODE_SOURCES = {
    'simulation_succeeded': frozenset({
        'simulation_adapter', 'recovery',
    }),
    'preflight_failed': frozenset({'simulation_adapter', 'recovery'}),
    'preflight_timeout': frozenset({'simulation_adapter', 'recovery'}),
    'navigating_failed': frozenset({'simulation_adapter', 'recovery'}),
    'navigating_timeout': frozenset({'simulation_adapter', 'recovery'}),
    'coverage_failed': frozenset({'simulation_adapter', 'recovery'}),
    'coverage_timeout': frozenset({'simulation_adapter', 'recovery'}),
    'live_ready_failed': frozenset({'simulation_adapter', 'recovery'}),
    'live_ready_timeout': frozenset({'simulation_adapter', 'recovery'}),
    'simulation_cancelled': frozenset({'simulation_adapter'}),
    'simulation_cancel_failed': frozenset({'simulation_adapter'}),
    'simulation_cancel_timeout': frozenset({'simulation_adapter'}),
    'authority_revoked': frozenset({'controller'}),
    'state_unavailable': frozenset({'controller'}),
    'state_stale': frozenset({'controller'}),
    'privacy_blocked': frozenset({'controller'}),
    'emergency_stop': frozenset({'controller'}),
    'map_changed': frozenset({'controller'}),
    'device_unavailable': frozenset({'controller'}),
    'recovery_unavailable': frozenset({'recovery'}),
    'authorization_expired': frozenset({'controller', 'recovery'}),
    'event_capacity_reached': frozenset({'controller'}),
}
_REQUEST_NAMESPACE_TRIGGER_DIGESTS = {
    'conversation_result_request_namespace_insert': (
        'e35a9108e6068937d5e0e9381b52d9df5ae30a077d1e822cf08aaa73e1c77837'
    ),
    'conversation_result_request_namespace_update': (
        '9e5b04750d198b8990eeb1df4f12368b39eeae931da6a06b15177a1a62610933'
    ),
    'conversation_turn_request_namespace_insert': (
        '8936ba73a7b284f36d33b43b22c7f481e6e742821ae5e3a31c62a754f36ca17b'
    ),
    'conversation_turn_request_namespace_update': (
        '065c31e205953c0cdb7c916584d58b2b80cafa7592787818890ba263d07e4856'
    ),
}


def _trusted_result_identifier(value: Any, field_name: str) -> str:
    """Validate an opaque trusted-result identifier without normalizing."""
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise ValidationError(
            f'{field_name} is not a valid opaque identifier'
        )
    return value


def _trusted_result_digest(value: Any, field_name: str) -> str:
    """Validate one lowercase SHA-256 digest."""
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise ValidationError(f'{field_name} is not a valid digest')
    return value


class ConversationNotFoundError(ValidationError):
    """Raised when a user-scoped conversation does not exist."""


class ConversationStateError(ValidationError):
    """Raised when a closed or expired conversation receives a turn."""


class ConversationConflictError(ValidationError):
    """Raised when a request or turn identifier is reused differently."""


class ConversationChangedError(ValidationError):
    """Raised when a session changes while model inference is running."""


class ConversationBusyError(ConversationStateError):
    """Raised when a retryable in-flight turn blocks a trusted result."""

    retryable = True


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


@dataclass(frozen=True, repr=False)
class TrustedRoomMissionTerminalResult:
    """Strict content-free terminal result from the simulated mission."""

    terminal_digest: str
    status: str
    code: str
    source: str
    sequence: int
    schema_version: int = TRUSTED_TOOL_RESULT_SCHEMA_VERSION
    phase: str = 'terminal'
    runtime_mode: str = 'simulation'
    simulated: bool = True
    physical_effects: bool = False
    viewer_live: bool = False
    durability: str = 'sqlite_local'
    lease_scope: str = 'database_device'

    def __post_init__(self) -> None:
        """Reject forged terminal outcomes and runtime markers."""
        _trusted_result_digest(
            self.terminal_digest,
            'terminal_digest',
        )
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != TRUSTED_TOOL_RESULT_SCHEMA_VERSION
        ):
            raise ValidationError(
                'trusted tool result schema version is invalid'
            )
        if self.status not in _TRUSTED_TOOL_RESULT_STATUSES:
            raise ValidationError(
                'trusted tool result status is invalid'
            )
        if self.code not in _TRUSTED_TOOL_RESULT_CODES:
            raise ValidationError(
                'trusted tool result code is invalid'
            )
        if self.code not in _TRUSTED_TOOL_RESULT_STATUS_CODES[self.status]:
            raise ValidationError(
                'trusted tool result status and code conflict'
            )
        if self.source not in _TRUSTED_TOOL_RESULT_SOURCES:
            raise ValidationError(
                'trusted tool result source is invalid'
            )
        if self.source not in _TRUSTED_TOOL_RESULT_CODE_SOURCES[self.code]:
            raise ValidationError(
                'trusted tool result source and code conflict'
            )
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValidationError(
                'trusted tool result sequence is invalid'
            )
        if (
            self.phase != 'terminal'
            or self.runtime_mode != 'simulation'
            or self.simulated is not True
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.durability != 'sqlite_local'
            or self.lease_scope != 'database_device'
        ):
            raise ValidationError(
                'trusted tool result simulation marker is invalid'
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the bounded result without environment or user content."""
        return {
            'schema_version': self.schema_version,
            'terminal_digest': self.terminal_digest,
            'status': self.status,
            'phase': self.phase,
            'code': self.code,
            'source': self.source,
            'sequence': self.sequence,
            'runtime_mode': self.runtime_mode,
            'simulated': self.simulated,
            'physical_effects': self.physical_effects,
            'viewer_live': self.viewer_live,
            'durability': self.durability,
            'lease_scope': self.lease_scope,
        }

    def __repr__(self) -> str:
        """Avoid reflecting the stable terminal digest into logs."""
        return (
            '<TrustedRoomMissionTerminalResult '
            f'status={self.status} code={self.code}>'
        )


@dataclass(frozen=True, repr=False)
class ConversationTrustedToolResult:
    """Owner-bound handoff envelope for one terminal Tool result."""

    feedback_id: str
    request_id: str
    tool_call_id: str
    user_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    source_revision: int
    result: TrustedRoomMissionTerminalResult
    schema_version: int = TRUSTED_TOOL_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate exact destination identity and a strict v1 payload."""
        _trusted_result_identifier(self.feedback_id, 'feedback_id')
        _trusted_result_identifier(self.request_id, 'request_id')
        _trusted_result_identifier(self.tool_call_id, 'tool_call_id')
        _trusted_result_identifier(
            self.session_instance_id,
            'session_instance_id',
        )
        if validate_user_id(self.user_id) != self.user_id:
            raise ValidationError('trusted tool result user is invalid')
        if (
            validate_conversation_id(self.conversation_id)
            != self.conversation_id
        ):
            raise ValidationError(
                'trusted tool result conversation is invalid'
            )
        if type(self.generation) is not int or self.generation < 1:
            raise ValidationError(
                'trusted tool result generation is invalid'
            )
        if (
            type(self.source_revision) is not int
            or self.source_revision < 0
        ):
            raise ValidationError(
                'trusted tool result source revision is invalid'
            )
        if type(self.result) is not TrustedRoomMissionTerminalResult:
            raise ValidationError(
                'trusted tool result payload is invalid'
            )
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != TRUSTED_TOOL_RESULT_SCHEMA_VERSION
        ):
            raise ValidationError(
                'trusted tool result envelope version is invalid'
            )

    def __repr__(self) -> str:
        """Avoid reflecting owner or execution identifiers in logs."""
        return '<ConversationTrustedToolResult opaque>'

    def to_dict(self) -> Dict[str, Any]:
        """Return the internal content-free persistence envelope."""
        return {
            'schema_version': self.schema_version,
            'feedback_id': self.feedback_id,
            'request_id': self.request_id,
            'tool_call_id': self.tool_call_id,
            'user_id': self.user_id,
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'generation': self.generation,
            'source_revision': self.source_revision,
            'result': TrustedRoomMissionTerminalResult.to_dict(
                self.result
            ),
        }


@dataclass(frozen=True, repr=False)
class TrustedToolResultCommit:
    """Durable receipt for one conversation Tool-result append."""

    commit_id: str
    envelope: ConversationTrustedToolResult
    conversation_revision_after: int
    committed_at: float
    cached: bool = False

    def __post_init__(self) -> None:
        """Validate the persisted receipt without weakening its envelope."""
        _trusted_result_identifier(self.commit_id, 'commit_id')
        if type(self.envelope) is not ConversationTrustedToolResult:
            raise ValidationError(
                'trusted tool result commit envelope is invalid'
            )
        if (
            type(self.conversation_revision_after) is not int
            or self.conversation_revision_after < 1
            or self.conversation_revision_after
            <= self.envelope.source_revision
        ):
            raise ValidationError(
                'trusted tool result commit revision is invalid'
            )
        if (
            type(self.committed_at) not in {int, float}
            or not math.isfinite(float(self.committed_at))
        ):
            raise ValidationError(
                'trusted tool result commit time is invalid'
            )
        if type(self.cached) is not bool:
            raise ValidationError(
                'trusted tool result cached marker is invalid'
            )

    def __repr__(self) -> str:
        """Avoid reflecting nested trusted execution identifiers."""
        return (
            '<TrustedToolResultCommit '
            f'revision={self.conversation_revision_after} '
            f'cached={self.cached}>'
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the content-free handoff receipt."""
        return {
            'commit_id': self.commit_id,
            'conversation_revision_after': (
                self.conversation_revision_after
            ),
            'committed_at': self.committed_at,
            'cached': self.cached,
            'envelope': self.envelope.to_dict(),
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
    trusted_tool_results: Tuple[TrustedToolResultCommit, ...] = ()


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
                CREATE TABLE IF NOT EXISTS
                    conversation_trusted_tool_results (
                    feedback_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    source_revision INTEGER NOT NULL
                        CHECK (source_revision >= 0),
                    request_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL,
                    commit_fingerprint TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK (
                        schema_version = 1
                    ),
                    terminal_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'succeeded', 'failed',
                            'cancelled', 'timed_out'
                        )
                    ),
                    phase TEXT NOT NULL CHECK (phase = 'terminal'),
                    code TEXT NOT NULL CHECK (
                        code IN (
                            'simulation_succeeded',
                            'preflight_failed',
                            'preflight_timeout',
                            'navigating_failed',
                            'navigating_timeout',
                            'coverage_failed',
                            'coverage_timeout',
                            'live_ready_failed',
                            'live_ready_timeout',
                            'simulation_cancelled',
                            'simulation_cancel_failed',
                            'simulation_cancel_timeout',
                            'authority_revoked',
                            'state_unavailable',
                            'state_stale',
                            'privacy_blocked',
                            'emergency_stop',
                            'map_changed',
                            'device_unavailable',
                            'recovery_unavailable',
                            'authorization_expired',
                            'event_capacity_reached'
                        )
                    ),
                    terminal_source TEXT NOT NULL CHECK (
                        terminal_source IN (
                            'controller',
                            'simulation_adapter',
                            'recovery'
                        )
                    ),
                    result_sequence INTEGER NOT NULL
                        CHECK (result_sequence >= 1),
                    runtime_mode TEXT NOT NULL CHECK (
                        runtime_mode = 'simulation'
                    ),
                    simulated INTEGER NOT NULL CHECK (simulated = 1),
                    physical_effects INTEGER NOT NULL CHECK (
                        physical_effects = 0
                    ),
                    viewer_live INTEGER NOT NULL CHECK (viewer_live = 0),
                    durability TEXT NOT NULL CHECK (
                        durability = 'sqlite_local'
                    ),
                    lease_scope TEXT NOT NULL CHECK (
                        lease_scope = 'database_device'
                    ),
                    commit_id TEXT NOT NULL UNIQUE,
                    conversation_revision_after INTEGER NOT NULL CHECK (
                        conversation_revision_after > source_revision
                    ),
                    committed_at REAL NOT NULL,
                    UNIQUE (user_id, request_id),
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
                CREATE TABLE IF NOT EXISTS
                    conversation_trusted_result_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (
                        schema_version = 1
                    ),
                    writer_protocol_version INTEGER NOT NULL CHECK (
                        writer_protocol_version = 1
                    )
                )
                '''
            )
            self._connection.execute(
                '''
                INSERT OR IGNORE INTO
                    conversation_trusted_result_metadata (
                        singleton,
                        schema_version,
                        writer_protocol_version
                    )
                VALUES (1, 1, 1)
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
            self._connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS
                    conversation_trusted_results_order_idx
                ON conversation_trusted_tool_results (
                    user_id,
                    conversation_id,
                    session_instance_id,
                    generation,
                    conversation_revision_after DESC
                )
                '''
            )
            self._create_request_namespace_triggers()
            self._verify_trusted_result_schema_locked()
            self._connection.commit()

    def _create_request_namespace_triggers(self) -> None:
        """Fence the shared per-user request namespace in raw SQL too."""
        self._connection.executescript(
            '''
            CREATE TRIGGER IF NOT EXISTS
                conversation_turn_request_namespace_insert
            BEFORE INSERT ON conversation_turns
            WHEN EXISTS (
                SELECT 1
                FROM conversation_trusted_tool_results
                WHERE user_id = NEW.user_id
                  AND request_id = NEW.request_id
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'conversation request namespace conflict'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS
                conversation_turn_request_namespace_update
            BEFORE UPDATE OF user_id, request_id ON conversation_turns
            WHEN EXISTS (
                SELECT 1
                FROM conversation_trusted_tool_results
                WHERE user_id = NEW.user_id
                  AND request_id = NEW.request_id
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'conversation request namespace conflict'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS
                conversation_result_request_namespace_insert
            BEFORE INSERT ON conversation_trusted_tool_results
            WHEN EXISTS (
                SELECT 1
                FROM conversation_turns
                WHERE user_id = NEW.user_id
                  AND request_id = NEW.request_id
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'conversation request namespace conflict'
                );
            END;

            CREATE TRIGGER IF NOT EXISTS
                conversation_result_request_namespace_update
            BEFORE UPDATE OF user_id, request_id
                ON conversation_trusted_tool_results
            WHEN EXISTS (
                SELECT 1
                FROM conversation_turns
                WHERE user_id = NEW.user_id
                  AND request_id = NEW.request_id
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'conversation request namespace conflict'
                );
            END;
            '''
        )

    def _verify_trusted_result_schema_locked(self) -> None:
        """Reject incompatible or weakened trusted-result databases."""
        metadata = self._connection.execute(
            '''
            SELECT schema_version, writer_protocol_version
            FROM conversation_trusted_result_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        metadata_count = self._connection.execute(
            '''
            SELECT COUNT(*) AS row_count
            FROM conversation_trusted_result_metadata
            '''
        ).fetchone()
        if (
            metadata is None
            or int(metadata_count['row_count']) != 1
            or int(metadata['schema_version'])
            != TRUSTED_TOOL_RESULT_SCHEMA_VERSION
            or int(metadata['writer_protocol_version']) != 1
        ):
            raise RuntimeError(
                'trusted tool result database version is incompatible'
            )
        expected_columns = {
            'feedback_id',
            'user_id',
            'conversation_id',
            'session_instance_id',
            'generation',
            'source_revision',
            'request_id',
            'tool_call_id',
            'request_fingerprint',
            'commit_fingerprint',
            'schema_version',
            'terminal_digest',
            'status',
            'phase',
            'code',
            'terminal_source',
            'result_sequence',
            'runtime_mode',
            'simulated',
            'physical_effects',
            'viewer_live',
            'durability',
            'lease_scope',
            'commit_id',
            'conversation_revision_after',
            'committed_at',
        }
        observed_columns = {
            str(row['name'])
            for row in self._connection.execute(
                'PRAGMA table_info(conversation_trusted_tool_results)'
            ).fetchall()
        }
        if observed_columns != expected_columns:
            raise RuntimeError(
                'trusted tool result database schema is incompatible'
            )
        trigger_rows = self._connection.execute(
            '''
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name LIKE 'conversation_%request_namespace_%'
            '''
        ).fetchall()
        trigger_names = {
            str(row['name'])
            for row in trigger_rows
        }
        if trigger_names != set(_REQUEST_NAMESPACE_TRIGGER_DIGESTS):
            raise RuntimeError(
                'trusted tool result request fence is incompatible'
            )
        for row in trigger_rows:
            normalized_sql = ' '.join(str(row['sql']).split())
            observed_digest = hashlib.sha256(
                normalized_sql.encode('utf-8')
            ).hexdigest()
            expected_digest = _REQUEST_NAMESPACE_TRIGGER_DIGESTS[
                str(row['name'])
            ]
            if observed_digest != expected_digest:
                raise RuntimeError(
                    'trusted tool result request fence is incompatible'
                )
        index_contracts = set()
        for index_row in self._connection.execute(
            'PRAGMA index_list(conversation_trusted_tool_results)'
        ).fetchall():
            index_name = str(index_row['name'])
            columns = tuple(
                str(column['name'])
                for column in self._connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                ).fetchall()
            )
            index_contracts.add((bool(index_row['unique']), columns))
        required_indexes = {
            (True, ('feedback_id',)),
            (True, ('tool_call_id',)),
            (True, ('commit_id',)),
            (True, ('user_id', 'request_id')),
            (
                False,
                (
                    'user_id',
                    'conversation_id',
                    'session_instance_id',
                    'generation',
                    'conversation_revision_after',
                ),
            ),
        }
        if not required_indexes.issubset(index_contracts):
            raise RuntimeError(
                'trusted tool result database index is incompatible'
            )
        collision = self._connection.execute(
            '''
            SELECT 1
            FROM conversation_turns AS turn
            JOIN conversation_trusted_tool_results AS result
              ON result.user_id = turn.user_id
             AND result.request_id = turn.request_id
            LIMIT 1
            '''
        ).fetchone()
        if collision is not None:
            raise RuntimeError(
                'trusted tool result request namespace is invalid'
            )
        rows = self._connection.execute(
            'SELECT * FROM conversation_trusted_tool_results'
        ).fetchall()
        for row in rows:
            commit = self._trusted_result_from_row(row, cached=True)
            expected = self._trusted_result_fingerprint(commit.envelope)
            expected_commit = self._trusted_result_commit_fingerprint(
                expected,
                commit.commit_id,
                commit.conversation_revision_after,
                commit.committed_at,
            )
            session_row = self._select_session_locked(
                commit.envelope.user_id,
                commit.envelope.conversation_id,
            )
            if (
                row['request_fingerprint'] != expected
                or row['commit_fingerprint'] != expected_commit
                or session_row is None
                or int(session_row['revision'])
                < commit.conversation_revision_after
            ):
                raise RuntimeError(
                    'trusted tool result binding is invalid'
                )

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
                trusted_results = self._trusted_results_locked(
                    session,
                    normalized_limit,
                )
                self._connection.commit()
                return ConversationSnapshot(
                    session=session,
                    turns=tuple(turns),
                    summary=summary,
                    trusted_tool_results=tuple(trusted_results),
                )
            except Exception:
                self._connection.rollback()
                raise

    def append_trusted_tool_result(
        self,
        envelope: ConversationTrustedToolResult,
    ) -> TrustedToolResultCommit:
        """Atomically append or exactly replay one trusted Tool result."""
        if type(envelope) is not ConversationTrustedToolResult:
            raise ValidationError(
                'trusted tool result envelope is invalid'
            )
        envelope = self._snapshot_trusted_result_envelope(envelope)
        fingerprint = self._trusted_result_fingerprint(envelope)
        failure: Optional[str] = None
        with self._lock:
            try:
                self._begin()
                now = self._now()
                self._expire_due_locked(now)
                existing_rows = self._existing_trusted_results_locked(
                    envelope,
                )
                if existing_rows:
                    if len(existing_rows) != 1:
                        raise ConversationConflictError(
                            'trusted tool result identity conflicts'
                        )
                    existing = existing_rows[0]
                    if not self._trusted_result_replay_matches(
                        existing,
                        envelope,
                        fingerprint,
                    ):
                        raise ConversationConflictError(
                            'trusted tool result identity conflicts'
                        )
                    result = self._trusted_result_from_row(
                        existing,
                        cached=True,
                    )
                    self._connection.commit()
                    return result
                request_collision = self._existing_request_locked(
                    envelope.user_id,
                    envelope.request_id,
                )
                if request_collision is not None:
                    raise ConversationConflictError(
                        'conversation request identity conflicts'
                    )
                session_row = self._select_session_locked(
                    envelope.user_id,
                    envelope.conversation_id,
                )
                session = self._require_active(session_row)
                if (
                    session.session_instance_id
                    != envelope.session_instance_id
                    or session.generation != envelope.generation
                ):
                    raise ConversationChangedError(
                        'trusted tool result destination changed'
                    )
                if session.revision < envelope.source_revision:
                    raise ConversationChangedError(
                        'trusted tool result source revision is ahead'
                    )
                pending = self._connection.execute(
                    '''
                    SELECT 1
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND status = 'pending'
                    LIMIT 1
                    ''',
                    (
                        envelope.user_id,
                        envelope.conversation_id,
                        envelope.session_instance_id,
                        envelope.generation,
                    ),
                ).fetchone()
                if pending is not None:
                    raise ConversationBusyError(
                        'conversation has a retryable pending turn'
                    )
                count_row = self._connection.execute(
                    '''
                    SELECT COUNT(*) AS result_count
                    FROM conversation_trusted_tool_results
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                    ''',
                    (
                        envelope.user_id,
                        envelope.conversation_id,
                        envelope.session_instance_id,
                        envelope.generation,
                    ),
                ).fetchone()
                if (
                    int(count_row['result_count'])
                    >= MAX_TRUSTED_TOOL_RESULTS_PER_GENERATION
                ):
                    raise ConversationStateError(
                        'trusted tool result limit reached'
                    )
                revision_after = session.revision + 1
                commit_id = f'conversation-tool-result-{uuid.uuid4()}'
                commit_fingerprint = (
                    self._trusted_result_commit_fingerprint(
                        fingerprint,
                        commit_id,
                        revision_after,
                        now,
                    )
                )
                terminal = envelope.result
                self._connection.execute(
                    '''
                    INSERT INTO conversation_trusted_tool_results (
                        feedback_id,
                        user_id,
                        conversation_id,
                        session_instance_id,
                        generation,
                        source_revision,
                        request_id,
                        tool_call_id,
                        request_fingerprint,
                        commit_fingerprint,
                        schema_version,
                        terminal_digest,
                        status,
                        phase,
                        code,
                        terminal_source,
                        result_sequence,
                        runtime_mode,
                        simulated,
                        physical_effects,
                        viewer_live,
                        durability,
                        lease_scope,
                        commit_id,
                        conversation_revision_after,
                        committed_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ''',
                    (
                        envelope.feedback_id,
                        envelope.user_id,
                        envelope.conversation_id,
                        envelope.session_instance_id,
                        envelope.generation,
                        envelope.source_revision,
                        envelope.request_id,
                        envelope.tool_call_id,
                        fingerprint,
                        commit_fingerprint,
                        envelope.schema_version,
                        terminal.terminal_digest,
                        terminal.status,
                        terminal.phase,
                        terminal.code,
                        terminal.source,
                        terminal.sequence,
                        terminal.runtime_mode,
                        int(terminal.simulated),
                        int(terminal.physical_effects),
                        int(terminal.viewer_live),
                        terminal.durability,
                        terminal.lease_scope,
                        commit_id,
                        revision_after,
                        now,
                    ),
                )
                cursor = self._connection.execute(
                    '''
                    UPDATE conversation_sessions
                    SET revision = ?,
                        updated_at = ?
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND revision = ?
                      AND status = 'active'
                    ''',
                    (
                        revision_after,
                        now,
                        envelope.user_id,
                        envelope.conversation_id,
                        envelope.session_instance_id,
                        envelope.generation,
                        session.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConversationChangedError(
                        'trusted tool result destination changed'
                    )
                stored = self._connection.execute(
                    '''
                    SELECT *
                    FROM conversation_trusted_tool_results
                    WHERE feedback_id = ?
                    ''',
                    (envelope.feedback_id,),
                ).fetchone()
                self._connection.commit()
                self._secure_file_permissions()
                return self._trusted_result_from_row(
                    stored,
                    cached=False,
                )
            except (
                ConversationBusyError,
                ConversationChangedError,
                ConversationConflictError,
                ConversationNotFoundError,
                ConversationStateError,
            ):
                self._connection.rollback()
                raise
            except sqlite3.IntegrityError:
                self._connection.rollback()
                failure = 'conflict'
            except Exception:
                self._connection.rollback()
                failure = 'storage'
        if failure == 'conflict':
            error = ConversationConflictError(
                'trusted tool result identity conflicts'
            )
            error.__cause__ = None
            error.__context__ = None
            raise error
        error = ConversationStateError(
            'trusted tool result storage failed'
        )
        error.__cause__ = None
        error.__context__ = None
        raise error

    def list_trusted_tool_results(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> List[TrustedToolResultCommit]:
        """Return current-generation trusted results in commit order."""
        normalized_limit = self._bounded_integer(
            limit,
            'trusted result limit',
            1,
            500,
        )
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        failed = False
        with self._lock:
            try:
                self._begin()
                self._expire_due_locked(self._now())
                row = self._select_session_locked(
                    normalized_user,
                    normalized_conversation,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'conversation was not found'
                    )
                session = self._session_from_row(row)
                results = self._trusted_results_locked(
                    session,
                    normalized_limit,
                )
                self._connection.commit()
                return results
            except ConversationNotFoundError:
                self._connection.rollback()
                raise
            except Exception:
                self._connection.rollback()
                failed = True
        if failed:
            error = ConversationStateError(
                'trusted tool result read failed'
            )
            error.__cause__ = None
            error.__context__ = None
            raise error
        raise RuntimeError('trusted tool result read did not finish')

    def get_trusted_tool_result(
        self,
        user_id: str,
        conversation_id: str,
        feedback_id: str,
    ) -> TrustedToolResultCommit:
        """Return one current-generation result without cross-owner hints."""
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        normalized_feedback = _trusted_result_identifier(
            feedback_id,
            'feedback_id',
        )
        failed = False
        with self._lock:
            try:
                self._begin()
                self._expire_due_locked(self._now())
                session_row = self._select_session_locked(
                    normalized_user,
                    normalized_conversation,
                )
                if session_row is None:
                    raise ConversationNotFoundError(
                        'trusted tool result was not found'
                    )
                session = self._session_from_row(session_row)
                row = self._connection.execute(
                    '''
                    SELECT *
                    FROM conversation_trusted_tool_results
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND feedback_id = ?
                    ''',
                    (
                        normalized_user,
                        normalized_conversation,
                        session.session_instance_id,
                        session.generation,
                        normalized_feedback,
                    ),
                ).fetchone()
                if row is None:
                    raise ConversationNotFoundError(
                        'trusted tool result was not found'
                    )
                self._connection.commit()
                return self._trusted_result_from_row(row, cached=True)
            except ConversationNotFoundError:
                self._connection.rollback()
                raise
            except Exception:
                self._connection.rollback()
                failed = True
        if failed:
            error = ConversationStateError(
                'trusted tool result read failed'
            )
            error.__cause__ = None
            error.__context__ = None
            raise error
        raise RuntimeError('trusted tool result read did not finish')

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
                trusted_request = self._connection.execute(
                    '''
                    SELECT 1
                    FROM conversation_trusted_tool_results
                    WHERE user_id = ? AND request_id = ?
                    LIMIT 1
                    ''',
                    (normalized_user, normalized_request),
                ).fetchone()
                if trusted_request is not None:
                    raise ConversationConflictError(
                        'conversation request identity conflicts'
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
    ) -> Tuple[ConversationSession, ConversationTurn]:
        """Commit one assistant response if the session is unchanged."""
        normalized_assistant = self._assistant_text(
            assistant_content
        )
        response_json = self._response_json(response)
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
            except ConversationChangedError:
                raise
            except Exception:
                self._connection.rollback()
                raise

    def fail_turn(self, token: BeginTurnToken) -> None:
        """Discard one pending turn after provider or safety failure."""
        with self._lock:
            self._delete_pending_locked(token)
            self._connection.commit()

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

    def get_completed_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> ConversationTurn:
        """Return one active-generation completed turn by exact identity."""
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        normalized_turn = validate_turn_id(turn_id)
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._select_session_locked(
                    normalized_user,
                    normalized_conversation,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'completed conversation turn was not found'
                    )
                session = self._require_active(row)
                turn_row = self._connection.execute(
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
                        normalized_user,
                        normalized_conversation,
                        session.session_instance_id,
                        session.generation,
                        normalized_turn,
                    ),
                ).fetchone()
                if turn_row is None:
                    raise ConversationNotFoundError(
                        'completed conversation turn was not found'
                    )
                self._connection.commit()
                return self._turn_from_row(turn_row)
            except Exception:
                self._connection.rollback()
                raise

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

    def _existing_trusted_results_locked(
        self,
        envelope: ConversationTrustedToolResult,
    ) -> List[sqlite3.Row]:
        """Resolve every globally or owner-scoped idempotency key."""
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_trusted_tool_results
            WHERE feedback_id = ?
               OR tool_call_id = ?
               OR (user_id = ? AND request_id = ?)
            ''',
            (
                envelope.feedback_id,
                envelope.tool_call_id,
                envelope.user_id,
                envelope.request_id,
            ),
        ).fetchall()

    @staticmethod
    def _snapshot_trusted_result_envelope(
        envelope: ConversationTrustedToolResult,
    ) -> ConversationTrustedToolResult:
        """Revalidate a detached scalar snapshot at the store boundary."""
        if type(envelope.result) is not TrustedRoomMissionTerminalResult:
            raise ValidationError(
                'trusted tool result payload is invalid'
            )
        source = envelope.result
        result = TrustedRoomMissionTerminalResult(
            terminal_digest=source.terminal_digest,
            status=source.status,
            code=source.code,
            source=source.source,
            sequence=source.sequence,
            schema_version=source.schema_version,
            phase=source.phase,
            runtime_mode=source.runtime_mode,
            simulated=source.simulated,
            physical_effects=source.physical_effects,
            viewer_live=source.viewer_live,
            durability=source.durability,
            lease_scope=source.lease_scope,
        )
        return ConversationTrustedToolResult(
            feedback_id=envelope.feedback_id,
            request_id=envelope.request_id,
            tool_call_id=envelope.tool_call_id,
            user_id=envelope.user_id,
            conversation_id=envelope.conversation_id,
            session_instance_id=envelope.session_instance_id,
            generation=envelope.generation,
            source_revision=envelope.source_revision,
            result=result,
            schema_version=envelope.schema_version,
        )

    @staticmethod
    def _trusted_result_replay_matches(
        row: sqlite3.Row,
        envelope: ConversationTrustedToolResult,
        fingerprint: str,
    ) -> bool:
        """Match all three keys and the complete canonical payload."""
        return (
            row['feedback_id'] == envelope.feedback_id
            and row['user_id'] == envelope.user_id
            and row['conversation_id'] == envelope.conversation_id
            and row['session_instance_id']
            == envelope.session_instance_id
            and int(row['generation']) == envelope.generation
            and int(row['source_revision'])
            == envelope.source_revision
            and row['request_id'] == envelope.request_id
            and row['tool_call_id'] == envelope.tool_call_id
            and row['request_fingerprint'] == fingerprint
        )

    @staticmethod
    def _trusted_result_fingerprint(
        envelope: ConversationTrustedToolResult,
    ) -> str:
        """Hash the full normalized owner, destination, and terminal input."""
        canonical = json.dumps(
            ConversationTrustedToolResult.to_dict(envelope),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _trusted_result_commit_fingerprint(
        request_fingerprint: str,
        commit_id: str,
        conversation_revision_after: int,
        committed_at: float,
    ) -> str:
        """Bind the immutable receipt fields to the canonical request."""
        canonical = json.dumps(
            {
                'request_fingerprint': request_fingerprint,
                'commit_id': commit_id,
                'conversation_revision_after': (
                    conversation_revision_after
                ),
                'committed_at': committed_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest()

    def _trusted_results_locked(
        self,
        session: ConversationSession,
        limit: int,
    ) -> List[TrustedToolResultCommit]:
        """Read only the session's current opaque instance and generation."""
        rows = self._connection.execute(
            '''
            SELECT *
            FROM conversation_trusted_tool_results
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
            ORDER BY conversation_revision_after DESC
            LIMIT ?
            ''',
            (
                session.user_id,
                session.conversation_id,
                session.session_instance_id,
                session.generation,
                limit,
            ),
        ).fetchall()
        return [
            self._trusted_result_from_row(row, cached=True)
            for row in reversed(rows)
        ]

    @staticmethod
    def _trusted_result_from_row(
        row: sqlite3.Row,
        *,
        cached: bool,
    ) -> TrustedToolResultCommit:
        """Rebuild and revalidate one content-free stored result."""
        if row is None:
            raise RuntimeError('trusted tool result is missing')
        if (
            int(row['simulated']) != 1
            or int(row['physical_effects']) != 0
            or int(row['viewer_live']) != 0
        ):
            raise RuntimeError(
                'trusted tool result simulation marker is invalid'
            )
        result = TrustedRoomMissionTerminalResult(
            terminal_digest=str(row['terminal_digest']),
            status=str(row['status']),
            code=str(row['code']),
            source=str(row['terminal_source']),
            sequence=int(row['result_sequence']),
            schema_version=int(row['schema_version']),
            phase=str(row['phase']),
            runtime_mode=str(row['runtime_mode']),
            simulated=bool(row['simulated']),
            physical_effects=bool(row['physical_effects']),
            viewer_live=bool(row['viewer_live']),
            durability=str(row['durability']),
            lease_scope=str(row['lease_scope']),
        )
        envelope = ConversationTrustedToolResult(
            feedback_id=str(row['feedback_id']),
            request_id=str(row['request_id']),
            tool_call_id=str(row['tool_call_id']),
            user_id=str(row['user_id']),
            conversation_id=str(row['conversation_id']),
            session_instance_id=str(row['session_instance_id']),
            generation=int(row['generation']),
            source_revision=int(row['source_revision']),
            result=result,
            schema_version=int(row['schema_version']),
        )
        commit = TrustedToolResultCommit(
            commit_id=str(row['commit_id']),
            envelope=envelope,
            conversation_revision_after=int(
                row['conversation_revision_after']
            ),
            committed_at=float(row['committed_at']),
            cached=cached,
        )
        request_fingerprint = (
            SQLiteConversationStore._trusted_result_fingerprint(
                envelope
            )
        )
        commit_fingerprint = (
            SQLiteConversationStore._trusted_result_commit_fingerprint(
                request_fingerprint,
                commit.commit_id,
                commit.conversation_revision_after,
                commit.committed_at,
            )
        )
        if (
            row['request_fingerprint'] != request_fingerprint
            or row['commit_fingerprint'] != commit_fingerprint
        ):
            raise RuntimeError('trusted tool result binding is invalid')
        return commit

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
