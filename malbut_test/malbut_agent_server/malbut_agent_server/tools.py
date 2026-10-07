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
            'Query the weather service at the saved or manually configured '
            'location and the today/tomorrow '
            'forecast. Read the returned data before answering weather questions. '
            'This tool takes no arguments and does not control robot movement.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'set_weather_location': ToolSpec(
        name='set_weather_location',
        description=(
            'Ask the weather service to resolve and save the robot weather location in its database. '
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


# These operations use fixed server-owned adapters; no raw ROS names or commands.
_ROBOT_OPERATIONS = {
    'get_robot_status': ('Read current robot, battery, runtime and active mission observations.', {}),
    'get_robot_observations': ('Read current camera detections and tracking observations. Do not infer identity.', {}),
    'list_saved_maps': ('List actually saved maps and the last selected map.', {}),
    'select_saved_map': ('Select one listed saved map and wait for localization. Never invent a map name.',
                         {'map': {'type': 'string', 'maxLength': 80}}),
    'delete_saved_map': ('Request deletion of one listed map. The server asks a bound confirmation before deletion.',
                         {'map': {'type': 'string', 'maxLength': 80}}),
    'get_map_zones': ('Read existing labeled polygons and their current allow/avoid/restricted settings.', {}),
    'update_map_zone': ('Change one existing polygon by its returned index. Null means preserve a field. '
                        'Never invent polygon geometry or revision tokens.', {
        'map': {'type': 'string', 'maxLength': 80},
        'index': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
        'name': {'type': ['string', 'null'], 'maxLength': 64},
        'behavior': {'type': ['string', 'null'], 'enum': ['allow', 'avoid', 'restricted', None]},
    }),
    'wake_robot': ('Prepare robot runtime using a listed map. Use null to reuse the selected map or ask if ambiguous.',
                   {'map': {'type': ['string', 'null'], 'maxLength': 80}}),
    'standby_robot': ('Stop the child robot runtime while keeping voice available. '
                      'The server confirms unrelated active work before stopping.', {}),
    'stop_robot_movement': ('Immediately request stopping ALL robot movement, including web and voice motions. '
                            'This does not disable dialogue or memory. Wait for reported stop result.', {}),
    'request_mapping': ('Create and save a new map through AutoSLAM. '
                        'Use a new short filename without path or extension; never overwrite a map.',
                        {'map_name': {'type': 'string', 'maxLength': 64}}),
    'request_relocalization': ('Find the robot pose on the selected saved map. Use auto normally; '
                               'global_search only when explicitly requested.',
                               {'method': {'type': 'string', 'enum': ['auto', 'global_search']}}),
    'request_manual_control': ('Enable the existing assisted web/joystick manual control. '
                              'This does not itself send velocity or move a requested distance.', {}),
    'request_recovery': ('Ask the native Bringup owner to recover failed owned components once. '
                        'Never run shell commands or claim healthy processes were restarted.', {}),
    'get_homecam_status': ('Read delegated Homecam camera, microphone and monitoring status.', {}),
    'get_homecam_events': ('Read recent Homecam events, using a bounded result count.', {
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'event_type': {'type': ['string', 'null'], 'enum': ['motion', 'person', 'dog', 'cat', None]},
    }),
    'get_homecam_recordings': ('Read recent Homecam recording metadata and authorized links.',
                              {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}),
    'get_homecam_falls': ('Read recent Homecam fall events; these are recorded observations, not a new diagnosis.',
                         {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}),
    'update_homecam_settings': ('Change only explicitly requested Homecam settings under existing owner delegation. '
                                'Use null for unchanged fields. Never change personal or story memory consent.', {
        key: {'type': ['boolean', 'null']} for key in
        ('cameraEnabled', 'microphoneEnabled', 'monitoringEnabled', 'fallEnabled')
    }),
    'confirm_pending_operation': ('Answer the server\'s most recent explicit operation confirmation. '
                                  'Only use for the current direct yes/no answer; no IDs or targets can be supplied.',
                                  {'confirm': {'type': 'boolean'}}),
}
ROBOT_OPERATION_TOOLS = tuple(_ROBOT_OPERATIONS)
SPEECH_DELEGATED_TOOLS = SPEECH_MISSION_TOOLS + ROBOT_OPERATION_TOOLS
for _name, (_description, _properties) in _ROBOT_OPERATIONS.items():
    TOOL_SPECS[_name] = ToolSpec(_name, _description, {
        'type': 'object', 'properties': _properties,
        'required': list(_properties), 'additionalProperties': False,
    })


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
    if non_null_types == ['boolean']:
        if type(value) is not bool:
            raise ValidationError(f'{field_name} must be boolean')
        return
    if non_null_types == ['integer']:
        if type(value) is not int or not schema.get('minimum', 0) <= value <= schema.get('maximum', 1000):
            raise ValidationError(f'{field_name} is outside its integer bounds')
        return
    raise RuntimeError(
        f'unsupported Tool schema type for {field_name}: {expected!r}'
    )
