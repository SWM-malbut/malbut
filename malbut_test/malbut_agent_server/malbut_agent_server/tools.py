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

# Private to the speech provider. This read never enters the actuator registry.
STORY_RECALL_TOOL = ToolSpec(
    name='recall_story_memory',
    description=(
        'Look up stories from earlier conversations with this user when their '
        'question depends on a past conversation, experience or personal context '
        'missing from the supplied recent dialogue. Use it before answering such '
        'a question, even if no story is visible yet. For a current robot command, '
        'weather, camera or patrol observation request, choose the appropriate '
        'function directly. This lookup cannot authorize a robot action.'
    ),
    parameters=EMPTY_PARAMETERS,
)

PERSONAL_RECALL_TOOL = ToolSpec(
    name='recall_personal_context',
    description=(
        'Read consented personal facts and available stories from earlier conversations. '
        'Use before answering about their name, nickname, pet, preferences or past '
        'experience when recent dialogue does not establish it. For personalized '
        'recommendations, focus on preference even when the current request contains no '
        'food or activity keywords. Use related for general past conversations. '
        'Choose current commands and observation queries directly.'
    ),
    parameters={
        'type': 'object', 'additionalProperties': False, 'required': ['focus'],
        'properties': {'focus': {'type': 'string',
                                  'enum': ['related', 'name', 'nickname', 'pet', 'preference']}},
    },
)

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
            'Search saved manual patrol photos/observations (read-only; never moves or captures). '
            'Always query fresh records for past sightings, last-seen object locations, or patrol recaps. '
            'query: short Korean object/place keyword or null for recent items (never a full sentence). '
            'since/until: timezone-aware ISO timestamps only when time is known, else null. '
            'Verify recent_context descriptions visually; report observation time without claiming the object is still there.'
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
            'Request one named indoor destination via Manager for a current movement request '
            'interpreted by meaning (e.g. "우리 거실로 가볼까?", "거실로 와바라", dialect, speech errors; '
            'not capability questions, negation, quotes, hypotheticals, map-name answers, or past history). '
            'Use only navigation_context registered locations; server resolves coordinates. '
            'Call without asking again for an exact match or a single clear close typo (e.g. "기실"->"거실"); '
            'server verifies similarity and requires confirmation for distant or ambiguous proposed names. '
            'If no plausible candidate or indistinguishable candidates, ask without calling. '
            'With confirmation_location, call only on actual approval (or propose an explicit new place). '
            'Bare "와바라"/"come here" without a named place requires destination clarification; no speaker approach or relative motion.'
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
            'Ask Manager to continuously follow the first visible person at a safe distance '
            'for current follow requests by meaning. Not a one-shot approach ("와바라"/"come here"). '
            'Never claim speaker identity, choose a named person, or claim completion before Manager reports it.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'request_patrol': ToolSpec(
        name='request_patrol',
        description=(
            'Ask Manager for one patrol of the selected saved map for a current patrol/inspection request. '
            'thoroughness from current request: light(대충/가볍게/간단히/대강), thorough(꼼꼼하게/구석구석/철저히), '
            'normal(unspecified/ordinary). Preserve qualifiers on topic switches (e.g. "지도는 됐고 대충 순찰해"->light). '
            'Cannot restrict to one room, set speed, or patrol continuously for a duration—explain limits instead. '
            'Never claim execution/completion before Manager reports it.'
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
            'Request cancellation of active/submitted/suspended foreground Manager missions '
            '(e.g. "이제 그만 따라와", "그쯤 하고 쉬어"). Do not call for dropping an unsubmitted '
            'clarification idea or for background monitoring/weather. Receipt does not prove termination.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'get_weather': ToolSpec(
        name='get_weather',
        description=(
            'Ask Manager for weather at the saved location and today/tomorrow forecast. '
            'Read returned data before answering; no movement control.'
        ),
        parameters=EMPTY_PARAMETERS,
    ),
    'set_weather_location': ToolSpec(
        name='set_weather_location',
        description=(
            'Save the robot weather location in Manager DB only when the user explicitly states/corrects '
            'their current location or answers a location clarification. Extract only the new area '
            '(not negated old area). Ignore one-off other-area queries, travel, quotes, or third-party locations; '
            'never invent coordinates; read the result before confirming.'
        ),
        parameters={
            'type': 'object',
            'properties': {'location': {
                'type': 'string',
                'description': 'Explicit new area, e.g. 경기도 수원시 우만동.',
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
        'Request AutoSLAM via Manager to explore and create a new map (not patrol). '
        'Extract map_name (1-64 chars, no path/ext, starting with Korean/ASCII letter/digit, '
        'then letters/digits/space/_/-): strip surrounding unquoted particles '
        '("이사집으로 하자"->"이사집", "새집으로 해"->"새집"), but copy explicitly quoted values '
        'verbatim ("새집으로"->"새집으로"). Ask if missing/invalid (never invent/default "home" or overwrite); '
        'bare "응"/"네" is not a name—re-ask while keeping mapping context. '
        'Completion means map saved, not every space explored.',
        {'map_name': {'type': 'string', 'maxLength': 64}},
    ),
    'request_relocalization': (
        'Request robot pose correction on the selected map when asked to actively find/recover/correct '
        'the robot position ("위치 찾아봐", "위치 보정해 줘"), even if current pose is unknown. '
        'method: auto normally; global_search only if whole-map search is explicitly requested. '
        'Bare status queries ("지금 어디야?") do not trigger correction (no room lookup tool; never invent a room '
        'or claim lost pose). Clarify person-location or ambiguous queries. No map switching, Bringup, or voice GIVEN_POSE.',
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
