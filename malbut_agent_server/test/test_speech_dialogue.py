"""Conversation reuse, bounded admission and worker-owned store lifetime."""

import hashlib
from concurrent.futures import CancelledError
import json
import threading
import time

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult, SpeechAgentRequest, ValidationError,
)
from malbut_agent_server.speech_dialogue import (
    DialogueWorker, ERROR_RESPONSE, SESSION_ERROR_RESPONSE,
    validate_dialogue_input,
)


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError('worker did not reach the expected condition')


def collect(worker, count):
    replies = []

    def drained():
        replies.extend(worker.drain())
        return len(replies) >= count

    wait_until(drained)
    return replies


def test_confirmation_preempts_pending_and_late_ordinary_answers():
    release = threading.Event()

    def respond(request, history):
        if request.utterance == 'first':
            assert release.wait(5)
        return AgentDecision(type='message', message=request.utterance, confidence=1.0)

    provider = FixedProvider(respond)
    factory = RuntimeFactory(provider)
    worker = DialogueWorker(factory, 'speaker', capacity=3)
    try:
        assert worker.submit('first-id', 'first')
        assert provider.entered.wait(5)
        assert worker.submit('queued-id', 'queued')
        worker.suspend()
        assert not worker.has_capacity()
        assert worker.drain() == []
        worker.resume()
        assert worker.submit('new-id', 'new')
        release.set()
        replies = collect(worker, 1)
        assert [(item['utterance_id'], item['text']) for item in replies] == [
            ('new-id', 'new'),
        ]
        assert [item[0].utterance for item in provider.calls] == ['first', 'new']
        assert worker.has_capacity()
    finally:
        release.set()
        worker.close()


def test_already_drained_answer_cannot_reappear_after_confirmation():
    worker = DialogueWorker(RuntimeFactory(), 'speaker')
    try:
        assert worker.submit('old-id', 'old')
        reply = collect(worker, 1)[0]
        worker.suspend()
        worker.resume()
        published = []
        assert worker.publish_reply(reply, lambda text: published.append(text) or True) is None
        assert published == []
    finally:
        worker.close()


class FixedProvider:
    """Use real context construction with controllable model responses."""

    def __init__(self, respond=None):
        self.calls = []
        self.respond = respond or self.answer
        self.entered = threading.Event()

    @staticmethod
    def answer(request, history):
        previous = history[-1].user_content if history else '없음'
        return AgentDecision(
            type='message', message=f'이전: {previous}; 현재: {request.utterance}',
            confidence=1.0,
        )

    def complete(
        self, request, memories, history, tools, conversation_summary=None,
    ):
        self.calls.append((request, list(history), tools))
        self.entered.set()
        return ProviderResult(
            decision=self.respond(request, history), provider='fixed-test',
            model='fixed-test', latency_ms=0.0,
        )


class RuntimeFactory:
    """Build a real orchestrator and record creation and cleanup threads."""

    def __init__(self, provider=None, database_path=':memory:'):
        self.provider = provider or FixedProvider()
        self.database_path = database_path
        self.created = []
        self.closed = []
        self.handled = []
        self.completed = []
        self.runtime = None

    def __call__(self):
        self.created.append(threading.get_ident())
        runtime = build_orchestrator(Settings(
            database_path=self.database_path,
        ))
        runtime.provider = self.provider
        original_handle = runtime.handle

        def handle(request):
            self.handled.append(request)
            response = original_handle(request)
            self.completed.append(response)
            return response

        runtime.handle = handle
        for name in ('conversation_store', 'memory_store'):
            store = getattr(runtime, name)
            close = store.close

            def tracked_close(name=name, close=close):
                self.closed.append((name, threading.get_ident()))
                close()

            store.close = tracked_close
        self.runtime = runtime
        return runtime


def test_cancellation_discards_queued_notices_and_releases_capacity():
    from malbut_agent_server.conversation_progress import claim_retry

    def respond(request, history):
        if request.utterance == '취소할 요청':
            assert claim_retry('다시 확인할게요.')
            raise CancelledError('canceled')
        return FixedProvider.answer(request, history)

    factory = RuntimeFactory(FixedProvider(respond))
    worker = DialogueWorker(factory, 'user', capacity=1)
    try:
        assert worker.submit('canceled', '취소할 요청')
        wait_until(lambda: factory.provider.entered.is_set() and worker.has_capacity())
        assert worker.drain() == []
        assert worker.submit('next', '다음 요청')
        reply = collect(worker, 1)[0]
        assert reply['kind'] == 'answer' and '다음 요청' in reply['text']
        assert len(factory.provider.calls) == 2
        assert factory.provider.calls[1][1] == []
    finally:
        worker.close()


@pytest.mark.parametrize('phase', ['runtime', 'session'])
def test_ready_waits_for_runtime_and_session_initialization(phase):
    """Expose readiness only after all asynchronous startup work has finished."""
    entered, release = threading.Event(), threading.Event()
    factory = RuntimeFactory()

    def wait():
        entered.set()
        assert release.wait(5)

    def delayed_runtime():
        if phase == 'runtime':
            wait()
        runtime = factory()
        if phase == 'session':
            create = runtime.conversation_store.create

            def delayed_session(user_id):
                wait()
                return create(user_id)

            runtime.conversation_store.create = delayed_session
        return runtime

    worker = DialogueWorker(delayed_runtime, 'speaker')
    try:
        assert entered.wait(5)
        assert not worker.ready
        assert worker.startup_error is None
        release.set()
        wait_until(lambda: worker.ready)
    finally:
        release.set()
        worker.close()
    assert not worker.ready


def test_same_worker_reuses_context_with_original_input_and_safe_ids():
    factory = RuntimeFactory()
    worker = DialogueWorker(factory, 'configured-speaker')
    first_id = '한 번의 발화\n' + 'x' * 300
    original = '  첫 번째 이야기\n'
    try:
        assert worker.submit(first_id, original)
        assert worker.submit('second', '두 번째 이야기')
        replies = collect(worker, 2)
        assert replies[0]['utterance_id'] == first_id
        assert replies[0]['kind'] == replies[1]['kind'] == 'answer'
        assert replies[0]['conversation_id'] == replies[1]['conversation_id']
        assert original.strip() in replies[1]['text']
        first, second = factory.handled
        assert first.utterance == original
        assert first.user_id == second.user_id == 'configured-speaker'
        assert first.available_tools == second.available_tools == ()
        digest = hashlib.sha256(first_id.encode('utf-8')).hexdigest()
        assert first.request_id == 'speech-request-' + digest
        assert first.turn_id == 'speech-turn-' + digest
        assert first.request_id != first.turn_id
        assert all(call[2] == [] for call in factory.provider.calls)
        stored = factory.runtime.conversation_store.snapshot(
            'configured-speaker', replies[0]['conversation_id'],
        )
        # The worker passes the original into the existing runtime. The store
        # retains its existing leading/trailing whitespace normalization.
        assert stored.turns[0].user_content == original.strip()
    finally:
        worker.close()
    assert factory.created == [factory.closed[0][1]]
    assert factory.created[0] != threading.get_ident()
    assert [name for name, _ in factory.closed] == [
        'conversation_store', 'memory_store',
    ]


def test_failed_provider_turn_does_not_poison_the_next_utterance():
    def respond(request, history):
        if request.utterance == '실패':
            raise ProviderError('private credential-like error content')
        return FixedProvider.answer(request, history)

    factory = RuntimeFactory(FixedProvider(respond))
    worker = DialogueWorker(factory, 'speaker')
    try:
        assert worker.submit('failure', '실패')
        assert worker.submit('success', '다시 안녕')
        replies = collect(worker, 2)
        assert replies[0]['kind'] == 'error'
        assert replies[0]['text'] == ERROR_RESPONSE
        assert replies[1]['kind'] == 'answer'
        assert '다시 안녕' in replies[1]['text']
        assert len(factory.provider.calls) == 2
        assert factory.provider.calls[1][1] == []
    finally:
        worker.close()


@pytest.mark.parametrize('session_problem', [
    'expired', 'closed', 'missing', 'turn_limit',
])
def test_inactive_session_rotates_but_legacy_turn_limit_is_reported(
    session_problem,
):
    now = [1000.0]
    factory = RuntimeFactory()

    def timed_runtime():
        runtime = factory()
        runtime.conversation_store._clock = lambda: now[0]
        runtime.conversation_store.max_turns_per_session = 10
        return runtime

    worker = DialogueWorker(timed_runtime, 'speaker')
    try:
        worker.submit('initial', '안녕')
        first = collect(worker, 1)[0]
        conversation_id = first['conversation_id']
        store = factory.runtime.conversation_store
        if session_problem == 'expired':
            now[0] += 3600
        elif session_problem == 'closed':
            store.close_session('speaker', conversation_id)
        elif session_problem == 'missing':
            store.delete('speaker', conversation_id)
        else:
            for index in range(9):
                worker.submit(f'fill-{index}', '추가 발화')
            assert len(collect(worker, 9)) == 9
        previous_calls = len(factory.provider.calls)
        for index in range(2):
            assert worker.submit(f'repeated-{index}', '다시 말할게')
            reply = collect(worker, 1)[0]
            if session_problem == 'turn_limit':
                assert reply['kind'] == 'error'
                assert reply['text'] == SESSION_ERROR_RESPONSE
                assert reply['conversation_id'] == conversation_id
            else:
                assert reply['kind'] == 'answer'
                assert reply['conversation_id'] != conversation_id
        assert len(factory.provider.calls) == previous_calls + (
            0 if session_problem == 'turn_limit' else 2
        )
    finally:
        worker.close()


def test_non_action_meanings_and_existing_tool_refusal_are_preserved():
    decisions = iter([
        AgentDecision(type='clarification', message='어떤 대화를 할까요?'),
        AgentDecision(type='refusal', message='이 요청은 지원하지 않아요.'),
        AgentDecision(type='tool_call', tool_name='navigate',
                      arguments={'location': '거실'}, message='이동할게요.'),
    ])
    provider = FixedProvider(lambda request, history: next(decisions))
    factory = RuntimeFactory(provider)
    worker = DialogueWorker(factory, 'speaker')
    try:
        for index in range(3):
            assert worker.submit(str(index), '발화')
        replies = collect(worker, 3)
        assert replies[0]['text'] == '어떤 대화를 할까요?'
        assert replies[1]['text'] == '이 요청은 지원하지 않아요.'
        assert '신뢰된 로컬 ROS 상태가 없어' in replies[2]['text']
        assert all(reply['kind'] == 'answer' for reply in replies)
        assert factory.completed[2].decision.type == 'refusal'
        assert not factory.completed[2].state_trusted
    finally:
        worker.close()


def test_capacity_includes_inflight_pending_and_unread_results():
    release = threading.Event()

    def respond(request, history):
        assert release.wait(5)
        return FixedProvider.answer(request, history)

    provider = FixedProvider(respond)
    factory = RuntimeFactory(provider)
    worker = DialogueWorker(factory, 'speaker', capacity=2)
    try:
        assert worker.submit('one', '첫 번째')
        assert provider.entered.wait(5)
        assert worker.submit('two', '두 번째')
        assert not worker.has_capacity()
        assert not worker.submit('three', '세 번째')
        release.set()
        wait_until(lambda: len(factory.completed) == 2)
        assert not worker.has_capacity()
        assert not worker.submit('three', '세 번째')
        assert len(collect(worker, 2)) == 2
        assert worker.has_capacity()
        assert worker.submit('three', '세 번째')
        assert collect(worker, 1)[0]['utterance_id'] == 'three'
        assert [call[0].utterance for call in provider.calls] == [
            '첫 번째', '두 번째', '세 번째',
        ]
    finally:
        release.set()
        worker.close()


def test_shutdown_discards_queued_and_late_results_before_closing_stores():
    release = threading.Event()

    def respond(request, history):
        assert release.wait(5)
        return FixedProvider.answer(request, history)

    provider = FixedProvider(respond)
    factory = RuntimeFactory(provider)
    worker = DialogueWorker(factory, 'speaker', capacity=3)
    closing = threading.Thread(target=worker.close)
    try:
        worker.submit('one', '진행 중')
        assert provider.entered.wait(5)
        worker.submit('two', '대기 중')
        closing.start()
        wait_until(lambda: not worker.has_capacity())
        assert not factory.closed
        assert closing.is_alive()
        release.set()
        closing.join(5)
        assert not closing.is_alive()
        assert worker.drain() == []
        assert len(provider.calls) == 1
        assert len(factory.closed) == 2
        assert not worker.submit('late', '늦은 발화')
    finally:
        release.set()
        if closing.ident is not None:
            closing.join(5)
        worker.close()


def test_new_worker_resumes_recent_conversation_on_the_same_database(tmp_path):
    path = str(tmp_path / 'conversation.sqlite3')
    conversations = []
    for index in range(2):
        factory = RuntimeFactory(database_path=path)
        worker = DialogueWorker(factory, 'speaker')
        try:
            worker.submit(f'utterance-{index}', '안녕')
            reply = collect(worker, 1)[0]
            conversations.append(reply['conversation_id'])
            assert len(factory.provider.calls[0][1]) == index
        finally:
            worker.close()
    assert conversations[0] == conversations[1]


def test_startup_failure_returns_queued_error_and_stops_accepting():
    release = threading.Event()

    def fail():
        assert release.wait(5)
        raise RuntimeError('private startup details')

    worker = DialogueWorker(fail, 'speaker')
    try:
        assert worker.submit('one', '안녕')
        release.set()
        reply = collect(worker, 1)[0]
        assert reply == {
            'utterance_id': 'one', 'text': ERROR_RESPONSE,
            'kind': 'error', 'conversation_id': None,
        }
        assert worker.startup_error == 'RuntimeError'
        assert not worker.ready
        assert not worker.has_capacity()
        assert not worker.submit('two', '다시 안녕')
    finally:
        release.set()
        worker.close()


def test_session_startup_failure_closes_both_stores():
    factory = RuntimeFactory()

    def fail_session():
        runtime = factory()

        def fail_create(user_id):
            raise ValueError('session unavailable')

        runtime.conversation_store.create = fail_create
        return runtime

    worker = DialogueWorker(fail_session, 'speaker')
    try:
        wait_until(lambda: worker.startup_error is not None)
        assert not worker.ready
    finally:
        worker.close()
    assert len(factory.closed) == 2


def test_worker_starts_after_one_hundred_expired_sessions(tmp_path):
    """A persisted quota of old sessions cannot permanently block speech."""
    database = str(tmp_path / 'speech.sqlite3')
    store = SQLiteConversationStore(database, clock=lambda: 1000.0)
    try:
        for number in range(100):
            store.create('speaker', f'expired-{number}')
    finally:
        store.close()

    factory = RuntimeFactory(database_path=database)
    worker = DialogueWorker(factory, 'speaker')
    try:
        wait_until(lambda: worker.ready or worker.startup_error is not None)
        assert worker.ready
        assert worker.startup_error is None
        assert worker.submit('fresh-utterance', '안녕')
        assert collect(worker, 1)[0]['kind'] == 'answer'
        assert factory.runtime.conversation_store._connection.execute(
            'SELECT COUNT(*) FROM conversation_sessions WHERE user_id = ?',
            ('speaker',),
        ).fetchone()[0] == 100
    finally:
        worker.close()


@pytest.mark.parametrize('utterance_id,text', [
    ('', '안녕'), (' \n', '안녕'), ('id', ''), ('id', ' \n'),
    ('id', 'x' * 16001), ('id', '\ud800'), ('\ud800', '안녕'),
])
def test_invalid_input_is_rejected_before_queue_admission(utterance_id, text):
    with pytest.raises(ValueError):
        validate_dialogue_input(utterance_id, text)


@pytest.mark.parametrize('length', [2001, 16000])
def test_complete_long_speech_reaches_provider_and_sqlite_once(length):
    """The final tail survives request copies, history, and JSON escaping."""
    marker = '마지막에 한 말까지 전부 전달됐습니다.'
    text = ('시작 ' + '가"\\\n' * length)[:length - len(marker)] + marker
    payloads = []

    def transport(_url, _headers, payload, _timeout):
        payloads.append(payload)
        decision = {'type': 'message', 'message': '모두 확인했어요.',
                    'reason': 'test', 'confidence': 1.0}
        if 'memory_proposal' in payload['text']['format']['schema']['properties']:
            decision['memory_proposal'] = None
        return {'status': 'completed', 'output': [{
            'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': json.dumps(decision)}],
        }]}

    factory = RuntimeFactory(provider=OpenAIResponsesProvider(
        api_key='unused-test-key', model='test-model', transport=transport,
    ))
    worker = DialogueWorker(factory, 'speaker')
    try:
        assert worker.submit('first', '이전 대화도 있습니다.')
        assert collect(worker, 1)[0]['kind'] == 'answer'
        assert worker.submit('long', text)
        reply = collect(worker, 1)[0]
        assert reply['kind'] == 'answer'
        assert len(payloads) == 2
        assert isinstance(factory.handled[-1], SpeechAgentRequest)
        data = json.loads(payloads[-1]['input'].split('\n', 1)[1])
        assert data['current_user_utterance'] == text
        assert data['conversation_history_untrusted'][0]['user'] == '이전 대화도 있습니다.'
        turns = factory.runtime.conversation_store.list_turns(
            'speaker', reply['conversation_id'],
        )
        assert len(turns) == 2
        assert turns[-1].user_content == text
        metrics = factory.completed[-1].provider_result.context_metrics
        assert metrics.current_utterance_included_chars == length
        assert 'current_user_utterance' not in metrics.truncated_sections
        assert metrics.model_input_chars <= metrics.max_model_input_chars
        # Speech support does not enlarge public HTTP input or spoken answers.
        with pytest.raises(ValidationError):
            AgentRequest.from_dict(factory.handled[-1].to_dict())
        with pytest.raises(ValidationError):
            AgentDecision(type='message', message='가' * 2001).validate()
    finally:
        worker.close()


def test_mock_dialogue_accepts_long_intro_without_optional_memory_failure():
    factory = RuntimeFactory(provider=MockProvider())
    worker = DialogueWorker(factory, 'speaker')
    text = '앞선 이야기입니다. ' * 250 + '내 이름은 민수야'
    try:
        assert worker.submit('long-intro', text)
        reply = collect(worker, 1)[0]
        assert reply['kind'] == 'answer'
        turns = factory.runtime.conversation_store.list_turns(
            'speaker', reply['conversation_id'],
        )
        assert len(turns) == 1 and turns[0].user_content == text
        assert factory.completed[0].provider_result.memory_proposal is None
    finally:
        worker.close()


@pytest.mark.parametrize('capacity', [0, -1, True, 1.5])
def test_bad_capacity_does_not_create_a_runtime(capacity):
    called = []
    with pytest.raises(ValueError):
        DialogueWorker(lambda: called.append(True), 'speaker', capacity)
    assert called == []
