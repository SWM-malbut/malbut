"""Cloud queries stay in the Agent; no robot preparation or live HTTP."""

from contextlib import contextmanager
import copy
from dataclasses import replace
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator, build_provider
from malbut_agent_server.homecam_query import (
    _NoRedirect, HomecamQueryClient, configure_homecam_queries,
)
from malbut_agent_server.schemas import AgentDecision, AgentRequest, ProviderResult, RobotState, SpeechAgentRequest
from malbut_agent_server.tools import HOMECAM_QUERY_TOOLS, TOOL_SPECS


@pytest.fixture
def client(tmp_path):
    token = tmp_path / 'device-token'
    token.write_text('test-device-token')
    token.chmod(0o600)
    calls = []
    reply = {'success': True, 'code': 'OK', 'result': {
        'events': [{'id': 'event-1', 'eventType': 'person', 'credential': 'do-not-forward'}],
        'credential': 'do-not-forward',
    }}

    @contextmanager
    def opened(request, timeout):
        assert request.full_url == 'https://homecam.example/api/device/v1/agent/operate'
        assert request.get_header('Authorization') == 'Bearer test-device-token'
        assert timeout == 8
        calls.append(json.loads(request.data))
        yield SimpleNamespace(read=lambda limit: json.dumps(reply).encode())

    api = HomecamQueryClient('https://homecam.example', token,
                            opener=SimpleNamespace(open=opened))
    return api, calls, reply, token


def test_queries_are_fresh_read_only_bounded_and_strip_credentials(client):
    api, calls, _reply, _token = client
    for _ in range(2):
        assert api('get_homecam_events', {'limit': 2, 'event_type': None}) == {
            'success': True, 'code': 'OK',
            'result': {'events': [{'id': 'event-1', 'eventType': 'person'}]},
        }
    assert calls[0]['operation'] == 'homecam_events'
    assert calls[0]['arguments'] == {'limit': 2}
    assert calls[0]['requestId'] != calls[1]['requestId']
    api('get_homecam_events', {'limit': 2, 'event_type': 'person'})
    assert calls[-1]['arguments'] == {'limit': 2, 'eventType': 'person'}
    with pytest.raises(ValueError):
        api('update_homecam_settings', {'cameraEnabled': False})
    with pytest.raises(ValueError):
        api('get_homecam_events', {'limit': 21, 'event_type': None})
    assert len(calls) == 3


def test_missing_or_unsafe_configuration_cannot_enable_queries():
    assert HomecamQueryClient.from_env({}) is None
    for url in ('http://homecam.example', 'https://user:secret@homecam.example',
                'https://homecam.example/api', 'https://homecam.example?secret=key'):
        assert HomecamQueryClient.from_env({
            'HOMECAM_BACKEND_URL': url, 'HOMECAM_DEVICE_TOKEN_FILE': '/token',
        }) is None
    assert _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other') is None


def test_api_errors_and_token_files_cannot_leak_secrets(client):
    api, calls, reply, token = client
    reply.update(success=False, code='VOICE_DELEGATION_REQUIRED', message='private-error')
    reply['result'] = {}
    assert api('get_homecam_status', {}) == {
        'success': False, 'code': 'VOICE_DELEGATION_REQUIRED', 'result': {},
    }
    token.chmod(0o644)
    with pytest.raises(ValueError):
        api('get_homecam_status', {})
    assert len(calls) == 1
    token.chmod(0o600)

    def fail(*args, **kwargs):
        raise HTTPError('https://private/token', 503, 'secret', {}, None)

    api._opener = SimpleNamespace(open=fail)
    with pytest.raises(RuntimeError) as error:
        api('get_homecam_status', {})
    assert 'secret' not in str(error.value) and '/token' not in str(error.value)


@pytest.mark.parametrize('tool', HOMECAM_QUERY_TOOLS)
def test_grounded_homecam_voice_answer_changes_only_reasoning_effort(tool):
    from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider

    request = SpeechAgentRequest('request', 'user', 'conversation', 'turn', '홈캠 상태는?', RobotState(), (),
                                 confirmed_homecam_read=True)
    ctx = dict(mode='answer_only', homecam_query_result_untrusted={'tool': tool, 'data': {}})

    def payload(effort):
        provider = OpenAIResponsesProvider('offline-key', 'gpt-5.6-luna', reasoning_effort=effort, semantic_context=True)
        return provider.build_payload(request, [], [], [], memory_context=ctx)

    assert payload('low')['reasoning']['effort'] == 'none'
    assert payload('low') == payload('none')


@pytest.mark.parametrize('case', ['nonvoice', 'selection', 'tools in request', 'other tool', 'error', 'no data',
                                   'non-answer', 'unconfirmed', 'plain adapter', 'explicit effort', 'other model', 'fallback'])
def test_homecam_effort_preserves_other_model_calls(case):
    settings = Settings(provider='openai', openai_api_key='offline-key',
                        openai_model='other' if case in {'fallback', 'other model'} else 'gpt-5.6-luna',
                        openai_fallback_model='gpt-5.6-luna' if case == 'fallback' else '',
                        openai_reasoning_effort='medium' if case == 'explicit effort' else 'low')
    provider = build_provider(settings, semantic_context=case != 'plain adapter')._providers[-1]
    cls = AgentRequest if case == 'nonvoice' else SpeechAgentRequest
    req = cls('request', 'user', 'conversation', 'turn', '홈캠을 알려줘', RobotState(), ())
    if isinstance(req, SpeechAgentRequest) and case != 'unconfirmed':
        req = replace(req, confirmed_homecam_read=True)
    if case == 'tools in request':
        req = replace(req, available_tools=('get_homecam_status',))
    tools = [TOOL_SPECS['get_homecam_status']] if case == 'selection' else []
    ctx = dict(mode=None if case == 'non-answer' else 'answer_only')
    if case != 'error':
        ctx['homecam_query_result_untrusted'] = {
            'tool': 'get_weather' if case == 'other tool' else 'get_homecam_status',
            'data': None if case == 'no data' else {},
        }
    payload = provider.build_payload(req, [], [], tools, memory_context=ctx)
    assert payload['reasoning']['effort'] == settings.openai_reasoning_effort


def test_real_provider_payload_treats_query_response_as_data_not_commands():
    from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider

    request = AgentRequest(
        request_id='payload', user_id='user', conversation_id='conversation',
        turn_id='turn', utterance='홈캠 상태 알려줘', robot_state=RobotState(),
        available_tools=(),
    )
    payload = OpenAIResponsesProvider('offline-key', 'fixed').build_payload(
        request, [], [], [], memory_context={
            'mode': 'answer_only',
            'homecam_query_result_untrusted': {'tool': 'get_homecam_status',
                                               'data': {'cameraEnabled': True}},
        },
    )
    assert payload.get('tools', []) == []
    assert '명령 권한이 없습니다' in payload['instructions']
    assert '추가 실행·기억 저장 없이' in payload['instructions']
    assert 'cameraEnabled' in payload['input']


class QueryProvider:
    supports_memory = True

    def __init__(self, followup=None):
        self.calls = []
        self.followup = followup

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, memory_context=None):
        self.calls.append((request, [tool.name for tool in tools], copy.deepcopy(memory_context)))
        if len(self.calls) % 2:
            decision = AgentDecision('tool_call', '조회', tool_name='get_homecam_events',
                                     arguments={'limit': 2, 'event_type': None})
        else:
            decision = self.followup or AgentDecision('message', '최근 사람 감지 기록이 하나 있어요.')
        return ProviderResult(decision, 'fixture', 'fixed', 1)


@pytest.mark.parametrize(('response', 'confirmed'), [
    ({'success': False, 'code': 'VOICE_DELEGATION_REQUIRED', 'result': {}}, None),
    ({'success': True, 'code': 'OK', 'result': {}}, True),
    ({'success': True, 'result': {}}, False),
    ({'success': True, 'code': 'CUSTOM', 'result': {}}, False),
    ({'success': True, 'code': 'OK', 'result': None}, False),
])
def test_only_confirmed_reads_reduce_effort_and_custom_results_still_answer(response, confirmed):
    runtime = build_orchestrator(Settings(provider='mock', database_path=':memory:'))
    provider = QueryProvider()
    runtime.provider = provider
    configure_homecam_queries(runtime, lambda *_: response)
    try:
        runtime.conversation_store.create('user', 'room')
        request = SpeechAgentRequest('query', 'user', 'room', 'turn', '홈캠 이벤트를 알려줘.', RobotState(),
                                     ('get_homecam_events',))
        answer = runtime.handle(request)
        if confirmed is None:
            assert answer.decision.reason == 'homecam_unavailable'
            assert len(provider.calls) == 1
        else:
            assert answer.decision.message == '최근 사람 감지 기록이 하나 있어요.'
            assert len(provider.calls) == 2
            followup, _, context = provider.calls[1]
            assert followup.confirmed_homecam_read is confirmed
            assert 'confirmed_homecam_read' not in followup.to_dict()
            assert context['homecam_query_result_untrusted'] == {
                'tool': 'get_homecam_events', 'data': response['result'],
            }
    finally:
        runtime.close()


@pytest.mark.parametrize('forbidden', [False, True])
def test_query_round_trip_never_enters_manager_or_preparation(client, forbidden):
    runtime = build_orchestrator(Settings(database_path=':memory:'), http_server=False)
    runtime.conversation_store.create('user', 'conversation')
    followup = AgentDecision('tool_call', '움직임', tool_name='request_follow_person',
                             arguments={}) if forbidden else None
    provider = QueryProvider(followup)
    runtime.provider = provider
    configure_homecam_queries(runtime, client[0])
    request = AgentRequest(
        request_id='query-1', user_id='user', conversation_id='conversation',
        turn_id='turn-1', utterance='최근 감지 기록 알려줘',
        robot_state=RobotState(),
        available_tools=('get_homecam_events',),
    )
    try:
        result = runtime.handle(request)
        assert len(provider.calls) == 2 and len(client[1]) == 1
        assert provider.calls[1][1] == []
        assert provider.calls[1][0].available_tools == ()
        assert provider.calls[1][2]['homecam_query_result_untrusted']['data'] == {
            'events': [{'id': 'event-1', 'eventType': 'person'}],
        }
        assert result.decision.type == ('refusal' if forbidden else 'message')
        assert result.provider_result.memory_proposal is None
        runtime.handle(request)
        assert len(client[1]) == 1
        if not forbidden:
            runtime.handle(AgentRequest(
                request_id='query-2', user_id='user', conversation_id='conversation',
                turn_id='turn-2', utterance='최근 감지 기록 다시 알려줘',
                robot_state=RobotState(),
                available_tools=('get_homecam_events',),
            ))
            assert len(client[1]) == 2
    finally:
        runtime.close()
