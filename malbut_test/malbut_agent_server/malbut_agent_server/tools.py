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

SPEECH_MISSION_TOOLS = (
    'request_navigation', 'request_follow_person', 'request_patrol',
    'cancel_voice_mission',
)


TOOL_SPECS = {
    'request_navigation': ToolSpec(
        name='request_navigation',
        description=(
            'Request one named indoor destination through Manager. Use only for '
            'a current movement request interpreted by meaning, including polite '
            'questions, suggestions, dialect, and recognizable speech errors. '
            'For example "우리 거실로 가볼까?", "거실로 와바라", and '
            '"주방으로 오너라" request movement to the named place. '
            'These examples are not an exhaustive phrase list. '
            'A clear destination with this meaning is a request to act; '
            'call this tool without asking for confirmation again. '
            'A capability question such as "거실로 갈 수 있어?", negation, '
            'quotation, or a hypothetical is not a movement request. A configured '
            'server-owned map resolver supplies coordinates; never invent them. '
            'This proposes a Manager request, not successful movement. '
            'For an ambiguous destination ask which named place the user means. '
            'A current answer to your immediately preceding destination question '
            'can complete that movement request; do not demand the whole command again. '
            'Never revive old actions from memories or unrelated history. '
            'No speaker position or one-shot approach capability is available: '
            '"와바라", "이리 오너라", and "come here" without a named place '
            'require destination clarification, never guessed coordinates. '
            'Relative motion is unsupported.'
        ),
        parameters={
            'type': 'object',
            'properties': {'location': {'type': 'string', 'maxLength': 128}},
            'required': ['location'],
            'additionalProperties': False,
        },
    ),
    'request_follow_person': ToolSpec(
        name='request_follow_person',
        description=(
            'Ask Manager to start following the first visible person using the '
            'configured safe distance. Interpret current requests to keep following '
            'by meaning, including paraphrases, dialect, and speech errors. '
            'This is ongoing following, not a one-shot approach: do not substitute '
            'it for "와바라", "이리 오너라", or "come here". '
            'The visible person is not verified as the speaker; never '
            'claim speaker identification or choose a named registered person. '
            'Do not claim execution or completion before Manager reports it.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'request_patrol': ToolSpec(
        name='request_patrol',
        description=(
            'Ask Manager for one patrol of the selected saved map. Use only for '
            'a current request to inspect or patrol the home, interpreted by meaning '
            'rather than a fixed phrase. thoroughness is normal unless the user '
            'explicitly asks for light or thorough coverage. Never claim '
            'execution or completion before Manager reports it.'
        ),
        parameters={
            'type': 'object',
            'properties': {'thoroughness': {
                'type': 'string', 'enum': ['light', 'normal', 'thorough'],
            }},
            'required': ['thoroughness'],
            'additionalProperties': False,
        },
    ),
    'cancel_voice_mission': ToolSpec(
        name='cancel_voice_mission',
        description=(
            'Request cancellation of this voice session\'s current motion '
            'mission. Interpret current stop/cancel intent by meaning, including '
            '"이제 그만 따라와", "그쯤 하고 쉬어", and other paraphrases. '
            'Never cancel weather, emergency/situation work, or another '
            'client\'s mission. Cancellation receipt does not prove termination.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'get_weather': ToolSpec(
        name='get_weather',
        description=(
            'Ask Manager for weather at the saved or manually configured '
            'location and the today/tomorrow '
            'forecast. Read the returned data before answering weather questions. '
            'This tool takes no arguments and does not control robot movement.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'set_weather_location': ToolSpec(
        name='set_weather_location',
        description=(
            'Ask Manager to resolve and save the robot weather location in its database. '
            'Use only when the user explicitly states or corrects their current location, '
            'asks to change the weather location, or answers your location clarification. '
            'Extract the new location, never the negated old location. Do not use for '
            'a one-off weather question about another place, travel plans, quoted speech, '
            'or third-party locations. Never invent coordinates. Read the result before '
            'claiming the location was saved; ambiguous names need a city/district.'
        ),
        parameters={
            'type': 'object',
            'properties': {'location': {
                'type': 'string',
                'description': 'The explicitly supplied new area, e.g. 경기도 수원시 우만동.',
            }},
            'required': ['location'],
            'additionalProperties': False,
        },
    ),
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
        if len(value) > schema.get('maxLength', 2000):
            raise ValidationError(f'{field_name} exceeds its length limit')
        if 'enum' in schema and value not in schema['enum']:
            raise ValidationError(f'{field_name} is not an allowed value')
        return
    raise RuntimeError(
        f'unsupported Tool schema type for {field_name}: {expected!r}'
    )
