"""SQLite-backed, user-isolated short-term conversation sessions.

Public facade composing the modular ``conversation_store`` Unit of Work and
single-responsibility repositories while preserving full API and legacy
internal compatibility.
"""

import sqlite3
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from malbut_agent_server.conversation_store.codecs import (
    bounded_integer as _bounded_integer_codec,
)
from malbut_agent_server.conversation_store.compaction import (
    CompactionRepository,
)
from malbut_agent_server.conversation_store.confirmations import (
    ConfirmationRepository,
)
from malbut_agent_server.conversation_store.database import (
    ConversationDatabase,
)
from malbut_agent_server.conversation_store.models import (
    CONFIRMATION_STORAGE_SCHEMA_VERSION,
    DEFAULT_SUMMARY_MAX_CHARS,
    MAX_CONFIRMATION_JSON_LENGTH,
    MAX_RESPONSE_JSON_LENGTH,
    SUMMARY_UPDATE_BATCH_SIZE,
    TEXT_TURN_CLAIM_OUTCOMES,
    TEXT_TURN_CLAIM_SCHEMA_VERSION,
    BeginTurnResult,
    BeginTurnToken,
    ConfirmationIntentAlreadyTerminalError,
    ConfirmationIntentConflictError,
    ConfirmationIntentNotFoundError,
    ConfirmationSchemaError,
    ConversationChangedError,
    ConversationConfig,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationSession,
    ConversationSnapshot,
    ConversationStateError,
    ConversationSummary,
    ConversationTurn,
    TextTurnRequestClaim,
    is_context_limit_response,
)
from malbut_agent_server.conversation_store.schema import (
    CONFIRMATION_INTENTS_SQL,
    CONFIRMATION_ONE_PENDING_INDEX_SQL,
    CONFIRMATION_RESPONSE_OWNER_INDEX_SQL,
    CONFIRMATION_SCHEMA_METADATA_SQL,
    CONFIRMATION_SCHEMA_OBJECTS as _CONFIRMATION_SCHEMA_OBJECTS,
    TEXT_TURN_REQUEST_CLAIMS_SQL,
    normalized_schema_sql as _normalized_schema_sql_fn,
)
from malbut_agent_server.conversation_store.sessions import (
    SessionRepository,
)
from malbut_agent_server.conversation_store.text_claims import (
    TextTurnClaimRepository,
)
from malbut_agent_server.conversation_store.turns import (
    TurnRepository,
)
from malbut_agent_server.schemas import (
    MAX_UTTERANCE_LENGTH,
    validate_conversation_id,
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
    )

__all__ = [
    'BeginTurnResult',
    'BeginTurnToken',
    'CONFIRMATION_INTENTS_SQL',
    'CONFIRMATION_ONE_PENDING_INDEX_SQL',
    'CONFIRMATION_RESPONSE_OWNER_INDEX_SQL',
    'CONFIRMATION_SCHEMA_METADATA_SQL',
    'CONFIRMATION_STORAGE_SCHEMA_VERSION',
    'ConfirmationIntentAlreadyTerminalError',
    'ConfirmationIntentConflictError',
    'ConfirmationIntentNotFoundError',
    'ConfirmationSchemaError',
    'ConversationChangedError',
    'ConversationConfig',
    'ConversationConflictError',
    'ConversationNotFoundError',
    'ConversationSession',
    'ConversationSnapshot',
    'ConversationStateError',
    'ConversationSummary',
    'ConversationTurn',
    'DEFAULT_SUMMARY_MAX_CHARS',
    'MAX_CONFIRMATION_JSON_LENGTH',
    'MAX_RESPONSE_JSON_LENGTH',
    'SQLiteConversationStore',
    'SUMMARY_UPDATE_BATCH_SIZE',
    'TEXT_TURN_CLAIM_OUTCOMES',
    'TEXT_TURN_CLAIM_SCHEMA_VERSION',
    'TEXT_TURN_REQUEST_CLAIMS_SQL',
    'TextTurnRequestClaim',
    '_CONFIRMATION_SCHEMA_OBJECTS',
    'is_context_limit_response',
]


class SQLiteConversationStore:
    """Thread-safe source of truth for short-term conversation history."""

    def __init__(
        self,
        database_path: str,
        ttl_seconds: int = 3600,
        history_limit: int = 10,
        max_sessions_per_user: int = 100,
        max_turns_per_session: int = 1000,
        summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
        summarizer: Optional[
            ExtractiveConversationSummarizer
        ] = None,
        clock: Callable[[], float] = time.time,
        semantic_context: bool = False,
    ) -> None:
        """Open a database and initialize the conversation schema."""
        if not database_path:
            raise ValueError('database_path must not be empty')
        if not isinstance(semantic_context, bool):
            raise ValueError('semantic_context must be a bool')
        self._config = ConversationConfig(
            ttl_seconds=_bounded_integer_codec(
                ttl_seconds,
                'ttl_seconds',
                60,
                2592000,
            ),
            history_limit=_bounded_integer_codec(
                history_limit,
                'history_limit',
                10,
                50,
            ),
            max_sessions_per_user=_bounded_integer_codec(
                max_sessions_per_user,
                'max_sessions_per_user',
                1,
                1000,
            ),
            max_turns_per_session=_bounded_integer_codec(
                max_turns_per_session,
                'max_turns_per_session',
                10,
                10000,
            ),
            summary_max_chars=_bounded_integer_codec(
                summary_max_chars,
                'summary_max_chars',
                256,
                8000,
            ),
            semantic_context=semantic_context,
        )
        self._db = ConversationDatabase(database_path, clock=clock)
        self.database_path = self._db.database_path
        self._connection = self._db.connection
        self._lock = self._db.lock
        self._sessions = SessionRepository(
            self._db,
            self._config,
            on_invalidate_pending_confirmations=(
                lambda user_id, conversation_id, result_code, now: (
                    self._confirmations.invalidate_pending_confirmations_locked(
                        user_id, conversation_id, result_code, now,
                    )
                )
            ),
            on_expire_due_confirmations=(
                lambda now: (
                    self._confirmations.expire_due_confirmations_locked(now)
                )
            ),
        )
        def advance_hook(token, now):
            return self._advance_summary_locked(token, now)

        self._compaction = CompactionRepository(
            self._db,
            self._sessions,
            self._config,
            summarizer=summarizer,
            advance_summary_hook=advance_hook,
        )
        self._text_claims = TextTurnClaimRepository(
            self._db,
            self._sessions,
        )
        self._confirmations = ConfirmationRepository(
            self._db,
            self._sessions,
            self._text_claims,
        )
        self._turns = TurnRepository(
            self._db,
            self._sessions,
            self._compaction,
            self._confirmations,
            self._text_claims,
            self._config,
            advance_summary_hook=advance_hook,
        )

    @property
    def database(self) -> ConversationDatabase:
        """Expose the shared SQLite Unit-of-Work boundary for sibling stores."""
        return self._db

    def locked(self):
        """Acquire the shared SQLite RLock without opening a write transaction."""
        return self._db.locked()

    def transaction(self, *, write: bool = True):
        """Run one atomic transaction on the shared SQLite connection."""
        return self._db.transaction(write=write)

    @property
    def semantic_context(self) -> bool:
        return self._config.semantic_context

    @semantic_context.setter
    def semantic_context(self, value: bool) -> None:
        self._config.semantic_context = value

    @property
    def ttl_seconds(self) -> int:
        return self._config.ttl_seconds

    @ttl_seconds.setter
    def ttl_seconds(self, value: int) -> None:
        self._config.ttl_seconds = value

    @property
    def history_limit(self) -> int:
        return self._config.history_limit

    @history_limit.setter
    def history_limit(self, value: int) -> None:
        self._config.history_limit = value

    @property
    def max_sessions_per_user(self) -> int:
        return self._config.max_sessions_per_user

    @max_sessions_per_user.setter
    def max_sessions_per_user(self, value: int) -> None:
        self._config.max_sessions_per_user = value

    @property
    def max_turns_per_session(self) -> int:
        return self._config.max_turns_per_session

    @max_turns_per_session.setter
    def max_turns_per_session(self, value: int) -> None:
        self._config.max_turns_per_session = value

    @property
    def summary_max_chars(self) -> int:
        return self._config.summary_max_chars

    @summary_max_chars.setter
    def summary_max_chars(self, value: int) -> None:
        self._config.summary_max_chars = value

    @property
    def _summarizer(self) -> Any:
        return self._compaction.summarizer

    @_summarizer.setter
    def _summarizer(self, value: Any) -> None:
        self._compaction.summarizer = value

    @property
    def _clock(self) -> Callable[[], float]:
        return self._db.clock

    @_clock.setter
    def _clock(self, value: Callable[[], float]) -> None:
        self._db.clock = value

    def close(self) -> None:
        """Close the underlying database connection."""
        self._db.close()

    def create(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
    ) -> ConversationSession:
        """Create or idempotently return one active session."""
        return self._sessions.create(user_id, conversation_id)

    def resume_or_create(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
        *,
        start_new: bool = False,
    ) -> ConversationSession:
        """Resume live context without counting startup or reads as dialogue."""
        return self._sessions.resume_or_create(
            user_id,
            conversation_id,
            start_new=start_new,
            create_fn=self.create,
            close_fn=self.close_session,
        )

    def get(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Return one session after lazily expiring due sessions."""
        return self._sessions.get(user_id, conversation_id)

    def get_summary(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[ConversationSummary]:
        """Return the summary for the current session generation."""
        return self._compaction.get_summary(user_id, conversation_id)

    def snapshot(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> ConversationSnapshot:
        """Read session, turns, and summary in one transaction."""
        normalized_limit = _bounded_integer_codec(
            limit,
            'turn limit',
            1,
            500,
        )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._db.transaction():
            self._sessions.expire_due_locked(self._db.now())
            row = self._sessions.select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            session = self._sessions.session_from_row(row)
            turns = self._turns.history_locked(
                normalized_user,
                normalized_id,
                session.session_instance_id,
                session.generation,
                normalized_limit,
            )
            summary_row = self._compaction.select_summary_locked(session)
            summary = (
                self._compaction.summary_from_row(summary_row)
                if summary_row is not None
                else None
            )
            return ConversationSnapshot(
                session=session,
                turns=tuple(turns),
                summary=summary,
            )

    def apply_compaction(
        self,
        token: BeginTurnToken,
        expected_summary: Optional[ConversationSummary],
        source_turns: Sequence[ConversationTurn],
        result: SummaryResult,
        *,
        guard: Optional[Callable[[sqlite3.Connection], bool]] = None,
    ) -> bool:
        """CAS an already generated summary while retaining later raw turns."""
        return self._compaction.apply_compaction(
            token,
            expected_summary,
            source_turns,
            result,
            guard=guard,
        )

    def begin_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        request_id: str,
        request_fingerprint: str,
        user_content: str,
        before_new_turn: Optional[Callable] = None,
        *,
        max_user_content_chars: int = MAX_UTTERANCE_LENGTH,
    ) -> BeginTurnResult:
        """Reserve one ordered turn or return its durable response."""
        return self._turns.begin_turn(
            user_id,
            conversation_id,
            turn_id,
            request_id,
            request_fingerprint,
            user_content,
            before_new_turn,
            max_user_content_chars=max_user_content_chars,
        )

    def complete_turn(
        self,
        token: BeginTurnToken,
        assistant_content: str,
        response: Dict[str, Any],
        confirmation_draft: Optional['ConfirmationDraft'] = None,
        commit_callback: Optional[Callable] = None,
        before_commit: Optional[Callable] = None,
    ) -> Tuple[ConversationSession, ConversationTurn]:
        """Atomically commit one response and optional confirmation."""
        return self._turns.complete_turn(
            token,
            assistant_content,
            response,
            confirmation_draft=confirmation_draft,
            commit_callback=commit_callback,
            before_commit=before_commit,
        )

    def fail_turn(self, token: BeginTurnToken) -> None:
        """Discard one pending turn after provider or safety failure."""
        self._turns.fail_turn(token)

    def pending_confirmation(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional['ConfirmationRecord']:
        """Return the exact current pending intent, if one exists."""
        return self._confirmations.pending_confirmation(
            user_id, conversation_id,
        )

    def confirmation_for_request(
        self,
        user_id: str,
        agent_request_id: str,
    ) -> 'ConfirmationRecord':
        """Return one durable intent selected by owner and agent request."""
        return self._confirmations.confirmation_for_request(
            user_id, agent_request_id,
        )

    def confirmation_for_response(
        self,
        user_id: str,
        response_id: str,
    ) -> Optional['ConfirmationRecord']:
        """Return an owner-scoped response only in its current context."""
        return self._confirmations.confirmation_for_response(
            user_id, response_id,
        )

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
        return self._text_claims.text_turn_request_claim(
            user_id, request_id, request_fingerprint,
        )

    def has_text_turn_claim_at_revision(
        self,
        user_id: str,
        conversation_id: str,
        session_instance_id: str,
        generation: int,
        revision: int,
    ) -> bool:
        """Return whether an exact conversation revision has a claim."""
        return self._text_claims.has_text_turn_claim_at_revision(
            user_id,
            conversation_id,
            session_instance_id,
            generation,
            revision,
        )

    def has_agent_request(self, user_id: str, request_id: str) -> bool:
        """Return whether the user already owns an Agent request ID."""
        return self._turns.has_agent_request(user_id, request_id)

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
        return self._text_claims.claim_text_turn_response(
            user_id,
            conversation_id,
            request_id=request_id,
            turn_id=turn_id,
            request_fingerprint=request_fingerprint,
            outcome=outcome,
            confirmation_request_id=confirmation_request_id,
            now=now,
        )

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
        return self._confirmations.invalidate_confirmation(
            user_id,
            conversation_id,
            result_code=result_code,
            now=now,
            expected_target_binding_digest=expected_target_binding_digest,
            response_id=response_id,
            response_turn_id=response_turn_id,
            text_turn_request_fingerprint=text_turn_request_fingerprint,
        )

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
        create_robot_action: bool = False,
        action_dispatch_window_seconds: float = 30.0,
        precommit_validator: Optional[
            Callable[[sqlite3.Connection], None]
        ] = None,
    ) -> 'ConfirmationRecord':
        """CAS a response and optionally create its action atomically."""
        return self._confirmations.resolve_confirmation(
            user_id,
            conversation_id,
            response_id=response_id,
            response_fingerprint=response_fingerprint,
            disposition=disposition,
            now=now,
            current_target_binding_digest=current_target_binding_digest,
            response_turn_id=response_turn_id,
            text_turn_request_fingerprint=text_turn_request_fingerprint,
            create_robot_action=create_robot_action,
            action_dispatch_window_seconds=action_dispatch_window_seconds,
            precommit_validator=precommit_validator,
        )

    def list_turns(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> List[ConversationTurn]:
        """Return completed turns in chronological order."""
        return self._turns.list_turns(user_id, conversation_id, limit)

    def reset(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Start a new active generation, retaining committed raw turns."""
        return self._sessions.reset(user_id, conversation_id)

    def close_session(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Close an active session without deleting committed history."""
        return self._sessions.close_session(user_id, conversation_id)

    def delete(
        self,
        user_id: str,
        conversation_id: str,
    ) -> bool:
        """Delete one session and all of its turns."""
        return self._sessions.delete(user_id, conversation_id)

    def purge_expired(self) -> int:
        """Mark every due active session as expired."""
        return self._sessions.purge_expired()

    # Legacy internal compatibility shims for sibling stores and tests.

    @staticmethod
    def _normalized_schema_sql(value: Optional[str]) -> str:
        return _normalized_schema_sql_fn(value)

    def _select_session_locked(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._sessions.select_session_locked(user_id, conversation_id)

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> ConversationTurn:
        return TurnRepository.turn_from_row(row)

    def _advance_summary_locked(
        self,
        token: BeginTurnToken,
        now: float,
    ) -> Optional[ConversationSummary]:
        return self._compaction.advance_summary_locked(token, now)

    @staticmethod
    def _extend_source_digest(
        previous_digest: str,
        turns: Sequence[Union[SummarySourceTurn, ConversationTurn]],
    ) -> str:
        return CompactionRepository.extend_source_digest(
            previous_digest,
            turns,
        )

    def _now(self) -> float:
        return self._db.now()

    def _begin(self) -> None:
        self._db.begin_immediate()
