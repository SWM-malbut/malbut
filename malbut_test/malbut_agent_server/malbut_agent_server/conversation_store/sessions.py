"""Session lifecycle repository (create, resume, reset, close, expire)."""

import sqlite3
import uuid
from typing import Callable, Optional

from malbut_agent_server.conversation_store.database import (
    ConversationDatabase,
)
from malbut_agent_server.conversation_store.models import (
    ConversationConfig,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationSession,
    ConversationStateError,
)
from malbut_agent_server.schemas import (
    validate_conversation_id,
    validate_user_id,
)


def _noop_invalidate(
    _user_id: str,
    _conversation_id: str,
    _result_code: str,
    _now: float,
) -> None:
    return None


def _noop_expire(_now: float) -> None:
    return None


class SessionRepository:
    """Manage user-scoped conversation sessions and TTL expiration."""

    def __init__(
        self,
        db: ConversationDatabase,
        config: ConversationConfig,
        *,
        on_invalidate_pending_confirmations: Callable[
            [str, str, str, float], None
        ] = _noop_invalidate,
        on_expire_due_confirmations: Callable[[float], None] = _noop_expire,
    ) -> None:
        self._db = db
        self._config = config
        self._on_invalidate_pending_confirmations = (
            on_invalidate_pending_confirmations
        )
        self._on_expire_due_confirmations = on_expire_due_confirmations

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
        now = self._db.now()
        with self._db.transaction() as conn:
            self.expire_due_locked(now)
            row = self.select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is not None:
                session = self.session_from_row(row)
                if session.status == 'active':
                    return session
                raise ConversationConflictError(
                    'conversation_id belongs to a closed or '
                    'expired session'
                )
            count_row = conn.execute(
                '''
                SELECT COUNT(*) AS session_count
                FROM conversation_sessions
                WHERE user_id = ?
                  AND (? = 0 OR status = 'active')
                ''',
                (normalized_user, int(self._config.semantic_context)),
            ).fetchone()
            session_count = int(count_row['session_count'])
            if (
                session_count >= self._config.max_sessions_per_user
                and not self._config.semantic_context
            ):
                # Retain expired history until admission needs its space.
                # The existing expiry and instance fences reject late turns.
                removed = conn.execute(
                    '''
                    DELETE FROM conversation_sessions
                    WHERE user_id = ? AND conversation_id IN (
                        SELECT conversation_id
                        FROM conversation_sessions
                        WHERE user_id = ? AND status = 'expired'
                        ORDER BY expires_at, created_at, conversation_id
                        LIMIT ?
                    )
                    ''',
                    (
                        normalized_user,
                        normalized_user,
                        session_count
                        - self._config.max_sessions_per_user
                        + 1,
                    ),
                ).rowcount
                session_count -= removed
            if session_count >= self._config.max_sessions_per_user:
                raise ConversationStateError(
                    'conversation session limit reached; '
                    'delete an old session'
                )
            conn.execute(
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
                    now + self._config.ttl_seconds,
                ),
            )
            row = self.select_session_locked(
                normalized_user,
                normalized_id,
            )
            return self.session_from_row(row)

    def resume_or_create(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
        *,
        start_new: bool = False,
        create_fn: Optional[Callable[[str], ConversationSession]] = None,
        close_fn: Optional[
            Callable[[str, str], ConversationSession]
        ] = None,
    ) -> ConversationSession:
        """Resume live context without counting startup or reads as dialogue."""
        normalized_user = validate_user_id(user_id)
        normalized_id = (
            validate_conversation_id(conversation_id)
            if conversation_id is not None
            else None
        )
        create_session = create_fn if create_fn is not None else self.create
        close_existing = (
            close_fn if close_fn is not None else self.close_session
        )
        with self._db.locked() as conn:
            self.expire_and_commit(self._db.now())
            if normalized_id is None:
                row = conn.execute(
                    '''
                    SELECT * FROM conversation_sessions
                    WHERE user_id = ? AND status = 'active'
                    ORDER BY updated_at DESC, created_at DESC, conversation_id
                    LIMIT 1
                    ''',
                    (normalized_user,),
                ).fetchone()
            else:
                row = self.select_session_locked(
                    normalized_user, normalized_id,
                )
            if row is not None and row['status'] == 'active':
                if not start_new:
                    return self.session_from_row(row)
                # Closing fences pending inference and compaction; raw turns stay.
                close_existing(normalized_user, row['conversation_id'])
            return create_session(normalized_user)

    def get(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Return one session after lazily expiring due sessions."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        now = self._db.now()
        with self._db.locked():
            self.expire_and_commit(now)
            row = self.select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            return self.session_from_row(row)

    def reset(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Start a new active generation, retaining committed raw turns."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        now = self._db.now()
        with self._db.transaction() as conn:
            self.expire_due_locked(now)
            row = self.select_session_locked(
                normalized_user,
                normalized_id,
            )
            session = self.require_active(row)
            self._on_invalidate_pending_confirmations(
                normalized_user,
                normalized_id,
                'confirmation_conversation_changed',
                now,
            )
            conn.execute(
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
            if not self._config.semantic_context:
                conn.execute(
                    '''
                    DELETE FROM conversation_summaries
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (normalized_user, normalized_id),
                )
            conn.execute(
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
                    now + self._config.ttl_seconds,
                    normalized_user,
                    normalized_id,
                ),
            )
            row = self.select_session_locked(
                normalized_user,
                normalized_id,
            )
            return self.session_from_row(row)

    def close_session(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Close an active session without deleting committed history."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        now = self._db.now()
        with self._db.transaction() as conn:
            self.expire_due_locked(now)
            row = self.select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            session = self.session_from_row(row)
            if session.status == 'expired':
                raise ConversationStateError(
                    'conversation has expired'
                )
            self._on_invalidate_pending_confirmations(
                normalized_user,
                normalized_id,
                'confirmation_session_closed',
                now,
            )
            conn.execute(
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
                conn.execute(
                    '''
                    UPDATE conversation_sessions
                    SET status = 'closed',
                        revision = revision + 1,
                        updated_at = ?
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (now, normalized_user, normalized_id),
                )
            row = self.select_session_locked(
                normalized_user,
                normalized_id,
            )
            return self.session_from_row(row)

    def delete(
        self,
        user_id: str,
        conversation_id: str,
    ) -> bool:
        """Delete one session and all of its turns."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._db.locked() as conn:
            cursor = conn.execute(
                '''
                DELETE FROM conversation_sessions
                WHERE user_id = ? AND conversation_id = ?
                ''',
                (normalized_user, normalized_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def purge_expired(self) -> int:
        """Mark every due active session as expired."""
        with self._db.transaction():
            return self.expire_due_locked(self._db.now())

    def expire_and_commit(self, now: float) -> None:
        """Lazily expire due sessions in one atomic transaction."""
        with self._db.transaction():
            self.expire_due_locked(now)

    def expire_due_locked(self, now: float) -> int:
        """Expire due active sessions and notify confirmation hooks."""
        conn = self._db.connection
        due_rows = conn.execute(
            '''
            SELECT user_id, conversation_id, session_instance_id
            FROM conversation_sessions
            WHERE status = 'active' AND expires_at <= ?
            ''',
            (now,),
        ).fetchall()
        for row in due_rows:
            self._on_invalidate_pending_confirmations(
                row['user_id'],
                row['conversation_id'],
                'confirmation_session_expired',
                now,
            )
            conn.execute(
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
            if not self._config.semantic_context:
                conn.execute(
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
        cursor = conn.execute(
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
        self._on_expire_due_confirmations(now)
        return cursor.rowcount

    def select_session_locked(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[sqlite3.Row]:
        """Select one raw session row inside the caller's lock."""
        return self._db.connection.execute(
            '''
            SELECT *
            FROM conversation_sessions
            WHERE user_id = ? AND conversation_id = ?
            ''',
            (user_id, conversation_id),
        ).fetchone()

    @staticmethod
    def require_active(
        row: Optional[sqlite3.Row],
    ) -> ConversationSession:
        """Ensure the selected session row exists and is currently active."""
        if row is None:
            raise ConversationNotFoundError(
                'conversation was not found'
            )
        session = SessionRepository.session_from_row(row)
        if session.status != 'active':
            raise ConversationStateError(
                f'conversation is {session.status}'
            )
        return session

    @staticmethod
    def session_from_row(
        row: sqlite3.Row,
    ) -> ConversationSession:
        """Map one SQLite row to an immutable ConversationSession."""
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
