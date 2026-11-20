"""Turn reservation, commit, failure cleanup, and history repository."""

import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from malbut_agent_server.conversation_store.codecs import (
    assistant_text,
    bounded_integer,
    load_response,
    required_text,
    response_json,
)
from malbut_agent_server.conversation_store.compaction import (
    CompactionRepository,
)
from malbut_agent_server.conversation_store.confirmation_codecs import (
    pending_confirmation_record,
)
from malbut_agent_server.conversation_store.confirmations import (
    ConfirmationRepository,
)
from malbut_agent_server.conversation_store.database import (
    ConversationDatabase,
)
from malbut_agent_server.conversation_store.models import (
    BeginTurnResult,
    BeginTurnToken,
    ConversationChangedError,
    ConversationConfig,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationSession,
    ConversationStateError,
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.conversation_store.sessions import (
    SessionRepository,
)
from malbut_agent_server.conversation_store.text_claims import (
    TextTurnClaimRepository,
)
from malbut_agent_server.memory_trace import record_trace
from malbut_agent_server.schemas import (
    MAX_SPEECH_TRANSCRIPT_LENGTH,
    MAX_UTTERANCE_LENGTH,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)

if TYPE_CHECKING:
    from malbut_agent_server.text_confirmation import ConfirmationDraft


class TurnRepository:
    """Manage ordered turn reservation, atomic commit, and history reads."""

    def __init__(
        self,
        db: ConversationDatabase,
        sessions: SessionRepository,
        compaction: CompactionRepository,
        confirmations: ConfirmationRepository,
        text_claims: TextTurnClaimRepository,
        config: ConversationConfig,
        *,
        advance_summary_hook: Optional[
            Callable[[BeginTurnToken, float], Optional[ConversationSummary]]
        ] = None,
    ) -> None:
        self._db = db
        self._sessions = sessions
        self._compaction = compaction
        self._confirmations = confirmations
        self._text_claims = text_claims
        self._config = config
        self._advance_summary_hook = advance_summary_hook

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
        if before_new_turn is not None and not callable(before_new_turn):
            raise TypeError('before_new_turn must be callable')
        max_user_content_chars = bounded_integer(
            max_user_content_chars,
            'max_user_content_chars',
            1,
            MAX_SPEECH_TRANSCRIPT_LENGTH,
        )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_turn = validate_turn_id(turn_id)
        normalized_request = required_text(request_id, 'request_id', 128)
        normalized_fingerprint = required_text(
            request_fingerprint,
            'request_fingerprint',
            128,
        )
        normalized_content = required_text(
            user_content,
            'user_content',
            max_user_content_chars,
        )
        now = self._db.now()
        with self._db.transaction() as conn:
            self._sessions.expire_due_locked(now)
            row = self._sessions.select_session_locked(
                normalized_user,
                normalized_id,
            )
            session = self._sessions.require_active(row)
            response_claim = self._text_claims.select_claim_locked(
                normalized_user,
                normalized_request,
            )
            if response_claim is not None:
                raise ConversationConflictError(
                    'request_id is already owned by a confirmation '
                    'response'
                )
            existing = self.existing_request_locked(
                normalized_user,
                normalized_request,
            )
            if existing is not None:
                return self._reuse_existing_locked(
                    existing,
                    normalized_id,
                    normalized_turn,
                    normalized_fingerprint,
                    normalized_content,
                    session,
                )
            turn_row = conn.execute(
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
            claimed_turn = conn.execute(
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
            pending = conn.execute(
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
            count_row = conn.execute(
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
            if (
                turn_count >= self._config.max_turns_per_session
                and not self._config.semantic_context
            ):
                raise ConversationStateError(
                    'conversation turn limit reached; '
                    'reset or create a new session'
                )
            if self._config.semantic_context:
                summary_row = self._compaction.select_summary_locked(session)
                summary = (
                    self._compaction.summary_from_row(summary_row)
                    if summary_row is not None
                    else None
                )
            else:
                summary = self._compaction.summary_for_window_locked(
                    session, turn_count, now,
                )
            history = self.history_locked(
                normalized_user,
                normalized_id,
                session.session_instance_id,
                session.generation,
                (
                    None
                    if self._config.semantic_context
                    else self._config.history_limit
                ),
                after_ordinal=(
                    summary.source_end_ordinal
                    if self._config.semantic_context and summary
                    else 0
                ),
            )
            ordinal = turn_count + 1
            if before_new_turn is not None:
                before_new_turn(conn)
            conn.execute(
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
                session_instance_id=session.session_instance_id,
                turn_id=normalized_turn,
                request_id=normalized_request,
                request_fingerprint=normalized_fingerprint,
                generation=session.generation,
                revision=session.revision,
                ordinal=ordinal,
            )
            return BeginTurnResult(
                session=session,
                history=tuple(history),
                summary=summary,
                token=token,
                cached_response=None,
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
        normalized_assistant = assistant_text(assistant_content)
        encoded_response = response_json(response)
        confirmation_record = pending_confirmation_record(confirmation_draft)
        now = self._db.now()
        changed_error: Optional[ConversationChangedError] = None
        with self._db.transaction() as conn:
            self._sessions.expire_due_locked(now)
            row = self._sessions.select_session_locked(
                token.user_id,
                token.conversation_id,
            )
            if row is None:
                changed_error = ConversationChangedError(
                    'conversation was deleted during model inference'
                )
            else:
                session = self._sessions.session_from_row(row)
                if (
                    session.status != 'active'
                    or session.session_instance_id
                    != token.session_instance_id
                    or session.generation != token.generation
                    or session.revision != token.revision
                ):
                    changed_error = ConversationChangedError(
                        'conversation changed during model inference; '
                        'retry with a new request_id and turn_id'
                    )
            if changed_error is None:
                if commit_callback is not None:
                    assistant_content, response = commit_callback(conn)
                    normalized_assistant = assistant_text(assistant_content)
                    encoded_response = response_json(response)
                cursor = conn.execute(
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
                        encoded_response,
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
                self.delete_pending_locked(token)
                conn.commit()
                raise changed_error
            if not self._config.semantic_context:
                advance = (
                    self._advance_summary_hook
                    if self._advance_summary_hook is not None
                    else self._compaction.advance_summary_locked
                )
                advance(token, now)
            self._confirmations.invalidate_pending_confirmations_locked(
                token.user_id,
                token.conversation_id,
                'confirmation_conversation_changed',
                now,
            )
            conn.execute(
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
                    now + self._config.ttl_seconds,
                    token.user_id,
                    token.conversation_id,
                    token.session_instance_id,
                ),
            )
            if confirmation_record is not None:
                self._confirmations.insert_confirmation_locked(
                    confirmation_record,
                    token,
                    response,
                    now,
                )
            session_row = self._sessions.select_session_locked(
                token.user_id,
                token.conversation_id,
            )
            turn_row = self.select_turn_locked(token)
            if before_commit is not None:
                # All final text/policy/session checks have succeeded. The
                # write transaction fences other writers during the short
                # ROS handoff; only then make this result durable.
                before_commit()
            record_trace(
                conn,
                'turn_committed',
                token.user_id,
                request_id=token.request_id,
                conversation_id=token.conversation_id,
                turn_id=token.turn_id,
                data={
                    'generation': token.generation,
                    'ordinal': token.ordinal,
                },
            )
            return (
                self._sessions.session_from_row(session_row),
                self.turn_from_row(turn_row),
            )

    def fail_turn(self, token: BeginTurnToken) -> None:
        """Discard one pending turn after provider or safety failure."""
        with self._db.locked() as conn:
            self.delete_pending_locked(token)
            conn.commit()

    def has_agent_request(self, user_id: str, request_id: str) -> bool:
        """Return whether the user already owns an Agent request ID."""
        normalized_user = validate_user_id(user_id)
        normalized_request = required_text(request_id, 'request_id', 128)
        with self._db.transaction():
            self._sessions.expire_due_locked(self._db.now())
            return (
                self.existing_request_locked(
                    normalized_user,
                    normalized_request,
                )
                is not None
            )

    def list_turns(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> List[ConversationTurn]:
        """Return completed turns in chronological order."""
        normalized_limit = bounded_integer(limit, 'turn limit', 1, 500)
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._db.locked():
            self._sessions.expire_and_commit(self._db.now())
            row = self._sessions.select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            session = self._sessions.session_from_row(row)
            return self.history_locked(
                normalized_user,
                normalized_id,
                session.session_instance_id,
                session.generation,
                normalized_limit,
            )

    def history_locked(
        self,
        user_id: str,
        conversation_id: str,
        session_instance_id: str,
        generation: int,
        limit: Optional[int],
        after_ordinal: int = 0,
    ) -> List[ConversationTurn]:
        """Load completed turns in chronological order inside the caller's lock."""
        rows = self._db.connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND status = 'completed'
              AND ordinal > ?
            ORDER BY ordinal DESC
            LIMIT ?
            ''',
            (
                user_id,
                conversation_id,
                session_instance_id,
                generation,
                after_ordinal,
                -1 if limit is None else limit,
            ),
        ).fetchall()
        return [self.turn_from_row(row) for row in reversed(rows)]

    def existing_request_locked(
        self,
        user_id: str,
        request_id: str,
    ) -> Optional[sqlite3.Row]:
        """Select any existing turn row owned by (user_id, request_id)."""
        return self._db.connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ? AND request_id = ?
            ''',
            (user_id, request_id),
        ).fetchone()

    def select_turn_locked(
        self,
        token: BeginTurnToken,
    ) -> Optional[sqlite3.Row]:
        """Select one completed turn row matching a BeginTurnToken."""
        return self._db.connection.execute(
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

    def delete_pending_locked(self, token: BeginTurnToken) -> None:
        """Remove one pending turn row matching a BeginTurnToken."""
        self._db.connection.execute(
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
    def _reuse_existing_locked(
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
        response = load_response(row['response_json'])
        return BeginTurnResult(
            session=session,
            history=(),
            summary=None,
            token=None,
            cached_response=response,
        )

    @staticmethod
    def turn_from_row(row: sqlite3.Row) -> ConversationTurn:
        """Map one completed turn SQLite row to a ConversationTurn."""
        if row is None or row['status'] != 'completed':
            raise RuntimeError('conversation turn is not completed')
        response = load_response(row['response_json'])
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
