"""Offline answer-only and automatic-extraction provider contract tests."""

import copy
import json

import pytest

from malbut_agent_server.automatic_memory_extractor import (
    ANSWER_ONLY_INSTRUCTIONS, AutomaticMemoryExtractor,
)
from malbut_agent_server.memory_contract import (
    MEMORY_INSTRUCTIONS, MEMORY_PROPOSAL_SCHEMA,
)
from malbut_agent_server.providers.base import AgentProvider, ProviderError
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider, TEXT_DECISION_SCHEMA,
)
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult, ProviderUsage, RobotState,
)
from malbut_agent_server.tools import TOOL_SPECS


def request():
    """Provide state and tools which the extractor must never forward."""
    return AgentRequest(
        request_id='original-request', user_id='private-user',
        conversation_id='conversation', turn_id='original-turn',
        utterance='난 김민재야',
        robot_state=RobotState(camera_available=True),
        available_tools=('navigate',),
    )


def proposal():
    """Describe a candidate without simulating semantic acceptance."""
    return {
        'operation': 'remember', 'facts': [{
            'kind': 'name', 'subject': 'user', 'attribute': 'name',
            'value': '김민재', 'evidence': request().utterance,
        }], 'target_ids': [], 'query': '', 'evidence': request().utterance,
    }


def response(value):
    """Supply a fixed wire response with known, independently checked usage."""
    return {
        'status': 'completed', 'model': 'offline-model', 'id': 'response-id',
        'usage': {'input_tokens': 20, 'output_tokens': 5, 'total_tokens': 25},
        'output': [{'type': 'message', 'content': [{
            'type': 'output_text', 'text': json.dumps(value),
        }]}],
    }


class FixtureProvider(AgentProvider):
    """Record bounded extraction inputs and return a test-owned result."""

    supports_memory = True

    def __init__(self):
        self.calls = []
        self.result = ProviderResult(
            decision=AgentDecision(type='message', message='internal-only'),
            provider='fixture', model='fixture-model', latency_ms=12,
            usage=ProviderUsage(20, 5, 25), memory_supported=True,
            memory_proposal=proposal(),
        )

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, memory_context=None):
        self.calls.append((request, memories, conversation_turns, tools,
                           conversation_summary, memory_context))
        return self.result


def test_answer_only_keeps_context_but_excludes_extraction_schema_and_prompt():
    calls = []
    context = {
        'mode': 'answer_only', 'enabled': True, 'pending_question': None,
        'memories': [{'content': 'untrusted-memory-marker'}],
    }
    original = copy.deepcopy(context)

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        return response({
            'type': 'message', 'message': '반가워요!',
            'reason': 'greeting', 'confidence': 1,
        })

    provider = OpenAIResponsesProvider(
        'offline-key', 'offline-model', transport=transport,
    )
    result = provider.complete(request(), [], [], [], memory_context=context)
    result.validate()
    payload = calls[0]
    assert len(calls) == 1 and context == original
    assert payload['text']['format']['schema'] == TEXT_DECISION_SCHEMA
    assert MEMORY_INSTRUCTIONS not in payload['instructions']
    assert ANSWER_ONLY_INSTRUCTIONS in payload['instructions']
    assert 'untrusted-memory-marker' not in payload['instructions']
    data = json.loads(payload['input'].split('\n', 1)[1])
    assert data['memory_management_context'] == original
    assert data['current_user_utterance'] == request().utterance
    assert result.memory_proposal is None and not result.memory_supported
    assert result.usage == ProviderUsage(20, 5, 25)


def test_answer_only_rejects_a_hidden_memory_proposal():
    value = {'type': 'message', 'message': '반가워요!', 'reason': 'greeting',
             'confidence': 1, 'memory_proposal': proposal()}
    provider = OpenAIResponsesProvider(
        'offline-key', 'offline-model', transport=lambda *_: response(value),
    )
    with pytest.raises(ProviderError):
        provider.complete(request(), [], [], [],
                          memory_context={'mode': 'answer_only'})


def test_answer_only_preserves_legitimate_tool_exposure_and_parsing():
    calls = []
    source = AgentRequest.from_dict(dict(
        request().to_dict(), utterance='거실로 가줘',
    ))

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        return {
            'status': 'completed', 'model': 'offline-model',
            'output': [{'type': 'function_call', 'name': 'navigate',
                        'arguments': json.dumps({'location': '거실'})}],
        }

    backend = OpenAIResponsesProvider(
        'offline-key', 'offline-model', transport=transport,
    )
    result = backend.complete(
        source, [], [], [TOOL_SPECS['navigate']],
        memory_context={'mode': 'answer_only'},
    )
    result.validate()
    assert result.decision.type == 'tool_call'
    assert result.decision.tool_name == 'navigate'
    assert result.decision.arguments == {'location': '거실'}
    assert result.memory_proposal is None and not result.memory_supported
    assert calls[0]['tools'] == [TOOL_SPECS['navigate'].to_openai_dict()]
    assert calls[0]['tool_choice'] == 'auto'
    assert '로봇 요청을 일반 대화로 바꾸지 않습니다' in (
        calls[0]['instructions']
    )


@pytest.mark.parametrize('candidate', [None, proposal()])
def test_extraction_schema_preserves_usage_and_no_tool_authority(candidate):
    calls = []

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        return response({'memory_proposal': candidate})

    backend = OpenAIResponsesProvider(
        'offline-key', 'offline-model', transport=transport,
    )
    result = AutomaticMemoryExtractor(backend).extract(request())
    result.validate()
    assert result.decision.type == 'message'
    assert result.decision.tool_name is None
    assert result.decision.arguments == {}
    assert result.memory_supported and result.memory_proposal == candidate
    assert result.usage == ProviderUsage(20, 5, 25)
    assert result.response_id == 'response-id'
    assert result.latency_ms >= 0 and result.context_metrics is not None
    assert len(calls) == 1
    payload = calls[0]
    assert 'tools' not in payload and 'tool_choice' not in payload
    schema = payload['text']['format']['schema']
    assert schema['required'] == ['memory_proposal']
    assert set(schema['properties']) == {'memory_proposal'}
    candidate_schema = schema['properties']['memory_proposal']['anyOf'][0]
    assert candidate_schema['properties']['operation']['enum'] == ['remember']
    assert len(MEMORY_PROPOSAL_SCHEMA['properties']['operation']['enum']) > 1
    data = json.loads(payload['input'].split('\n', 1)[1])
    assert data['current_user_utterance'] == request().utterance
    assert data['conversation_history_untrusted'] == []
    assert data['conversation_summary_untrusted'] is None
    assert data['memory_context_untrusted'] == []
    assert data['available_tools'] == []
    assert data['memory_management_context']['mode'] == 'automatic_extraction'
    assert 'private-user' not in payload['input']
    assert 'original-request' not in payload['input']


def test_extractor_isolates_request_and_does_not_alias_provider_result():
    backend = FixtureProvider()
    original = request()
    result = AutomaticMemoryExtractor(backend).extract(original)
    bounded, memories, turns, tools, summary, context = backend.calls[0]
    assert bounded.request_id != original.request_id
    assert bounded.turn_id != original.turn_id
    assert bounded.user_id == original.user_id
    assert bounded.utterance == original.utterance
    assert bounded.robot_state == RobotState()
    assert bounded.available_tools == ()
    assert memories == turns == tools == [] and summary is None
    assert context['enabled'] is True
    assert context['memories'] == [] and context['pending_question'] is None
    assert original.available_tools == ('navigate',)
    result.memory_proposal['facts'][0]['value'] = 'changed'
    assert backend.result.memory_proposal['facts'][0]['value'] == '김민재'
    assert result.usage == backend.result.usage


def test_extraction_preserves_unknown_usage_and_ignores_reasoning_envelope():
    wire = response({'memory_proposal': None})
    del wire['usage']
    wire['output'].insert(0, {'type': 'reasoning', 'summary': []})
    backend = OpenAIResponsesProvider(
        'offline-key', 'offline-model', transport=lambda *_: wire,
    )
    result = AutomaticMemoryExtractor(backend).extract(request())
    assert result.usage == ProviderUsage()
    assert result.memory_proposal is None


@pytest.mark.parametrize('mutation', [
    'operation', 'targets', 'query', 'kind', 'extra', 'evidence',
    'fact_evidence', 'unsupported', 'tool', 'refusal', 'invalid_usage',
])
def test_extractor_rejects_malformed_or_nonautomatic_results(mutation):
    backend = FixtureProvider()
    candidate = backend.result.memory_proposal
    if mutation == 'operation':
        candidate['operation'] = 'forget'
    elif mutation == 'targets':
        candidate['target_ids'] = ['target']
    elif mutation == 'query':
        candidate['query'] = '기억'
    elif mutation == 'kind':
        candidate['facts'][0]['kind'] = 'password'
    elif mutation == 'extra':
        candidate['facts'][0]['authority'] = True
    elif mutation == 'evidence':
        candidate['evidence'] = 'not in source'
    elif mutation == 'fact_evidence':
        candidate['facts'][0]['evidence'] = 'not in source'
    elif mutation == 'unsupported':
        backend.result.memory_supported = False
    elif mutation == 'tool':
        backend.result.memory_proposal = None
        backend.result.decision = AgentDecision(
            type='tool_call', message='no', tool_name='navigate',
        )
    elif mutation == 'refusal':
        backend.result.decision.type = 'refusal'
    else:
        backend.result.usage = ProviderUsage(-1, 5, 4)
    with pytest.raises(ProviderError):
        AutomaticMemoryExtractor(backend).extract(request())


@pytest.mark.parametrize('mutation', [
    'multiple_messages', 'multiple_parts', 'function_call', 'other_tool',
    'refusal', 'incomplete', 'not_json', 'nan', 'missing', 'extra',
    'operation',
])
def test_extraction_adapter_rejects_malformed_wire_output(mutation):
    wire = response({'memory_proposal': proposal()})
    if mutation == 'multiple_messages':
        wire['output'] *= 2
    elif mutation == 'multiple_parts':
        wire['output'][0]['content'] *= 2
    elif mutation in {'function_call', 'other_tool'}:
        wire['output'].append({
            'type': 'function_call' if mutation == 'function_call' else
                    'web_search_call', 'name': 'navigate', 'arguments': '{}',
        })
    elif mutation == 'refusal':
        wire['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'no'}]
    elif mutation == 'incomplete':
        wire['status'] = 'incomplete'
    else:
        text = wire['output'][0]['content'][0]
        if mutation == 'not_json':
            text['text'] = '{'
        elif mutation == 'nan':
            text['text'] = '{"memory_proposal":NaN}'
        else:
            value = json.loads(text['text'])
            if mutation == 'missing':
                del value['memory_proposal']
            elif mutation == 'extra':
                value['message'] = 'unexpected answer'
            else:
                value['memory_proposal']['operation'] = 'correct'
            text['text'] = json.dumps(value)
    backend = OpenAIResponsesProvider(
        'offline-key', 'offline-model', transport=lambda *_: wire,
    )
    with pytest.raises(ProviderError):
        AutomaticMemoryExtractor(backend).extract(request())


def test_extractor_rejects_unsupported_provider_without_calling_it():
    backend = FixtureProvider()
    backend.supports_memory = False
    with pytest.raises(ProviderError):
        AutomaticMemoryExtractor(backend).extract(request())
    assert backend.calls == []


def test_extraction_adapter_rejects_tool_context_before_transport():
    calls = []
    backend = OpenAIResponsesProvider(
        'offline-key', 'offline-model',
        transport=lambda *args: calls.append(args),
    )
    with pytest.raises(ValueError, match='isolated context'):
        backend.complete(request(), [], [], [],
                         memory_context={'mode': 'automatic_extraction'})
    assert calls == []
