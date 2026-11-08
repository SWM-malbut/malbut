"""Offline contract tests for the OpenAI Responses API adapter."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.schemas import AgentDecision, AgentRequest, SpeechAgentRequest
from malbut_agent_server.tools import select_tool_specs


def _request() -> AgentRequest:
    return AgentRequest.from_dict(
        {
            'request_id': 'openai-test',
            'user_id': 'private-user-id',
            'conversation_id': 'private-conversation-id',
            'turn_id': 'private-turn-id',
            'utterance': '거실로 가줘',
            'robot_state': {
                'battery_percent': 80,
                'navigation_available': True,
                'localization_ok': True,
            },
            'available_tools': ['navigate'],
        }
    )


def test_builds_strict_responses_payload_and_parses_tool_call() -> None:
    """The adapter follows current flat Responses function schemas."""
    captured: Dict[str, Any] = {}

    def transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        captured.update(
            {
                'url': url,
                'headers': headers,
                'payload': payload,
                'timeout': timeout,
            }
        )
        return {
            'id': 'resp_test',
            'status': 'completed',
            'model': 'test-model',
            'output': [
                {
                    'type': 'function_call',
                    'call_id': 'call_test',
                    'name': 'navigate',
                    'arguments': '{"location":"거실"}',
                }
            ],
            'usage': {
                'input_tokens': 10,
                'output_tokens': 4,
                'total_tokens': 14,
            },
        }

    provider = OpenAIResponsesProvider(
        api_key='test-only-key',
        model='test-model',
        transport=transport,
    )
    result = provider.complete(
        _request(),
        [],
        [],
        select_tool_specs(['navigate']),
    )

    tool = captured['payload']['tools'][0]
    assert captured['url'].endswith('/v1/responses')
    assert captured['payload']['parallel_tool_calls'] is False
    assert captured['payload']['store'] is False
    assert captured['payload']['reasoning'] == {
        'effort': 'none',
        'context': 'current_turn',
    }
    assert captured['payload']['max_output_tokens'] == 500
    assert tool['type'] == 'function'
    assert tool['strict'] is True
    assert tool['parameters']['additionalProperties'] is False
    assert tool['parameters']['required'] == ['location']
    assert 'private-user-id' not in captured['payload']['input']
    assert (
        'private-conversation-id'
        not in captured['payload']['input']
    )
    assert 'private-turn-id' not in captured['payload']['input']
    assert (
        captured['headers']['X-Client-Request-Id']
        .startswith('malbut-')
    )
    assert 'openai-test' not in captured[
        'headers'
    ]['X-Client-Request-Id']
    assert result.decision.tool_name == 'navigate'
    assert result.decision.arguments == {'location': '거실'}
    assert result.usage.total_tokens == 14


def test_non_reasoning_chat_payload_omits_reasoning_options() -> None:
    """A fast Chat role can use models that reject reasoning options."""
    provider = OpenAIResponsesProvider(
        api_key='test-only-key',
        model='gpt-4.1-mini',
        include_reasoning=False,
    )

    request = AgentRequest.from_dict({
        **_request().to_dict(),
        'utterance': '안녕',
        'available_tools': [],
    })
    payload = provider.build_payload(request, [], [], [])

    assert payload['model'] == 'gpt-4.1-mini'
    assert 'reasoning' not in payload
    assert 'tools' not in payload
    assert 'tool_choice' not in payload


def test_end_conversation_decision_is_requested_only_for_foreground_speech():
    provider = OpenAIResponsesProvider(api_key='test-only-key', model='test-model')
    voice = SpeechAgentRequest.from_dict({
        **_request().to_dict(),
        'utterance': '그 얘기는 다음에 마저 하고 오늘은 이만 인사할게.',
        'available_tools': [],
    })
    for memory in (None, {'mode': 'answer_only'}):
        payload = provider.build_payload(voice, [], [], [], memory_context=memory)
        assert 'reason=end_conversation' in payload['instructions']
        assert voice.utterance in payload['input']
    for memory in ({'mode': 'automatic_extraction'}, {'mode': 'source_review'}):
        payload = provider.build_payload(voice, [], [], [], memory_context=memory)
        assert 'reason=end_conversation' not in payload['instructions']
    assert 'reason=end_conversation' not in provider.build_payload(_request(), [], [], [])['instructions']


@pytest.mark.parametrize('kind,reason,expected', [
    ('message', 'end_conversation', True),
    ('message', 'end_conversation_requested', False),
    ('clarification', 'end_conversation', False),
    ('refusal', 'end_conversation', False),
    ('tool_call', 'end_conversation', False),
])
def test_only_an_exact_closing_message_ends_voice_listening(kind, reason, expected):
    decision = AgentDecision(kind, '다음에 봬요.', reason=reason)
    assert decision.ends_conversation is expected


def test_parses_structured_text_message() -> None:
    """Non-action text follows the separate strict text schema."""
    text_decision = {
        'type': 'clarification',
        'message': '어느 방으로 갈까?',
        'reason': 'ambiguous_destination',
        'confidence': 0.8,
    }

    def transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        del url, headers, timeout
        assert 'tools' not in payload
        assert 'tool_choice' not in payload
        return {
            'status': 'completed',
            'output': [
                {
                    'type': 'message',
                    'content': [
                        {
                            'type': 'output_text',
                            'text': json.dumps(
                                text_decision,
                                ensure_ascii=False,
                            ),
                        }
                    ],
                }
            ],
        }

    provider = OpenAIResponsesProvider(
        api_key='test-only-key',
        model='test-model',
        transport=transport,
    )
    result = provider.complete(_request(), [], [], [])
    assert result.decision.type == 'clarification'
    assert result.decision.message == '어느 방으로 갈까?'


@pytest.mark.parametrize(
    'output',
    [
        [],
        [
            {
                'type': 'function_call',
                'name': 'navigate',
                'arguments': '{}',
            },
            {
                'type': 'function_call',
                'name': 'capture_photo',
                'arguments': '{}',
            },
        ],
        [
            {
                'type': 'function_call',
                'name': 'navigate',
                'arguments': '{"location":NaN}',
            },
        ],
        [
            {
                'type': 'function_call',
                'name': 'navigate',
                'arguments': '{"location":"거실"}',
            },
            {
                'type': 'message',
                'content': [
                    {
                        'type': 'refusal',
                        'refusal': '이 행동은 수행할 수 없습니다.',
                    }
                ],
            },
        ],
    ],
)
def test_rejects_missing_or_multiple_outputs(
    output: List[Dict[str, Any]],
) -> None:
    """Malformed or multi-action provider output fails closed."""

    def transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        del url, headers, payload, timeout
        return {'status': 'completed', 'output': output}

    provider = OpenAIResponsesProvider(
        api_key='test-only-key',
        model='test-model',
        transport=transport,
    )
    with pytest.raises(ProviderError):
        provider.complete(_request(), [], [], [])


@pytest.mark.parametrize(
    'base_url',
    [
        'http://example.com/v1',
        'https://example.com/v1',
        'https://api.openai.com/v2',
        'https://user@api.openai.com/v1',
        'https://api.openai.com/v1?target=other',
    ],
)
def test_non_official_base_urls_are_rejected(base_url: str) -> None:
    """The OpenAI credential is pinned to its official API origin."""
    with pytest.raises(ValueError):
        OpenAIResponsesProvider(
            api_key='test-only-key',
            model='test-model',
            base_url=base_url,
        )


def test_transport_preserves_unicode_and_individual_image_inputs() -> None:
    """Compact wire JSON preserves every frame and non-ASCII prompt verbatim."""
    captured = []
    payload = {
        'model': 'test-model',
        'store': False,
        'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': '거실·안방의 사람과 상자 📦를 구분해 주세요.'},
            *[{'type': 'input_image', 'detail': 'high',
               'image_url': f'data:image/jpeg;base64,frame{index:02d}'}
              for index in range(24)],
        ]}],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *args):
            del format_string, args

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            captured.append(body)
            response = json.dumps({'summary': '거실의 상자 📦'}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = OpenAIResponsesProvider._urllib_transport(
            f'http://127.0.0.1:{server.server_address[1]}/v1/responses',
            {'Content-Type': 'application/json'}, payload, 2,
        )
        assert len(captured) == 1
        assert captured[0].isascii()
        assert json.loads(captured[0]) == payload
        assert result == {'summary': '거실의 상자 📦'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_authorization_is_never_followed_to_a_redirect() -> None:
    """Default transport fails before a credential can reach a new URL."""
    sink_calls = []

    class SinkHandler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *args):
            del format_string, args

        def do_POST(self):
            sink_calls.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()

    sink = ThreadingHTTPServer(('127.0.0.1', 0), SinkHandler)
    sink_thread = threading.Thread(
        target=sink.serve_forever,
        daemon=True,
    )
    sink_thread.start()
    sink_url = (
        f'http://127.0.0.1:{sink.server_address[1]}/captured'
    )

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *args):
            del format_string, args

        def do_POST(self):
            self.send_response(307)
            self.send_header('Location', sink_url)
            self.end_headers()

    redirect = ThreadingHTTPServer(
        ('127.0.0.1', 0),
        RedirectHandler,
    )
    redirect_thread = threading.Thread(
        target=redirect.serve_forever,
        daemon=True,
    )
    redirect_thread.start()
    try:
        with pytest.raises(ProviderError):
            OpenAIResponsesProvider._urllib_transport(
                (
                    'http://127.0.0.1:'
                    f'{redirect.server_address[1]}/v1/responses'
                ),
                {
                    'Authorization': 'Bearer local-test-key',
                    'Content-Type': 'application/json',
                },
                {'model': 'test-model', 'input': 'test'},
                2,
            )
        assert sink_calls == []
    finally:
        redirect.shutdown()
        redirect.server_close()
        sink.shutdown()
        sink.server_close()
        redirect_thread.join(timeout=2)
        sink_thread.join(timeout=2)


def test_provider_repr_and_validation_never_expose_key() -> None:
    """Diagnostics redact the key and bound costly request settings."""
    key = 'sk-test-never-render-this-value'
    provider = OpenAIResponsesProvider(
        api_key=key,
        model='test-model',
    )
    assert key not in repr(provider)
    assert 'api_key=<redacted>' in repr(provider)

    with pytest.raises(ValueError):
        OpenAIResponsesProvider(
            api_key=key,
            model='test-model',
            reasoning_effort='unbounded',
        )
    with pytest.raises(ValueError):
        OpenAIResponsesProvider(
            api_key=key,
            model='test-model',
            max_output_tokens=50000,
        )


def test_invalid_structured_confidence_is_a_provider_error() -> None:
    """Invalid remote output is not blamed on the HTTP client."""

    def transport(*_args):
        return {
            'status': 'completed',
            'output': [
                {
                    'type': 'message',
                    'content': [
                        {
                            'type': 'output_text',
                            'text': (
                                '{"type":"message",'
                                '"message":"invalid",'
                                '"reason":"test",'
                                '"confidence":2}'
                            ),
                        }
                    ],
                }
            ],
        }

    provider = OpenAIResponsesProvider(
        api_key='test-only-key',
        model='test-model',
        transport=transport,
    )
    with pytest.raises(ProviderError):
        provider.complete(_request(), [], [], [])


def test_response_requires_explicit_completed_status() -> None:
    """Missing lifecycle status is not accepted as a final response."""

    def transport(*_args):
        return {'output': []}

    provider = OpenAIResponsesProvider(
        api_key='test-only-key',
        model='test-model',
        transport=transport,
    )
    with pytest.raises(ProviderError, match='not completed'):
        provider.complete(_request(), [], [], [])
