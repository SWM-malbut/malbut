"""Real turn commits continue during blocked semantic compaction."""

import json
import threading

import pytest

from malbut_agent_server.context_compaction import BackgroundContextCompactor
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.reliable import (
    ContextBudgetExceeded, ReliableProvider,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import AgentDecision, AgentRequest, ProviderResult
from malbut_agent_server.summarization import SummaryResult


class _Provider(AgentProvider):
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None):
        self.calls.append((request, tuple(conversation_turns), conversation_summary))
        if self.error:
            raise self.error
        return ProviderResult(
            decision=AgentDecision(type='message', message='응답했어요.'),
            provider='test', model='deterministic', latency_ms=0.0,
        )


class _Counter:
    """Control the threshold exactly while checking actual summary reduction."""
    estimate = 0

    def __call__(self, value):
        if isinstance(value, dict) and 'conversation_history_untrusted' in value:
            if value['current_user_utterance']:
                return self.estimate
            return sum(len(turn['user']) + len(turn['assistant'])
                       for turn in value['conversation_history_untrusted'])
        return len(value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False,
        ))


class _BlockedSummarizer:
    def __init__(self, outcome='요약: 친구가 고양이를 키운다.'):
        self.entered, self.release = threading.Event(), threading.Event()
        self.outcome = outcome
        self.calls = []

    def summarize(self, previous, source, recent, target):
        self.calls.append((previous, source, recent, target))
        self.entered.set()
        assert self.release.wait(5), 'test must release the summary request'
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SummaryResult(self.outcome, '{}', 'openai-semantic-v1')


class _Lab:
    def __init__(self, path, outcome='요약: 친구가 고양이를 키운다.'):
        self.path = str(path)
        self.store = SQLiteConversationStore(self.path, semantic_context=True)
        self.memory = SQLiteMemoryStore(self.path)
        self.provider, self.counter = _Provider(), _Counter()
        self.summarizer = _BlockedSummarizer(outcome)
        self.compactor = BackgroundContextCompactor(
            self.store, self.memory, self.summarizer,
            token_budget=1024, token_counter=self.counter,
        )
        self.runtime = AgentOrchestrator(
            self.provider, self.memory, self.store, SafetyPolicy(),
            context_compactor=self.compactor,
        )
        self.ordinal = 0
        self.store.create('alice', 'room')

    def say(self, text):
        self.ordinal += 1
        return self.runtime.handle(AgentRequest.from_dict({
            'request_id': f'request-{self.ordinal}', 'user_id': 'alice',
            'conversation_id': 'room', 'turn_id': f'turn-{self.ordinal}',
            'utterance': text, 'robot_state': {}, 'available_tools': [],
        }))

    def finish_summary(self):
        self.summarizer.release.set()
        self.compactor._thread.join(timeout=2)
        assert not self.compactor._thread.is_alive()

    def close(self):
        self.summarizer.release.set()
        self.runtime.close()


def test_threshold_background_reply_order_and_restart_keep_all_raw_turns(tmp_path):
    lab = _Lab(tmp_path / 'context.sqlite3')
    try:
        first_text = ('친구가 고양이를 키워. ' + '대화 원문 ' * 160).strip()
        lab.say(first_text)
        lab.say('이 말은 최근 원문으로 남아요.')
        lab.counter.estimate = 921  # Just below 90% of 1024.
        lab.say('이어지는 대화예요.')
        assert not lab.summarizer.calls

        lab.counter.estimate = 922
        current = lab.say('현재 발화예요.')
        assert lab.summarizer.entered.wait(2)
        following = lab.say('압축 중에 새로 말했어요.')
        assert current.decision.message and following.decision.message
        assert lab.compactor._thread.is_alive()
        assert not lab.summarizer.release.is_set()
        assert lab.store.get_summary('alice', 'room') is None
        assert len(lab.summarizer.calls) == 1
        _, source, recent, target = lab.summarizer.calls[0]
        assert [turn.ordinal for turn in source] == [1]
        assert [turn.ordinal for turn in recent] == [2, 3]
        assert target == (int(1024 * 0.3)
                          - sum(len(turn.user_content) + len(turn.assistant_content)
                                for turn in recent))
        assert source[0].user_content == first_text

        lab.finish_summary()
        lab.counter.estimate = 0
        lab.say('압축이 끝난 뒤의 질문이에요.')
        _, history, summary = lab.provider.calls[-1]
        assert summary.content == lab.summarizer.outcome
        assert summary.source_end_ordinal == 1
        assert [turn.ordinal for turn in history] == [2, 3, 4, 5]
        snapshot = lab.store.snapshot('alice', 'room')
        assert [turn.ordinal for turn in snapshot.turns] == [1, 2, 3, 4, 5, 6]
        assert snapshot.turns[0].user_content == first_text
    finally:
        lab.close()

    reopened = SQLiteConversationStore(lab.path, semantic_context=True)
    try:
        snapshot = reopened.snapshot('alice', 'room')
        assert [turn.ordinal for turn in snapshot.turns] == [1, 2, 3, 4, 5, 6]
        assert snapshot.summary.content == lab.summarizer.outcome
        begin = reopened.begin_turn('alice', 'room', 'turn-7', 'request-7',
                                    'fingerprint-7', '재시작 뒤 이어갈게.')
        assert [turn.ordinal for turn in begin.history] == [2, 3, 4, 5, 6]
        assert begin.summary == snapshot.summary
    finally:
        reopened.close()


def test_summary_api_excludes_recent_correction_and_current_but_keeps_raw_history(
    tmp_path,
):
    from malbut_agent_server.semantic_summary import OpenAISemanticSummarizer

    lab = _Lab(tmp_path / 'summary-boundary.sqlite3', '모임은 일요일이에요.')
    entered, release = threading.Event(), threading.Event()
    payloads = []

    def transport(url, headers, payload, timeout):
        payloads.append(payload)
        entered.set()
        assert release.wait(5), 'test must release the summary request'
        return {'status': 'completed', 'output': [{
            'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{
                'type': 'output_text', 'text': '일요일 모임의 장소를 정했어요.',
            }],
        }]}

    try:
        lab.say('모임은 일요일이에요. ' + '이전 준비 이야기 ' * 100)
        lab.say('시작 시간은 오후 두 시예요.')
        lab.counter.estimate = 922
        lab.say('지금까지 정한 내용을 이어갈게요.')
        assert lab.summarizer.entered.wait(2)
        lab.finish_summary()
        previous = lab.store.get_summary('alice', 'room')
        lab.counter.estimate = 0
        lab.say('장소는 은서 집이에요. ' + '장소를 고른 이야기 ' * 100)
        correction = '시작 시간은 오후 세 시로 바뀌었어요.'
        lab.say(correction)
        before = lab.store.snapshot('alice', 'room').turns

        lab.compactor.summarizer = OpenAISemanticSummarizer(
            api_key='test-only', transport=transport,
        )
        lab.counter.estimate = 922
        current = '끝나는 시간은 네 시 그대로예요.'
        lab.say(current)
        assert entered.wait(2)
        assert len(payloads) == 1
        sent = json.loads(payloads[0]['input'])
        assert set(sent) == {
            'previous_summary_untrusted', 'source_turns_untrusted',
            'summary_target_tokens',
        }
        assert sent['previous_summary_untrusted'] == previous.content
        assert sent['source_turns_untrusted'] == [
            {'ordinal': turn.ordinal, 'user': turn.user_content,
             'assistant': turn.assistant_content}
            for turn in before[1:4]
        ]
        assert correction not in payloads[0]['input']
        assert current not in payloads[0]['input']
        release.set()
        lab.compactor._thread.join(timeout=2)
        assert not lab.compactor._thread.is_alive()

        lab.counter.estimate = 0
        following = '그럼 이야기를 이어가 주세요.'
        lab.say(following)
        _, history, summary = lab.provider.calls[-1]
        assert summary.content == '일요일 모임의 장소를 정했어요.'
        assert summary.source_end_ordinal == 4
        expected_recent = [
            (5, correction, '응답했어요.'), (6, current, '응답했어요.'),
        ]
        assert [(turn.ordinal, turn.user_content, turn.assistant_content)
                for turn in history] == expected_recent
        assert [(turn.ordinal, turn.user_content, turn.assistant_content)
                for turn in lab.store.snapshot('alice', 'room').turns] == [
            (turn.ordinal, turn.user_content, turn.assistant_content)
            for turn in before
        ] + [(6, current, '응답했어요.'), (7, following, '응답했어요.')]
    finally:
        release.set()
        lab.close()


@pytest.mark.parametrize('outcome', [
    RuntimeError('simulated failure'), '더 긴 결과 ' * 2000,
], ids=['failure', 'nonreducing'])
def test_failed_or_nonreducing_summary_keeps_context_and_does_not_retry_each_turn(
    tmp_path, outcome,
):
    lab = _Lab(tmp_path / 'failed.sqlite3', outcome)
    try:
        lab.say('보존할 원문 ' * 100)
        lab.say('최근 발화는 원문으로 유지해요.')
        lab.counter.estimate = 922
        lab.say('압축을 시작할 발화예요.')
        assert lab.summarizer.entered.wait(2)
        lab.finish_summary()
        lab.say('실패해도 다음 대화는 계속해요.')
        assert len(lab.summarizer.calls) == 1
        assert lab.store.get_summary('alice', 'room') is None
        assert [turn.ordinal for turn in lab.provider.calls[-1][1]] == [1, 2, 3]
        assert len(lab.store.snapshot('alice', 'room').turns) == 4
    finally:
        lab.close()


@pytest.mark.parametrize('invalidate', ['reset', 'memory_policy'])
def test_reset_or_memory_change_fences_late_summary(tmp_path, invalidate):
    lab = _Lab(tmp_path / 'stale.sqlite3')
    try:
        lab.say('이전에 말한 원문 ' * 100)
        lab.say('최근 발화는 원문으로 유지해요.')
        lab.counter.estimate = 922
        lab.say('압축 시작 발화예요.')
        assert lab.summarizer.entered.wait(2)
        if invalidate == 'reset':
            lab.store.reset('alice', 'room')
        else:
            lab.memory.invalidate_answers('alice')
        lab.finish_summary()
        assert lab.store.get_summary('alice', 'room') is None
        with lab.store._lock:
            assert lab.store._connection.execute(
                'SELECT COUNT(*) FROM conversation_turns WHERE status="completed"',
            ).fetchone()[0] == 3
    finally:
        lab.close()


def test_context_hard_limit_is_explained_without_retry_or_fallback_calls():
    primary, fallback = _Provider(ContextBudgetExceeded()), _Provider()
    reliable = ReliableProvider(
        [primary, fallback], max_retries=3,
        sleep=lambda _: pytest.fail('input limit must not retry'),
    )
    request = AgentRequest.from_dict({
        'request_id': 'limit-request', 'user_id': 'alice',
        'conversation_id': 'room', 'turn_id': 'limit-turn',
        'utterance': '대화를 계속하자.', 'robot_state': {}, 'available_tools': [],
    })
    result = reliable.complete(request, [], [], [])
    assert result.decision.type == 'refusal'
    assert result.decision.reason == 'conversation_context_limit'
    assert '분량을 넘었어요' in result.decision.message
    assert len(primary.calls) == 1
    assert fallback.calls == []


def test_factory_enables_compaction_only_for_foreground_openai(monkeypatch):
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator
    from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider

    def no_network(*args, **kwargs):
        pytest.fail('factory construction must not call OpenAI')

    monkeypatch.setattr(OpenAIResponsesProvider, '_urllib_transport', no_network)
    settings = Settings.from_env({
        'MALBUT_AGENT_PROVIDER': 'openai', 'OPENAI_API_KEY': 'test-only',
        'MALBUT_AGENT_DB': ':memory:',
        'MALBUT_AGENT_CONVERSATION_TOKEN_BUDGET': '8192',
        'OPENAI_REASONING_EFFORT': 'low',
    })
    runtime = build_orchestrator(settings, http_server=False)
    try:
        assert runtime.conversation_store.semantic_context
        assert runtime.context_compactor.token_budget == 8192
        assert runtime.context_compactor.summarizer.model == 'gpt-6-luna'
        assert runtime.context_compactor.summarizer.reasoning_effort == 'low'
        assert Settings().openai_summary_model == 'gpt-6-luna'
        assert Settings().openai_summary_reasoning_effort == 'low'
        assert runtime.provider._providers[0].model == 'gpt-5.6-luna'
        assert runtime.provider._providers[0].reasoning_effort == 'low'
        assert settings.request_timeout_seconds == 5
        assert settings.provider_total_timeout_seconds == 11
        assert runtime.context_compactor.summarizer.timeout_seconds == 30
        assert runtime.provider._providers[0].semantic_context
        assert runtime.provider._providers[0].max_input_tokens == 16384
        assert not runtime.memory_source_reviewer.provider._providers[0].semantic_context
        assert not runtime.automatic_memory_extractor.provider._providers[0].semantic_context
        assert runtime.context_compactor._thread is None
    finally:
        runtime.close()
    mock = build_orchestrator(Settings())
    try:
        assert mock.context_compactor is None
        assert not mock.conversation_store.semantic_context
    finally:
        mock.close()
