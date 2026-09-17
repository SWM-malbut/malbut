"""Immutable simulation action identity and lifecycle rules."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional


MAX_ACTION_ID_LENGTH = 256
MAX_ACTION_ARGUMENT_BYTES = 16384
_DIGEST_LENGTH = 64


class ActionState(str, Enum):
    """Durable lifecycle states for one bounded robot action."""

    PENDING_PREFLIGHT = 'PENDING_PREFLIGHT'
    CLAIMED = 'CLAIMED'
    DISPATCH_INTENT = 'DISPATCH_INTENT'
    STARTED = 'STARTED'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    CANCELED = 'CANCELED'
    BLOCKED = 'BLOCKED'
    UNKNOWN = 'UNKNOWN'


ACTION_TERMINAL_STATES = frozenset({
    ActionState.SUCCEEDED,
    ActionState.FAILED,
    ActionState.CANCELED,
    ActionState.BLOCKED,
    ActionState.UNKNOWN,
})

_ALLOWED_TRANSITIONS = {
    ActionState.PENDING_PREFLIGHT: frozenset({
        ActionState.CLAIMED,
        ActionState.BLOCKED,
        ActionState.CANCELED,
    }),
    ActionState.CLAIMED: frozenset({
        ActionState.CLAIMED,
        ActionState.DISPATCH_INTENT,
        ActionState.BLOCKED,
        ActionState.CANCELED,
    }),
    ActionState.DISPATCH_INTENT: frozenset({
        ActionState.STARTED,
        ActionState.FAILED,
        ActionState.CANCELED,
        ActionState.UNKNOWN,
    }),
    ActionState.STARTED: frozenset({
        ActionState.SUCCEEDED,
        ActionState.FAILED,
        ActionState.CANCELED,
        ActionState.UNKNOWN,
    }),
}


def _identifier(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f'{field_name} must be a string')
    result = value.strip()
    if not result or len(result) > MAX_ACTION_ID_LENGTH:
        raise ValueError(f'{field_name} is invalid')
    if any(ord(character) < 32 or ord(character) == 127
           for character in result):
        raise ValueError(f'{field_name} contains control characters')
    return result


def _positive_integer(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 1 or value > (1 << 63) - 1:
        raise ValueError(f'{field_name} is invalid')
    return value


def _timestamp(value: Any, field_name: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f'{field_name} must be a number')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'{field_name} is invalid')
    return result


def _digest(value: Any, field_name: str) -> str:
    result = _identifier(value, field_name)
    if len(result) != _DIGEST_LENGTH or any(
        character not in '0123456789abcdef' for character in result
    ):
        raise ValueError(f'{field_name} is invalid')
    return result


def _canonical_arguments(value: Any) -> tuple[Mapping[str, Any], str, str]:
    if type(value) is not dict:
        raise ValueError('arguments must be an object')
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (OverflowError, TypeError, ValueError):
        raise ValueError('arguments are not JSON-safe') from None
    if len(encoded) > MAX_ACTION_ARGUMENT_BYTES:
        raise ValueError('arguments are too large')
    decoded = json.loads(encoded.decode('utf-8'))
    return (
        _deep_freeze(decoded),
        encoded.decode('utf-8'),
        hashlib.sha256(encoded).hexdigest(),
    )


def _deep_freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({
            key: _deep_freeze(item) for key, item in value.items()
        })
    if type(value) is list:
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ActionBinding:
    """Exact approved proposal and owner context bound to an action."""

    confirmation_request_id: str
    proposal_fingerprint: str = field(repr=False)
    arguments_digest: str = field(repr=False)
    target_binding_digest: str = field(repr=False)
    user_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    conversation_revision: int
    decision_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    target_room_name: str
    target_room_category: str
    confirmation_state_evidence_id: str
    confirmation_state_observed_at: float
    confirmation_safety_policy_revision: str
    _arguments_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate and freeze every execution-relevant binding."""
        for name in (
            'confirmation_request_id',
            'user_id',
            'conversation_id',
            'session_instance_id',
            'decision_id',
            'tool_name',
            'target_room_name',
            'target_room_category',
            'confirmation_state_evidence_id',
            'confirmation_safety_policy_revision',
        ):
            object.__setattr__(self, name, _identifier(
                getattr(self, name), name
            ))
        object.__setattr__(
            self,
            'generation',
            _positive_integer(self.generation, 'generation'),
        )
        object.__setattr__(
            self,
            'conversation_revision',
            _positive_integer(
                self.conversation_revision,
                'conversation_revision',
            ),
        )
        for name in (
            'proposal_fingerprint',
            'arguments_digest',
            'target_binding_digest',
        ):
            object.__setattr__(
                self,
                name,
                _digest(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'confirmation_state_observed_at',
            _timestamp(
                self.confirmation_state_observed_at,
                'confirmation_state_observed_at',
            ),
        )
        arguments, encoded, calculated_digest = _canonical_arguments(
            dict(self.arguments)
        )
        if calculated_digest != self.arguments_digest:
            raise ValueError('arguments do not match arguments_digest')
        object.__setattr__(self, 'arguments', arguments)
        object.__setattr__(self, '_arguments_json', encoded)

    def arguments_dict(self) -> dict[str, Any]:
        """Return a detached JSON object for an outbound adapter."""
        return json.loads(self._arguments_json)


@dataclass(frozen=True)
class DispatchAuthorization:
    """Fresh deterministic authority captured immediately before send."""

    state_evidence_id: str
    state_observed_at: float
    safety_policy_revision: str
    target_binding_digest: str = field(repr=False)
    authorized_at: float = 0.0
    simulation: bool = True
    physical_authorized: bool = False

    def __post_init__(self) -> None:
        """Reject stale-shaped or physical authority at the domain edge."""
        for name in ('state_evidence_id', 'safety_policy_revision'):
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
        observed_at = _timestamp(
            self.state_observed_at,
            'state_observed_at',
        )
        authorized_at = _timestamp(self.authorized_at, 'authorized_at')
        if observed_at > authorized_at:
            raise ValueError('state evidence postdates authorization')
        object.__setattr__(self, 'state_observed_at', observed_at)
        object.__setattr__(self, 'authorized_at', authorized_at)
        if self.simulation is not True:
            raise ValueError('dispatch authorization must be simulation-only')
        if self.physical_authorized is not False:
            raise ValueError('physical dispatch authority is forbidden')


@dataclass(frozen=True)
class RobotAction:
    """One server-owned, simulation-only robot action snapshot."""

    action_id: str
    operation_id: str
    binding: ActionBinding
    state: ActionState
    revision: int
    created_at: float
    updated_at: float
    dispatch_expires_at: float
    result_code: Optional[str] = None
    dispatch_authorization: Optional[DispatchAuthorization] = None
    simulation: bool = True
    physical_authorized: bool = False

    def __post_init__(self) -> None:
        """Enforce server identity, lifecycle, and authority invariants."""
        object.__setattr__(
            self,
            'action_id',
            _identifier(self.action_id, 'action_id'),
        )
        object.__setattr__(
            self,
            'operation_id',
            _identifier(self.operation_id, 'operation_id'),
        )
        if type(self.binding) is not ActionBinding:
            raise TypeError('binding must be an ActionBinding')
        if type(self.state) is not ActionState:
            try:
                object.__setattr__(self, 'state', ActionState(self.state))
            except (TypeError, ValueError):
                raise ValueError('action state is invalid') from None
        object.__setattr__(
            self,
            'revision',
            _positive_integer(self.revision, 'revision'),
        )
        created_at = _timestamp(self.created_at, 'created_at')
        updated_at = _timestamp(self.updated_at, 'updated_at')
        dispatch_expires_at = _timestamp(
            self.dispatch_expires_at,
            'dispatch_expires_at',
        )
        if updated_at < created_at:
            raise ValueError('updated_at predates action creation')
        if dispatch_expires_at <= created_at:
            raise ValueError('dispatch expiry must follow action creation')
        object.__setattr__(self, 'created_at', created_at)
        object.__setattr__(self, 'updated_at', updated_at)
        object.__setattr__(
            self,
            'dispatch_expires_at',
            dispatch_expires_at,
        )
        if self.result_code is not None:
            object.__setattr__(
                self,
                'result_code',
                _identifier(self.result_code, 'result_code'),
            )
        if self.simulation is not True:
            raise ValueError('SWM25-132 actions must be simulation-only')
        if self.physical_authorized is not False:
            raise ValueError('physical authority is forbidden')
        forbidden_server_ids = {
            self.binding.confirmation_request_id,
            self.binding.decision_id,
        }
        if (
            self.action_id == self.operation_id
            or self.action_id in forbidden_server_ids
            or self.operation_id in forbidden_server_ids
        ):
            raise ValueError('server action identities must be distinct')
        terminal = self.state in ACTION_TERMINAL_STATES
        if terminal != (self.result_code is not None):
            raise ValueError(
                'terminal state and result_code must be present together'
            )
        states_requiring_authorization = {
            ActionState.DISPATCH_INTENT,
            ActionState.STARTED,
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.UNKNOWN,
        }
        if self.state in states_requiring_authorization:
            if type(self.dispatch_authorization) is not DispatchAuthorization:
                raise ValueError(
                    'sent action state requires dispatch authorization'
                )
            if (
                self.dispatch_authorization.target_binding_digest
                != self.binding.target_binding_digest
            ):
                raise ValueError(
                    'dispatch authorization target binding changed'
                )
            if self.dispatch_authorization.authorized_at < created_at:
                raise ValueError(
                    'dispatch authorization predates action creation'
                )
            if self.dispatch_authorization.state_observed_at < created_at:
                raise ValueError(
                    'dispatch state evidence predates action creation'
                )
            if (
                self.dispatch_authorization.safety_policy_revision
                != self.binding.confirmation_safety_policy_revision
            ):
                raise ValueError('dispatch safety policy revision changed')
        elif (
            self.state is ActionState.CANCELED
            and self.dispatch_authorization is not None
        ):
            if (
                type(self.dispatch_authorization)
                is not DispatchAuthorization
                or self.dispatch_authorization.target_binding_digest
                != self.binding.target_binding_digest
                or self.dispatch_authorization.authorized_at < created_at
                or self.dispatch_authorization.state_observed_at < created_at
                or self.dispatch_authorization.safety_policy_revision
                != self.binding.confirmation_safety_policy_revision
            ):
                raise ValueError(
                    'canceled action authorization is invalid'
                )
        elif self.dispatch_authorization is not None:
            raise ValueError(
                'unsent action cannot carry dispatch authorization'
            )

    @property
    def is_terminal(self) -> bool:
        """Return whether the action may never be dispatched again."""
        return self.state in ACTION_TERMINAL_STATES

    def transition(
        self,
        next_state: ActionState,
        *,
        updated_at: float,
        result_code: Optional[str] = None,
        dispatch_authorization: Optional[DispatchAuthorization] = None,
    ) -> 'RobotAction':
        """Apply one deterministic state transition and CAS revision."""
        if type(next_state) is not ActionState:
            try:
                next_state = ActionState(next_state)
            except (TypeError, ValueError):
                raise ValueError('next action state is invalid') from None
        if next_state not in _ALLOWED_TRANSITIONS.get(
            self.state,
            frozenset(),
        ):
            raise ValueError(
                f'action transition {self.state.value} -> '
                f'{next_state.value} is forbidden'
            )
        return replace(
            self,
            state=next_state,
            revision=self.revision + 1,
            updated_at=updated_at,
            result_code=result_code,
            dispatch_authorization=(
                self.dispatch_authorization
                if dispatch_authorization is None
                else dispatch_authorization
            ),
        )
