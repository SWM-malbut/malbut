"""Durable text-turn request claim repository (idempotent request namespace)."""

import sqlite3
from typing import TYPE_CHECKING, Optional, Tuple

from malbut_agent_server.conversation_store.codecs import (
    bounded_integer,
    finite_timestamp,
    required_digest,
    required_text,
)
from malbut_agent_server.conversation_store.confirmation_codecs import (
    confirmation_record_from_row,
    require_text_turn_claim_context,
    require_text_turn_claim_match,
    text_turn_claim_from_row,
)
from malbut_agent_server.conversation_store.database import (
    ConversationDatabase,
)
from malbut_agent_server.conversation_store.models import (
    TEXT_TURN_CLAIM_OUTCOMES,
    TEXT_TURN_CLAIM_SCHEMA_VERSION,
    ConfirmationIntentConflictError,
    ConfirmationSchemaError,
    ConversationConflictError,
    ConversationSession,
    TextTurnRequestClaim,
)
from malbut_agent_server.conversation_store.sessions import (
    SessionRepository,
)
from malbut_agent_server.schemas import (
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)

if TYPE_CHECKING:
    from malbut_agent_server.text_confirmation import ConfirmationRecord


class TextTurnClaimRepository:
    """Manage unified text-turn request claims and replay checks."""

    def __init__(
        self,
        db: ConversationDatabase,
        sessions: SessionRepository,
    ) -> None:
        self._db = db
        self._sessions = sessions

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
        normalized_request = required_text(
            request_id,
            'request_id',
            128,
        )
        normalized_fingerprint = required_digest(
            request_fingerprint,
            'request_fingerprint',
        )
        with self._db.transaction():
            self._sessions.expire_due_locked(self._db.now())
            claim = self.select_claim_locked(
                normalized_user,
                normalized_request,
            )
            if claim is None:
                return None
            if claim.request_fingerprint != normalized_fingerprint:
                raise ConfirmationIntentConflictError(
                    'text turn request_id was reused with different '
                    'payload'
                )
            session = self._sessions.require_active(
                self._sessions.select_session_locked(
                    normalized_user,
                    claim.conversation_id,
                )
            )
            require_text_turn_claim_context(claim, session)
            record = self.linked_confirmation_locked(claim)
            return claim, record

    def has_text_turn_claim_at_revision(
        self,
        user_id: str,
        conversation_id: str,
        session_instance_id: str,
        generation: int,
        revision: int,
    ) -> bool:
        """Return whether an exact conversation revision has a claim."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_session_instance = required_text(
            session_instance_id,
            'session_instance_id',
            128,
        )
        normalized_generation = bounded_integer(
            generation,
            'generation',
            1,
            2 ** 63 - 1,
        )
        normalized_revision = bounded_integer(
            revision,
            'revision',
            0,
            2 ** 63 - 1,
        )
        with self._db.transaction() as conn:
            self._sessions.expire_due_locked(self._db.now())
            row = conn.execute(
                '''
                SELECT 1
                FROM text_turn_request_claims
                WHERE user_id = ?
                  AND conversation_id = ?
                  AND session_instance_id = ?
                  AND generation = ?
                  AND revision = ?
                LIMIT 1
                ''',
                (
                    normalized_user,
                    normalized_id,
                    normalized_session_instance,
                    normalized_generation,
                    normalized_revision,
                ),
            ).fetchone()
            return row is not None

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
        normalized_request = required_text(
            request_id,
            'request_id',
            128,
        )
        normalized_turn = validate_turn_id(turn_id)
        normalized_fingerprint = required_digest(
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
            normalized_confirmation = required_text(
                confirmation_request_id,
                'confirmation_request_id',
                128,
            )
        claimed_at = finite_timestamp(now, 'now')
        with self._db.transaction() as conn:
            self._sessions.expire_due_locked(claimed_at)
            session = self._sessions.require_active(
                self._sessions.select_session_locked(
                    normalized_user,
                    normalized_id,
                )
            )
            existing = self.select_claim_locked(
                normalized_user,
                normalized_request,
            )
            if existing is not None:
                require_text_turn_claim_match(
                    existing,
                    conversation_id=normalized_id,
                    session=session,
                    turn_id=normalized_turn,
                    request_fingerprint=normalized_fingerprint,
                    outcome=outcome,
                    confirmation_request_id=normalized_confirmation,
                )
                record = self.linked_confirmation_locked(existing)
                return existing, record
            pending_turn = conn.execute(
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
                    normalized_user,
                    normalized_id,
                    session.session_instance_id,
                    session.generation,
                ),
            ).fetchone()
            if pending_turn is not None:
                raise ConversationConflictError(
                    'another turn is already in progress'
                )
            self.require_namespace_available_locked(
                normalized_user,
                normalized_id,
                session,
                normalized_request,
                normalized_turn,
            )
            pending_row = self.current_pending_confirmation_locked(
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
                record = confirmation_record_from_row(pending_row)
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
            self.insert_claim_locked(claim)
            return claim, record

    def select_claim_locked(
        self,
        user_id: str,
        request_id: str,
    ) -> Optional[TextTurnRequestClaim]:
        """Select one text-turn request claim inside the caller's lock."""
        row = self._db.connection.execute(
            '''
            SELECT *
            FROM text_turn_request_claims
            WHERE user_id = ? AND request_id = ?
            ''',
            (user_id, request_id),
        ).fetchone()
        return (
            text_turn_claim_from_row(row)
            if row is not None
            else None
        )

    def insert_claim_locked(
        self,
        claim: TextTurnRequestClaim,
    ) -> None:
        """Persist one content-free text-turn request claim."""
        if claim.outcome not in TEXT_TURN_CLAIM_OUTCOMES:
            raise ValidationError('text turn claim outcome is unsupported')
        try:
            self._db.connection.execute(
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

    def require_namespace_available_locked(
        self,
        user_id: str,
        conversation_id: str,
        session: ConversationSession,
        request_id: str,
        turn_id: str,
    ) -> None:
        """Ensure request_id and turn_id are unclaimed across claims and turns."""
        existing_claim = self.select_claim_locked(user_id, request_id)
        if existing_claim is not None:
            raise ConfirmationIntentConflictError(
                'text response request_id is already claimed'
            )
        conn = self._db.connection
        request_owner = conn.execute(
            '''
            SELECT 1
            FROM conversation_turns
            WHERE user_id = ? AND request_id = ?
            LIMIT 1
            ''',
            (user_id, request_id),
        ).fetchone()
        if request_owner is not None:
            raise ConfirmationIntentConflictError(
                'request_id is already owned by an agent turn'
            )
        turn_owner = conn.execute(
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

    def linked_confirmation_locked(
        self,
        claim: TextTurnRequestClaim,
    ) -> Optional['ConfirmationRecord']:
        """Load and verify the confirmation record bound to a claim."""
        if claim.confirmation_request_id is None:
            return None
        row = self._db.connection.execute(
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
        record = confirmation_record_from_row(row)
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

    def current_pending_confirmation_locked(
        self,
        session: ConversationSession,
    ) -> Optional[sqlite3.Row]:
        """Select the pending confirmation matching the exact session revision."""
        return self._db.connection.execute(
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
