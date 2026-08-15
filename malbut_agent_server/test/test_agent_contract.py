"""Tests for the provider-neutral agent boundary and safety gate."""

import math

import pytest

from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ContextMetrics,
    ProviderResult,
    ProviderUsage,
    RobotState,
    ValidationError,
)
from malbut_agent_server.tools import TOOL_SPECS, select_tool_specs


def request(
    utterance: str = '거실로 가줘',
    *,
    tools=('navigate',),
    robot_state=None,
) -> AgentRequest:
    """Build one valid boundary request for focused policy tests."""
    state = {
        'battery_percent': 80,
        'navigation_available': True,
        'localization_ok': True,
        'camera_available': True,
    }
    if robot_state is not None:
        state.update(robot_state)
    return AgentRequest.from_dict(
        {
            'request_id': 'request-1',
            'user_id': 'user-1',
            'conversation_id': 'conversation-1',
            'turn_id': 'turn-1',
            'utterance': utterance,
            'robot_state': state,
            'available_tools': list(tools),
        }
    )


def decision(
    tool_name: str = 'navigate',
    arguments=None,
) -> AgentDecision:
    """Build one high-level model proposal."""
    return AgentDecision(
        type='tool_call',
        message='요청을 확인했어.',
        tool_name=tool_name,
        arguments=(
            {'location': '거실'}
            if arguments is None and tool_name == 'navigate'
            else (arguments or {})
        ),
    )


def test_request_rejects_unknown_or_non_finite_state() -> None:
    """Unversioned or non-finite state cannot cross the boundary."""
    with pytest.raises(ValidationError):
        request(robot_state={'raw_motor_speed': 2})
    with pytest.raises(ValidationError):
        request(robot_state={'battery_percent': math.nan})


def test_request_requires_safe_session_identifiers() -> None:
    """Every turn has server-owned identifiers without control bytes."""
    payload = request().to_dict()
    payload.pop('turn_id')
    with pytest.raises(ValidationError):
        AgentRequest.from_dict(payload)

    payload = request().to_dict()
    payload['conversation_id'] = 'conversation\n1'
    with pytest.raises(ValidationError):
        AgentRequest.from_dict(payload)


def test_request_deduplicates_available_tools() -> None:
    """Duplicate names cannot produce duplicate provider functions."""
    value = request(tools=('navigate', 'navigate'))
    assert value.available_tools == ('navigate',)


def test_non_tool_decision_cannot_smuggle_arguments() -> None:
    """Only a validated tool proposal may carry action arguments."""
    value = AgentDecision(
        type='message',
        message='안녕',
        arguments={'location': '거실'},
    )
    with pytest.raises(ValidationError):
        value.validate()


@pytest.mark.parametrize('message', ['', '   ', '\n\t'])
def test_decision_message_must_contain_visible_text(
    message: str,
) -> None:
    """Every decision has text safe for persistence and speech output."""
    value = AgentDecision(type='message', message=message)
    with pytest.raises(
        ValidationError,
        match='decision message must not be empty',
    ):
        value.validate()


def test_tool_allowlist_contains_no_low_level_motion_control() -> None:
    """The LLM never receives raw velocity, PWM, or e-stop release."""
    forbidden = {'cmd_vel', 'motor_pwm', 'set_velocity', 'release_estop'}
    assert forbidden.isdisjoint(TOOL_SPECS)
    assert set(TOOL_SPECS) == {
        'navigate',
        'detect_pet',
        'capture_photo',
        'send_notification',
        'get_robot_status',
    }


def test_selected_tool_schemas_are_strict_and_ordered() -> None:
    """Unknown tools are omitted and extra arguments are rejected."""
    selected = select_tool_specs(['navigate', 'unknown', 'capture_photo'])
    assert [item.name for item in selected] == [
        'navigate',
        'capture_photo',
    ]
    assert all(
        item.parameters['additionalProperties'] is False
        for item in selected
    )


def test_model_proposal_never_executes_with_untrusted_state() -> None:
    """HTTP-supplied state cannot authorize a robot action."""
    result = SafetyPolicy().evaluate(request(), decision())
    assert result.allowed is False
    assert result.code == 'untrusted_robot_state'


@pytest.mark.parametrize(
    ('state', 'expected_code'),
    [
        ({'emergency_stop': True}, 'emergency_stop'),
        ({'battery_percent': 10}, 'battery_low'),
        ({'navigation_available': False}, 'navigation_unavailable'),
        ({'localization_ok': False}, 'localization_unavailable'),
    ],
)
def test_navigation_fails_closed_on_unsafe_local_state(
    state: dict,
    expected_code: str,
) -> None:
    """Trusted state still has to satisfy every local safety guard."""
    result = SafetyPolicy().evaluate(
        request(robot_state=state),
        decision(),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == expected_code


def test_navigation_requires_current_turn_destination_intent() -> None:
    """A model cannot invent an action from prior context."""
    result = SafetyPolicy().evaluate(
        request('오늘 날씨가 어때?'),
        decision(),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == 'current_turn_intent_missing'


def test_navigation_typo_requires_clarification_instead_of_execution() -> None:
    """An ambiguous destination typo must fail closed at the local gate."""
    result = SafetyPolicy().evaluate(
        request('거시롤 가 줘'),
        decision(),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == 'current_turn_intent_missing'


def test_explicit_navigation_request_can_pass_policy_only() -> None:
    """Passing the gate authorizes a proposal, not direct motor control."""
    result = SafetyPolicy().evaluate(
        request(),
        decision(),
        state_trusted=True,
    )
    assert result.allowed is True
    assert result.code == 'allowed'


def test_camera_tool_respects_privacy_mode() -> None:
    """Camera proposals are denied while local privacy mode is active."""
    result = SafetyPolicy().evaluate(
        request(
            '사진 찍어줘',
            tools=('capture_photo',),
            robot_state={'privacy_mode': True},
        ),
        decision('capture_photo'),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == 'privacy_mode'


@pytest.mark.parametrize(
    ('keyword', 'value'),
    [
        ('allowed_locations', ['거실', '']),
        ('allowed_locations', ['거실', 1]),
        ('minimum_navigation_battery', True),
        ('minimum_navigation_battery', -0.1),
        ('minimum_navigation_battery', 100.1),
        ('maximum_action_ttl_ms', True),
        ('maximum_action_ttl_ms', 0),
        ('maximum_action_ttl_ms', 60001),
    ],
)
def test_safety_policy_configuration_is_strictly_bounded(
    keyword: str,
    value: object,
) -> None:
    """Malformed local policy cannot weaken a safety threshold."""
    with pytest.raises(ValueError):
        SafetyPolicy(**{keyword: value})  # type: ignore[arg-type]


def test_action_ttl_and_missing_validator_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired proposals and incomplete validator registration are denied."""
    expiring = decision()
    expiring.expires_in_ms = 10001
    ttl_result = SafetyPolicy().evaluate(
        request(),
        expiring,
        state_trusted=True,
    )
    assert ttl_result.code == 'ttl_too_long'

    monkeypatch.delattr(SafetyPolicy, '_validate_navigate')
    validator_result = SafetyPolicy().evaluate(
        request(),
        decision(),
        state_trusted=True,
    )
    assert validator_result.code == 'missing_validator'


def test_non_action_unknown_and_unavailable_tool_dispatch() -> None:
    """Distinguish text, hallucination, and unavailable Tools."""
    policy = SafetyPolicy()
    non_action = policy.evaluate(
        request(),
        AgentDecision(type='message', message='안녕'),
        state_trusted=True,
    )
    assert non_action.to_dict() == {
        'allowed': True,
        'code': 'not_an_action',
        'reason': '행동 요청이 아닙니다.',
    }

    unknown = policy.evaluate(
        request(),
        decision('unlock_door'),
        state_trusted=True,
    )
    unavailable = policy.evaluate(
        request(tools=()),
        decision(),
        state_trusted=True,
    )
    assert unknown.code == 'unknown_tool'
    assert unavailable.code == 'tool_unavailable'


@pytest.mark.parametrize(
    'utterance',
    [
        '거실로 갈 수 있는 기능을 알려줘',
        '거실로 가는 요청 취소해',
        '거실로 가는 건 원하지 않아',
        '거실로 가지마',
        '거실 이동 금지',
        'do not go to living room',
        'go not allowed to living room',
        'go to living room not allowed',
        'go to living room is not allowed right now',
        'go to living room is forbidden by policy',
        'go to living room is prohibited by policy',
        'go to living room is disallowed right now',
        'go to living room is banned by policy',
        'moving to living room is not permitted',
        '"go to living room is forbidden"이라는 문장을 번역해',
    ],
)
def test_navigation_rejects_meta_cancel_and_prohibition_intent(
    utterance: str,
) -> None:
    """Quoted, cancelled, or prohibited movement is never current intent."""
    location = (
        'living_room'
        if 'living room' in utterance
        else '거실'
    )
    result = SafetyPolicy().evaluate(
        request(utterance),
        decision('navigate', {'location': location}),
        state_trusted=True,
    )
    assert result.code == 'current_turn_intent_missing'


@pytest.mark.parametrize(
    ('utterance', 'location'),
    [
        ('거실로 가줘', '거실'),
        ('거실로 이동해줘', '거실'),
        ('go to living room', 'living_room'),
        ('go to living room right now', 'living_room'),
        ('please move to living room', 'living_room'),
        ('please move to living room under current policy', 'living_room'),
    ],
)
def test_navigation_prohibition_detection_preserves_positive_intent(
    utterance: str,
    location: str,
) -> None:
    """Trailing-prohibition checks do not suppress direct safe commands."""
    result = SafetyPolicy().evaluate(
        request(utterance),
        decision('navigate', {'location': location}),
        state_trusted=True,
    )
    assert result.code == 'allowed'


def test_missing_intent_checker_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered validator without an intent checker cannot authorize."""
    monkeypatch.delattr(SafetyPolicy, '_has_navigate_intent')
    result = SafetyPolicy().evaluate(
        request(),
        decision(),
        state_trusted=True,
    )
    assert result.code == 'current_turn_intent_missing'


@pytest.mark.parametrize(
    ('arguments', 'state', 'expected_code'),
    [
        ({}, {}, 'invalid_arguments'),
        ({'location': 1}, {}, 'invalid_arguments'),
        ({'location': '옥상'}, {}, 'location_not_allowed'),
        (
            {'location': '거실'},
            {'forbidden_zones': ['거실']},
            'forbidden_zone',
        ),
        ({'location': '거실'}, {'battery_percent': None}, 'battery_unknown'),
    ],
)
def test_navigation_validator_rejects_unbounded_destinations_and_state(
    arguments: dict,
    state: dict,
    expected_code: str,
) -> None:
    """Every destination and trusted navigation prerequisite is bounded."""
    result = SafetyPolicy().evaluate(
        request(robot_state=state),
        decision('navigate', arguments),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == expected_code


def test_low_battery_may_only_navigate_to_charger() -> None:
    """The explicit charger exception does not authorize other locations."""
    result = SafetyPolicy().evaluate(
        request(
            '충전소로 가줘',
            robot_state={'battery_percent': 1},
        ),
        decision('navigate', {'location': '충전소'}),
        state_trusted=True,
    )
    assert result.allowed is True
    assert result.code == 'allowed'


@pytest.mark.parametrize(
    ('tool_name', 'utterance', 'arguments', 'state', 'expected_code'),
    [
        ('capture_photo', '사진 찍어줘', {'extra': True}, {}, 'invalid_arguments'),
        (
            'capture_photo',
            '사진 찍어줘',
            {},
            {'camera_available': False},
            'camera_unavailable',
        ),
        ('capture_photo', '사진 찍어줘', {}, {}, 'allowed'),
        ('detect_pet', '강아지 찾아줘', {}, {}, 'allowed'),
        (
            'get_robot_status',
            '배터리 알려줘',
            {'extra': True},
            {},
            'invalid_arguments',
        ),
        ('get_robot_status', '배터리 알려줘', {}, {}, 'allowed'),
    ],
)
def test_camera_detection_and_status_validators_are_fail_closed(
    tool_name: str,
    utterance: str,
    arguments: dict,
    state: dict,
    expected_code: str,
) -> None:
    """Read and camera Tools enforce arguments, health, privacy, and intent."""
    result = SafetyPolicy().evaluate(
        request(utterance, tools=(tool_name,), robot_state=state),
        decision(tool_name, arguments),
        state_trusted=True,
    )
    assert result.code == expected_code
    assert result.allowed is (expected_code == 'allowed')


@pytest.mark.parametrize(
    ('tool_name', 'utterance'),
    [
        ('capture_photo', '사진 찍지 마'),
        ('capture_photo', '사진을 보여줘'),
        ('detect_pet', '강아지 찾지 마'),
        ('detect_pet', '강아지가 어디 있어?'),
        ('detect_pet', 'find the cat is prohibited'),
        ('get_robot_status', '배터리 확인하지 마'),
        ('get_robot_status', '배터리가 충분해?'),
        ('get_robot_status', 'check battery is prohibited'),
        ('capture_photo', 'take a photo is prohibited'),
    ],
)
def test_camera_detection_and_status_require_positive_action_intent(
    tool_name: str,
    utterance: str,
) -> None:
    """A subject mention or explicit prohibition cannot authorize a Tool."""
    result = SafetyPolicy().evaluate(
        request(utterance, tools=(tool_name,)),
        decision(tool_name),
        state_trusted=True,
    )
    assert result.code == 'current_turn_intent_missing'


def test_notification_rejects_explicit_english_prohibition() -> None:
    """A valid notification payload cannot override a prohibited utterance."""
    result = SafetyPolicy().evaluate(
        request(
            'notify caregiver status update is prohibited',
            tools=('send_notification',),
        ),
        decision(
            'send_notification',
            {'message': 'status update', 'image_id': None},
        ),
        state_trusted=True,
    )
    assert result.code == 'current_turn_intent_missing'


@pytest.mark.parametrize(
    ('arguments', 'state', 'expected_code'),
    [
        ({'message': '도착'}, {}, 'invalid_arguments'),
        ({'message': '', 'image_id': None}, {}, 'invalid_arguments'),
        ({'message': '도착', 'image_id': 7}, {}, 'invalid_arguments'),
        (
            {'message': '도착', 'image_id': 'image-1'},
            {'privacy_mode': True},
            'privacy_mode',
        ),
        (
            {'message': '도착', 'image_id': 'image-1'},
            {},
            'image_attachment_unverified',
        ),
        (
            {'message': 'password=secret', 'image_id': None},
            {},
            'sensitive_notification',
        ),
        (
            {'message': '비밀', 'image_id': None},
            {},
            'current_turn_intent_missing',
        ),
        ({'message': '도착', 'image_id': None}, {}, 'allowed'),
    ],
)
def test_notification_payload_and_current_utterance_are_bound(
    arguments: dict,
    state: dict,
    expected_code: str,
) -> None:
    """Notifications reject media, secrets, and text absent from this turn."""
    result = SafetyPolicy().evaluate(
        request(
            '가족에게 도착했다고 알려줘',
            tools=('send_notification',),
            robot_state=state,
        ),
        decision('send_notification', arguments),
        state_trusted=True,
    )
    assert result.code == expected_code
    assert result.allowed is (expected_code == 'allowed')


@pytest.mark.parametrize(
    ('state', 'message'),
    (
        (None, None),
        ([], 'robot_state must be an object'),
        ({'battery_percent': True}, 'battery_percent must be a number'),
        ({'privacy_mode': 1}, 'privacy_mode must be a boolean'),
        ({'forbidden_zones': 'kitchen'}, 'forbidden_zones must be'),
        ({'forbidden_zones': ['']}, 'forbidden_zones must be'),
        ({'forbidden_zones': ['zone'] * 51}, 'too many items'),
    ),
)
def test_robot_state_strictly_validates_optional_fields(
    state,
    message,
) -> None:
    """Robot-state defaults are safe and malformed supplied values fail."""
    if message is None:
        assert RobotState.from_dict(state) == RobotState()
        return
    with pytest.raises(ValidationError, match=message):
        RobotState.from_dict(state)


@pytest.mark.parametrize(
    ('field_name', 'value', 'message'),
    (
        ('body', None, 'request body must be an object'),
        ('available_tools', 'navigate', 'available_tools must be a list'),
        ('available_tools', ['tool'] * 33, 'too many items'),
        ('utterance', 'x' * 2001, 'utterance must be at most'),
        ('request_id', '', 'request_id must not be empty'),
    ),
)
def test_request_rejects_malformed_envelope_fields(
    field_name: str,
    value,
    message: str,
) -> None:
    """The request envelope rejects wrong types, sizes, and empty IDs."""
    if field_name == 'body':
        payload = value
    else:
        payload = request().to_dict()
        payload[field_name] = value
    with pytest.raises(ValidationError, match=message):
        AgentRequest.from_dict(payload)


@pytest.mark.parametrize(
    ('overrides', 'message'),
    (
        ({'type': 1}, 'decision type must be a string'),
        ({'type': 'unknown'}, 'unknown decision type'),
        ({'message': 1}, 'decision message must be a string'),
        ({'message': 'x' * 2001}, 'decision message is too long'),
        ({'arguments': []}, 'decision arguments must be an object'),
        ({'reason': 'x' * 1001}, 'decision reason is invalid'),
        ({'confidence': True}, 'confidence must be a number'),
        ({'confidence': float('nan')}, 'confidence must be finite'),
        ({'expires_in_ms': True}, 'expires_in_ms must be an integer'),
        ({'expires_in_ms': 0}, 'expires_in_ms must be between'),
        ({'tool_name': 'navigate'}, 'only tool_call decisions'),
    ),
)
def test_decision_rejects_invalid_provider_fields(
    overrides,
    message: str,
) -> None:
    """Every provider-controlled decision field fails closed."""
    arguments = {'type': 'message', 'message': '안전한 응답'}
    arguments.update(overrides)
    with pytest.raises(ValidationError, match=message):
        AgentDecision(**arguments).validate()


def test_usage_rejects_inconsistent_total() -> None:
    """Total usage cannot be lower than its input and output sum."""
    with pytest.raises(ValidationError, match='total_tokens is smaller'):
        ProviderUsage(
            input_tokens=4,
            output_tokens=3,
            total_tokens=6,
        ).validate()


@pytest.mark.parametrize(
    ('value', 'message'),
    (
        (None, 'context metrics must be an object'),
        (
            {'recent_conversation': []},
            'recent_conversation context metric must be an object',
        ),
        (
            {'model_input': {'truncated_sections': 'memory'}},
            'truncated_sections must be a list',
        ),
        (
            {'model_input': {'truncated_sections': [1]}},
            'truncated_sections must contain strings',
        ),
        (
            {'conversation_summary': {'summary_id': 1}},
            'summary_id must be a string or null',
        ),
        (
            {'model_input': {'overflow_fallback': 1}},
            'overflow_fallback must be a boolean',
        ),
        (
            {'recent_conversation': {'turn_count': True}},
            'turn_count context metric must be non-negative',
        ),
    ),
)
def test_context_metrics_reject_malformed_persisted_values(
    value,
    message: str,
) -> None:
    """Persisted metrics cannot smuggle malformed diagnostic values."""
    with pytest.raises(ValidationError, match=message):
        ContextMetrics.from_dict(value)


@pytest.mark.parametrize(
    ('overrides', 'message'),
    (
        ({'provider': ''}, 'provider result provider is invalid'),
        ({'response_id': 'bad\nresponse'}, 'response_id is invalid'),
        ({'input_chars': True}, 'input_chars is invalid'),
    ),
)
def test_provider_result_rejects_invalid_diagnostic_metadata(
    overrides,
    message: str,
) -> None:
    """Content-free provider diagnostics retain strict scalar types."""
    arguments = {
        'decision': AgentDecision(type='message', message='응답'),
        'provider': 'provider',
        'model': 'model',
        'latency_ms': 1.0,
    }
    arguments.update(overrides)
    with pytest.raises(ValidationError, match=message):
        ProviderResult(**arguments).validate()
