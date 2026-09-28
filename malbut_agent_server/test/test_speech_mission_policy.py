"""Check structural proposal bounds; language interpretation belongs to the model."""

from types import SimpleNamespace

import pytest

from malbut_agent_server.gateway import (
    PROPOSAL_ONLY, SIMULATION_ONLY, ToolCapability,
    production_registry, simulation_registry,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import AgentDecision, AgentRequest, RobotState, ValidationError
from malbut_agent_server.speech_mission_policy import configure_speech_missions
from malbut_agent_server.tools import SPEECH_MISSION_TOOLS, validate_tool_arguments


def runtime(*, navigation=True, simulation=False):
    result = SimpleNamespace(
        capability_registry=simulation_registry() if simulation else production_registry(),
        safety_policy=SafetyPolicy(),
    )
    return configure_speech_missions(result, navigation_enabled=navigation)


def evaluate(utterance, tool, arguments=None, *, policy=None, ttl=5000):
    rt = policy or runtime()
    request = AgentRequest(
        request_id='request', user_id='speaker', conversation_id='conversation',
        turn_id='turn', utterance=utterance, robot_state=RobotState(),
        available_tools=rt.speech_mission_tools,
    )
    decision = AgentDecision(
        type='tool_call', tool_name=tool, arguments=arguments or {},
        message='모델이 주장한 실행 완료', expires_in_ms=ttl,
    )
    return rt.safety_policy.evaluate(request, decision, state_trusted=False)


@pytest.mark.parametrize('utterance,tool,arguments', [
    ('우리 거실로 가볼까', 'request_navigation', {'location': '거실'}),
    ('주방으로 가 주시겠어요', 'request_navigation', {'location': '주방'}),
    ('거실로 와바라', 'request_navigation', {'location': '거실'}),
    ('주방으로 오너라', 'request_navigation', {'location': '주방'}),
    ('어, 그… 거실로 좀 가주이소', 'request_navigation', {'location': '거실'}),
    # The model may resolve a destination-only answer from prior clarification.
    ('거실', 'request_navigation', {'location': '거실'}),
    ('제 뒤를 따라오실래요', 'request_follow_person', {}),
    ('집안을 좀 꼼꼼히 둘러봐 줘', 'request_patrol', {'thoroughness': 'thorough'}),
    ('집안 한 번 가볍게 돌아주겠니', 'request_patrol', {'thoroughness': 'light'}),
    ('이제 그만 따라오셔도 돼요', 'cancel_voice_mission', {}),
])
def test_structured_model_proposals_are_not_limited_by_utterance_grammar(
    utterance, tool, arguments,
):
    # Decisions are fixture outputs, not evidence of a model's language accuracy.
    result = evaluate(utterance, tool, arguments)
    assert result.allowed
    assert result.code == 'manager_request'


@pytest.mark.parametrize('tool,arguments', [
    ('request_follow_person', {'target_person_id': 'model-selected'}),
    ('cancel_voice_mission', {'request_id': 'somebody-else'}),
    ('request_navigation', {'location': '거실', 'pose': {'x': 2}}),
    ('request_navigation', {'location': 'x' * 129}),
    ('request_navigation', {'location': 3}),
    ('request_patrol', {'thoroughness': 'fast'}),
    ('request_patrol', {'thoroughness': 1}),
    ('request_patrol', {}),
])
def test_strict_schema_rejects_unbounded_or_invented_payload(tool, arguments):
    with pytest.raises(ValidationError):
        validate_tool_arguments(tool, arguments)
    assert evaluate('따라와', tool, arguments).code == 'invalid_arguments'


@pytest.mark.parametrize('ttl', [0, -1, True, 10001, float('inf')])
def test_invalid_ttl_cannot_delegate(ttl):
    assert not evaluate('따라와', 'request_follow_person', ttl=ttl).allowed


@pytest.mark.parametrize('registry_factory', [production_registry, simulation_registry])
def test_non_speech_registries_disable_every_mission_tool(registry_factory):
    registry = registry_factory()
    assert registry.effective_names(SPEECH_MISSION_TOOLS) == []
    for name in SPEECH_MISSION_TOOLS:
        entry = registry.get(name)
        assert not entry.available
        assert entry.mode == PROPOSAL_ONLY
        assert entry.adapter is None
        with pytest.raises(ValueError, match='through Manager'):
            ToolCapability(name=name, mode=SIMULATION_ONLY)
    assert len(registry.to_dict()['capabilities']) > len(SPEECH_MISSION_TOOLS)


def test_navigation_opt_in_and_runtime_configuration_are_isolated():
    first = runtime(navigation=False)
    second = runtime()
    assert 'request_navigation' not in first.speech_mission_tools
    assert 'request_navigation' in second.speech_mission_tools
    assert first.capability_registry.get('get_weather').available
    assert evaluate('거실로 가', 'request_navigation', {'location': '거실'},
                    policy=first).code == 'tool_unavailable'
    configure_speech_missions(second, navigation_enabled=False)
    assert 'request_navigation' not in second.speech_mission_tools


def test_legacy_navigate_still_requires_trusted_state():
    rt = runtime()
    request = AgentRequest(
        request_id='request', user_id='speaker', conversation_id='conversation',
        turn_id='turn', utterance='거실로 가', robot_state=RobotState(),
        available_tools=('navigate',),
    )
    decision = AgentDecision(type='tool_call', tool_name='navigate',
                             arguments={'location': '거실'}, message='proposal')
    result = rt.safety_policy.evaluate(request, decision, state_trusted=False)
    assert result.code == 'untrusted_robot_state'
    assert not result.allowed


def test_unavailable_request_subset_cannot_be_overridden_by_model():
    rt = runtime()
    request = AgentRequest(
        request_id='request', user_id='speaker', conversation_id='conversation',
        turn_id='turn', utterance='따라와', robot_state=RobotState(), available_tools=(),
    )
    decision = AgentDecision(type='tool_call', tool_name='request_follow_person',
                             arguments={}, message='proposal')
    assert rt.safety_policy.evaluate(request, decision).code == 'tool_unavailable'


def test_real_orchestrator_commits_proposal_without_fabricating_state_or_execution():
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator
    from malbut_agent_server.schemas import ProviderResult

    class Provider:
        calls = 0

        def complete(self, request, memories, history, tools, conversation_summary=None):
            self.calls += 1
            assert 'request_follow_person' in {tool.name for tool in tools}
            return ProviderResult(
                decision=AgentDecision(
                    type='tool_call', tool_name='request_follow_person',
                    arguments={}, message='Manager에 따라가기를 요청할게요.',
                ),
                provider='test', model='test', latency_ms=0.0,
            )

    rt = build_orchestrator(Settings(database_path=':memory:'))
    try:
        configure_speech_missions(rt)
        provider = Provider()
        rt.provider = provider
        session = rt.conversation_store.create('speaker')
        request = AgentRequest(
            request_id='request', user_id='speaker',
            conversation_id=session.conversation_id, turn_id='turn', utterance='따라와',
            robot_state=RobotState(), available_tools=rt.speech_mission_tools,
        )
        result = rt.handle(request)
        assert result.decision.type == 'tool_call'
        assert result.safety.code == 'manager_request'
        assert not result.state_trusted
        assert not result.to_dict()['execution']['authorized']
        assert not result.to_dict()['execution']['proposal_authorized']
        cached = rt.handle(request)
        assert cached.decision_id == result.decision_id
        assert provider.calls == 1
        stored = rt.conversation_store.snapshot('speaker', session.conversation_id)
        assert len(stored.turns) == 1
    finally:
        rt.close()
