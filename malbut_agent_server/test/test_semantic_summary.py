"""Contract checks for semantic summaries without paid API calls."""

import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.semantic_summary import (
    OpenAISemanticSummarizer,
    count_tokens,
)
from malbut_agent_server.summarization import SummarySourceTurn


def _response(text='친구가 고양이를 키운다. 토요일 약속은 제안 단계이며 확정되지 않았다.'):
    return {
        'status': 'completed',
        'output': [{
            'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text}],
        }],
    }


def test_summary_keeps_full_prefix_separate_from_recent_raw_context():
    """Only the covered prefix reaches the API; recent corrections stay raw."""
    calls = []
    text = '긴 대화 내용 ' * 100 + (
        '친구가 고양이를 키워. 동생은 수영을 좋아해. '
        '모임 이름은 파랑을 제안했지만 거절했고 초록으로 정했어. '
        '기본은 존댓말이고 직전 답변만 반말 허용이었어. '
        '이번 세션에는 이모티콘을 빼줘.'
    )

    def transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return _response()

    result = OpenAISemanticSummarizer('test-key', transport=transport).summarize(
        SimpleNamespace(content='원래 토요일로 제안했지만 확정하지 않았다.'),
        [SummarySourceTurn(8, 'turn-8', text, '누구의 고양이인지 구분할게.')],
        [SummarySourceTurn(9, 'turn-9', '아니, 일요일 15시로 하자.', '확정할게.')],
        target_tokens=12,
    )

    assert len(calls) == 1
    url, headers, payload, timeout = calls[0]
    assert url == 'https://api.openai.com/v1/responses'
    assert headers['Authorization'] == 'Bearer test-key'
    assert timeout == 30
    assert payload['store'] is False
    assert payload['truncation'] == 'disabled'
    assert payload['reasoning'] == {'effort': 'none'}
    assert payload['max_output_tokens'] == 8192
    assert 'tools' not in payload
    body = json.loads(payload['input'])
    assert body['source_turns_untrusted'][0]['user'] == text
    assert body['source_turns_untrusted'][0]['ordinal'] == 8
    assert set(body) == {
        'source_turns_untrusted', 'previous_summary_untrusted',
        'summary_target_tokens',
    }
    assert '일요일 15시' not in payload['input']
    assert body['previous_summary_untrusted'].startswith('원래 토요일')
    assert body['summary_target_tokens'] == 12
    assert '입력된 범위 끝의 진행 상태만' in payload['instructions']
    for preservation_rule in (
        '잠시 중단된 주제', '다른 사람의 취향·취미·관계',
        '거절한 후보', '종료된 일회성 예외', '돌아갈 기본 설정',
        '현재 세션에만 적용되는 변경', '지워서 분량을 맞추지 않는다',
    ):
        assert preservation_rule in payload['instructions']
    assert result.content == _response()['output'][0]['content'][0]['text']
    assert count_tokens(result.content) > 12  # Preserve meaning above soft target.
    assert result.algorithm == 'openai-semantic-v1'
    assert json.loads(result.state_json)['model'] == 'gpt-5.6-luna'
    assert json.loads(result.state_json)['prompt_revision'] == 4
    assert json.loads(result.state_json)['reasoning_effort'] == 'none'
    assert not result.fallback_used


@pytest.mark.parametrize('effort', ['unsupported', '', None, True])
def test_invalid_reasoning_effort_is_rejected_before_transport(effort):
    with pytest.raises(ValueError, match='reasoning_effort'):
        OpenAISemanticSummarizer(
            'test-key', reasoning_effort=effort,
            transport=lambda *args: pytest.fail('invalid effort reached transport'),
        )


@pytest.mark.parametrize('summary_model,summary_effort', [
    ('', ''), ('gpt-5.6-luna', ''), ('', 'none'), ('gpt-5.6-luna', 'none'),
])
def test_configured_reasoning_effort_reaches_summary_payload_and_provenance(
        monkeypatch, summary_model, summary_effort):
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator
    from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider

    calls = []

    def transport(_url, _headers, payload, timeout):
        assert timeout == 37
        calls.append(payload)
        return _response()

    monkeypatch.setattr(OpenAIResponsesProvider, '_urllib_transport', transport)
    runtime = build_orchestrator(Settings.from_env({
        'MALBUT_AGENT_PROVIDER': 'openai', 'OPENAI_API_KEY': 'test-only',
        'MALBUT_AGENT_DB': ':memory:', 'OPENAI_MODEL': 'gpt-6-astra',
        'OPENAI_REASONING_EFFORT': 'low',
        'OPENAI_SUMMARY_MODEL': summary_model,
        'OPENAI_SUMMARY_REASONING_EFFORT': summary_effort,
        'MALBUT_AGENT_TIMEOUT_SECONDS': '37',
        'MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS': '40',
    }), http_server=False)
    try:
        assert calls == []
        foreground = runtime.provider._providers[0]
        assert foreground.model == 'gpt-6-astra'
        assert foreground.reasoning_effort == 'low'
        result = runtime.context_compactor.summarizer.summarize(
            None, [SummarySourceTurn(1, 'turn-1', '일요일이야.', '알겠어.')], [], 100,
        )
        assert len(calls) == 1
        assert calls[0]['model'] == (summary_model or 'gpt-6-astra')
        assert calls[0]['reasoning'] == {'effort': summary_effort or 'low'}
        metadata = json.loads(result.state_json)
        assert metadata['model'] == calls[0]['model']
        assert metadata['reasoning_effort'] == (summary_effort or 'low')
    finally:
        runtime.close()


@pytest.mark.parametrize('name,value', [
    ('OPENAI_SUMMARY_MODEL', 'invalid model'),
    ('OPENAI_SUMMARY_MODEL', 'model\nname'),
    ('OPENAI_SUMMARY_MODEL', 'x' * 129),
    ('OPENAI_SUMMARY_REASONING_EFFORT', 'unsupported'),
])
def test_invalid_summary_environment_setting_is_rejected(name, value):
    from malbut_agent_server.config import Settings

    settings = Settings.from_env({
        'MALBUT_AGENT_PROVIDER': 'openai', 'OPENAI_API_KEY': 'test-only',
        name: value,
    })
    with pytest.raises(ValueError, match=name):
        settings.validate_for_dialogue()


@pytest.mark.parametrize('response', [
    {'status': 'incomplete', 'output': _response()['output']},
    {'status': 'failed', 'error': {'message': 'sensitive server detail'}},
    {'status': 'completed', 'output': None},
    {'status': 'completed', 'output': [{'type': 'function_call'}]},
    {'status': 'completed', 'output': [{
        'type': 'message', 'role': 'assistant', 'status': 'completed',
        'content': [{'type': 'refusal', 'refusal': 'refused'}],
    }]},
    {'status': 'completed', 'output': [{
        'type': 'message', 'role': 'assistant', 'status': 'incomplete',
        'content': [{'type': 'output_text', 'text': 'partial'}],
    }]},
    _response(' '),
    _response('token ' * 9000),
])
def test_unusable_summary_never_becomes_a_result(response):
    summarizer = OpenAISemanticSummarizer(
        'test-key', transport=lambda *args: response,
    )
    with pytest.raises(ProviderError):
        summarizer.summarize(
            None, [SummarySourceTurn(1, 'turn-1', '일요일이야.', '알겠어.')], [], 100,
        )


def test_local_token_count_handles_korean_json_and_untrusted_special_tokens():
    value = {'user': '안녕 <|endoftext|>', 'assistant': '반가워'}
    serialized = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    assert count_tokens(value) == count_tokens(serialized) > 0
    assert count_tokens('') == 0


def test_invalid_source_and_nonofficial_endpoint_do_not_call_transport():
    def transport(*args):
        pytest.fail('invalid input must not reach transport')

    with pytest.raises(ValueError, match='official'):
        OpenAISemanticSummarizer(
            'test-key', base_url='https://example.com', transport=transport,
        )
    summarizer = OpenAISemanticSummarizer('test-key', transport=transport)
    turn = SummarySourceTurn(1, 'turn-1', '사용자 말', '응답')
    with pytest.raises(ValueError, match='precede'):
        summarizer.summarize(None, [turn], [turn], 100)
    with pytest.raises(ValueError, match='positive'):
        summarizer.summarize(None, [turn], [], True)
