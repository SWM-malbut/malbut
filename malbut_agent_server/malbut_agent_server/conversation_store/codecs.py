"""Pure validation and JSON serialization helpers for conversation storage."""

import json
import math
from typing import Any, Dict

from malbut_agent_server.conversation_store.models import (
    MAX_RESPONSE_JSON_LENGTH,
)
from malbut_agent_server.schemas import (
    MAX_UTTERANCE_LENGTH,
    ValidationError,
)


def load_response(value: Any) -> Dict[str, Any]:
    """Decode one persisted assistant response JSON object."""
    if not isinstance(value, str):
        raise RuntimeError('stored conversation response is missing')
    try:
        response = json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            'stored conversation response is invalid'
        ) from error
    if not isinstance(response, dict):
        raise RuntimeError(
            'stored conversation response must be an object'
        )
    return response


def response_json(value: Any) -> str:
    """Validate and canonically serialize one response payload."""
    if not isinstance(value, dict):
        raise ValidationError('response must be an object')
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    if len(rendered) > MAX_RESPONSE_JSON_LENGTH:
        raise ValidationError('response is too large')
    return rendered


def required_text(value: Any, name: str, maximum: int) -> str:
    """Validate a nonblank bounded string field."""
    if not isinstance(value, str):
        raise ValidationError(f'{name} must be a string')
    result = value.strip()
    if not result:
        raise ValidationError(f'{name} must not be empty')
    if len(result) > maximum:
        raise ValidationError(
            f'{name} must be at most {maximum} characters'
        )
    return result


def required_digest(value: Any, name: str) -> str:
    """Validate a 64-character lowercase SHA-256 hex digest."""
    result = required_text(value, name, 64)
    if len(result) != 64 or any(
        character not in '0123456789abcdef'
        for character in result
    ):
        raise ValidationError(
            f'{name} must be a lowercase SHA-256 digest'
        )
    return result


def assistant_text(value: Any) -> str:
    """Validate one bounded assistant reply string."""
    if not isinstance(value, str):
        raise ValidationError('assistant_content must be a string')
    if len(value) > MAX_UTTERANCE_LENGTH:
        raise ValidationError('assistant_content is too long')
    return value


def bounded_integer(
    value: Any,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    """Validate an integer strictly within inclusive bounds."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if value < minimum or value > maximum:
        raise ValueError(
            f'{name} must be between {minimum} and {maximum}'
        )
    return value


def finite_timestamp(value: Any, name: str) -> float:
    """Validate a finite numeric UNIX timestamp."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValidationError(f'{name} must be a timestamp')
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f'{name} must be finite')
    return result
