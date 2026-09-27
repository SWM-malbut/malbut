"""Voice delegates explicit requests without weakening the legacy state gate."""

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
    ('제이크야 나 따라와', 'request_follow_person', {}),
    ('따라와 줘', 'request_follow_person', {}),
    ('나 좀 따라와', 'request_follow_person', {}),
    ('보이는 사람을 따라와 주세요', 'request_follow_person', {}),
    ('please follow me', 'request_follow_person', {}),
    ('거실로 가', 'request_navigation', {'location': '거실'}),
    ('제이크 지금 주방으로 가 줘!', 'request_navigation', {'location': '주방'}),
    ('거실로 이동해', 'request_navigation', {'location': 'living_room'}),
    ('서재로 이동해 주세요', 'request_navigation', {'location': '서재'}),
    ('please go to kitchen', 'request_navigation', {'location': 'kitchen'}),
    ('순찰해줘', 'request_patrol', {'thoroughness': 'normal'}),
    ('순찰 시작해', 'request_patrol', {'thoroughness': 'normal'}),
    ('집안을 꼼꼼히 순찰해 줘', 'request_patrol', {'thoroughness': 'thorough'}),
    ('가볍게 순찰해', 'request_patrol', {'thoroughness': 'light'}),
    ('start a thorough patrol', 'request_patrol', {'thoroughness': 'thorough'}),
    ('멈춰', 'cancel_voice_mission', {}),
    ('취소해 줘', 'cancel_voice_mission', {}),
    ('진행 중인 순찰을 취소해', 'cancel_voice_mission', {}),
    ('그만 따라와', 'cancel_voice_mission', {}),
    ('따라오지 마', 'cancel_voice_mission', {}),
    ('stop following', 'cancel_voice_mission', {}),
])
def test_direct_intent_allows_only_manager_delegation(utterance, tool, arguments):
    result = evaluate(utterance, tool, arguments)
    assert result.allowed
    assert result.code == 'manager_request'


@pytest.mark.parametrize('utterance,tool,arguments', [
    ('"따라와"라고 말했어', 'request_follow_person', {}),
    ('나 따라와라고 말해줘', 'request_follow_person', {}),
    ('따라와라는 문장을 번역해', 'request_follow_person', {}),
    ('따라올 수 있어?', 'request_follow_person', {}),
    ('따라오지 마', 'request_follow_person', {}),
    ('안 따라왔으면 좋겠어', 'request_follow_person', {}),
    ('나중에 따라와', 'request_follow_person', {}),
    ('만약 내가 가면 따라와', 'request_follow_person', {}),
    ('엄마를 따라와', 'request_follow_person', {}),
    ('저기 가', 'request_navigation', {'location': '거실'}),
    ('거실로 가', 'request_navigation', {'location': '주방'}),
    ('거실로 가라고 했어', 'request_navigation', {'location': '거실'}),
    ('거실로 가지 마', 'request_navigation', {'location': '거실'}),
    ('거실로 가고 따라와', 'request_navigation', {'location': '거실'}),
    ('거실과 주방으로 가', 'request_navigation', {'location': '주방'}),
    ('따라와 그리고 순찰해', 'request_patrol', {'thoroughness': 'normal'}),
    ('순찰하고 따라와', 'request_follow_person', {}),
    ('내일 순찰해', 'request_patrol', {'thoroughness': 'normal'}),
    ('순찰하지 마', 'request_patrol', {'thoroughness': 'normal'}),
    ('꼼꼼히 순찰해', 'request_patrol', {'thoroughness': 'light'}),
    ('순찰해', 'request_patrol', {'thoroughness': 'thorough'}),
    ('순찰을 취소하지 마', 'cancel_voice_mission', {}),
    ('날씨 조회를 취소해', 'cancel_voice_mission', {}),
    ('상황 대응을 취소해', 'cancel_voice_mission', {}),
    ('멈춰라고 말해', 'cancel_voice_mission', {}),
    ('취소하고 주방으로 가', 'cancel_voice_mission', {}),
    ('do not follow me', 'request_follow_person', {}),
    ('if you can follow me', 'request_follow_person', {}),
])
def test_quoted_negated_hypothetical_and_multiple_tasks_do_not_delegate(
    utterance, tool, arguments,
):
    result = evaluate(utterance, tool, arguments)
    assert not result.allowed
    assert result.code == 'current_turn_intent_missing'


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
