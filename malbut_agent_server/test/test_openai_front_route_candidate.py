"""Offline contracts for the observe-only OpenAI route candidate client."""

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from malbut_agent_server.adapters.outbound.openai_front_route_candidate import (  # noqa: E501
    FRONT_ROUTE_CANDIDATE_SCHEMA,
    MAX_CANDIDATE_RESPONSE_BYTES,
    FrontRouteCandidateError,
    OpenAIFrontRouteCandidateClient,
    _NoRedirectHandler,
)
from malbut_agent_server.domain.front_route import (
    FrontRoute,
    FrontRouteRequest,
)


def _response(
    route: str = 'robot_action_request',
) -> dict[str, Any]:
    return {
        'id': 'resp-test',
        'status': 'completed',
        'error': None,
        'model': 'gpt-test-router',
        'output': [
            {
                'type': 'message',
                'role': 'assistant',
                'status': 'completed',
                'content': [
                    {
                        'type': 'output_text',
                        'text': json.dumps({'route': route}),
                    },
                ],
            },
        ],
        'usage': {
            'input_tokens': 31,
            'output_tokens': 7,
            'total_tokens': 38,
        },
    }


def _request(text: str = '거실로 가줘') -> FrontRouteRequest:
    return FrontRouteRequest(
        request_id='request-1',
        user_message=text,
    )


def _client(**overrides: Any) -> OpenAIFrontRouteCandidateClient:
    values: dict[str, Any] = {
        'api_key': 'test-only-secret-canary',
        'model': 'gpt-test-router',
        'transport': lambda *_args: _response(),
    }
    values.update(overrides)
    return OpenAIFrontRouteCandidateClient(**values)


@pytest.mark.parametrize('route', list(FrontRoute))
def test_all_five_routes_parse_as_unpromoted_candidates(
    route: FrontRoute,
) -> None:
    """Every public enum value survives the strict candidate boundary."""
    client = _client(
        transport=lambda *_args: _response(route.value),
        clock=iter((10.0, 10.125)).__next__,
    )

    result = client.classify(_request())

    assert result.route is route
    assert result.model == 'gpt-test-router'
    assert result.latency_ms == 125.0
    assert result.input_tokens == 31
    assert result.output_tokens == 7


def test_classification_makes_one_tool_free_non_persisted_request() -> None:
    """A probe input causes exactly one small Responses API attempt."""
    calls = []

    def transport(url, headers, payload, timeout_seconds):
        calls.append((url, headers, payload, timeout_seconds))
        return _response('general_conversation')

    client = _client(transport=transport)
    result = client.classify(_request('안녕, 오늘 어때?'))

    assert result.route is FrontRoute.GENERAL_CONVERSATION
    assert len(calls) == 1
    url, headers, payload, timeout_seconds = calls[0]
    assert url == 'https://api.openai.com/v1/responses'
    assert timeout_seconds == 2
    assert headers['Authorization'] == 'Bearer test-only-secret-canary'
    assert headers['X-Client-Request-Id'].startswith('malbut-front-')
    assert 'request-1' not in headers['X-Client-Request-Id']
    assert payload['store'] is False
    assert payload['max_output_tokens'] == 64
    assert payload['text']['format']['strict'] is True
    assert payload['text']['format']['schema'] == (
        FRONT_ROUTE_CANDIDATE_SCHEMA
    )
    assert set(payload).isdisjoint(
        {'tools', 'tool_choice', 'parallel_tool_calls'}
    )
    decoded_input = json.loads(payload['input'])
    assert decoded_input == {
        'recent_messages': [],
        'current_user_message': '안녕, 오늘 어때?',
    }


def test_each_payload_owns_an_isolated_schema_copy() -> None:
    """Mutating one experiment payload cannot weaken later requests."""
    client = _client()
    first = client.build_payload(_request())
    first_schema = first['text']['format']['schema']
    first_schema['additionalProperties'] = True
    first_schema['properties']['route']['enum'].append('malicious_route')

    second = client.build_payload(_request())
    second_schema = second['text']['format']['schema']

    assert second_schema['additionalProperties'] is False
    assert 'malicious_route' not in second_schema['properties']['route'][
        'enum'
    ]
    assert FRONT_ROUTE_CANDIDATE_SCHEMA['additionalProperties'] is False


def test_client_repr_never_contains_api_key() -> None:
    """Routine diagnostics cannot reveal the credential."""
    client = _client()
    rendered = repr(client)
    assert 'test-only-secret-canary' not in rendered
    assert 'api_key=<redacted>' in rendered


@pytest.mark.parametrize(
    ('override', 'code'),
    [
        ({'status': 'in_progress'}, 'provider_response_incomplete'),
        ({'error': {'code': 'server_error'}}, 'provider_response_invalid'),
        ({'output': None}, 'provider_response_invalid'),
        ({'output': []}, 'provider_response_invalid'),
        (
            {
                'output': [
                    {
                        'type': 'message',
                        'role': 'user',
                        'status': 'completed',
                        'content': [
                            {'type': 'output_text', 'text': '{}'},
                        ],
                    },
                ],
            },
            'provider_response_invalid',
        ),
        (
            {
                'output': [
                    {
                        'type': 'message',
                        'role': 'assistant',
                        'status': 'in_progress',
                        'content': [
                            {
                                'type': 'output_text',
                                'text': '{"route":"general_conversation"}',
                            },
                        ],
                    },
                ],
            },
            'provider_response_incomplete',
        ),
        (
            {
                'output': [
                    {
                        'type': 'message',
                        'role': 'assistant',
                        'content': [
                            {
                                'type': 'output_text',
                                'text': '{"route":"general_conversation"}',
                            },
                        ],
                    },
                ],
            },
            'provider_response_incomplete',
        ),
        (
            {
                'output': [
                    {'type': 'function_call', 'name': 'navigate'},
                ],
            },
            'provider_output_forbidden',
        ),
        (
            {
                'output': [
                    {
                        'type': 'message',
                        'role': 'assistant',
                        'status': 'completed',
                        'content': [
                            {'type': 'refusal', 'refusal': 'no'},
                        ],
                    },
                ],
            },
            'provider_refusal',
        ),
        (
            {
                'output': [
                    {
                        'type': 'message',
                        'role': 'assistant',
                        'status': 'completed',
                        'content': [
                            {'type': 'output_text', 'text': '{}'},
                            {'type': 'other', 'text': 'hidden'},
                        ],
                    },
                ],
            },
            'provider_output_forbidden',
        ),
        (
            {
                'output': [
                    {
                        'type': 'message',
                        'role': 'assistant',
                        'status': 'completed',
                        'content': [
                            {'type': 'output_text', 'text': '{}'},
                        ],
                    },
                ],
            },
            'provider_route_invalid',
        ),
    ],
)
def test_invalid_provider_outputs_fail_with_bounded_codes(
    override: dict[str, Any],
    code: str,
) -> None:
    """Refusal, Tool output, and malformed routes are never candidates."""
    response = _response()
    response.update(override)
    client = _client(transport=lambda *_args: response)

    with pytest.raises(FrontRouteCandidateError) as raised:
        client.classify(_request())

    assert raised.value.code == code
    assert str(raised.value) == code


@pytest.mark.parametrize(
    'route_text',
    (
        '{"route":"abstain"}',
        '{"route":"robot_action_request","extra":true}',
        '{"route":"robot_action_request","route":"general_conversation"}',
        'null',
        'not-json',
    ),
)
def test_wire_result_cannot_expand_the_five_route_contract(
    route_text: str,
) -> None:
    """Unknown, extra, duplicate, and non-object payloads are rejected."""
    response = _response()
    response['output'][0]['content'][0]['text'] = route_text
    client = _client(transport=lambda *_args: response)

    with pytest.raises(FrontRouteCandidateError) as raised:
        client.classify(_request())

    assert raised.value.code == 'provider_route_invalid'


def test_transport_failure_is_not_retried_or_changed_to_abstain() -> None:
    """Operational failures stay errors after one adapter attempt."""
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        raise FrontRouteCandidateError('provider_timeout')

    client = _client(transport=transport)

    with pytest.raises(FrontRouteCandidateError) as raised:
        client.classify(_request())

    assert raised.value.code == 'provider_timeout'
    assert calls == 1


def test_untrusted_error_code_is_replaced_before_it_can_escape() -> None:
    """Adapter exception text cannot become CLI-visible output."""
    error = FrontRouteCandidateError('PRIVATE-ERROR-CANARY')
    assert error.code == 'provider_error'
    assert str(error) == 'provider_error'
    assert 'PRIVATE-ERROR-CANARY' not in repr(error)


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('api_key', ''),
        ('model', ''),
        ('model', 'bad\nmodel'),
        ('timeout_seconds', 0),
        ('timeout_seconds', 11),
        ('timeout_seconds', True),
        ('max_output_tokens', 31),
        ('max_output_tokens', 257),
        ('base_url', 'http://api.openai.com/v1'),
        ('base_url', 'https://example.com/v1'),
        ('base_url', None),
    ],
)
def test_client_configuration_is_bounded(
    field: str,
    value: Any,
) -> None:
    """Credentials cannot be sent to an untrusted or unbounded target."""
    values = {
        'api_key': 'test-only-key',
        'model': 'gpt-test-router',
        'transport': lambda *_args: _response(),
    }
    values[field] = value
    with pytest.raises(ValueError):
        OpenAIFrontRouteCandidateClient(**values)


def test_wrong_request_type_is_rejected_before_transport() -> None:
    """Only the exact bounded FrontRouteRequest crosses the adapter."""
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return _response()

    client = _client(transport=transport)
    with pytest.raises(TypeError):
        client.classify(object())  # type: ignore[arg-type]
    assert calls == 0


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body


class _FakeOpener:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_default_transport_performs_one_bounded_non_redirecting_post(
    monkeypatch,
) -> None:
    """The concrete transport uses one POST with one bounded read."""
    body = json.dumps(_response()).encode('utf-8')
    response = _FakeHTTPResponse(body)
    opener = _FakeOpener(response)
    handlers = []

    def build_opener(handler):
        handlers.append(handler)
        return opener

    monkeypatch.setattr(urllib.request, 'build_opener', build_opener)

    decoded = OpenAIFrontRouteCandidateClient._urllib_transport(
        'https://api.openai.com/v1/responses',
        {
            'Authorization': 'Bearer test-only-key',
            'Content-Type': 'application/json',
        },
        {'store': False},
        2,
    )

    assert decoded == _response()
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.full_url == 'https://api.openai.com/v1/responses'
    assert request.get_method() == 'POST'
    assert timeout == 2
    assert response.read_sizes == [MAX_CANDIDATE_RESPONSE_BYTES + 1]
    assert len(handlers) == 1
    assert isinstance(handlers[0], _NoRedirectHandler)


@pytest.mark.parametrize(
    ('failure', 'code'),
    [
        (TimeoutError(), 'provider_timeout'),
        (
            urllib.error.URLError(OSError('network canary')),
            'provider_network_error',
        ),
        (
            urllib.error.HTTPError(
                'https://api.openai.com/v1/responses',
                429,
                'rate limited',
                None,
                None,
            ),
            'provider_http_429',
        ),
    ],
)
def test_default_transport_maps_failures_without_retry(
    monkeypatch,
    failure: BaseException,
    code: str,
) -> None:
    """Timeout, network, and HTTP failures stay bounded after one open."""
    opener = _FakeOpener(failure)
    monkeypatch.setattr(
        urllib.request,
        'build_opener',
        lambda _handler: opener,
    )

    with pytest.raises(FrontRouteCandidateError) as raised:
        OpenAIFrontRouteCandidateClient._urllib_transport(
            'https://api.openai.com/v1/responses',
            {'Authorization': 'Bearer test-only-key'},
            {'store': False},
            2,
        )

    assert raised.value.code == code
    assert len(opener.calls) == 1
    assert 'network canary' not in str(raised.value)


@pytest.mark.parametrize(
    ('body', 'code'),
    [
        (
            b'x' * (MAX_CANDIDATE_RESPONSE_BYTES + 1),
            'provider_response_too_large',
        ),
        (b'{"status":"completed","status":"completed"}',
         'provider_response_invalid'),
        (b'{"value":NaN}', 'provider_response_invalid'),
        (b'not-json', 'provider_response_invalid'),
    ],
)
def test_default_transport_rejects_oversized_or_invalid_json(
    monkeypatch,
    body: bytes,
    code: str,
) -> None:
    """The concrete decoder rejects ambiguous provider bodies."""
    opener = _FakeOpener(_FakeHTTPResponse(body))
    monkeypatch.setattr(
        urllib.request,
        'build_opener',
        lambda _handler: opener,
    )

    with pytest.raises(FrontRouteCandidateError) as raised:
        OpenAIFrontRouteCandidateClient._urllib_transport(
            'https://api.openai.com/v1/responses',
            {'Authorization': 'Bearer test-only-key'},
            {'store': False},
            2,
        )

    assert raised.value.code == code
    assert len(opener.calls) == 1


def test_redirect_handler_never_forwards_a_request() -> None:
    """Authorization headers cannot follow a provider redirect."""
    result = _NoRedirectHandler().redirect_request(
        urllib.request.Request('https://api.openai.com/v1/responses'),
        None,
        307,
        'redirect',
        {},
        'https://example.com/steal',
    )
    assert result is None
