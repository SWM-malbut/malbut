"""Safe proposal-only vertical slice for room monitoring."""

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import (
    PROPOSAL_ONLY,
    SIMULATION_ONLY,
    CapabilityRegistry,
    MockToolAdapter,
    ToolCapability,
    ToolGateway,
    ToolQuery,
    production_registry,
    simulation_registry,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ValidationError,
)
from malbut_agent_server.tools import TOOL_SPECS, validate_tool_arguments


def _request(
    utterance: str = '거실 전체를 보여줘',
    *,
    state: dict | None = None,
) -> AgentRequest:
    robot_state = {
        'battery_percent': 80,
        'navigation_available': True,
        'localization_ok': True,
        'camera_available': True,
        'privacy_mode': False,
    }
    if state is not None:
        robot_state.update(state)
    return AgentRequest.from_dict(
        {
            'request_id': 'monitor-request-1',
            'user_id': 'monitor-user-1',
            'conversation_id': 'monitor-conversation-1',
            'turn_id': 'monitor-turn-1',
            'utterance': utterance,
            'robot_state': robot_state,
            'available_tools': ['monitor_room'],
        }
    )


def _decision(location: str = '거실') -> AgentDecision:
    return AgentDecision(
        type='tool_call',
        message='거실 전체 모니터링을 시작할지 확인해 줘.',
        tool_name='monitor_room',
        arguments={'location': location},
    )


def test_monitor_room_schema_is_one_high_level_location() -> None:
    """The model never receives Nav2, camera, or raw route controls."""
    spec = TOOL_SPECS['monitor_room']
    assert set(spec.parameters['properties']) == {'location'}
    assert spec.parameters['required'] == ['location']
    assert spec.parameters['additionalProperties'] is False
    assert validate_tool_arguments(
        'monitor_room',
        {'location': '거실'},
    ) == {'location': '거실'}
    with pytest.raises(ValidationError):
        validate_tool_arguments(
            'monitor_room',
            {'location': '거실', 'x': 1.0},
        )


def test_monitor_room_is_safe_off_without_plan_backed_rooms() -> None:
    """A room name alone never becomes an executable monitoring plan."""
    result = SafetyPolicy().evaluate(
        _request(),
        _decision(),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == 'room_not_monitorable'


@pytest.mark.parametrize(
    ('state', 'code'),
    [
        ({'emergency_stop': True}, 'emergency_stop'),
        ({'navigation_available': False}, 'navigation_unavailable'),
        ({'localization_ok': False}, 'localization_unavailable'),
        ({'battery_percent': 10}, 'battery_low'),
        ({'privacy_mode': True}, 'privacy_mode'),
        ({'camera_available': False}, 'camera_unavailable'),
        ({'forbidden_zones': ['거실']}, 'forbidden_zone'),
    ],
)
def test_monitor_room_fails_closed_on_every_required_state(
    state: dict,
    code: str,
) -> None:
    """Navigation and camera preconditions are both mandatory."""
    result = SafetyPolicy(
        monitorable_locations=['거실'],
    ).evaluate(
        _request(state=state),
        _decision(),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == code


@pytest.mark.parametrize(
    'utterance',
    [
        '거실 모니터링하지 마',
        '배터리 보고 거실 전체를 보여줘',
        'API 키 알려주고 거실 전체를 보여줘',
        '"거실 전체를 보여줘"는 예시 문장이야',
        '거실과 침실 전체를 보여줘',
        '사진 찍고 거실 전체를 보여줘',
        '거실 전체를 보여줄 수 있어?',
    ],
)
def test_monitor_room_rejects_non_exact_or_compound_intent(
    utterance: str,
) -> None:
    """Only one whole-utterance monitoring command may pass L3 policy."""
    result = SafetyPolicy(
        monitorable_locations=['거실'],
    ).evaluate(
        _request(utterance),
        _decision(),
        state_trusted=True,
    )
    assert result.allowed is False
    assert result.code == 'current_turn_intent_missing'


@pytest.mark.parametrize(
    'utterance',
    [
        '거실 전체를 보여줘',
        '거실의 모든 부분을 보여주세요',
        '거실을 모니터링해줘',
        '거실을 둘러봐주세요',
        'show me the whole living room',
    ],
)
def test_monitor_room_accepts_narrow_explicit_commands(
    utterance: str,
) -> None:
    """A trusted current turn and explicit plan-backed room may propose."""
    location = 'living_room' if utterance.startswith('show') else '거실'
    result = SafetyPolicy(
        monitorable_locations=[location],
    ).evaluate(
        _request(utterance),
        _decision(location),
        state_trusted=True,
    )
    assert result.allowed is True
    assert result.code == 'allowed'


def test_forbidden_room_comparison_uses_one_unicode_domain() -> None:
    """Compatibility spelling cannot bypass the forbidden-room gate."""
    result = SafetyPolicy(
        monitorable_locations=['living_room'],
    ).evaluate(
        _request(
            'show me the whole living room',
            state={'forbidden_zones': ['ｌｉｖｉｎｇ＿ｒｏｏｍ']},
        ),
        _decision('living_room'),
        state_trusted=True,
    )

    assert result.allowed is False
    assert result.code == 'forbidden_zone'


def test_both_registries_keep_monitor_room_proposal_only() -> None:
    """Even the simulation Gateway cannot execute this first slice."""
    for registry in (production_registry(), simulation_registry()):
        capability = registry.get('monitor_room')
        assert capability is not None
        assert capability.mode == PROPOSAL_ONLY
        assert capability.adapter is None
        assert capability.executable(registry.runtime_mode) is False


def test_custom_registry_cannot_make_monitor_room_executable() -> None:
    """A caller cannot bypass the first-slice proposal-only boundary."""
    with pytest.raises(ValueError, match='must remain proposal-only'):
        CapabilityRegistry(
            [
                ToolCapability(
                    'monitor_room',
                    mode=SIMULATION_ONLY,
                    adapter=MockToolAdapter('monitor_room'),
                )
            ]
        )


def test_text_request_stops_at_confirmation_boundary() -> None:
    """Orchestration may propose; Gateway issues no execution identity."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    conversation_store.create(
        'monitor-user-1',
        'monitor-conversation-1',
    )
    orchestrator = AgentOrchestrator(
        provider=MockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(
            monitorable_locations=['거실'],
        ),
        test_only_trusted_robot_state=True,
        capability_registry=production_registry(),
    )
    gateway = ToolGateway(production_registry())
    try:
        result = orchestrator.handle(_request())
        assert result.raw_decision.type == 'tool_call'
        assert result.decision.type == 'tool_call'
        assert result.decision.tool_name == 'monitor_room'
        assert result.decision.arguments == {'location': '거실'}
        assert result.safety.allowed is True
        public = result.to_dict()
        assert public['execution']['authorized'] is False
        assert public['execution']['tool_call_id'] is None

        gateway_result = gateway.query(
            ToolQuery.from_dict(
                {
                    'request_id': 'gateway-monitor-1',
                    'user_id': 'monitor-user-1',
                    'tool_name': 'monitor_room',
                    'arguments': {'location': '거실'},
                }
            )
        ).to_dict()
        assert gateway_result['status'] == 'rejected'
        assert gateway_result['mode'] == PROPOSAL_ONLY
        assert gateway_result['error']['code'] == 'confirmation_required'
        assert 'tool_call_id' not in gateway_result
    finally:
        gateway.close()
        conversation_store.close()
        memory_store.close()
