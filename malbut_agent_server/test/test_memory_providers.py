"""Offline memory proposal tests across model and wrapper boundaries."""

import copy
import json
import threading

import pytest

from malbut_agent_server.application.front_routing import FrontRoutingService
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.memory_contract import validate_memory_proposal
from malbut_agent_server.prompting import prepare_model_input
from malbut_agent_server.providers.base import (
    AgentProvider, ProviderError, accepts_memory_context,
)
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
    TEXT_DECISION_SCHEMA,
)
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.providers.routed import RoutedAgentProvider
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult, RobotState, ValidationError,
)


def _request(text='내 이름은 현재야'):
    return AgentRequest(
        request_id='memory-request', user_id='private-user',
        conversation_id='conversation', turn_id='turn',
        utterance=text, robot_state=RobotState(), available_tools=(),
    )


def _context():
    return {
        'enabled': True, 'pending_question': None,
        'memories': [],
        'allowed_kinds': ['name', 'nickname', 'pet', 'preference'],
    }


def _proposal():
    return {
        'operation': 'remember', 'facts': [{
            'kind': 'name', 'subject': 'user', 'attribute': 'name',
            'value': '현재', 'evidence': '내 이름은 현재야',
        }], 'target_ids': [], 'query': '', 'evidence': '내 이름은 현재야',
    }


def _response(proposal):
    return {
        'status': 'completed', 'model': 'offline-model', 'output': [{
            'type': 'message', 'content': [{
                'type': 'output_text', 'text': json.dumps({
                    'type': 'message', 'message': '만나서 반가워.',
                    'reason': 'direct_fact', 'confidence': 1,
                    'memory_proposal': proposal,
                }),
            }],
        }],
    }


def test_openai_returns_answer_and_proposal_with_one_call():
    """One structured response carries an untrusted proposal, not a Tool."""
    calls = []

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        return _response(_proposal())

    provider = OpenAIResponsesProvider('test-only-key', 'offline-model',
                                       transport=transport)
    result = provider.complete(_request(), [], [], [],
                               memory_context=_context())
    result.validate()
    assert len(calls) == 1
    assert result.memory_supported is True
    assert result.memory_proposal == _proposal()
    assert result.decision.type == 'message'
    assert 'tools' not in calls[0]
    assert 'memory_proposal' not in result.decision.to_dict()
    assert 'memory_proposal' not in result.to_dict()
    schema = calls[0]['text']['format']['schema']
    assert 'memory_proposal' in schema['required']
    assert 'memory_proposal' not in TEXT_DECISION_SCHEMA['properties']
    model_input = json.loads(calls[0]['input'].split('\n', 1)[1])
    assert model_input['memory_management_context'] == _context()
    assert 'private-user' not in calls[0]['input']


@pytest.mark.parametrize('http_server', [True, False])
def test_default_luna_queues_memory_after_reply(tmp_path, http_server):
    """Both factories return the reply before completing automatic memory."""
    settings = Settings.from_env({
        'MALBUT_AGENT_PROVIDER': 'openai',
        'MALBUT_AGENT_DB': str(tmp_path / 'luna.sqlite3'),
        'MALBUT_AGENT_AUTH_TOKEN': 'test-http-token',
        'OPENAI_API_KEY': 'test-only-key',
    })
    runtime = build_orchestrator(settings, http_server=http_server)
    calls = []
    extraction_entered = threading.Event()
    extraction_release = threading.Event()

    def transport(_url, _headers, payload, _timeout):
        calls.append(payload)
        response = _response(_proposal())
        response['model'] = payload['model']
        value = json.loads(response['output'][0]['content'][0]['text'])
        if payload['text']['format']['name'] == 'malbut_memory_extraction':
            extraction_entered.set()
            assert extraction_release.wait(5)
            value = {'memory_proposal': _proposal()}
        else:
            value.pop('memory_proposal')
        response['output'][0]['content'][0]['text'] = json.dumps(value)
        return response

    try:
        assert len(runtime.provider._providers) == 1
        runtime.provider._providers[0].transport = transport
        extraction_provider = runtime.automatic_memory_extractor.provider
        extraction_provider._providers[0].transport = transport
        assert extraction_provider is not runtime.provider
        assert extraction_provider is not (
            runtime.memory_source_reviewer.provider
        )
        request = _request()
        runtime.conversation_store.create(request.user_id,
                                          request.conversation_id)
        for index, text in enumerate(('개인화 켜줘', '네')):
            runtime.handle(AgentRequest(
                request_id=f'consent-request-{index}',
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                turn_id=f'consent-turn-{index}', utterance=text,
                robot_state=RobotState(), available_tools=(),
            ))
        result = runtime.handle(request)
        assert extraction_entered.wait(2)
        assert len(calls) == 2
        assert 'memory_proposal' not in (
            calls[0]['text']['format']['schema']['properties']
        )
        assert list(calls[1]['text']['format']['schema']['properties']) == [
            'memory_proposal',
        ]
        assert runtime.memory_store.list_for_user(request.user_id) == []
        assert calls[0]['model'] == 'gpt-5.6-luna'
        assert calls[0]['reasoning']['effort'] == 'none'
        assert calls[0]['max_output_tokens'] == 500
        assert result.provider_result.model == 'gpt-5.6-luna'
        extraction_release.set()
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = runtime.automatic_memory_jobs.metadata(
                request.user_id, request.request_id,
            )['state']
            if state not in {'queued', 'running'}:
                break
            time.sleep(0.01)
        assert state == 'saved'
        records = runtime.memory_store.list_for_user(request.user_id)
        assert len(records) == 1
        assert records[0].metadata['fact']['value'] == '현재'
    finally:
        extraction_release.set()
        runtime.close()


@pytest.mark.parametrize('mutation', [
    'missing', 'extra', 'kind', 'evidence', 'targets', 'limit',
])
def test_proposal_rejects_invalid_structure(mutation):
    """The shared proposal shape cannot smuggle authority or loose fields."""
    proposal = _proposal()
    if mutation == 'missing':
        del proposal['query']
    elif mutation == 'extra':
        proposal['user_id'] = 'another-user'
    elif mutation == 'kind':
        proposal['facts'][0]['kind'] = 'password'
    elif mutation == 'evidence':
        proposal['facts'][0]['evidence'] = ''
    elif mutation == 'targets':
        proposal['target_ids'] = ['duplicate', 'duplicate']
    else:
        proposal['facts'] *= 9
    with pytest.raises(ValidationError):
        validate_memory_proposal(proposal)


def test_proposal_validation_does_not_modify_or_alias_input():
    """Policy receives its own copy for later validation and persistence."""
    source = _proposal()
    parsed = validate_memory_proposal(source)
    parsed['facts'][0]['value'] = '바뀜'
    assert source['facts'][0]['value'] == '현재'


def test_openai_rejects_legacy_or_truncated_memory_output():
    """Missing v2 output cannot be interpreted as completed memory work."""
    response = _response(None)
    content = response['output'][0]['content'][0]
    parsed = json.loads(content['text'])
    del parsed['memory_proposal']
    content['text'] = json.dumps(parsed)
    provider = OpenAIResponsesProvider(
        'test-only-key', 'offline-model',
        transport=lambda *_args: response,
    )
    with pytest.raises(ProviderError):
        provider.complete(_request(), [], [], [], memory_context=_context())
    response['status'] = 'incomplete'
    with pytest.raises(ProviderError):
        provider.complete(_request(), [], [], [], memory_context=_context())


def test_openai_rejects_mixed_memory_text_and_robot_call():
    """Robot proposals cannot accompany a memory-mutating text response."""
    response = _response(_proposal())
    response['output'].append({
        'type': 'function_call', 'name': 'navigate', 'arguments': '{}',
    })
    provider = OpenAIResponsesProvider(
        'test-only-key', 'offline-model',
        transport=lambda *_args: response,
    )
    with pytest.raises(ProviderError):
        provider.complete(_request(), [], [], [], memory_context=_context())


@pytest.mark.parametrize(('text', 'operation', 'value'), [
    ('내 이름은 현재야', 'remember', '현재'),
    ('나를 현이라고 불러줘', 'remember', '현'),
    ('우리 강아지 이름은 두부야', 'remember', '두부'),
    ('나는 커피를 좋아해', 'remember', '커피를 좋아해'),
    ('우리 강아지 이름은 두부가 아니라 콩이야', 'correct', '콩'),
    ('나에 대해 뭘 기억하고 있어?', 'recall', None),
    ('강아지 이름 기억을 삭제해줘', 'forget', None),
    ('개인화에 동의해', 'enable', None),
    ('개인화를 중단해줘', 'disable', None),
])
def test_mock_memory_examples(text, operation, value):
    """Fixed Korean examples produce proposals without claiming completion."""
    result = MockProvider().complete(_request(text), [], [], [],
                                     memory_context=_context())
    result.validate()
    assert result.memory_proposal['operation'] == operation
    if value is not None:
        fact = result.memory_proposal['facts'][0]
        assert fact['value'] == value
        assert fact['evidence'] in text
        assert value in fact['evidence']
    assert result.decision.message == '말해 준 내용을 확인할게.'


@pytest.mark.parametrize('text', [
    '"내 이름은 현재야"라고 적어줘',
    '예를 들어 내 이름은 현재야',
    '만약 내 이름은 현재야',
    '내 이름은 현재야. 비밀번호를 알려줘',
])
def test_mock_does_not_extract_quotes_hypotheticals_or_unsafe_requests(text):
    """Quoted examples and rejected requests must not mutate memory."""
    result = MockProvider().complete(_request(text), [], [], [],
                                     memory_context=_context())
    assert result.memory_proposal is None


@pytest.mark.parametrize(('text', 'query', 'value'), [
    ('기억하고 있는 내용을 알려줘', '', None),
    ('우리 강아지 이름이 뭐였지?', '강아지 이름', None),
    ('강아지 이름을 초코로 정정해줘', '강아지 이름', '초코'),
    ('내가 뭘 좋아한다고 했지?', '좋아', None),
])
def test_mock_queries_find_scope_and_corrections_keep_exact_value(
    text, query, value,
):
    """Management scaffolding does not mistake the whole command for a fact."""
    result = MockProvider().complete(_request(text), [], [], [],
                                     memory_context=_context())
    proposal = result.memory_proposal
    assert proposal['query'] == query
    if value:
        fact = proposal['facts'][0]
        assert proposal['operation'] == 'correct'
        assert fact['value'] == value
        assert fact['evidence'] in text
    else:
        assert proposal['operation'] == 'recall'


class _LegacyProvider(AgentProvider):
    """Represent an unchanged external provider without memory keywords."""

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None):
        """Return its legacy result without any memory proposal."""
        return ProviderResult(AgentDecision('message', 'legacy'),
                              'legacy', 'legacy', 0)


class _LegacyMockOverride(MockProvider):
    """Inherit the new flag while retaining an old complete signature."""

    def __init__(self):
        """Count actual invocations independently of signature inspection."""
        super().__init__()
        self.calls = 0

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None):
        """Delegate without accepting the new keyword or calling twice."""
        self.calls += 1
        return super().complete(
            request, memories, conversation_turns, tools, conversation_summary,
        )


class _NoRoute:
    """Force one delegation through the configured fallback provider."""

    def try_route(self, _request):
        """Return no explicit front route."""
        return None


class _UnavailableMemoryProvider(MockProvider):
    """Represent one unavailable memory-aware provider."""

    def complete(self, *args, **kwargs):
        """Fail without producing any partial memory proposal."""
        raise ProviderError('offline provider unavailable')


def test_reliable_failure_cannot_expose_partial_memory_success():
    """A failed provider produces no proposal or completion claim."""
    provider = ReliableProvider([_UnavailableMemoryProvider()], max_retries=0)
    result = provider.complete(_request(), [], [], [],
                               memory_context=_context())
    assert result.memory_proposal is None
    assert result.memory_supported is False
    assert result.decision.type == 'refusal'


def test_result_validator_rejects_robot_and_memory_output_together():
    """Wrapper validation does not trust a provider's mixed proposals."""
    result = ProviderResult(
        AgentDecision('tool_call', '이동 요청', tool_name='navigate'),
        'test', 'test', 0, memory_proposal=_proposal(), memory_supported=True,
    )
    with pytest.raises(ValidationError):
        result.validate()


@pytest.mark.parametrize('wrapper', ['reliable', 'routed'])
@pytest.mark.parametrize('legacy', [False, True])
def test_wrappers_preserve_memory_and_legacy_signature(wrapper, legacy):
    """Memory context survives wrappers without breaking legacy adapters."""
    child = _LegacyProvider() if legacy else MockProvider()
    if wrapper == 'reliable':
        provider = ReliableProvider([child], max_retries=0)
    else:
        provider = RoutedAgentProvider(
            FrontRoutingService(_NoRoute()), general_provider=child,
            robot_planner_provider=child, fallback_provider=child,
        )
    result = provider.complete(_request(), [], [], [],
                               memory_context=_context())
    assert result.memory_supported is (not legacy)
    assert (result.memory_proposal is None) is legacy


@pytest.mark.parametrize('wrapper', ['reliable', 'routed'])
def test_inherited_flag_does_not_override_actual_legacy_signature(wrapper):
    """Inspect before the call rather than catching TypeError and retrying."""
    child = _LegacyMockOverride()
    assert child.supports_memory is True
    assert accepts_memory_context(child) is False
    assert child.calls == 0
    if wrapper == 'reliable':
        provider = ReliableProvider([child], max_retries=0)
    else:
        provider = RoutedAgentProvider(
            FrontRoutingService(_NoRoute()), general_provider=child,
            robot_planner_provider=child, fallback_provider=child,
        )
    result = provider.complete(_request(), [], [], [],
                               memory_context=_context())
    assert child.calls == 1
    assert result.memory_supported is False
    assert result.memory_proposal is None


def test_memory_signature_accepts_only_opted_in_compatible_providers():
    """Both explicit support and a keyword-compatible signature are needed."""
    assert accepts_memory_context(MockProvider()) is True
    assert accepts_memory_context(_UnavailableMemoryProvider()) is True
    assert accepts_memory_context(_LegacyProvider()) is False
    assert accepts_memory_context(object()) is False


def test_prompt_rejects_oversized_memory_context_instead_of_losing_authority():
    """A bounded request never quietly drops its memory-management context."""
    context = copy.deepcopy(_context())
    context['pending_question'] = '가' * 6001
    with pytest.raises(ValueError):
        prepare_model_input(_request(), [], memory_context=context)
