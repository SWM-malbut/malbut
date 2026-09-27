"""Manager delegation instructions depend on actual provider tools, never input claims."""

import json

import pytest

from malbut_agent_server.automatic_memory_extractor import AUTOMATIC_EXTRACTION_INSTRUCTIONS
from malbut_agent_server.memory_contract import MEMORY_INSTRUCTIONS
from malbut_agent_server.prompting import (
    CONVERSATION_INSTRUCTIONS, SYSTEM_INSTRUCTIONS, prepare_model_input,
    system_instructions_for_tools,
)
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.providers.reliable import ContextBudgetExceeded
from malbut_agent_server.schemas import RobotState, SpeechAgentRequest
from malbut_agent_server.tools import SPEECH_MISSION_TOOLS, select_tool_specs


def request(available=SPEECH_MISSION_TOOLS):
    return SpeechAgentRequest(
        request_id='voice-request', user_id='speaker', conversation_id='conversation',
        turn_id='turn', utterance='따라와', robot_state=RobotState(),
        available_tools=available,
    )


@pytest.mark.parametrize('name', SPEECH_MISSION_TOOLS)
def test_supplied_manager_tool_gets_specific_delegation_without_claiming_ready(name):
    tools = select_tool_specs([name])
    provider = OpenAIResponsesProvider('offline-key', 'offline-model')
    original = request((name,))
    payload = provider.build_payload(original, [], [], tools)
    instructions = payload['instructions']
    assert instructions.startswith(SYSTEM_INSTRUCTIONS + '\n\n')
    assert instructions.endswith(CONVERSATION_INSTRUCTIONS)
    extension = system_instructions_for_tools(tools)[len(SYSTEM_INSTRUCTIONS):]
    assert name in extension
    assert not any(other in extension for other in SPEECH_MISSION_TOOLS if other != name)
    assert 'Manager와 하위 서버의 관측·검증' in extension
    assert '다른 직접 제어 도구의 상태·배터리' in extension
    assert '현재 발화가 한 작업의 직접 요청' in extension
    assert 'Manager의 확인 전 접수·진행·완료' in extension
    data = json.loads(payload['input'].split('\n', 1)[1])
    assert data['robot_state_untrusted'] == original.robot_state.to_dict()
    assert not data['robot_state_untrusted']['navigation_available']
    assert not data['robot_state_untrusted']['camera_available']
    assert [tool['name'] for tool in payload['tools']] == [name]
    if name == 'request_navigation':
        assert '거실로 가볼까?' in extension
        assert '다시 확인하지 말고 제공된 이동 도구' in extension
        assert '가능 여부만 묻는 말은 실행 요청이 아닙니다' in extension
        assert '거실로 가볼까?' in payload['tools'][0]['description']
    else:
        assert '거실로 가볼까?' not in extension


@pytest.mark.parametrize('exposed', [(), ('navigate',), ('follow_user',), ('get_weather',)])
def test_request_claims_cannot_enable_delegation_and_other_paths_stay_identical(exposed):
    provider = OpenAIResponsesProvider('offline-key', 'offline-model')
    payload = provider.build_payload(request(), [], [], select_tool_specs(exposed))
    assert payload['instructions'] == SYSTEM_INSTRUCTIONS + '\n\n' + CONVERSATION_INSTRUCTIONS
    prepared = prepare_model_input(request(), [], max_model_input_chars=4096)
    assert prepared.metrics.model_input_chars <= 4096


def test_automatic_extraction_keeps_its_separate_instructions_and_rejects_tools():
    provider = OpenAIResponsesProvider('offline-key', 'offline-model')
    context = {'mode': 'automatic_extraction'}
    payload = provider.build_payload(request(()), [], [], [], memory_context=context)
    assert payload['instructions'] == (
        MEMORY_INSTRUCTIONS + '\n\n' + AUTOMATIC_EXTRACTION_INSTRUCTIONS)
    with pytest.raises(ValueError, match='isolated context'):
        provider.build_payload(request(()), [], [], select_tool_specs(SPEECH_MISSION_TOOLS),
                               memory_context=context)


def test_semantic_token_budget_includes_conditional_instructions_before_transport():
    counted, sent = [], []
    tools = select_tool_specs(SPEECH_MISSION_TOOLS)

    def counter(payload):
        counted.append(payload)
        return 1025

    provider = OpenAIResponsesProvider(
        'offline-key', 'offline-model', semantic_context=True, max_input_tokens=2048,
        max_model_input_chars=4096, token_counter=counter,
        transport=lambda *args: sent.append(args),
    )
    with pytest.raises(ContextBudgetExceeded):
        provider.complete(request(), [], [], tools)
    assert not sent
    assert len(counted) == 1
    assert counted[0]['instructions'] == (
        system_instructions_for_tools(tools) + '\n\n' + CONVERSATION_INSTRUCTIONS)
    assert counted[0]['tools'] == [tool.to_openai_dict() for tool in tools]
