"""Conversation summary and compaction repository."""

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Callable, List, Optional, Sequence, Union

from malbut_agent_server.conversation_store.database import (
    ConversationDatabase,
)
from malbut_agent_server.conversation_store.models import (
    SUMMARY_UPDATE_BATCH_SIZE,
    BeginTurnToken,
    ConversationConfig,
    ConversationNotFoundError,
    ConversationSession,
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.conversation_store.sessions import (
    SessionRepository,
)
from malbut_agent_server.schemas import (
    validate_conversation_id,
    validate_user_id,
)
from malbut_agent_server.summarization import (
    ExtractiveConversationSummarizer,
    SummaryResult,
    SummarySourceTurn,
)


class CompactionRepository:
    """Persist and advance rolling summaries for turns outside the raw window."""

    def __init__(
        self,
        db: ConversationDatabase,
        sessions: SessionRepository,
        config: ConversationConfig,
        *,
        summarizer: Optional[ExtractiveConversationSummarizer] = None,
        advance_summary_hook: Optional[
            Callable[[BeginTurnToken, float], Optional[ConversationSummary]]
        ] = None,
    ) -> None:
        self._db = db
        self._sessions = sessions
        self._config = config
        self.summarizer = (
            summarizer
            if summarizer is not None
            else ExtractiveConversationSummarizer()
        )
        self._advance_summary_hook = advance_summary_hook

    def get_summary(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[ConversationSummary]:
        """Return the summary for the current session generation."""
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
            summary_row = self.select_summary_locked(session)
            return (
                self.summary_from_row(summary_row)
                if summary_row is not None
                else None
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
        sources = tuple(source_turns)
        if (
            not self._config.semantic_context
            or not sources
            or not isinstance(result, SummaryResult)
            or not isinstance(result.content, str)
            or not result.content.strip()
            or not isinstance(result.state_json, str)
            or not isinstance(result.algorithm, str)
            or not result.algorithm.startswith('openai-semantic-')
            or result.fallback_used
        ):
            return False
        if (
            expected_summary is not None
            and not expected_summary.summarizer.startswith(
                'openai-semantic-'
            )
        ):
            expected_summary = None
        previous_end = (
            expected_summary.source_end_ordinal
            if expected_summary
            else 0
        )
        start, end = sources[0].ordinal, sources[-1].ordinal
        if (
            start not in {1, previous_end + 1}
            or end >= token.ordinal
            or [turn.ordinal for turn in sources]
            != list(range(start, end + 1))
            or any(
                turn.user_id != token.user_id
                or turn.conversation_id != token.conversation_id
                or turn.session_instance_id != token.session_instance_id
                or turn.generation != token.generation
                for turn in sources
            )
        ):
            return False
        with self._db.transaction() as conn:
            now = self._db.now()
            self._sessions.expire_due_locked(now)
            row = self._sessions.select_session_locked(
                token.user_id, token.conversation_id,
            )
            session = (
                self._sessions.session_from_row(row) if row else None
            )
            if (
                session is None
                or session.status != 'active'
                or session.session_instance_id
                != token.session_instance_id
                or session.generation != token.generation
                or session.revision < token.revision
            ):
                return False
            summary_row = self.select_summary_locked(session)
            existing = (
                self.summary_from_row(summary_row)
                if summary_row is not None
                else None
            )
            stored = self.summary_source_turns_locked(
                token, start - 1, end,
            )
            if (
                existing != expected_summary
                or [(turn.ordinal, turn.turn_id) for turn in stored]
                != [(turn.ordinal, turn.turn_id) for turn in sources]
                or (guard is not None and not guard(conn))
            ):
                return False
            previous_digest = (
                existing.source_digest if existing and start != 1 else ''
            )
            self.save_summary_locked(
                session,
                existing,
                result,
                end,
                self.extend_source_digest(previous_digest, sources),
                now,
            )
            return True

    def advance_summary_locked(
        self,
        token: BeginTurnToken,
        now: float,
    ) -> Optional[ConversationSummary]:
        """Atomically extend the summary beyond the recent raw window."""
        source_end = token.ordinal - self._config.history_limit
        if source_end < 1:
            return None
        session_row = self._sessions.select_session_locked(
            token.user_id,
            token.conversation_id,
        )
        session = self._sessions.session_from_row(session_row)
        existing_row = self.select_summary_locked(session)
        existing = (
            self.summary_from_row(existing_row)
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
        source_turns = self.summary_source_turns_locked(
            token,
            previous_end,
            source_end,
        )
        expected_ordinals = list(
            range(previous_end + 1, source_end + 1)
        )
        actual_ordinals = [turn.ordinal for turn in source_turns]
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
            result = self.summarize_in_batches(
                self.summarizer,
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
            full_turns = self.summary_source_turns_locked(
                token,
                0,
                source_end,
            )
            full_ordinals = [turn.ordinal for turn in full_turns]
            if full_ordinals != list(range(1, source_end + 1)):
                full_turns = []
            recovery = ExtractiveConversationSummarizer()
            result = self.summarize_in_batches(
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

        source_digest = self.extend_source_digest(
            digest_previous,
            digest_turns,
        )
        return self.save_summary_locked(
            session,
            existing,
            SummaryResult(
                content, state_json, summarizer_name, fallback_used,
            ),
            source_end,
            source_digest,
            now,
        )

    def save_summary_locked(
        self,
        session: ConversationSession,
        existing: Optional[ConversationSummary],
        result: SummaryResult,
        source_end: int,
        source_digest: str,
        now: float,
    ) -> ConversationSummary:
        """Persist one derived summary without changing raw turns or activity."""
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
        self._db.connection.execute(
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
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            ''',
            (
                session.user_id,
                session.conversation_id,
                session.session_instance_id,
                session.generation,
                summary_id,
                summary_revision,
                result.content,
                result.state_json,
                1,
                source_end,
                source_end,
                source_digest,
                result.algorithm,
                int(result.fallback_used),
                created_at,
                now,
            ),
        )
        row = self.select_summary_locked(session)
        return self.summary_from_row(row)

    def summary_source_turns_locked(
        self,
        token: BeginTurnToken,
        start_exclusive: int,
        end_inclusive: int,
    ) -> List[SummarySourceTurn]:
        """Load committed source turns within the requested ordinal slice."""
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

    def summarize_in_batches(
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
                max_chars=self._config.summary_max_chars,
            )
            if (
                not isinstance(candidate, SummaryResult)
                or not isinstance(candidate.content, str)
                or not isinstance(candidate.state_json, str)
                or not isinstance(candidate.algorithm, str)
                or len(candidate.content) > self._config.summary_max_chars
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

    def summary_for_window_locked(
        self,
        session: ConversationSession,
        completed_turn_count: int,
        now: float,
    ) -> Optional[ConversationSummary]:
        """Align persisted summary coverage with the configured raw N."""
        desired_end = max(
            0,
            completed_turn_count - self._config.history_limit,
        )
        row = self.select_summary_locked(session)
        existing = (
            self.summary_from_row(row)
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
            self._db.connection.execute(
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
        advance = (
            self._advance_summary_hook
            if self._advance_summary_hook is not None
            else self.advance_summary_locked
        )
        return advance(synthetic, now)

    @staticmethod
    def extend_source_digest(
        previous_digest: str,
        turns: Sequence[Union[SummarySourceTurn, ConversationTurn]],
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

    def select_summary_locked(
        self,
        session: ConversationSession,
    ) -> Optional[sqlite3.Row]:
        """Select the persisted summary row for one session generation."""
        return self._db.connection.execute(
            '''
            SELECT *
            FROM conversation_summaries
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND (? = 0 OR summarizer LIKE 'openai-semantic-%')
            ''',
            (
                session.user_id,
                session.conversation_id,
                session.session_instance_id,
                session.generation,
                int(self._config.semantic_context),
            ),
        ).fetchone()

    @staticmethod
    def summary_from_row(
        row: sqlite3.Row,
    ) -> ConversationSummary:
        """Map one SQLite row to an immutable ConversationSummary."""
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
            source_start_ordinal=int(row['source_start_ordinal']),
            source_end_ordinal=int(row['source_end_ordinal']),
            source_turn_count=int(row['source_turn_count']),
            source_digest=row['source_digest'],
            summarizer=row['summarizer'],
            fallback_used=bool(row['fallback_used']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
        )
