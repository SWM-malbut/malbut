"""Offline SSE boundary tests with real keep-alive HTTP and no model calls."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import threading

import pytest

from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.providers.response_stream import (
    ResponseEvents, message_candidate, read_response_stream,
)
from malbut_agent_server.schemas import AgentRequest
from malbut_agent_server.speech_prefetch import PrefetchTurn, prefetch_scope
from test_openai_http_pool import local_server, respond


TEXT = '안녕하세요. 다음 이야기를 들려주세요.'
PREFIX = '안녕하세요. 다'
DECISION = {'type': 'message', 'message': TEXT, 'reason': '', 'confidence': 1.0}
RAW = json.dumps(DECISION, ensure_ascii=False)


def final_response(decision=None):
    return {'id': 'resp-1', 'status': 'completed', 'model': 'fixture', 'output': [
        {'id': 'msg-1', 'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': json.dumps(
                DECISION if decision is None else decision, ensure_ascii=False)}]}]}


def source_events():
    return [
        {'type': 'response.created', 'response': {'id': 'resp-1'}},
        {'type': 'response.output_item.added', 'output_index': 0,
         'item': {'id': 'reason-1', 'type': 'reasoning'}},
        {'type': 'response.output_item.added', 'output_index': 1,
         'item': {'id': 'msg-1', 'type': 'message', 'role': 'assistant'}},
        {'type': 'response.content_part.added', 'output_index': 1, 'item_id': 'msg-1',
         'content_index': 0, 'part': {'type': 'output_text', 'text': ''}},
    ]


def delta(text, **changes):
    return {'type': 'response.output_text.delta', 'output_index': 1, 'item_id': 'msg-1',
            'content_index': 0, 'delta': text, **changes}


def sse(events):
    return b''.join(('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
                    for event in events)


def complete(adapter, turn=None):
    request = AgentRequest.from_dict({'request_id': 'req-1', 'user_id': 'speaker',
                                     'conversation_id': 'conversation-1', 'turn_id': 'turn-1',
                                     'utterance': '인사해 줘', 'available_tools': []})
    with prefetch_scope(turn):
        return adapter.complete(request, [], [], [])


@pytest.fixture(autouse=True)
def direct(monkeypatch):
    monkeypatch.setattr('urllib.request.getproxies', lambda: {})


@pytest.fixture
def provider():
    adapter = OpenAIResponsesProvider(api_key='offline-fixture', model='fixture')
    yield adapter
    adapter.close()


@pytest.mark.parametrize('raw,expected', [
    ('{"type":"message","message":"안녕. 다', '안녕. 다'),
    ('{"type":"message","message":"안녕.   다', '안녕.   다'),
    ('{"type":"message","message":"안녕."', '안녕.'),
    ('{"type":"message","message":"안녕. ', None),
    ('{"type":"message","message":"3.14가 값', None),
    ('{"type":"message","message":"\\"안녕. 다', None),
    ('{"type":"message","message":"한 줄\\n끝. 다', '한 줄\n끝. 다'),
    ('{"type":"message","message":"\\uD83D\\uDE00. 다', '😀. 다'),
    ('{"type":"message","message":"안녕. \\uD83D', None),
    ('{"type":"message","message":"안녕. \\uD83D\\uDE', None),
    ('{"type":"message","message":"안녕. \\x', None),
    ('{"type":"tool_call","message":"실행. 다', None),
    ('{"message":"안녕. 다', None),
    ('{"nested":{"type":"message","message":"안녕. 다', None),
    ('{"type":"message","nested":{"message":"중첩."},"message":"인사. 다', '인사. 다'),
    ('{"type":"message","type":"message","message":"안녕. 다', None),
    ('{"type":"message","message":null', None),
])
def test_only_complete_top_level_plain_message_prefix_is_a_candidate(raw, expected):
    assert message_candidate(raw) == expected


def test_candidate_always_matches_exact_decoded_prefix_across_unicode_and_escapes():
    message = '경로 \\와 “인용. 표현”이에요. 😀 다음 문장.'
    raw = json.dumps({'type': 'message', 'message': message}, ensure_ascii=True)
    for position in range(len(raw) + 1):
        candidate = message_candidate(raw[:position])
        assert candidate is None or message.startswith(candidate)


def test_prepare_precedes_completion_but_result_and_token_require_final_validation(provider):
    prepared, release = threading.Event(), threading.Event()
    emitted = []

    def emit(event):
        emitted.append(event)
        if event.operation == 'prepare':
            prepared.set()

    turn = PrefetchTurn('req-1', 'utterance-1', emit)
    prefix = sse(source_events() + [delta('{"type":"message","message":"' + PREFIX)])
    suffix = sse([{'type': 'response.completed', 'response': final_response()}]) + b'data: [DONE]\n\n'

    def reply(handler, request):
        assert request.body['stream'] is True
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Content-Length', str(len(prefix + suffix)))
        handler.end_headers()
        # Split bytes inside Korean UTF-8 code points as a network would.
        for at in range(0, len(prefix), 7):
            handler.wfile.write(prefix[at:at + 7])
        handler.wfile.flush()
        assert release.wait(3)
        handler.wfile.write(suffix)
        handler.wfile.flush()

    with local_server(reply) as server, ThreadPoolExecutor(1) as workers:
        provider.base_url = server.url.rsplit('/', 1)[0]
        future = workers.submit(complete, provider, turn)
        try:
            assert prepared.wait(3)
            assert not future.done()
            # No completed provider decision exists and therefore no final hint.
            assert turn._approved_text is None
            assert emitted[0].text == PREFIX
            assert emitted[0].request_id == 'utterance-1'
        finally:
            release.set()
        assert future.result(3).decision.message == TEXT
        assert turn.hint(TEXT)['prepared_id'] == emitted[0].prepared_id
        assert complete(provider, PrefetchTurn('req-1', 'utterance-2', lambda _: None)).decision.message == TEXT
        assert server.accepted == 1
        assert len(server.calls) == 2


def test_chunked_stream_uses_same_connection_after_terminal_event(provider):
    body = sse(source_events() + [delta(RAW), {'type': 'response.completed', 'response': final_response()}])

    def reply(handler, request):
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Transfer-Encoding', 'chunked')
        handler.end_headers()
        for at in range(0, len(body), 29):
            chunk = body[at:at + 29]
            handler.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
        handler.wfile.write(b'0\r\n\r\n')
        handler.wfile.flush()

    with local_server(reply) as server:
        provider.base_url = server.url.rsplit('/', 1)[0]
        for n in range(2):
            assert complete(provider, PrefetchTurn('req-1', str(n), lambda _: None)).decision.message == TEXT
        assert server.accepted == 1


@pytest.mark.parametrize('ending', ['missing', 'failed', 'incomplete', 'invalid_json', 'wrong_id', 'bad_decision'])
def test_stream_failure_cancels_candidate_without_replaying_post(provider, ending):
    events = source_events() + [delta('{"type":"message","message":"' + PREFIX)]
    if ending in {'failed', 'incomplete'}:
        events.append({'type': 'response.' + ending, 'response': {'id': 'resp-1'}})
    elif ending in {'wrong_id', 'bad_decision'}:
        response = final_response({'type': 'message', 'message': TEXT} if ending == 'bad_decision' else None)
        if ending == 'wrong_id':
            response['id'] = 'other-response'
        events.append({'type': 'response.completed', 'response': response})
    body = sse(events) + (b'data: {invalid}\n\n' if ending == 'invalid_json' else b'')
    emitted = []
    turn = PrefetchTurn('req-1', 'utterance-1', emitted.append)
    with local_server(lambda h, req: respond(h, body, extra_headers={'Content-Type': 'text/event-stream'})) as server:
        provider.base_url = server.url.rsplit('/', 1)[0]
        with pytest.raises(ProviderError):
            complete(provider, turn)
        assert len(server.calls) == 1
    assert [e.operation for e in emitted] == ['prepare', 'cancel']
    assert turn.hint(TEXT) == {}


@pytest.mark.parametrize('change', [
    {'output_index': 0}, {'output_index': True}, {'content_index': False},
    {'item_id': 'other'}, {'content_index': 1}, {'delta': 3},
])
def test_delta_scope_mismatch_never_prepares(change):
    emitted = []
    turn = PrefetchTurn('req-1', 'u', emitted.append)
    turn.begin_attempt()
    parser = ResponseEvents(turn)
    for event in source_events():
        parser.feed(event)
    parser.feed(delta(RAW, **change))
    assert emitted == []
    assert parser.blocked


@pytest.mark.parametrize('extra', [
    {'type': 'response.output_item.added', 'output_index': 2, 'item': {'id': 'tool', 'type': 'function_call'}},
    {'type': 'response.output_item.added', 'output_index': 2, 'item': {'id': 'msg-2', 'type': 'message', 'role': 'assistant'}},
    {'type': 'response.content_part.added', 'output_index': 1, 'item_id': 'msg-1', 'content_index': 1, 'part': {'type': 'refusal'}},
])
def test_ambiguous_or_tool_source_cancels_existing_hint(extra):
    emitted = []
    turn = PrefetchTurn('req-1', 'u', emitted.append)
    turn.begin_attempt()
    parser = ResponseEvents(turn)
    for event in source_events() + [delta(RAW), extra, delta(RAW)]:
        parser.feed(event)
    assert [e.operation for e in emitted] == ['prepare', 'cancel']


def test_nonstream_fallback_and_default_are_single_requests(provider):
    emitted = []
    turn = PrefetchTurn('req-1', 'u', emitted.append)
    with local_server(lambda h, req: respond(h, final_response(), extra_headers={'Content-Type': 'application/json'})) as server:
        provider.base_url = server.url.rsplit('/', 1)[0]
        assert complete(provider, turn).decision.message == TEXT
        assert complete(provider).decision.message == TEXT
        assert [c.body.get('stream') for c in server.calls] == [True, None]
        assert server.accepted == 1
    assert emitted == []


def test_server_rejecting_stream_does_not_trigger_unapproved_second_post(provider):
    with local_server(lambda h, req: respond(h, {}, 400)) as server:
        provider.base_url = server.url.rsplit('/', 1)[0]
        with pytest.raises(ProviderError, match='HTTP status 400'):
            complete(provider, PrefetchTurn('req-1', 'u', lambda _: None))
        assert len(server.calls) == 1


@pytest.mark.parametrize('mode', ['injected', 'proxy'])
def test_injected_transport_and_proxy_keep_the_original_nonstream_contract(provider, monkeypatch, mode):
    payloads = []

    def transport(url, headers, payload, timeout):
        payloads.append(payload)
        return final_response()

    if mode == 'proxy':
        monkeypatch.setattr('urllib.request.getproxies', lambda: {'https': 'http://unused-proxy'})
        monkeypatch.setattr('urllib.request.proxy_bypass', lambda _: False)
        monkeypatch.setattr(provider, '_urllib_transport', transport)
    else:
        monkeypatch.setattr(provider, 'transport', transport)
    assert complete(provider, PrefetchTurn('req-1', 'u', lambda _: None)).decision.message == TEXT
    assert len(payloads) == 1 and 'stream' not in payloads[0]


@pytest.mark.parametrize('body,limit,error', [
    (b'data: \xff\n\n', 1024, 'UTF-8'),
    (b'data: {"type":NaN}\n\n', 1024, 'JSON'),
    (b':' + b'x' * 1024, 1024, 'size limit'),
    (b'data: []\n\n', 1024, 'object'),
])
def test_sse_total_byte_utf8_and_json_bounds(body, limit, error):
    stream = BytesIO(body)
    stream.headers = {'Content-Type': 'text/event-stream'}
    turn = PrefetchTurn('r', 'u', lambda _: None)
    with pytest.raises(ProviderError, match=error):
        read_response_stream(stream, limit, turn)


def test_terminal_and_ordering_are_not_ambiguous():
    parser = ResponseEvents(PrefetchTurn('r', 'u', lambda _: None))
    parser.feed({'type': 'response.created', 'response': {'id': 'resp-1'}, 'sequence_number': 0})
    with pytest.raises(ProviderError, match='ordering'):
        parser.feed({'type': 'response.in_progress', 'sequence_number': 0})
    parser.feed({'type': 'response.completed', 'response': final_response(), 'sequence_number': 1})
    with pytest.raises(ProviderError, match='after completion'):
        parser.feed(delta(RAW))


def test_only_existing_reliability_policy_retries_and_retires_failed_attempt(provider):
    from malbut_agent_server.conversation_progress import request_scope
    from malbut_agent_server.providers.reliable import ReliableProvider

    emitted, calls = [], []

    def reply(handler, request):
        calls.append(request)
        terminal = ({'type': 'response.failed', 'response': {'id': 'resp-1'}}
                    if len(calls) == 1 else {'type': 'response.completed', 'response': final_response()})
        respond(handler, sse(source_events() + [delta(RAW), terminal]),
                extra_headers={'Content-Type': 'text/event-stream'})

    reliable = ReliableProvider([provider], max_retries=1, base_delay_seconds=0,
                                attempt_timeout_seconds=2, total_timeout_seconds=10)
    turn = PrefetchTurn('req-1', 'u', emitted.append)
    with local_server(reply) as server, request_scope():
        provider.base_url = server.url.rsplit('/', 1)[0]
        assert complete(reliable, turn).decision.message == TEXT
        assert len(calls) == 2
    assert [event.operation for event in emitted] == ['prepare', 'cancel', 'prepare']
    assert emitted[0].generation_id != emitted[2].generation_id
    assert turn.hint(TEXT)['prepared_id'] == emitted[2].prepared_id


@pytest.mark.parametrize('has_terminal', [False, True])
def test_lost_stream_with_announced_larger_body_cannot_reuse_connection(provider, has_terminal):
    emitted, calls = [], []

    def reply(handler, request):
        calls.append(request)
        if len(calls) > 1:
            respond(handler, final_response(), extra_headers={'Content-Type': 'application/json'})
            return
        body = sse(source_events() + [delta(RAW)])
        if has_terminal:
            body += sse([{'type': 'response.completed', 'response': final_response()}])
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Content-Length', str(len(body) + 500))
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
        handler.close_connection = True

    turn = PrefetchTurn('req-1', 'u', emitted.append)
    with local_server(reply) as server:
        provider.base_url = server.url.rsplit('/', 1)[0]
        expected_error = 'network error' if has_terminal else 'before completion'
        with pytest.raises(ProviderError, match=expected_error):
            complete(provider, turn)
        assert len(calls) == 1
        assert complete(provider).decision.message == TEXT
        assert server.accepted == 2
    assert [event.operation for event in emitted] == ['prepare', 'cancel']
