"""High-level robot tools exposed to language models."""

import copy
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.speech_nav2 import NAV2_TOOL_DEFINITIONS


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
    'cancel_voice_mission', 'request_mapping', 'request_relocalization',
    'request_manual_control', 'request_recovery',
    *NAV2_TOOL_DEFINITIONS,
)

HOMECAM_QUERY_TOOLS = (
    'get_homecam_status', 'get_homecam_events',
    'get_homecam_recordings', 'get_homecam_falls',
)

PATROL_QUERY_TOOLS = ('search_patrol_observations',)


TOOL_SPECS = {
    'search_patrol_observations': ToolSpec(
        name='search_patrol_observations',
        description=(
            'Search saved photos and observations from manual robot patrols. '
            'Use for what was seen earlier, where an object was last seen, or a patrol recap '
            '(아까 순찰 때 뭐 봤어, 리모컨 어디서 봤어). Always query fresh records. '
            'This reads past evidence and never starts movement or takes a new photo. '
            'query is a short Korean object/place keyword, or null for recent observations; '
            'do not put a whole question in query. since/until are timezone-aware ISO timestamps '
            'only when the requested time is known, otherwise null. '
            'Results may include recent_context candidates with different wording; '
            'check their visual descriptions for relevance, never assume they match the query. '
            'Report the observation time; do not claim an object is still there.'
        ),
        parameters={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'query': {'type': ['string', 'null'], 'maxLength': 120},
                'since': {'type': ['string', 'null'], 'maxLength': 64},
                'until': {'type': ['string', 'null'], 'maxLength': 64},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5},
            },
            'required': ['query', 'since', 'until', 'limit'],
        },
    ),
    'request_navigation': ToolSpec(
        name='request_navigation',
        description=(
            'Request one named indoor destination through Manager. Use only for '
            'a current movement request interpreted by meaning, including polite '
            'questions, suggestions, dialect, and recognizable speech errors. '
            'First determine actual movement intent from the conversation. '
            'The presence of navigation_context or a place-like value does not '
            'establish that intent. A name supplied in response to a map-name '
            'question completes mapping; do not check it against destinations '
            'or ask where to move. Apply the destination rules below only to '
            'actual movement requests. '
            'For example "우리 거실로 가볼까?", "거실로 와바라", and '
            '"주방으로 오너라" request movement to the named place. '
            'These examples are not an exhaustive phrase list. '
            'An exact registered destination with this meaning is a request to act; '
            'call this tool without asking for confirmation again. '
            'A capability question such as "거실로 갈 수 있어?", negation, '
            'quotation, or a hypothetical is not a movement request. A configured '
            'server-owned map resolver supplies coordinates; never invent them. '
            'This proposes a Manager request, not successful movement. '
            'When navigation_context is supplied, use only its registered locations. '
            'Prefer an exact registered name. For a close typo such as "기실" with '
            'one clearly similar registered candidate "거실", propose that candidate '
            'without asking again. The server checks similarity and competing names, '
            'and requires confirmation for distant or ambiguous proposed names. '
            'With indistinguishable competing candidates or no plausible candidate, '
            'ask which registered place the user means without calling this tool. '
            'If confirmation_location is present, call this tool with that location '
            'only when the current reply actually approves that movement. Rejection '
            'or an unrelated reply is not approval; an explicit changed destination '
            'is a new request. Never claim movement while confirmation is required. '
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
            'rather than a fixed phrase. Choose coverage from the CURRENT request: '
            '대충, 가볍게, 간단히, or 대강 means light; 꼼꼼하게, 구석구석, '
            'or 철저히 means thorough. Use normal only when no coverage preference '
            'is given, or ordinary coverage is explicitly requested. A topic switch '
            'does not remove qualifiers in the new request: after a map-name '
            'question, 지도는 됐고 대충 순찰해 requests light patrol. These are '
            'examples of meaning, not an exhaustive phrase list. This tool cannot '
            'restrict patrol to one room, set a speed, or patrol continuously '
            'for a requested duration. If those are requirements, explain the '
            'limitation without starting a broader or different patrol. Never claim '
            'execution or completion before Manager reports it.'
        ),
        parameters={
            'type': 'object',
            'properties': {'thoroughness': {
                'type': 'string', 'enum': ['light', 'normal', 'thorough'],
                'description': 'Coverage, not speed: light=가볍게/대충, normal=보통/미지정, '
                               'thorough=꼼꼼하게/구석구석. Preserve the current user preference.',
            }},
            'required': ['thoroughness'],
            'additionalProperties': False,
        },
    ),
    'cancel_voice_mission': ToolSpec(
        name='cancel_voice_mission',
        description=(
            'Request cancellation of current foreground Manager missions, '
            'including submitted goals waiting to execute and suspended work. '
            'An unanswered clarification, such as asking what to name a new map, '
            'has not submitted that mission. Dropping that unsubmitted idea and '
            'asking for an explanation is a conversation change, not an Action '
            'cancellation; answer the current question without this tool. '
            'Still use this tool for an actual stop/cancel command, including '
            '"이제 그만 따라와", "그쯤 하고 쉬어", and other paraphrases. '
            'Do not cancel background monitoring or weather work. '
            'Cancellation receipt does not prove termination.'
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


# Fixed requests only: no runtime startup, map switching, or preparation chain.
_ADDITIONAL_TOOLS = {
    'request_mapping': (
        'Request AutoSLAM through the existing Manager capability. '
        'Use for a current request to explore and create a map, not a request '
        'to patrol an existing map. Supply the user\'s new map filename without '
        'extension or path; preserve spaces and Korean characters INSIDE the '
        'name, but extract only the name from the sentence. Naming particles '
        'and instructions surrounding it are not part of the filename: '
        '"이름은 이사집으로 하자" means map_name="이사집"; a current reply '
        '"이사한 집으로 해" to your name question means map_name="이사한 집". '
        'When the user explicitly quotes a filename, copy the ENTIRE quoted value '
        'before interpreting any surrounding naming instruction. For example, '
        'a name-only answer containing the quoted value "새집으로" means '
        'map_name="새집으로", and "집으로 해" means map_name="집으로 해". '
        'Do not treat syllables INSIDE that quoted value as particles or commands. '
        'In contrast, the unquoted instruction 새집으로 해 means map_name="새집". '
        'This is literal value preservation, not a blanket suffix removal. If no name '
        'was supplied, ask what to call the map; do not invent a name or use '
        'home as an implicit default. A bare affirmation such as 응 or 네 does '
        'not supply a filename: keep the pending mapping context and ask for '
        'the name again. Do not treat that acknowledgement as a new unrelated '
        'conversation or abandon the request. A current answer to that question can '
        'complete the request. Names must be 1-64 characters: start with a Korean '
        'syllable, ASCII letter, or digit; then those characters, spaces, hyphens '
        'or underscores only. Ask for another name if invalid; do not silently '
        'sanitize an exact filename. Never overwrite a map. Manager and the existing '
        'AutoSLAM server prepare the runtime. Completion means exploration '
        'ended and the map was saved, not that every space was explored.',
        {'map_name': {'type': 'string', 'maxLength': 64}},
    ),
    'request_relocalization': (
        'Request pose correction on the already selected map. '
        'Use when asked to actively find, recover, or correct the ROBOT\'S '
        'position, including 지금 어디인지 위치 찾아봐, 위치 다시 찾아줘, '
        'or 위치 보정해 줘. These request localization even when the current '
        'pose is unknown; do not refuse them as unsupported location lookup. '
        'Use auto normally, global_search only when the user explicitly asks '
        'to search the whole map. A bare status question such as 지금 어디야? '
        'or 어느 방에 있어? alone does not request correction: there is no '
        'current-room lookup tool, so do not invent a room or start localization '
        'to answer it. Missing lookup data is not evidence of a localization '
        'failure; do not claim the robot has lost its pose. If the user asks '
        'about a person\'s location or the intent '
        'to run localization is unclear, clarify. This does not select a map or start '
        'Bringup. GIVEN_POSE requires a verified map pose and is available '
        'through the operator interface, not this voice tool; never invent '
        'coordinates or substitute a different correction method for it.',
        {'method': {'type': 'string', 'enum': ['auto', 'global_search']}},
    ),
    'request_manual_control': (
        'Request existing AssistedTeleop via Manager. This enables manual input, '
        'not a velocity or distance command.', {},
    ),
    'request_recovery': (
        'Request the existing manual recovery Action via Manager once. '
        'This does not start Bringup or invent a recovery procedure.', {},
    ),
    'get_homecam_status': (
        'Query the cloud API for current Homecam settings and apply receipts. '
        'A saved setting is not proof that the robot applied it.', {},
    ),
    'get_homecam_events': (
        'Query recent Homecam events. Read the new result before answering.',
        {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
         'event_type': {'type': ['string', 'null'],
                        'enum': ['motion', 'person', 'dog', 'cat', None]}},
    ),
    'get_homecam_recordings': (
        'Query recent Homecam recording metadata, not video content.',
        {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
    ),
    'get_homecam_falls': (
        'Query recorded Homecam fall incidents. These are observations, '
        'not a fresh medical diagnosis.',
        {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
    ),
}
for _name, (_description, _properties) in _ADDITIONAL_TOOLS.items():
    TOOL_SPECS[_name] = ToolSpec(_name, _description, {
        'type': 'object', 'properties': _properties,
        'required': list(_properties), 'additionalProperties': False,
    })

for _name, (_description, _properties) in NAV2_TOOL_DEFINITIONS.items():
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
    if tool_name == 'request_mapping':
        normalize_mapping_name(arguments['map_name'])
    return copy.deepcopy(arguments)


def normalize_mapping_name(value):
    """Share filename validation between proposal policy and ROS preparation."""
    if isinstance(value, str):
        value = unicodedata.normalize('NFC', value.strip())
    if (not isinstance(value, str)
            or re.fullmatch(r'[A-Za-z0-9가-힣][A-Za-z0-9가-힣 _-]{0,63}', value) is None):
        raise ValidationError('mapping requires a filename without path or extension')
    return value


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
    if non_null_types == ['integer']:
        if (type(value) is not int
                or not schema.get('minimum', 0) <= value <= schema.get('maximum', 1000)):
            raise ValidationError(f'{field_name} is outside its integer bounds')
        return
    if non_null_types == ['number']:
        if (type(value) not in (int, float)
                or not schema.get('minimum', -10000) <= value <= schema.get('maximum', 10000)
                or not math.isfinite(value)):
            raise ValidationError(f'{field_name} is outside its numeric bounds')
        return
    if non_null_types == ['array']:
        if (not isinstance(value, list)
                or not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 128)):
            raise ValidationError(f'{field_name} is outside its array bounds')
        for index, item in enumerate(value):
            _validate_schema_value(schema['items'], item, f'{field_name}[{index}]', depth=depth + 1)
        return
    raise RuntimeError(
        f'unsupported Tool schema type for {field_name}: {expected!r}'
    )
