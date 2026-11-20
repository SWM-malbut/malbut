"""Durable confirmation intent lifecycle repository."""

import hashlib
import math
import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

from malbut_agent_server.conversation_store.codecs import (
    finite_timestamp,
    required_digest,
    required_text,
)
from malbut_agent_server.conversation_store.confirmation_codecs import (
    confirmation_record_from_row,
    confirmation_storage_values,
    require_confirmation_context,
    require_text_turn_claim_match,
    validate_confirmation_response,
    verified_confirmation_resolution,
)
from malbut_agent_server.conversation_store.database import (
    ConversationDatabase,
)
from malbut_agent_server.conversation_store.models import (
    BeginTurnToken,
    ConfirmationIntentAlreadyTerminalError,
    ConfirmationIntentConflictError,
    ConfirmationIntentNotFoundError,
    ConfirmationSchemaError,
    ConversationChangedError,
    ConversationSession,
    TextTurnRequestClaim,
)
from malbut_agent_server.conversation_store.sessions import (
    SessionRepository,
)
from malbut_agent_server.conversation_store.text_claims import (
    TextTurnClaimRepository,
)
from malbut_agent_server.schemas import (
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)

if TYPE_CHECKING:
    from malbut_agent_server.text_confirmation import ConfirmationRecord


class ConfirmationRepository:
    """Manage pending, resolved, expired, and invalidated confirmation intents."""

    def __init__(
        self,
        db: ConversationDatabase,
        sessions: SessionRepository,
        text_claims: TextTurnClaimRepository,
    ) -> None:
        self._db = db
        self._sessions = sessions
        self._text_claims = text_claims

    def pending_confirmation(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional['ConfirmationRecord']:
        """Return the exact current pending intent, if one exists."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._db.transaction() as conn:
            now = self._db.now()
            self._sessions.expire_due_locked(now)
            session = self._sessions.require_active(
                self._sessions.select_session_locked(
                    normalized_user,
                    normalized_id,
                )
            )
            row = conn.execute(
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
                self.invalidate_confirmation_row_locked(
                    row,
                    'confirmation_conversation_changed',
                    now,
                )
                row = None
            return (
                confirmation_record_from_row(row)
                if row is not None
                else None
            )

    def confirmation_for_request(
        self,
        user_id: str,
        agent_request_id: str,
    ) -> 'ConfirmationRecord':
        """Return one durable intent selected by owner and agent request."""
        normalized_user = validate_user_id(user_id)
        normalized_request = required_text(
            agent_request_id,
            'agent_request_id',
            128,
        )
        with self._db.transaction() as conn:
            self._sessions.expire_due_locked(self._db.now())
            row = conn.execute(
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
            return confirmation_record_from_row(row)

    def confirmation_for_response(
        self,
        user_id: str,
        response_id: str,
    ) -> Optional['ConfirmationRecord']:
        """Return an owner-scoped response only in its current context."""
        normalized_user = validate_user_id(user_id)
        normalized_response = required_text(
            response_id,
            'response_id',
            128,
        )
        with self._db.transaction() as conn:
            self._sessions.expire_due_locked(self._db.now())
            row = conn.execute(
                '''
                SELECT *
                FROM confirmation_intents
                WHERE user_id = ? AND response_id = ?
                ''',
                (normalized_user, normalized_response),
            ).fetchone()
            if row is None:
                return None
            session_row = self._sessions.select_session_locked(
                normalized_user,
                row['conversation_id'],
            )
            if session_row is None:
                return None
            session = self._sessions.session_from_row(session_row)
            if (
                session.status == 'active'
                and row['session_instance_id'] == session.session_instance_id
                and int(row['generation']) == session.generation
                and int(row['revision']) == session.revision
            ):
                return confirmation_record_from_row(row)
            return None

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
        normalized_code = required_text(result_code, 'result_code', 128)
        if normalized_code == 'confirmation_pending':
            raise ValidationError(
                'invalidation result_code must be terminal'
            )
        invalidated_at = finite_timestamp(now, 'now')
        expected_target = (
            required_text(
                expected_target_binding_digest,
                'expected_target_binding_digest',
                128,
            )
            if expected_target_binding_digest is not None
            else None
        )
        (
            normalized_response_id,
            normalized_response_turn,
            normalized_text_fingerprint,
        ) = self._validate_invalidation_claim_args(
            response_id,
            response_turn_id,
            text_turn_request_fingerprint,
        )
        with self._db.transaction():
            self._sessions.expire_due_locked(invalidated_at)
            session = self._sessions.require_active(
                self._sessions.select_session_locked(
                    normalized_user,
                    normalized_id,
                )
            )
            if normalized_response_id is not None:
                replay = self._replay_invalidation_claim_locked(
                    normalized_user,
                    normalized_id,
                    session,
                    normalized_response_id,
                    normalized_response_turn,
                    normalized_text_fingerprint,
                    normalized_code,
                )
                if replay is not None:
                    return replay
            row = self._select_pending_or_fail_locked(
                normalized_user,
                normalized_id,
                session,
                ordered_terminal_check=False,
            )
            target_digest = (
                row['target_binding_digest']
                if expected_target is None
                else expected_target
            )
            require_confirmation_context(row, session, target_digest)
            if invalidated_at < float(row['issued_at']):
                raise ConfirmationIntentConflictError(
                    'confirmation invalidation predates proposal'
                )
            record = confirmation_record_from_row(row)
            invalidated = record.invalidate(
                normalized_code,
                resolved_at=invalidated_at,
            )
            cursor = self.write_confirmation_record_locked(
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
                self._text_claims.insert_claim_locked(
                    TextTurnRequestClaim(
                        user_id=normalized_user,
                        request_id=normalized_response_id,
                        conversation_id=normalized_id,
                        session_instance_id=session.session_instance_id,
                        generation=session.generation,
                        revision=session.revision,
                        turn_id=normalized_response_turn,
                        request_fingerprint=normalized_text_fingerprint,
                        outcome='confirmation_invalidated',
                        confirmation_request_id=(
                            invalidated.confirmation_request_id
                        ),
                        created_at=invalidated_at,
                    )
                )
            return invalidated

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
        (
            normalized_user,
            normalized_id,
            normalized_response_id,
            normalized_response_fingerprint,
            normalized_response_turn,
            normalized_text_fingerprint,
            resolved_at,
            target_digest,
        ) = self._validate_resolve_args(
            user_id=user_id,
            conversation_id=conversation_id,
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
        with self._db.transaction() as conn:
            self._sessions.expire_due_locked(resolved_at)
            session = self._sessions.require_active(
                self._sessions.select_session_locked(
                    normalized_user,
                    normalized_id,
                )
            )
            if precommit_validator is not None:
                precommit_validator(conn)
            claim_replay = self._replay_resolution_claim_locked(
                normalized_user=normalized_user,
                normalized_id=normalized_id,
                session=session,
                normalized_response_id=normalized_response_id,
                normalized_response_turn=normalized_response_turn,
                normalized_response_fingerprint=(
                    normalized_response_fingerprint
                ),
                normalized_text_fingerprint=normalized_text_fingerprint,
                disposition=disposition,
                target_digest=target_digest,
            )
            if claim_replay is not None:
                return claim_replay
            self._text_claims.require_namespace_available_locked(
                normalized_user,
                normalized_id,
                session,
                normalized_response_id,
                normalized_response_turn,
            )
            owner_replay = self._replay_response_owner_locked(
                normalized_user=normalized_user,
                normalized_id=normalized_id,
                session=session,
                normalized_response_id=normalized_response_id,
                normalized_response_turn=normalized_response_turn,
                normalized_response_fingerprint=(
                    normalized_response_fingerprint
                ),
                normalized_text_fingerprint=normalized_text_fingerprint,
                disposition=disposition,
                target_digest=target_digest,
                resolved_at=resolved_at,
            )
            if owner_replay is not None:
                return owner_replay
            return self._resolve_pending_row_locked(
                normalized_user=normalized_user,
                normalized_id=normalized_id,
                session=session,
                normalized_response_id=normalized_response_id,
                normalized_response_turn=normalized_response_turn,
                normalized_response_fingerprint=(
                    normalized_response_fingerprint
                ),
                normalized_text_fingerprint=normalized_text_fingerprint,
                disposition=disposition,
                target_digest=target_digest,
                resolved_at=resolved_at,
                create_robot_action=create_robot_action,
                action_dispatch_window_seconds=action_dispatch_window_seconds,
            )

    def insert_confirmation_locked(
        self,
        record: 'ConfirmationRecord',
        token: BeginTurnToken,
        response: Dict[str, Any],
        now: float,
    ) -> None:
        """Persist one newly proposed pending confirmation atomically with a turn."""
        if record.disposition != 'pending':
            raise ConfirmationIntentConflictError(
                'new confirmation intent must be pending'
            )
        expected = (
            record.user_id == token.user_id
            and record.conversation_id == token.conversation_id
            and record.session_instance_id == token.session_instance_id
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
        validate_confirmation_response(record, response)
        values = confirmation_storage_values(record, now)
        try:
            self._db.connection.execute(
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

    def confirmation_row_for_record_locked(
        self,
        record: 'ConfirmationRecord',
    ) -> sqlite3.Row:
        """Select and verify the durable row backing a confirmation record."""
        row = self._db.connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE confirmation_request_id = ?
            ''',
            (record.confirmation_request_id,),
        ).fetchone()
        if row is None or confirmation_record_from_row(row) != record:
            raise ConfirmationSchemaError(
                'confirmation record is not durably bound'
            )
        return row

    def write_confirmation_record_locked(
        self,
        old_row: sqlite3.Row,
        record: 'ConfirmationRecord',
        updated_at: float,
        *,
        expected_state: str,
    ) -> sqlite3.Cursor:
        """Compare-and-swap one confirmation row to a new terminal state."""
        values = confirmation_storage_values(
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
            return self._db.connection.execute(
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

    def invalidate_confirmation_row_locked(
        self,
        row: sqlite3.Row,
        result_code: str,
        now: float,
    ) -> None:
        """CAS-invalidate one pending confirmation row."""
        record = confirmation_record_from_row(row)
        invalidated = record.invalidate(result_code, resolved_at=now)
        cursor = self.write_confirmation_record_locked(
            row,
            invalidated,
            now,
            expected_state='pending',
        )
        if cursor.rowcount != 1:
            raise ConfirmationIntentConflictError(
                'confirmation invalidation compare-and-swap failed'
            )

    def invalidate_pending_confirmations_locked(
        self,
        user_id: str,
        conversation_id: str,
        result_code: str,
        now: float,
    ) -> None:
        """Invalidate all pending confirmation intents for one conversation."""
        rows = self._db.connection.execute(
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
            self.invalidate_confirmation_row_locked(row, result_code, now)

    def expire_due_confirmations_locked(self, now: float) -> None:
        """Transition all past-due pending confirmations to expired."""
        rows = self._db.connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE state = 'pending' AND expires_at <= ?
            ORDER BY expires_at, confirmation_request_id
            ''',
            (now,),
        ).fetchall()
        for row in rows:
            record = confirmation_record_from_row(row)
            expired = record.expire(resolved_at=now)
            cursor = self.write_confirmation_record_locked(
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
    def _validate_invalidation_claim_args(
        response_id: Optional[str],
        response_turn_id: Optional[str],
        text_turn_request_fingerprint: Optional[str],
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
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
        if response_id is None:
            return None, None, None
        return (
            required_text(response_id, 'response_id', 128),
            validate_turn_id(response_turn_id),
            required_digest(
                text_turn_request_fingerprint,
                'text_turn_request_fingerprint',
            ),
        )

    def _replay_invalidation_claim_locked(
        self,
        normalized_user: str,
        normalized_id: str,
        session: ConversationSession,
        normalized_response_id: str,
        normalized_response_turn: str,
        normalized_text_fingerprint: str,
        normalized_code: str,
    ) -> Optional['ConfirmationRecord']:
        existing_claim = self._text_claims.select_claim_locked(
            normalized_user,
            normalized_response_id,
        )
        if existing_claim is not None:
            require_text_turn_claim_match(
                existing_claim,
                conversation_id=normalized_id,
                session=session,
                turn_id=normalized_response_turn,
                request_fingerprint=normalized_text_fingerprint,
                outcome='confirmation_invalidated',
                confirmation_request_id=(
                    existing_claim.confirmation_request_id
                ),
            )
            replay = self._text_claims.linked_confirmation_locked(
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
            return replay
        self._text_claims.require_namespace_available_locked(
            normalized_user,
            normalized_id,
            session,
            normalized_response_id,
            normalized_response_turn,
        )
        return None

    def _select_pending_or_fail_locked(
        self,
        normalized_user: str,
        normalized_id: str,
        session: ConversationSession,
        *,
        ordered_terminal_check: bool,
    ) -> sqlite3.Row:
        conn = self._db.connection
        row = conn.execute(
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
        if row is not None:
            return row
        if ordered_terminal_check:
            terminal = conn.execute(
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
        else:
            terminal = conn.execute(
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

    @staticmethod
    def _validate_resolve_args(
        *,
        user_id: str,
        conversation_id: str,
        response_id: str,
        response_fingerprint: str,
        disposition: str,
        now: float,
        current_target_binding_digest: str,
        response_turn_id: Optional[str],
        text_turn_request_fingerprint: Optional[str],
        create_robot_action: bool,
        action_dispatch_window_seconds: float,
        precommit_validator: Optional[
            Callable[[sqlite3.Connection], None]
        ],
    ) -> Tuple[
        str, str, str, str, str, Optional[str], float, str,
    ]:
        if (
            precommit_validator is not None
            and not callable(precommit_validator)
        ):
            raise TypeError('precommit_validator must be callable')
        if type(create_robot_action) is not bool:
            raise TypeError('create_robot_action must be a boolean')
        if (
            isinstance(action_dispatch_window_seconds, bool)
            or not isinstance(
                action_dispatch_window_seconds,
                (int, float),
            )
            or not math.isfinite(float(action_dispatch_window_seconds))
            or not 1.0 <= float(action_dispatch_window_seconds) <= 120.0
        ):
            raise ValueError(
                'action_dispatch_window_seconds must be from 1 to 120'
            )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_response_id = required_text(
            response_id, 'response_id', 128,
        )
        normalized_response_fingerprint = required_text(
            response_fingerprint, 'response_fingerprint', 128,
        )
        if response_turn_id is None:
            legacy_digest = hashlib.sha256(
                normalized_response_id.encode('utf-8')
            ).hexdigest()[:32]
            normalized_response_turn = (
                f'legacy-confirmation-response-{legacy_digest}'
            )
        else:
            normalized_response_turn = validate_turn_id(response_turn_id)
        normalized_text_fingerprint = None
        if text_turn_request_fingerprint is not None:
            if response_turn_id is None:
                raise ValidationError(
                    'text response claim requires response_turn_id'
                )
            normalized_text_fingerprint = required_digest(
                text_turn_request_fingerprint,
                'text_turn_request_fingerprint',
            )
        if disposition not in {'approve', 'deny', 'cancel'}:
            raise ValidationError(
                'confirmation disposition is unsupported'
            )
        resolved_at = finite_timestamp(now, 'now')
        target_digest = required_text(
            current_target_binding_digest,
            'current_target_binding_digest',
            128,
        )
        return (
            normalized_user,
            normalized_id,
            normalized_response_id,
            normalized_response_fingerprint,
            normalized_response_turn,
            normalized_text_fingerprint,
            resolved_at,
            target_digest,
        )

    def _replay_resolution_claim_locked(
        self,
        *,
        normalized_user: str,
        normalized_id: str,
        session: ConversationSession,
        normalized_response_id: str,
        normalized_response_turn: str,
        normalized_response_fingerprint: str,
        normalized_text_fingerprint: Optional[str],
        disposition: str,
        target_digest: str,
    ) -> Optional['ConfirmationRecord']:
        existing_claim = self._text_claims.select_claim_locked(
            normalized_user,
            normalized_response_id,
        )
        if existing_claim is None:
            return None
        if normalized_text_fingerprint is None:
            raise ConfirmationIntentConflictError(
                'response_id is owned by a text turn claim'
            )
        require_text_turn_claim_match(
            existing_claim,
            conversation_id=normalized_id,
            session=session,
            turn_id=normalized_response_turn,
            request_fingerprint=normalized_text_fingerprint,
            outcome='confirmation_resolved',
            confirmation_request_id=existing_claim.confirmation_request_id,
        )
        replay = self._text_claims.linked_confirmation_locked(
            existing_claim
        )
        if (
            replay is None
            or replay.response_id != normalized_response_id
            or replay.response_turn_id != normalized_response_turn
            or replay.response_fingerprint != normalized_response_fingerprint
            or replay.requested_disposition != disposition
        ):
            raise ConfirmationIntentConflictError(
                'confirmation response claim does not match '
                'its terminal record'
            )
        require_confirmation_context(
            self.confirmation_row_for_record_locked(replay),
            session,
            target_digest,
        )
        return replay

    def _replay_response_owner_locked(
        self,
        *,
        normalized_user: str,
        normalized_id: str,
        session: ConversationSession,
        normalized_response_id: str,
        normalized_response_turn: str,
        normalized_response_fingerprint: str,
        normalized_text_fingerprint: Optional[str],
        disposition: str,
        target_digest: str,
        resolved_at: float,
    ) -> Optional['ConfirmationRecord']:
        response_owner = self._db.connection.execute(
            '''
            SELECT *
            FROM confirmation_intents
            WHERE user_id = ? AND response_id = ?
            ''',
            (normalized_user, normalized_response_id),
        ).fetchone()
        if response_owner is None:
            return None
        require_confirmation_context(
            response_owner,
            session,
            target_digest,
        )
        replay = confirmation_record_from_row(response_owner)
        if (
            replay.response_fingerprint != normalized_response_fingerprint
            or replay.response_turn_id != normalized_response_turn
            or replay.requested_disposition != disposition
        ):
            raise ConfirmationIntentConflictError(
                'confirmation response_id was reused with '
                'different payload'
            )
        if normalized_text_fingerprint is not None:
            self._text_claims.insert_claim_locked(
                TextTurnRequestClaim(
                    user_id=normalized_user,
                    request_id=normalized_response_id,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    revision=session.revision,
                    turn_id=normalized_response_turn,
                    request_fingerprint=normalized_text_fingerprint,
                    outcome='confirmation_resolved',
                    confirmation_request_id=replay.confirmation_request_id,
                    created_at=resolved_at,
                )
            )
        return replay

    def _resolve_pending_row_locked(
        self,
        *,
        normalized_user: str,
        normalized_id: str,
        session: ConversationSession,
        normalized_response_id: str,
        normalized_response_turn: str,
        normalized_response_fingerprint: str,
        normalized_text_fingerprint: Optional[str],
        disposition: str,
        target_digest: str,
        resolved_at: float,
        create_robot_action: bool,
        action_dispatch_window_seconds: float,
    ) -> 'ConfirmationRecord':
        row = self._select_pending_or_fail_locked(
            normalized_user,
            normalized_id,
            session,
            ordered_terminal_check=True,
        )
        require_confirmation_context(row, session, target_digest)
        record = confirmation_record_from_row(row)
        if resolved_at < record.issued_at:
            raise ConfirmationIntentConflictError(
                'confirmation response predates its proposal'
            )
        resolution = verified_confirmation_resolution(
            record,
            user_id=normalized_user,
            conversation_id=normalized_id,
            session_instance_id=session.session_instance_id,
            generation=session.generation,
            response_id=normalized_response_id,
            response_turn_id=normalized_response_turn,
            response_fingerprint=normalized_response_fingerprint,
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
        cursor = self.write_confirmation_record_locked(
            row,
            terminal_record,
            resolved_at,
            expected_state='pending',
        )
        if cursor.rowcount != 1:
            raise ConfirmationIntentConflictError(
                'confirmation compare-and-swap failed'
            )
        if (
            create_robot_action
            and terminal_record.disposition == 'approved'
        ):
            from malbut_agent_server.adapters.outbound import (
                insert_action_for_approved_confirmation,
            )

            insert_action_for_approved_confirmation(
                self._db.connection,
                terminal_record,
                now=resolved_at,
                dispatch_window=float(action_dispatch_window_seconds),
            )
        if normalized_text_fingerprint is not None:
            self._text_claims.insert_claim_locked(
                TextTurnRequestClaim(
                    user_id=normalized_user,
                    request_id=normalized_response_id,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    revision=session.revision,
                    turn_id=normalized_response_turn,
                    request_fingerprint=normalized_text_fingerprint,
                    outcome='confirmation_resolved',
                    confirmation_request_id=(
                        terminal_record.confirmation_request_id
                    ),
                    created_at=resolved_at,
                )
            )
        return terminal_record
