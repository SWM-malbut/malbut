"""Pure contracts for a fast, non-authorizing Front Router."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


MAX_FRONT_REQUEST_ID_CHARS = 128
MAX_FRONT_MESSAGE_CHARS = 2000
MAX_FRONT_HISTORY_MESSAGES = 16
MAX_FRONT_HISTORY_MESSAGE_CHARS = 300
MAX_FRONT_HISTORY_CHARS = 4000
MAX_FRONT_ROUTE_JSON_CHARS = 1024


class FrontMessageRole(str, Enum):
    """Conversation roles that may be projected to the Front Router."""

    USER = 'user'
    ASSISTANT = 'assistant'


class FrontRoute(str, Enum):
    """The only five semantic routes visible outside the router."""

    GENERAL_CONVERSATION = 'general_conversation'
    CLARIFICATION_REQUIRED = 'clarification_required'
    ROBOT_STATUS_QUERY = 'robot_status_query'
    CURRENT_ACTION_QUERY = 'current_action_query'
    ROBOT_ACTION_REQUEST = 'robot_action_request'


@dataclass(frozen=True)
class FrontMessage:
    """One bounded, untrusted conversation message for route context."""

    role: FrontMessageRole
    content: str

    def __post_init__(self) -> None:
        """Reject mutable, unbounded, or privileged context values."""
        if not isinstance(self.role, FrontMessageRole):
            raise ValueError('role must be a FrontMessageRole')
        object.__setattr__(
            self,
            'content',
            _bounded_text(
                self.content,
                'content',
                maximum_chars=MAX_FRONT_HISTORY_MESSAGE_CHARS,
            ),
        )


@dataclass(frozen=True)
class FrontRouteRequest:
    """A bounded text-only projection passed to a Front Router Port."""

    request_id: str
    user_message: str
    recent_messages: tuple[FrontMessage, ...] = ()

    def __post_init__(self) -> None:
        """Validate correlation, current text, and immutable history."""
        object.__setattr__(
            self,
            'request_id',
            _bounded_identifier(
                self.request_id,
                'request_id',
                maximum_chars=MAX_FRONT_REQUEST_ID_CHARS,
            ),
        )
        object.__setattr__(
            self,
            'user_message',
            _bounded_text(
                self.user_message,
                'user_message',
                maximum_chars=MAX_FRONT_MESSAGE_CHARS,
            ),
        )
        if type(self.recent_messages) is not tuple:
            raise ValueError('recent_messages must be a tuple')
        if len(self.recent_messages) > MAX_FRONT_HISTORY_MESSAGES:
            raise ValueError('recent_messages contains too many messages')
        if any(
            type(message) is not FrontMessage
            for message in self.recent_messages
        ):
            raise ValueError('recent_messages contains an invalid message')
        history_chars = sum(
            len(message.content) for message in self.recent_messages
        )
        if history_chars > MAX_FRONT_HISTORY_CHARS:
            raise ValueError('recent_messages is too large')


@dataclass(frozen=True)
class FrontRouteMatch:
    """One high-confidence route with no response or authority fields."""

    route: FrontRoute

    def __post_init__(self) -> None:
        """Accept only one of the five closed semantic routes."""
        if not isinstance(self.route, FrontRoute):
            raise ValueError('route must be a FrontRoute')


def decode_front_route_match(payload: Any) -> FrontRouteMatch:
    """Decode one strict JSON-like route object with no extra fields."""
    if type(payload) is not dict:
        raise ValueError('Front Router result must be an object')
    if set(payload) != {'route'}:
        raise ValueError('Front Router result fields are invalid')
    route_value = payload['route']
    if type(route_value) is not str:
        raise ValueError('Front Router route must be a string')
    try:
        route = FrontRoute(route_value)
    except ValueError:
        raise ValueError('Front Router route is unknown') from None
    return FrontRouteMatch(route=route)


def parse_front_route_match(payload: str) -> FrontRouteMatch:
    """Parse a bounded raw JSON result without losing duplicate keys."""
    if type(payload) is not str:
        raise ValueError('Front Router JSON must be a string')
    if not payload or len(payload) > MAX_FRONT_ROUTE_JSON_CHARS:
        raise ValueError('Front Router JSON size is invalid')
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError('Front Router JSON is invalid') from error
    return decode_front_route_match(value)


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Front Router JSON contains duplicate keys')
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> Any:
    raise ValueError(
        f'Front Router JSON contains non-finite value: {value}'
    )


def _bounded_identifier(
    value: Any,
    field_name: str,
    *,
    maximum_chars: int,
) -> str:
    result = _bounded_text(
        value,
        field_name,
        maximum_chars=maximum_chars,
    )
    if any(ord(character) < 32 for character in result):
        raise ValueError(f'{field_name} contains an invalid character')
    return result


def _bounded_text(
    value: Any,
    field_name: str,
    *,
    maximum_chars: int,
) -> str:
    if type(value) is not str:
        raise ValueError(f'{field_name} must be a string')
    result = value.strip()
    if not result or len(result) > maximum_chars:
        raise ValueError(f'{field_name} is invalid')
    if any(
        ord(character) < 32 and character not in '\n\t\r'
        for character in result
    ) or any(
        ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in result
    ):
        raise ValueError(f'{field_name} contains an invalid character')
    return result
