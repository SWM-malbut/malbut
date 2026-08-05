"""Tests for the provider-neutral agent boundary and safety gate."""

import math

import pytest

from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
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
