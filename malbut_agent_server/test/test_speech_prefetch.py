"""Silent preparation never bypasses orchestration or publication guards."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.orchestrator import MemoryChangedError
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.schemas import AgentDecision
from malbut_agent_server.speech_dialogue import DialogueWorker, MEMORY_CHANGED_RESPONSE
from malbut_agent_server.speech_prefetch import PrefetchTurn, current_prefetch, prefetch_scope
from test_speech_dialogue import FixedProvider, RuntimeFactory, collect


TEXT = '안녕하세요. 다음 이야기를 들려주세요.'
PREFIX = '안녕하세요. 다'


class PreviewProvider(FixedProvider):
    def __init__(self, *, blocked=False, fail=False):
        super().__init__(lambda req, history: AgentDecision(type='message', message=TEXT))
        self.prepared = threading.Event()
        self.release = threading.Event()
        self.fail = fail
        self.turns = []
        if not blocked:
            self.release.set()

    def complete(self, request, *args, **kwargs):
        turn = current_prefetch(request.request_id)
        self.turns.append(turn)
        if turn is not None and turn.begin_attempt():
            turn.prepare(PREFIX)
        self.prepared.set()
        assert self.release.wait(5)
        if self.fail:
            raise ProviderError('offline interrupted stream')
        result = super().complete(request, *args, **kwargs)
        if turn is not None:
            turn.approve_provider(result.decision)
        return result


def test_opt_in_is_strict_and_off_by_default():
    assert Settings().speech_prefetch_enabled is False
    assert Settings.from_env({}).speech_prefetch_enabled is False
    for text in ('true', '1', 'TRUE'):
        assert Settings.from_env({'MALBUT_AGENT_SPEECH_PREFETCH_ENABLED': text}).speech_prefetch_enabled
    for text in ('false', '0'):
        assert not Settings.from_env({'MALBUT_AGENT_SPEECH_PREFETCH_ENABLED': text}).speech_prefetch_enabled
    with pytest.raises(ValueError):
        Settings.from_env({'MALBUT_AGENT_SPEECH_PREFETCH_ENABLED': 'yes'})
    with pytest.raises(ValueError):
        Settings(speech_prefetch_enabled='false').validate_for_dialogue(require_api_key=False)


def test_default_worker_never_exposes_streaming_hints():
    provider = PreviewProvider()
    worker = DialogueWorker(RuntimeFactory(provider), 'speaker')
    try:
        assert worker.submit('u', '안녕')
        assert collect(worker, 1)[0]['text'] == TEXT
        assert provider.turns == [None]
        assert worker.drain_preparations() == []
    finally:
        worker.close()


def test_preparation_can_start_while_runtime_waits_but_one_final_requires_commit():
    provider = PreviewProvider(blocked=True)
    factory = RuntimeFactory(provider)
    worker = DialogueWorker(factory, 'speaker', prefetch_enabled=True)
    try:
        assert worker.submit('utterance', '안녕')
        assert provider.prepared.wait(5)
        commands = worker.drain_preparations()
        assert [c.operation for c in commands] == ['prepare']
        assert worker.drain() == []
        assert factory.completed == []  # No orchestration or memory commit yet.
        provider.release.set()
        reply = collect(worker, 1)[0]
        assert len(factory.completed) == 1
        assert reply._prepared_hint == {}
        sent = []
        result = worker.publish_reply(reply, lambda text: sent.append((text, dict(reply._prepared_hint))) or True)
        assert result['text'] == TEXT
        assert sent == [(TEXT, {'prepared_id': commands[0].prepared_id,
                               'generation_id': commands[0].generation_id})]
        assert '_prepared_hint' not in json.dumps(result)
        assert worker.drain_preparations() == []
    finally:
        provider.release.set()
        worker.close()


@pytest.mark.parametrize('invalidator', ['memory', 'rewritten', 'publish_failed', 'suspend', 'new_input', 'close'])
def test_stale_or_changed_final_discards_preparation(invalidator):
    provider = PreviewProvider()
    worker = DialogueWorker(RuntimeFactory(provider), 'speaker', prefetch_enabled=True)
    try:
        assert worker.submit('old', '안녕')
        reply = collect(worker, 1)[0]
        command = worker.drain_preparations()[0]
        if invalidator == 'memory':
            def invalid_memory():
                raise MemoryChangedError('fixture changed')
            reply._memory_validator = invalid_memory
        elif invalidator == 'rewritten':
            reply['text'] = '검증 과정에서 바뀐 답변이에요.'
        elif invalidator == 'suspend':
            worker.suspend()
        elif invalidator == 'new_input':
            assert worker.submit('new', '새 질문')
        elif invalidator == 'close':
            worker.close()
        sent = []
        result = worker.publish_reply(reply, lambda text: sent.append(dict(reply._prepared_hint)) or invalidator != 'publish_failed')
        if invalidator in {'suspend', 'close', 'publish_failed'}:
            assert result is None
        if invalidator == 'memory':
            assert result['text'] == MEMORY_CHANGED_RESPONSE
        if invalidator != 'publish_failed':
            assert all(item == {} for item in sent)
        cancellations = [event for event in worker.drain_preparations()
                         if event.operation == 'cancel' and event.prepared_id == command.prepared_id]
        assert len(cancellations) == 1
    finally:
        worker.close()


def test_inflight_generation_invalidation_cannot_recreate_late_candidate():
    provider = PreviewProvider(blocked=True)
    worker = DialogueWorker(RuntimeFactory(provider), 'speaker', prefetch_enabled=True)
    try:
        assert worker.submit('old', '안녕')
        assert provider.prepared.wait(5)
        command = worker.drain_preparations()[0]
        turn = provider.turns[0]
        worker.suspend()
        worker.resume()
        turn.prepare('지나간 답변. 다')
        assert turn.begin_attempt() is False
        provider.release.set()
        assert turn.hint(TEXT) == {}
        events = worker.drain_preparations()
        assert [(e.operation, e.prepared_id) for e in events] == [('cancel', command.prepared_id)]
    finally:
        provider.release.set()
        worker.close()


def test_provider_error_after_candidate_cannot_become_final_token():
    provider = PreviewProvider(blocked=True, fail=True)
    worker = DialogueWorker(RuntimeFactory(provider), 'speaker', prefetch_enabled=True)
    try:
        assert worker.submit('u', '안녕')
        assert provider.prepared.wait(5)
        command = worker.drain_preparations()[0]
        provider.release.set()
        reply = collect(worker, 1)[0]
        assert reply['kind'] == 'error'
        assert reply._prepared_hint == {}
        assert worker.drain_preparations()[0].prepared_id == command.prepared_id
    finally:
        provider.release.set()
        worker.close()


def test_attempt_retry_uses_new_ids_and_exact_final_text():
    emitted = []
    turn = PrefetchTurn('r', 'u', emitted.append)
    assert turn.begin_attempt()
    turn.prepare(PREFIX)
    first = emitted[-1]
    turn.fail_attempt()
    assert turn.begin_attempt()
    turn.prepare(PREFIX)
    second = emitted[-1]
    assert first.generation_id != second.generation_id
    assert first.prepared_id != second.prepared_id
    turn.approve_provider(AgentDecision(type='message', message=TEXT))
    assert turn.hint(TEXT)['prepared_id'] == second.prepared_id
    assert turn.hint('접두어 ' + TEXT) == {}
    assert [e.operation for e in emitted] == ['prepare', 'cancel', 'prepare', 'cancel']


@pytest.mark.parametrize('decision', [
    AgentDecision(type='tool_call', message=TEXT, tool_name='navigate', arguments={}),
    AgentDecision(type='refusal', message=TEXT),
    AgentDecision(type='message', message='다른 답변이에요.'),
])
def test_tool_refusal_or_changed_provider_decision_discards_candidate(decision):
    emitted = []
    turn = PrefetchTurn('r', 'u', emitted.append)
    turn.begin_attempt()
    turn.prepare(PREFIX)
    turn.approve_provider(decision)
    assert turn.hint(TEXT) == {}
    assert [e.operation for e in emitted] == ['prepare', 'cancel']


def test_context_is_scoped_to_one_request_and_never_inherited_by_background_thread():
    turn = PrefetchTurn('r', 'u', lambda _: None)
    with prefetch_scope(turn), ThreadPoolExecutor(1) as workers:
        assert current_prefetch('r') is turn
        assert current_prefetch('other') is None
        assert workers.submit(current_prefetch, 'r').result() is None
    assert current_prefetch('r') is None


def test_prepare_is_bounded_one_per_attempt_and_emitter_failure_is_a_cache_miss():
    emitted = []
    turn = PrefetchTurn('r', 'u', emitted.append)
    turn.begin_attempt()
    for invalid in ('x' * 513, '\ud83d', '', None):
        turn.prepare(invalid)
    assert not emitted
    turn.prepare(PREFIX)
    turn.prepare(PREFIX + '른')
    assert len(emitted) == 1

    def unavailable(_event):
        raise RuntimeError('publisher is unavailable')
    unavailable_turn = PrefetchTurn('r', 'u', unavailable)
    unavailable_turn.begin_attempt()
    unavailable_turn.prepare(PREFIX)
    unavailable_turn.approve_provider(AgentDecision(type='message', message=TEXT))
    assert unavailable_turn.hint(TEXT) == {}
