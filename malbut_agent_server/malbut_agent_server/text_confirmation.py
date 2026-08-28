"""Durable, non-authorizing confirmation contracts for text turns."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from malbut_agent_server.schemas import (
    MAX_ID_LENGTH,
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)
from malbut_agent_server.tools import validate_tool_arguments


CONFIRMATION_SCHEMA_VERSION = 1
MAX_CONFIRMATION_MESSAGE_LENGTH = 1000
MAX_CONFIRMATION_ARGUMENT_BYTES = 16384
MAX_CONFIRMATION_TTL_SECONDS = 120.0
_SHA256_LENGTH = 64

PENDING = 'pending'
APPROVED = 'approved'
DENIED = 'denied'
CANCELED = 'canceled'
EXPIRED = 'expired'
INVALIDATED = 'invalidated'

APPROVE = 'approve'
DENY = 'deny'
CANCEL = 'cancel'
REQUESTED_DISPOSITIONS = frozenset({APPROVE, DENY, CANCEL})
TERMINAL_DISPOSITIONS = frozenset({
    APPROVED,
    DENIED,
    CANCELED,
    EXPIRED,
    INVALIDATED,
})

_APPROVE_RESPONSES = frozenset({
    '응', '네', '예', '좋아', '승인', '승인해', '해줘', '시작해줘',
    'yes', 'approve',
})
_DENY_RESPONSES = frozenset({
    '아니', '아니요', '싫어', '거절', '거절해', 'no', 'deny',
})
_CANCEL_RESPONSES = frozenset({
    '취소', '취소해', '그만', '그만해', '하지마', 'cancel',
})
_TERMINAL_PUNCTUATION = frozenset('.,!?~\u3002\uff01\uff1f')


class ConfirmationDomainConflictError(ValidationError):
    """Raised when a response does not match the pending proposal."""


class ConfirmationAlreadyTerminalError(ValidationError):
    """Raised when a different response targets a terminal record."""


def _strict_object(
    value: Any,
    fields: frozenset[str],
    field_name: str,
) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        raise ValidationError(f'{field_name} has an invalid shape')
    return value


def _identifier(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise ValidationError(f'{field_name} must be a string')
    result = value.strip()
    if not result or len(result) > MAX_ID_LENGTH:
        raise ValidationError(f'{field_name} is invalid')
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in result
    ):
        raise ValidationError(
            f'{field_name} must not contain control characters'
        )
    return result


def _message(value: Any) -> str:
    if type(value) is not str:
        raise ValidationError('confirmation message must be a string')
    result = value.strip()
    if not result or len(result) > MAX_CONFIRMATION_MESSAGE_LENGTH:
        raise ValidationError('confirmation message is invalid')
    if any(
        ord(character) < 32 and character not in '\n\t'
        for character in result
    ):
        raise ValidationError(
            'confirmation message contains control characters'
        )
    return result


def _positive_integer(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 1 or value > (1 << 63) - 1:
        raise ValidationError(f'{field_name} is invalid')
    return value


def _timestamp(value: Any, field_name: str) -> float:
    if type(value) not in {int, float}:
        raise ValidationError(f'{field_name} must be a number')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValidationError(f'{field_name} is invalid')
    return result


def _digest(value: Any, field_name: str) -> str:
    result = _identifier(value, field_name)
    if len(result) != _SHA256_LENGTH or any(
        character not in '0123456789abcdef'
        for character in result
    ):
        raise ValidationError(f'{field_name} is invalid')
    return result


def _canonical_json(value: Any, field_name: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (OverflowError, TypeError, ValueError):
        raise ValidationError(f'{field_name} is not JSON-safe') from None
    if len(encoded) > MAX_CONFIRMATION_ARGUMENT_BYTES:
        raise ValidationError(f'{field_name} is too large')
    return encoded.decode('utf-8')


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        _canonical_json(value, 'confirmation binding').encode('utf-8')
    ).hexdigest()


def _canonical_arguments(
    tool_name: str,
    value: Any,
) -> tuple[Mapping[str, Any], str, str]:
    try:
        validated = validate_tool_arguments(tool_name, value)
    except (TypeError, ValidationError):
        raise ValidationError(
            'confirmation arguments are invalid'
        ) from None
    encoded = _canonical_json(validated, 'confirmation arguments')
    frozen = MappingProxyType(
        json.loads(encoded)
    )
    return (
        frozen,
        encoded,
        hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
    )


def normalize_confirmation_text(text: Any) -> str:
    """Normalize one exact response without semantic or substring matching."""
    if type(text) is not str:
        raise TypeError('confirmation response text must be a string')
    normalized = unicodedata.normalize('NFKC', text).casefold().strip()
    if any(
        ord(character) < 32 and not character.isspace()
        for character in normalized
    ):
        return ''
    compact = ''.join(
        character for character in normalized
        if not character.isspace()
    )
    while compact and compact[-1] in _TERMINAL_PUNCTUATION:
        compact = compact[:-1]
    return compact


def classify_confirmation_text(text: Any) -> Optional[str]:
    """Return approve, deny, cancel, or None for an ambiguous response."""
    normalized = normalize_confirmation_text(text)
    if normalized in _APPROVE_RESPONSES:
        return APPROVE
    if normalized in _DENY_RESPONSES:
        return DENY
    if normalized in _CANCEL_RESPONSES:
        return CANCEL
    return None


@dataclass(frozen=True)
class ConfirmationDraft:
    """One exact proposal binding that grants no execution authority."""

    confirmation_request_id: str
    user_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    revision: int
    ordinal: int
    turn_id: str
    request_id: str
    decision_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    message: str
    target_room_name: str
    target_room_category: str
    target_binding_digest: str = field(repr=False)
    state_evidence_id: str = field(repr=False)
    state_observed_at: float
    safety_policy_revision: str
    issued_at: float
    expires_at: float
    execution_authorized: bool = False
    consume_once: bool = False
    schema_version: int = CONFIRMATION_SCHEMA_VERSION
    _arguments_json: str = field(init=False, repr=False)
    _arguments_digest: str = field(init=False, repr=False)
    _proposal_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate and freeze every execution-relevant proposal field."""
        if self.schema_version != CONFIRMATION_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation schema_version is unsupported'
            )
        for name in (
            'confirmation_request_id',
            'session_instance_id',
            'request_id',
            'decision_id',
            'tool_name',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(self, 'user_id', validate_user_id(self.user_id))
        object.__setattr__(
            self,
            'conversation_id',
            validate_conversation_id(self.conversation_id),
        )
        object.__setattr__(self, 'turn_id', validate_turn_id(self.turn_id))
        for name in ('generation', 'revision', 'ordinal'):
            object.__setattr__(
                self,
                name,
                _positive_integer(getattr(self, name), name),
            )
        arguments, encoded, arguments_digest = _canonical_arguments(
            self.tool_name,
            self.arguments,
        )
        object.__setattr__(self, 'arguments', arguments)
        object.__setattr__(self, '_arguments_json', encoded)
        object.__setattr__(self, '_arguments_digest', arguments_digest)
        object.__setattr__(self, 'message', _message(self.message))
        for name in ('target_room_name', 'target_room_category'):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'target_binding_digest',
            _digest(self.target_binding_digest, 'target_binding_digest'),
        )
        object.__setattr__(
            self,
            'state_evidence_id',
            _identifier(self.state_evidence_id, 'state_evidence_id'),
        )
        object.__setattr__(
            self,
            'safety_policy_revision',
            _identifier(
                self.safety_policy_revision,
                'safety_policy_revision',
            ),
        )
        issued_at = _timestamp(self.issued_at, 'issued_at')
        state_observed_at = _timestamp(
            self.state_observed_at,
            'state_observed_at',
        )
        expires_at = _timestamp(self.expires_at, 'expires_at')
        if expires_at <= issued_at:
            raise ValidationError('confirmation expiry must follow issue time')
        if expires_at - issued_at > MAX_CONFIRMATION_TTL_SECONDS:
            raise ValidationError('confirmation expiry is too far away')
        if state_observed_at > issued_at:
            raise ValidationError(
                'state evidence cannot be observed after confirmation issue'
            )
        object.__setattr__(self, 'issued_at', issued_at)
        object.__setattr__(self, 'state_observed_at', state_observed_at)
        object.__setattr__(self, 'expires_at', expires_at)
        if self.execution_authorized is not False:
            raise ValidationError('confirmation cannot authorize execution')
        if self.consume_once is not False:
            raise ValidationError('confirmation cannot be consumed as action')
        object.__setattr__(
            self,
            '_proposal_fingerprint',
            _sha256(self._proposal_binding()),
        )

    @classmethod
    def create(cls, **values: Any) -> 'ConfirmationDraft':
        """Create a draft, generating only its opaque confirmation ID."""
        values = dict(values)
        values.setdefault('confirmation_request_id', str(uuid.uuid4()))
        return cls(**values)

    @classmethod
    def from_orchestration(
        cls,
        result: Any,
        token: Any,
        target: Any,
        *,
        confirmation_request_id: Optional[str] = None,
        confirmation_expires_at: Optional[float] = None,
    ) -> 'ConfirmationDraft':
        """Bind a safe tool proposal, conversation CAS, and named target."""
        decision = result.decision
        if (
            decision.type != 'tool_call'
            or not result.safety.allowed
            or result.state_trusted is not True
        ):
            raise ValidationError(
                'only a trusted allowed action may request confirmation'
            )
        if (
            result.request_id != token.request_id
            or result.conversation_id != token.conversation_id
            or result.turn_id != token.turn_id
            or result.conversation_generation != token.generation
            or result.conversation_revision != token.revision + 1
            or result.conversation_ordinal != token.ordinal
        ):
            raise ConfirmationDomainConflictError(
                'orchestration result does not match conversation token'
            )
        if (
            not result.state_evidence_id
            or result.state_observed_at is None
            or not result.safety_policy_revision
        ):
            raise ConfirmationDomainConflictError(
                'trusted state provenance is missing'
            )
        prompt = (
            f'{target.room_name}으로 이동할까요? '
            '네, 아니요, 또는 취소라고 답해 주세요.'
        )
        return cls.create(
            confirmation_request_id=(
                confirmation_request_id or str(uuid.uuid4())
            ),
            user_id=token.user_id,
            conversation_id=token.conversation_id,
            session_instance_id=token.session_instance_id,
            generation=token.generation,
            revision=token.revision + 1,
            ordinal=token.ordinal,
            turn_id=token.turn_id,
            request_id=token.request_id,
            decision_id=result.decision_id,
            tool_name=decision.tool_name,
            arguments=decision.arguments,
            message=prompt,
            target_room_name=target.room_name,
            target_room_category=target.room_category,
            target_binding_digest=target.binding_digest,
            state_evidence_id=result.state_evidence_id,
            state_observed_at=result.state_observed_at,
            safety_policy_revision=result.safety_policy_revision,
            issued_at=result.issued_at,
            expires_at=(
                result.expires_at
                if confirmation_expires_at is None
                else confirmation_expires_at
            ),
        )

    @property
    def arguments_digest(self) -> str:
        """Return the hash of the canonical validated arguments."""
        return self._arguments_digest

    @property
    def proposal_fingerprint(self) -> str:
        """Return the hash binding the proposal and confirmation context."""
        return self._proposal_fingerprint

    def arguments_dict(self) -> dict[str, Any]:
        """Return a detached arguments object."""
        return json.loads(self._arguments_json)

    def is_expired(self, now: float) -> bool:
        """Use an explicit wall-clock sample for deterministic expiry."""
        return _timestamp(now, 'now') >= self.expires_at

    def _proposal_binding(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'confirmation_request_id': self.confirmation_request_id,
            'user_id': self.user_id,
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'generation': self.generation,
            'revision': self.revision,
            'ordinal': self.ordinal,
            'turn_id': self.turn_id,
            'request_id': self.request_id,
            'decision_id': self.decision_id,
            'tool_name': self.tool_name,
            'arguments_digest': self.arguments_digest,
            'message': self.message,
            'target_room_name': self.target_room_name,
            'target_room_category': self.target_room_category,
            'target_binding_digest': self.target_binding_digest,
            'state_evidence_id': self.state_evidence_id,
            'state_observed_at': self.state_observed_at,
            'safety_policy_revision': self.safety_policy_revision,
            'issued_at': self.issued_at,
            'expires_at': self.expires_at,
        }

    def to_private_dict(self) -> dict[str, Any]:
        """Return the durable server-only proposal record."""
        return {
            **self._proposal_binding(),
            'arguments': self.arguments_dict(),
            'message': self.message,
            'target_room_name': self.target_room_name,
            'target_room_category': self.target_room_category,
            'proposal_fingerprint': self.proposal_fingerprint,
            'execution_authorized': False,
            'consume_once': False,
            'tool_call_id': None,
            'physical_authorized': False,
        }

    @classmethod
    def from_private_dict(cls, value: Any) -> 'ConfirmationDraft':
        """Rebuild and re-hash one strict durable proposal."""
        fields = frozenset({
            'schema_version', 'confirmation_request_id', 'user_id',
            'conversation_id', 'session_instance_id', 'generation',
            'revision', 'ordinal', 'turn_id', 'request_id', 'decision_id',
            'tool_name', 'arguments', 'arguments_digest', 'message',
            'target_room_name', 'target_room_category',
            'target_binding_digest', 'issued_at', 'expires_at',
            'state_evidence_id', 'state_observed_at',
            'safety_policy_revision',
            'proposal_fingerprint', 'execution_authorized', 'consume_once',
            'tool_call_id', 'physical_authorized',
        })
        raw = _strict_object(value, fields, 'confirmation draft')
        if (
            raw['execution_authorized'] is not False
            or raw['consume_once'] is not False
            or raw['tool_call_id'] is not None
            or raw['physical_authorized'] is not False
        ):
            raise ValidationError('confirmation draft carries authority')
        draft = cls(
            schema_version=raw['schema_version'],
            confirmation_request_id=raw['confirmation_request_id'],
            user_id=raw['user_id'],
            conversation_id=raw['conversation_id'],
            session_instance_id=raw['session_instance_id'],
            generation=raw['generation'],
            revision=raw['revision'],
            ordinal=raw['ordinal'],
            turn_id=raw['turn_id'],
            request_id=raw['request_id'],
            decision_id=raw['decision_id'],
            tool_name=raw['tool_name'],
            arguments=raw['arguments'],
            message=raw['message'],
            target_room_name=raw['target_room_name'],
            target_room_category=raw['target_room_category'],
            target_binding_digest=raw['target_binding_digest'],
            state_evidence_id=raw['state_evidence_id'],
            state_observed_at=raw['state_observed_at'],
            safety_policy_revision=raw['safety_policy_revision'],
            issued_at=raw['issued_at'],
            expires_at=raw['expires_at'],
        )
        if (
            raw['arguments_digest'] != draft.arguments_digest
            or raw['proposal_fingerprint'] != draft.proposal_fingerprint
        ):
            raise ValidationError('confirmation draft digest mismatch')
        return draft

    def to_public_dict(self) -> dict[str, Any]:
        """Expose only the semantic target and a non-authorizing status."""
        return {
            'confirmation_request_id': self.confirmation_request_id,
            'status': 'awaiting_confirmation',
            'message': self.message,
            'proposal': {
                'tool_name': self.tool_name,
                'arguments': self.arguments_dict(),
                'target': {
                    'room_name': self.target_room_name,
                    'room_category': self.target_room_category,
                    'execution_authorized': False,
                },
            },
            'issued_at': self.issued_at,
            'expires_at': self.expires_at,
            'execution': _no_authority(),
        }


@dataclass(frozen=True)
class ConfirmationResolution:
    """One locally classified response bound to a pending record."""

    confirmation_request_id: str
    proposal_fingerprint: str = field(repr=False)
    caller_user_id: str
    caller_conversation_id: str
    caller_session_instance_id: str
    caller_generation: int
    response_id: str
    response_turn_id: str
    response_fingerprint: str = field(repr=False)
    requested_disposition: str
    schema_version: int = CONFIRMATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate a response binding without granting action authority."""
        if self.schema_version != CONFIRMATION_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation resolution schema is unsupported'
            )
        for name in (
            'confirmation_request_id',
            'caller_session_instance_id',
            'response_id',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'response_turn_id',
            validate_turn_id(self.response_turn_id),
        )
        object.__setattr__(
            self,
            'proposal_fingerprint',
            _digest(self.proposal_fingerprint, 'proposal_fingerprint'),
        )
        object.__setattr__(
            self,
            'response_fingerprint',
            _digest(self.response_fingerprint, 'response_fingerprint'),
        )
        object.__setattr__(
            self,
            'caller_user_id',
            validate_user_id(self.caller_user_id),
        )
        object.__setattr__(
            self,
            'caller_conversation_id',
            validate_conversation_id(self.caller_conversation_id),
        )
        object.__setattr__(
            self,
            'caller_generation',
            _positive_integer(
                self.caller_generation,
                'caller_generation',
            ),
        )
        if self.requested_disposition not in REQUESTED_DISPOSITIONS:
            raise ValidationError(
                'confirmation disposition is unsupported'
            )

    @classmethod
    def create(
        cls,
        record: 'ConfirmationRecord',
        *,
        caller_user_id: str,
        caller_conversation_id: str,
        caller_session_instance_id: str,
        caller_generation: int,
        response_id: str,
        response_turn_id: str,
        response_text: str,
    ) -> 'ConfirmationResolution':
        """Classify exact text and bind no raw transcript into storage."""
        normalized = normalize_confirmation_text(response_text)
        disposition = classify_confirmation_text(response_text)
        if disposition is None:
            raise ValidationError('confirmation response is ambiguous')
        fingerprint = _sha256({
            'confirmation_request_id': record.confirmation_request_id,
            'proposal_fingerprint': record.proposal_fingerprint,
            'response_id': response_id,
            'response_turn_id': response_turn_id,
            'normalized_response': normalized,
            'requested_disposition': disposition,
        })
        return cls.from_verified_response(
            record,
            caller_user_id=caller_user_id,
            caller_conversation_id=caller_conversation_id,
            caller_session_instance_id=caller_session_instance_id,
            caller_generation=caller_generation,
            response_id=response_id,
            response_turn_id=response_turn_id,
            response_fingerprint=fingerprint,
            requested_disposition=disposition,
        )

    @classmethod
    def from_verified_response(
        cls,
        record: 'ConfirmationRecord',
        **values: Any,
    ) -> 'ConfirmationResolution':
        """Build from server-verified context and a content-free digest."""
        resolution = cls(
            confirmation_request_id=record.confirmation_request_id,
            proposal_fingerprint=record.proposal_fingerprint,
            **values,
        )
        if not record.matches_resolution(resolution):
            raise ConfirmationDomainConflictError(
                'confirmation response does not match pending proposal'
            )
        return resolution

    def to_private_dict(self) -> dict[str, Any]:
        """Return the strict server-only response binding."""
        return {
            'schema_version': self.schema_version,
            'confirmation_request_id': self.confirmation_request_id,
            'proposal_fingerprint': self.proposal_fingerprint,
            'caller_user_id': self.caller_user_id,
            'caller_conversation_id': self.caller_conversation_id,
            'caller_session_instance_id': self.caller_session_instance_id,
            'caller_generation': self.caller_generation,
            'response_id': self.response_id,
            'response_turn_id': self.response_turn_id,
            'response_fingerprint': self.response_fingerprint,
            'requested_disposition': self.requested_disposition,
            'execution_authorized': False,
        }

    @classmethod
    def from_private_dict(cls, value: Any) -> 'ConfirmationResolution':
        """Rebuild one strict non-authorizing response binding."""
        fields = frozenset({
            'schema_version', 'confirmation_request_id',
            'proposal_fingerprint', 'caller_user_id',
            'caller_conversation_id', 'caller_session_instance_id',
            'caller_generation', 'response_id', 'response_turn_id',
            'response_fingerprint', 'requested_disposition',
            'execution_authorized',
        })
        raw = _strict_object(value, fields, 'confirmation resolution')
        if raw['execution_authorized'] is not False:
            raise ValidationError('confirmation resolution carries authority')
        return cls(**{
            key: raw[key]
            for key in fields
            if key != 'execution_authorized'
        })

    def to_public_dict(self) -> dict[str, Any]:
        """Expose the response disposition without private digests."""
        return {
            'response_id': self.response_id,
            'response_turn_id': self.response_turn_id,
            'requested_disposition': self.requested_disposition,
            'execution_authorized': False,
        }


@dataclass(frozen=True)
class ConfirmationRecord:
    """Pending or terminal confirmation state with zero action authority."""

    draft: ConfirmationDraft
    disposition: str = PENDING
    result_code: str = 'confirmation_pending'
    requested_disposition: Optional[str] = None
    response_id: Optional[str] = None
    response_turn_id: Optional[str] = None
    response_fingerprint: Optional[str] = field(default=None, repr=False)
    resolved_at: Optional[float] = None
    execution_authorized: bool = False
    consume_once: bool = False
    schema_version: int = CONFIRMATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate pending and terminal state invariants."""
        if type(self.draft) is not ConfirmationDraft:
            raise TypeError('draft must be a ConfirmationDraft')
        if self.schema_version != CONFIRMATION_SCHEMA_VERSION:
            raise ValidationError('confirmation record schema is unsupported')
        if self.disposition not in {PENDING, *TERMINAL_DISPOSITIONS}:
            raise ValidationError('confirmation record disposition is invalid')
        object.__setattr__(
            self,
            'result_code',
            _identifier(self.result_code, 'result_code'),
        )
        if (
            self.execution_authorized is not False
            or self.consume_once is not False
        ):
            raise ValidationError('confirmation record carries authority')
        if self.disposition == PENDING:
            if any(value is not None for value in (
                self.requested_disposition,
                self.response_id,
                self.response_turn_id,
                self.response_fingerprint,
                self.resolved_at,
            )) or self.result_code != 'confirmation_pending':
                raise ValidationError('pending confirmation state is invalid')
            return
        resolved_at = _timestamp(self.resolved_at, 'resolved_at')
        object.__setattr__(self, 'resolved_at', resolved_at)
        if self.disposition in {EXPIRED, INVALIDATED}:
            if any(value is not None for value in (
                self.requested_disposition,
                self.response_id,
                self.response_turn_id,
                self.response_fingerprint,
            )):
                raise ValidationError('system terminal state is invalid')
            return
        if self.requested_disposition not in REQUESTED_DISPOSITIONS:
            raise ValidationError('terminal requested disposition is invalid')
        object.__setattr__(
            self,
            'response_id',
            _identifier(self.response_id, 'response_id'),
        )
        object.__setattr__(
            self,
            'response_turn_id',
            validate_turn_id(self.response_turn_id),
        )
        object.__setattr__(
            self,
            'response_fingerprint',
            _digest(self.response_fingerprint, 'response_fingerprint'),
        )

    @classmethod
    def pending(cls, draft: ConfirmationDraft) -> 'ConfirmationRecord':
        """Create the initial non-authorizing pending state."""
        return cls(draft=draft)

    def __getattr__(self, name: str) -> Any:
        """Delegate immutable proposal fields to the bound draft."""
        if name.startswith('_'):
            raise AttributeError(name)
        try:
            return getattr(self.draft, name)
        except AttributeError:
            raise AttributeError(name) from None

    @property
    def confirmation_request_id(self) -> str:
        """Return the opaque identifier of the bound confirmation."""
        return self.draft.confirmation_request_id

    @property
    def proposal_fingerprint(self) -> str:
        """Return the digest of all execution-relevant proposal fields."""
        return self.draft.proposal_fingerprint

    @property
    def arguments_digest(self) -> str:
        """Return the digest of the validated proposal arguments."""
        return self.draft.arguments_digest

    def arguments_dict(self) -> dict[str, Any]:
        """Return a detached copy of the validated arguments."""
        return self.draft.arguments_dict()

    @property
    def is_terminal(self) -> bool:
        """Return whether no further response may change this record."""
        return self.disposition != PENDING

    def is_expired(self, now: float) -> bool:
        """Compare the bound deadline with an explicit clock sample."""
        return self.draft.is_expired(now)

    def matches_resolution(self, resolution: ConfirmationResolution) -> bool:
        """Check user, session, generation, and proposal binding equality."""
        return (
            type(resolution) is ConfirmationResolution
            and resolution.confirmation_request_id
            == self.confirmation_request_id
            and resolution.proposal_fingerprint
            == self.proposal_fingerprint
            and resolution.caller_user_id == self.user_id
            and resolution.caller_conversation_id == self.conversation_id
            and resolution.caller_session_instance_id
            == self.session_instance_id
            and resolution.caller_generation == self.generation
        )

    def resolve(
        self,
        resolution: ConfirmationResolution,
        *,
        resolved_at: float,
    ) -> 'ConfirmationRecord':
        """Apply one exact response without creating execution authority."""
        if self.is_terminal:
            if (
                self.response_id == resolution.response_id
                and self.response_turn_id == resolution.response_turn_id
                and self.response_fingerprint
                == resolution.response_fingerprint
                and self.requested_disposition
                == resolution.requested_disposition
            ):
                return self
            raise ConfirmationAlreadyTerminalError(
                'confirmation record is already terminal'
            )
        if not self.matches_resolution(resolution):
            raise ConfirmationDomainConflictError(
                'confirmation response does not match proposal'
            )
        terminal = {
            APPROVE: (APPROVED, 'confirmation_approved'),
            DENY: (DENIED, 'confirmation_denied'),
            CANCEL: (CANCELED, 'confirmation_canceled'),
        }[resolution.requested_disposition]
        return ConfirmationRecord(
            draft=self.draft,
            disposition=terminal[0],
            result_code=terminal[1],
            requested_disposition=resolution.requested_disposition,
            response_id=resolution.response_id,
            response_turn_id=resolution.response_turn_id,
            response_fingerprint=resolution.response_fingerprint,
            resolved_at=resolved_at,
        )

    def invalidate(
        self,
        result_code: str,
        *,
        resolved_at: float,
    ) -> 'ConfirmationRecord':
        """Close a pending record after its trusted binding changes."""
        if self.is_terminal:
            return self
        return ConfirmationRecord(
            draft=self.draft,
            disposition=INVALIDATED,
            result_code=result_code,
            resolved_at=resolved_at,
        )

    def expire(self, *, resolved_at: float) -> 'ConfirmationRecord':
        """Close a pending record after its immutable deadline."""
        if self.is_terminal:
            return self
        return ConfirmationRecord(
            draft=self.draft,
            disposition=EXPIRED,
            result_code='confirmation_expired',
            resolved_at=resolved_at,
        )

    def to_private_dict(self) -> dict[str, Any]:
        """Return the strict durable record with explicit zero authority."""
        return {
            'schema_version': self.schema_version,
            'draft': self.draft.to_private_dict(),
            'disposition': self.disposition,
            'result_code': self.result_code,
            'requested_disposition': self.requested_disposition,
            'response_id': self.response_id,
            'response_turn_id': self.response_turn_id,
            'response_fingerprint': self.response_fingerprint,
            'resolved_at': self.resolved_at,
            'execution_authorized': False,
            'consume_once': False,
            'tool_call_id': None,
            'physical_authorized': False,
        }

    @classmethod
    def from_private_dict(cls, value: Any) -> 'ConfirmationRecord':
        """Rebuild and validate one strict durable confirmation record."""
        fields = frozenset({
            'schema_version', 'draft', 'disposition', 'result_code',
            'requested_disposition', 'response_id', 'response_fingerprint',
            'response_turn_id', 'resolved_at', 'execution_authorized',
            'consume_once',
            'tool_call_id', 'physical_authorized',
        })
        raw = _strict_object(value, fields, 'confirmation record')
        if (
            raw['execution_authorized'] is not False
            or raw['consume_once'] is not False
            or raw['tool_call_id'] is not None
            or raw['physical_authorized'] is not False
        ):
            raise ValidationError('confirmation record carries authority')
        return cls(
            schema_version=raw['schema_version'],
            draft=ConfirmationDraft.from_private_dict(raw['draft']),
            disposition=raw['disposition'],
            result_code=raw['result_code'],
            requested_disposition=raw['requested_disposition'],
            response_id=raw['response_id'],
            response_turn_id=raw['response_turn_id'],
            response_fingerprint=raw['response_fingerprint'],
            resolved_at=raw['resolved_at'],
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Expose confirmation state with no execution-capable fields."""
        value = self.draft.to_public_dict()
        value['status'] = (
            'awaiting_confirmation'
            if self.disposition == PENDING
            else self.disposition
        )
        value['result_code'] = self.result_code
        value['resolved_at'] = self.resolved_at
        value['execution'] = _no_authority()
        return value


def _no_authority() -> dict[str, Any]:
    return {
        'authorized': False,
        'execution_authorized': False,
        'consume_once': False,
        'tool_call_id': None,
        'physical_authorized': False,
        'nav2_start_count': 0,
        'nav2_cancel_count': 0,
    }
