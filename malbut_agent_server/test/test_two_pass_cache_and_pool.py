"""Regression tests for 2-pass prompt-cache reuse, stream short-circuiting, and pooled HTTP clients."""

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.homecam_query import HomecamQueryClient
from malbut_agent_server.patrol_observer import OpenAIPatrolObserver
from malbut_agent_server.prompting import (
    CONVERSATION_INSTRUCTIONS,
    GROUNDED_QUERY_CONVERSATION_INSTRUCTIONS,
    HOMECAM_RESULT_INSTRUCTIONS,
    PATROL_RESULT_INSTRUCTIONS,
    WEATHER_SYSTEM_INSTRUCTIONS,
)
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
    _TWO_PASS_FOLLOWUP_DIRECTIVE,
)
from malbut_agent_server.providers.response_stream import ResponseEvents
from malbut_agent_server.schemas import RobotState, SpeechAgentRequest
from malbut_agent_server.semantic_summary import OpenAISemanticSummarizer
from malbut_agent_server.speech_prefetch import PrefetchTurn
from malbut_agent_server.story_extractor import OpenAIStoryExtractor
from malbut_agent_server.tools import (
    HOMECAM_QUERY_TOOLS,
    PATROL_QUERY_TOOLS,
    PERSONAL_RECALL_TOOL,
    SPEECH_MISSION_TOOLS,
    select_tool_specs,
)
from test_openai_http_pool import local_server, respond
from test_openai_stream import delta, source_events


def _live_tools():
    names = (
        list(SPEECH_MISSION_TOOLS)
        + ['get_weather', 'set_weather_location']
        + list(HOMECAM_QUERY_TOOLS)
        + list(PATROL_QUERY_TOOLS)
    )
    return names, select_tool_specs(names) + [PERSONAL_RECALL_TOOL]


def _history():
    return [
        ConversationTurn(
            'conv-1', 'speaker', 'inst-1', f't{i}', f'r{i}', f'fp{i}', 1, i,
            f'사용자 발화 {i}', f'말벗 답변 {i}',
            {'decision': {'type': 'message', 'message': f'말벗 답변 {i}'}},
            float(i), float(i) + 0.5,
        )
        for i in range(1, 4)
    ]


def _summary():
    return ConversationSummary(
        'sum-1', 'speaker', 'conv-1', 'inst-1', 1, 1,
        '사용자는 거실 정리와 주말 나들이를 이야기했다.',
        1, 1, 1, 'a' * 64, 'openai-semantic-v1', False, 1.0, 2.0,
    )


def _speech_request(
    utterance: str,
    *,
    request_id: str = 'req-two-pass',
    available_tools=(),
    confirmed_homecam_read: bool = False,
) -> SpeechAgentRequest:
    scene = {
        'map': {'revision': 'm1', 'landmarks': [{'location': '거실', 'x': 1.0, 'y': 2.0}]},
        'image': {'available': False},
    }
    return SpeechAgentRequest(
        request_id=request_id,
        user_id='speaker',
        conversation_id='conv-1',
        turn_id='turn-4',
        utterance=utterance,
        robot_state=RobotState(),
        available_tools=tuple(available_tools),
        scene_context=scene,
        confirmed_homecam_read=confirmed_homecam_read,
    )


def _tool_call_response(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': 'resp-call-1',
        'status': 'completed',
        'model': 'gpt-5.6-luna',
        'output': [{
            'type': 'function_call',
            'name': tool_name,
            'arguments': json.dumps(arguments, ensure_ascii=False),
        }],
    }


def _text_response(message: str = '확인했어요.') -> dict[str, Any]:
    return {
        'id': 'resp-call-2',
        'status': 'completed',
        'model': 'gpt-5.6-luna',
        'output': [{
            'type': 'message',
            'content': [{
                'type': 'output_text',
                'text': json.dumps({
                    'type': 'message',
                    'message': message,
                    'reason': 'grounded_answer',
                    'confidence': 1.0,
                }, ensure_ascii=False),
            }],
        }],
    }


@pytest.mark.parametrize(
    ('utterance', 'tool_name', 'tool_args', 'followup_ctx', 'weather_ctx',
     'confirmed_homecam', 'expected_delta_rule', 'expected_effort'),
    [
        (
            '홈캠 상태 알려줘',
            'get_homecam_status',
            {},
            {'mode': 'answer_only', 'enabled': True, 'homecam_query_result_untrusted': {
                'tool': 'get_homecam_status', 'data': {'cameraEnabled': True},
            }},
            None,
            True,
            HOMECAM_RESULT_INSTRUCTIONS,
            'none',
        ),
        (
            '리모컨 어디서 봤어?',
            'search_patrol_observations',
            {'query': '리모컨'},
            {'mode': 'answer_only', 'enabled': True, 'patrol_observations_untrusted': {
                'observations': [{'observation_id': 'obs-1', 'objects': []}],
            }},
            None,
            False,
            PATROL_RESULT_INSTRUCTIONS,
            'none',
        ),
        (
            '내가 예전에 말한 전시 일정 언제였지?',
            PERSONAL_RECALL_TOOL.name,
            {'focus': 'related'},
            {'mode': 'answer_only', 'enabled': True, 'story_memory_untrusted': {
                'stories': [{'story_id': 's1', 'title': '전시', 'current': []}],
            }},
            None,
            False,
            _TWO_PASS_FOLLOWUP_DIRECTIVE,
            'low',
        ),
        (
            '이번 주말에 비 올까?',
            'get_weather',
            {},
            {'mode': 'answer_only', 'enabled': True},
            {'status': 'fresh', 'location': '수원시 우만동', 'current': {'condition': '맑음', 'temperature_c': 15.0}},
            False,
            WEATHER_SYSTEM_INSTRUCTIONS,
            'none',
        ),
    ],
)
def test_two_pass_call2_reuses_call1_prefix_with_tool_choice_none(
    utterance: str,
    tool_name: str,
    tool_args: dict[str, Any],
    followup_ctx: dict[str, Any],
    weather_ctx: dict[str, Any] | None,
    confirmed_homecam: bool,
    expected_delta_rule: str,
    expected_effort: str,
) -> None:
    """Call 2 reuses Call 1's warm tools/developer/history prefix with tool_choice=none."""
    payloads: list[dict[str, Any]] = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        if len(payloads) == 1:
            return _tool_call_response(tool_name, tool_args)
        return _text_response('조회 결과를 안내해 드릴게요.')

    provider = OpenAIResponsesProvider(
        'offline-key',
        'gpt-5.6-luna',
        transport=transport,
        semantic_context=True,
        reasoning_effort='low',
        prompt_cache_enabled=True,
    )
    names, tools = _live_tools()
    history = _history()
    summary = _summary()
    try:
        call1_req = _speech_request(utterance, available_tools=names)
        first = provider.complete(
            call1_req, [], history, tools,
            conversation_summary=summary,
            memory_context={'mode': 'answer_only', 'enabled': True, 'response_settings': {}},
        )
        assert first.decision.type == 'tool_call'
        assert first.decision.tool_name == tool_name

        call2_req = replace(
            call1_req, available_tools=(), confirmed_homecam_read=confirmed_homecam,
        )
        second = provider.complete(
            call2_req, [], history, [],
            conversation_summary=summary,
            memory_context=followup_ctx,
            weather_context=weather_ctx,
        )
        assert second.decision.type == 'message'
        assert second.memory_proposal is None
        assert len(payloads) == 2

        p1, p2 = payloads
        assert p2['prompt_cache_key'] == p1['prompt_cache_key']
        assert p2['tools'] == p1['tools']
        assert p1['tool_choice'] == 'auto'
        assert p2['tool_choice'] == 'none'
        assert p2['input'][0] == p1['input'][0]
        assert [b['text'] for b in p2['input'][1]['content'][:-1]] == [
            b['text'] for b in p1['input'][1]['content'][:-1]
        ]
        assert len(p2['input']) == 3
        assert p2['input'][2]['role'] == 'developer'
        delta_text = p2['input'][2]['content'][0]['text']
        assert _TWO_PASS_FOLLOWUP_DIRECTIVE in delta_text
        assert expected_delta_rule in delta_text
        assert sum(
            'prompt_cache_breakpoint' in block
            for msg in p2['input']
            for block in msg['content']
        ) <= 3
        assert p2['reasoning']['effort'] == expected_effort
    finally:
        provider.close()


def test_two_pass_tool_choice_none_rejects_unexpected_function_call() -> None:
    """Defense-in-depth: a server returning function_call under tool_choice=none fails."""
    calls = 0

    def transport(_url, _headers, payload, _timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _tool_call_response('get_homecam_status', {})
        assert payload['tool_choice'] == 'none'
        return _tool_call_response('request_patrol', {})

    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna', transport=transport,
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    names, tools = _live_tools()
    try:
        req = _speech_request('홈캠 상태 알려줘', available_tools=names)
        provider.complete(
            req, [], _history(), tools,
            memory_context={'mode': 'answer_only', 'enabled': True},
        )
        with pytest.raises(ProviderError, match='tool_choice was none'):
            provider.complete(
                replace(req, available_tools=(), confirmed_homecam_read=True),
                [], _history(), [],
                memory_context={
                    'mode': 'answer_only', 'enabled': True,
                    'homecam_query_result_untrusted': {'tool': 'get_homecam_status', 'data': {}},
                },
            )
    finally:
        provider.close()


def test_two_pass_prefix_skips_weather_fastpath_memory_schema_and_falls_back_on_budget() -> None:
    """Fast-path weather, memory-proposal Call 1, and tight token budgets stay safe."""
    payloads: list[dict[str, Any]] = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        if len(payloads) in {1, 3, 5}:
            return _tool_call_response(
                'get_weather' if len(payloads) == 1 else 'get_homecam_status', {},
            )
        return _text_response('답변이에요.')

    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna', transport=transport,
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    names, tools = _live_tools()
    try:
        # 1) Basic weather fast-path Call 1 (~650 tok) keeps standalone compact Call 2.
        w_req = _speech_request('오늘 날씨 어때?', request_id='req-w', available_tools=names)
        provider.complete(
            w_req, [], _history(), tools,
            memory_context={'mode': 'answer_only', 'enabled': True},
        )
        provider.complete(
            replace(w_req, available_tools=()), [], _history(), [],
            memory_context={'mode': 'answer_only', 'enabled': True},
            weather_context={'status': 'fresh', 'location': '수원시', 'current': {'condition': '맑음', 'temperature_c': 12.0}},
        )
        assert 'tools' not in payloads[1] and 'tool_choice' not in payloads[1]

        # 2) Foreground memory_proposal Call 1 never shares its schema with Call 2.
        m_req = _speech_request('홈캠 상태 알려줘', request_id='req-m', available_tools=names)
        provider.complete(
            m_req, [], _history(), tools,
            memory_context={'enabled': True},
        )
        provider.complete(
            replace(m_req, available_tools=(), confirmed_homecam_read=True), [], _history(), [],
            memory_context={
                'mode': 'answer_only', 'enabled': True,
                'homecam_query_result_untrusted': {'tool': 'get_homecam_status', 'data': {}},
            },
        )
        assert 'memory_proposal' in payloads[2]['text']['format']['schema']['properties']
        assert 'memory_proposal' not in payloads[3]['text']['format']['schema']['properties']
        assert 'tools' not in payloads[3]

        # 3) Budget-tight Call 2 falls back to compact tool-free payload instead of failing.
        def fake_counter(payload):
            return 20000 if payload.get('tools') else 2000

        provider.token_counter = fake_counter
        provider.max_input_tokens = 25000
        b_req = _speech_request('홈캠 상태 알려줘', request_id='req-b', available_tools=names)
        provider.complete(
            b_req, [], _history(), tools,
            memory_context={'mode': 'answer_only', 'enabled': True},
        )
        # Tighten limit before Call 2 so tools-attached Call 2 exceeds budget while compact fits.
        provider.max_input_tokens = 5000
        provider.complete(
            replace(b_req, available_tools=(), confirmed_homecam_read=True), [], _history(), [],
            memory_context={
                'mode': 'answer_only', 'enabled': True,
                'homecam_query_result_untrusted': {'tool': 'get_homecam_status', 'data': {}},
            },
        )
        assert 'tools' not in payloads[5] and 'tool_choice' not in payloads[5]
    finally:
        provider.close()


def test_grounded_patrol_followup_uses_compact_instructions_and_none_effort() -> None:
    """Standalone speech patrol Call 2 matches homecam/weather grounding parity."""
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    try:
        payload = provider.build_payload(
            _speech_request('리모컨 어디서 봤어?', available_tools=()),
            [], [], [],
            memory_context={
                'mode': 'answer_only',
                'enabled': True,
                'patrol_observations_untrusted': {
                    'observations': [{'observation_id': 'obs-1', 'objects': []}],
                },
            },
        )
        dev = payload['input'][0]['content'][0]['text']
        assert PATROL_RESULT_INSTRUCTIONS in dev
        assert GROUNDED_QUERY_CONVERSATION_INSTRUCTIONS in dev
        assert CONVERSATION_INSTRUCTIONS not in dev
        assert payload['reasoning']['effort'] == 'none'
    finally:
        provider.close()


@pytest.mark.parametrize(
    'payload_text',
    [
        '{"type":"tool_call","message":"거실로 이동할게요. ' + '가나다라마바사' * 20 + '"}',
        '{"type":"refusal","message":"안전상 수행할 수 없어요. ' + '가나다라마바사' * 20 + '"}',
        '{"type":"message","message":"","reason":"' + 'empty_reason_' * 20 + '"}',
        'not-json-at-all ' + 'abcdefghij' * 15,
    ],
)
def test_stream_short_circuits_non_previewable_responses(
    monkeypatch: pytest.MonkeyPatch,
    payload_text: str,
) -> None:
    """ResponseEvents stops calling message_candidate once preview is impossible."""
    import malbut_agent_server.providers.response_stream as rs

    calls = 0
    original = rs.message_candidate

    def counted(text):
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(rs, 'message_candidate', counted)
    prepared: list[str] = []
    turn = PrefetchTurn('req-stream', 'u', prepared.append)
    turn.begin_attempt()
    parser = ResponseEvents(turn)
    for event in source_events():
        parser.feed(event)
    chunks = [payload_text[i:i + 4] for i in range(0, len(payload_text), 4)]
    for chunk in chunks:
        parser.feed(delta(chunk))
    assert prepared == []
    assert calls < len(chunks) // 2


def test_auxiliary_openai_clients_reuse_keep_alive_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic summarizer, story extractor, and patrol observer reuse TCP sockets."""
    monkeypatch.setattr('urllib.request.getproxies', lambda: {})

    # 1) OpenAISemanticSummarizer
    summary_body = {
        'status': 'completed',
        'output': [{
            'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': '대화 요약'}],
        }],
    }
    with local_server(lambda h, _req: respond(h, summary_body)) as server:
        summarizer = OpenAISemanticSummarizer('local-test-key')
        summarizer.base_url = server.url.rsplit('/', 1)[0]
        try:
            turn = ConversationTurn(
                'conv-1', 'u', 'inst', 't1', 'r1', 'fp', 1, 1,
                '안녕', '안녕하세요', {}, 1.0, 2.0,
            )
            for _ in range(3):
                res = summarizer.summarize(None, [turn], [], 200)
                assert res.content == '대화 요약'
            assert server.accepted == 1
        finally:
            summarizer.close()
        with pytest.raises(ProviderError):
            summarizer.summarize(None, [turn], [], 200)

    # 2) OpenAIStoryExtractor (backed by OpenAISemanticSummarizer)
    story_body = {
        'status': 'completed',
        'output': [{
            'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': '{"updates":[]}'}],
        }],
    }
    with local_server(lambda h, _req: respond(h, story_body)) as server:
        extractor = OpenAIStoryExtractor('local-test-key', 'gpt-5.6-luna')
        extractor.backend.base_url = server.url.rsplit('/', 1)[0]
        try:
            sources = [{'id': 's1', 'role': 'user', 'text': '안녕', 'created_at': 1.0, 'completed_at': 2.0}]
            for _ in range(2):
                assert extractor.extract([], sources) == []
            assert server.accepted == 1
        finally:
            extractor.close()

    # 3) OpenAIPatrolObserver
    patrol_body = {
        'status': 'completed',
        'output': [{'type': 'message', 'content': [{
            'type': 'output_text',
            'text': json.dumps({'summary': '거실', 'objects': [], 'uncertainties': []}),
        }]}],
    }
    with local_server(lambda h, _req: respond(h, patrol_body)) as server:
        observer = OpenAIPatrolObserver('local-test-key', 'gpt-5.6-luna', 'https://api.openai.com/v1')
        observer.base_url = server.url.rsplit('/', 1)[0]
        try:
            meta = {'captured_at': '2026-11-21T00:00:00Z', 'capture_rooms': ['거실'], 'simulated': False}
            for _ in range(2):
                analyzed = observer.analyze(meta, b'\xff\xd8\xff\xd9')
                assert analyzed['summary'] == '거실'
            assert server.accepted == 1
        finally:
            observer.close()


def test_homecam_query_client_reuses_keep_alive_and_preserves_403_json_code(
    tmp_path: Path,
) -> None:
    """HomecamQueryClient pools connections and keeps 401/403 structured error codes."""
    token = tmp_path / 'device-token'
    token.write_text('local-device-token', encoding='utf-8')
    token.chmod(0o600)

    mode = {'status': 200}

    def reply(handler, _req):
        if mode['status'] == 403:
            respond(
                handler,
                {'success': False, 'code': 'VOICE_DELEGATION_REQUIRED', 'result': {}},
                status=403,
            )
            return
        respond(handler, {'success': True, 'code': 'OK', 'result': {'cameraEnabled': True}})

    with local_server(reply) as server:
        client = HomecamQueryClient('https://homecam.example', token)
        client._url = server.url
        try:
            for _ in range(3):
                assert client('get_homecam_status', {}) == {
                    'success': True, 'code': 'OK', 'result': {'cameraEnabled': True},
                }
            assert server.accepted == 1

            mode['status'] = 403
            assert client('get_homecam_status', {}) == {
                'success': False, 'code': 'VOICE_DELEGATION_REQUIRED', 'result': {},
            }
        finally:
            client.close()
        with pytest.raises(RuntimeError, match='Homecam query unavailable'):
            client('get_homecam_status', {})


def test_empty_recall_followup_uses_none_while_nonempty_recall_keeps_low() -> None:
    """Empty recall Call 2 drops reasoning overhead; retrieved facts/stories keep low."""
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    try:
        req = _speech_request('내가 오늘 점심에 뭘 먹었는지 알아?', available_tools=())
        empty_ctx = {
            'mode': 'answer_only', 'enabled': True, 'memories': [],
            'story_memory_untrusted': {'stories': []},
        }
        p_empty = provider.build_payload(req, [], [], [], memory_context=empty_ctx)
        assert p_empty['reasoning']['effort'] == 'none'

        nonempty_ctx = {
            'mode': 'answer_only', 'enabled': True, 'memories': [],
            'story_memory_untrusted': {'stories': [{'story_id': 's1', 'title': '점심'}]},
        }
        p_nonempty = provider.build_payload(req, [], [], [], memory_context=nonempty_ctx)
        assert p_nonempty['reasoning']['effort'] == 'low'

        event_ctx = dict(empty_ctx, event_attribute_evidence='행사 당시 근거만 사용')
        p_event = provider.build_payload(req, [], [], [], memory_context=event_ctx)
        assert p_event['reasoning']['effort'] == 'low'
    finally:
        provider.close()


@pytest.mark.parametrize(('utterance', 'expected_effort'), [
    ('오늘 처음으로 집에서 빵을 구워봤어. 결과가 생각보다 괜찮더라.', 'none'),
    ('이제 다음 회의 전에 해 볼 작은 준비 한 가지만 제안해줘.', 'none'),
    ('주말에 민서랑 한강을 걷기로 했어. 나는 많이 걷고 싶지는 않아.', 'none'),
    ('나를 따라와 줘', 'none'),
    ('제자리에서 오른쪽으로 45도 돌아줘', 'none'),
    ('세모에는 편이 몇 개 있어?', 'low'),
    ('전에 말한 우리 고양이 이름이 뭐였지?', 'low'),
    ('거실까지 갈 수 있는지만 궁금해. 지금은 이동하지 마.', 'low'),
    ('우리 고양이 이름은 해달콩이야. 고양이 이름으로 기억해 줘.', 'low'),
    ('아 내가 반대로 말했네. 빨간 상자에 장난감, 파란 상자에 책이 있어.', 'low'),
    ('앞에 뭐가 보여?', 'low'),
    ('홈캠 카메라랑 모니터링이 지금 켜져 있어?', 'low'),
    ('새 지도를 만들어 줘', 'low'),
])
def test_selective_fast_turn_reasoning_effort_gates_safe_vs_complex_speech_turns(
    utterance: str,
    expected_effort: str,
) -> None:
    """Fast-turn effort='none' applies only to direct, low-ambiguity speech turns."""
    names, tools = _live_tools()
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low',
        fast_turn_reasoning_effort='none', prompt_cache_enabled=True,
    )
    disabled = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low',
        fast_turn_reasoning_effort='low', prompt_cache_enabled=True,
    )
    try:
        req = _speech_request(utterance, available_tools=names)
        payload = provider.build_payload(
            req, [], [], tools, memory_context={'enabled': True, 'response_settings': {}},
        )
        assert payload['reasoning']['effort'] == expected_effort
        disabled_payload = disabled.build_payload(
            req, [], [], tools, memory_context={'enabled': True, 'response_settings': {}},
        )
        assert disabled_payload['reasoning']['effort'] == 'low'
    finally:
        provider.close()
        disabled.close()
