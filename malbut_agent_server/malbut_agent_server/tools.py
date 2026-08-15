"""High-level robot tools exposed to language models."""

import copy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from malbut_agent_server.schemas import ValidationError


@dataclass(frozen=True)
class ToolSpec:
    """One strict function schema."""

    name: str
    description: str
    parameters: Dict[str, Any]

    def to_openai_dict(self) -> Dict[str, Any]:
        """Return a Responses API strict function tool."""
        return {
            'type': 'function',
            'name': self.name,
            'description': self.description,
            'strict': True,
            'parameters': self.parameters,
        }


EMPTY_PARAMETERS: Dict[str, Any] = {
    'type': 'object',
    'properties': {},
    'required': [],
    'additionalProperties': False,
}


TOOL_SPECS = {
    'navigate': ToolSpec(
        name='navigate',
        description=(
            'Move through the verified Nav2 stack to one named indoor '
            'destination. Never use this for raw motor control.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'location': {
                    'type': 'string',
                    'description': (
                        'A named destination such as 거실, 주방, 침실, '
                        '현관, or 충전소.'
                    ),
                },
            },
            'required': ['location'],
            'additionalProperties': False,
        },
    ),
    'detect_pet': ToolSpec(
        name='detect_pet',
        description=(
            'Inspect the current camera view for a pet without moving.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'capture_photo': ToolSpec(
        name='capture_photo',
        description=(
            'Capture one still image from the camera after privacy checks.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'send_notification': ToolSpec(
        name='send_notification',
        description=(
            'Send a short notification to the registered caregiver.'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'message': {
                    'type': 'string',
                    'description': 'Short notification text.',
                },
                'image_id': {
                    'type': ['string', 'null'],
                    'description': (
                        'A previously captured image identifier, or null.'
                    ),
                },
            },
            'required': ['message', 'image_id'],
            'additionalProperties': False,
        },
    ),
    'get_robot_status': ToolSpec(
        name='get_robot_status',
        description=(
            'Read battery and subsystem status without changing robot state.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
}


def select_tool_specs(names: Iterable[str]) -> List[ToolSpec]:
    """Return registered specs in request order, ignoring unknown names."""
    return [
        TOOL_SPECS[name]
        for name in names
        if name in TOOL_SPECS
    ]


def validate_tool_arguments(
    tool_name: str,
    arguments: Any,
) -> Dict[str, Any]:
    """Validate one Tool payload against its strict registered schema."""
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        raise ValidationError('unknown tool')
    _validate_schema_value(
        spec.parameters,
        arguments,
        'arguments',
        depth=0,
    )
    return copy.deepcopy(arguments)


def _validate_schema_value(
    schema: Dict[str, Any],
    value: Any,
    field_name: str,
    *,
    depth: int,
) -> None:
    """Validate the bounded JSON Schema subset used by Tool specs."""
    if depth > 8:
        raise ValidationError(f'{field_name} is nested too deeply')
    expected = schema.get('type')
    expected_types = (
        list(expected)
        if isinstance(expected, list)
        else [expected]
    )
    if value is None and 'null' in expected_types:
        return
    non_null_types = [
        item for item in expected_types if item != 'null'
    ]
    if non_null_types == ['object']:
        if not isinstance(value, dict):
            raise ValidationError(f'{field_name} must be an object')
        properties = schema.get('properties', {})
        required = schema.get('required', [])
        missing = [name for name in required if name not in value]
        if missing:
            names = ', '.join(sorted(missing))
            raise ValidationError(
                f'{field_name} is missing required fields: {names}'
            )
        if schema.get('additionalProperties') is False:
            unknown = set(value) - set(properties)
            if unknown:
                names = ', '.join(sorted(unknown))
                raise ValidationError(
                    f'{field_name} contains unknown fields: {names}'
                )
        for name, item in value.items():
            item_schema = properties.get(name)
            if item_schema is not None:
                _validate_schema_value(
                    item_schema,
                    item,
                    f'{field_name}.{name}',
                    depth=depth + 1,
                )
        return
    if non_null_types == ['string']:
        if not isinstance(value, str):
            raise ValidationError(f'{field_name} must be a string')
        if not value.strip():
            raise ValidationError(f'{field_name} must not be empty')
        if len(value) > 2000:
            raise ValidationError(
                f'{field_name} must be at most 2000 characters'
            )
        return
    raise RuntimeError(
        f'unsupported Tool schema type for {field_name}: {expected!r}'
    )
