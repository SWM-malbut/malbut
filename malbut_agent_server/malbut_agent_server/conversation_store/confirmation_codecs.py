"""Pure validation, serialization, and row-mapping codecs for confirmations."""

import hashlib
import json
import math
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Optional

from malbut_agent_server.conversation_store.codecs import (
    bounded_integer,
    finite_timestamp,
    required_digest,
    required_text,
)
from malbut_agent_server.conversation_store.models import (
    CONFIRMATION_STORAGE_SCHEMA_VERSION,
    MAX_CONFIRMATION_JSON_LENGTH,
    TEXT_TURN_CLAIM_OUTCOMES,
    TEXT_TURN_CLAIM_SCHEMA_VERSION,
    ConfirmationIntentConflictError,
    ConfirmationSchemaError,
    ConversationChangedError,
    ConversationSession,
    TextTurnRequestClaim,
)
from malbut_agent_server.schemas import (
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)

if TYPE_CHECKING:
    from malbut_agent_server.text_confirmation import (
        ConfirmationDraft,
        ConfirmationRecord,
        ConfirmationResolution,
    )


def pending_confirmation_record(
    draft: Optional['ConfirmationDraft'],
) -> Optional['ConfirmationRecord']:
    """Convert a validated ConfirmationDraft into a pending record."""
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


def verified_confirmation_resolution(
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
    """Construct a verified ConfirmationResolution for one owner response."""
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


def validate_confirmation_response(
    record: 'ConfirmationRecord',
    response: Dict[str, Any],
) -> None:
    """Verify a pending confirmation draft matches its source turn response."""
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


def confirmation_state(record: 'ConfirmationRecord') -> str:
    """Derive the indexed lifecycle state from one confirmation record."""
    if record.disposition == 'pending':
        return 'pending'
    if record.disposition == 'invalidated':
        return 'invalidated'
    return 'resolved'


def confirmation_storage_values(
    record: 'ConfirmationRecord',
    updated_at: float,
    *,
    created_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Serialize one ConfirmationRecord into indexed SQLite column values."""
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
        'state': confirmation_state(record),
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


def confirmation_record_from_row(
    row: sqlite3.Row,
) -> 'ConfirmationRecord':
    """Deserialize and verify every indexed column and authority invariant."""
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
        record = ConfirmationRecord.from_private_dict(private_value)
    except Exception as error:
        raise ConfirmationSchemaError(
            'stored confirmation record cannot be trusted'
        ) from error
    expected = confirmation_storage_values(
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


def require_confirmation_context(
    row: sqlite3.Row,
    session: ConversationSession,
    target_binding_digest: str,
) -> None:
    """Verify confirmation row still belongs to the active session & target."""
    if (
        row['user_id'] != session.user_id
        or row['conversation_id'] != session.conversation_id
        or row['session_instance_id'] != session.session_instance_id
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


def text_turn_claim_from_row(
    row: sqlite3.Row,
) -> TextTurnRequestClaim:
    """Deserialize and verify authority invariants of one text-turn claim."""
    try:
        if int(row['schema_version']) != TEXT_TURN_CLAIM_SCHEMA_VERSION:
            raise ValueError('unsupported claim schema')
        claim = TextTurnRequestClaim(
            user_id=validate_user_id(row['user_id']),
            request_id=required_text(
                row['request_id'],
                'request_id',
                128,
            ),
            conversation_id=validate_conversation_id(
                row['conversation_id']
            ),
            session_instance_id=required_text(
                row['session_instance_id'],
                'session_instance_id',
                128,
            ),
            generation=bounded_integer(
                row['generation'],
                'generation',
                1,
                2 ** 63 - 1,
            ),
            revision=bounded_integer(
                row['revision'],
                'revision',
                0,
                2 ** 63 - 1,
            ),
            turn_id=validate_turn_id(row['turn_id']),
            request_fingerprint=required_digest(
                row['request_fingerprint'],
                'request_fingerprint',
            ),
            outcome=required_text(
                row['outcome'],
                'outcome',
                64,
            ),
            confirmation_request_id=(
                None
                if row['confirmation_request_id'] is None
                else required_text(
                    row['confirmation_request_id'],
                    'confirmation_request_id',
                    128,
                )
            ),
            created_at=finite_timestamp(
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


def require_text_turn_claim_context(
    claim: TextTurnRequestClaim,
    session: ConversationSession,
) -> None:
    """Verify a persisted text-turn claim still matches the session context."""
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


def require_text_turn_claim_match(
    claim: TextTurnRequestClaim,
    *,
    conversation_id: str,
    session: ConversationSession,
    turn_id: str,
    request_fingerprint: str,
    outcome: str,
    confirmation_request_id: Optional[str],
) -> None:
    """Verify a replayed text-turn claim matches every original request field."""
    require_text_turn_claim_context(claim, session)
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
