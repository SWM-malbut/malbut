"""Actual SQLite and speech queue ordering, revocation, timeout and failure."""

import sqlite3
import threading
from unittest.mock import patch

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.schemas import AgentDecision
from malbut_agent_server.speech_dialogue import DialogueWorker, MEMORY_CHANGED_RESPONSE
from test_speech_dialogue import FixedProvider, RuntimeFactory, collect, wait_until


def test_delivery_setting_is_explicit_and_validated():
    assert Settings.from_env({}).speech_publish_before_save is True
    assert not Settings.from_env({'MALBUT_AGENT_SPEECH_PUBLISH_BEFORE_SAVE': 'false'}).speech_publish_before_save
    assert Settings.from_env({'MALBUT_AGENT_SPEECH_PUBLISH_BEFORE_SAVE': 'true'}).speech_publish_before_save
    with pytest.raises(ValueError):
        Settings.from_env({'MALBUT_AGENT_SPEECH_PUBLISH_BEFORE_SAVE': 'yes'})


@pytest.mark.parametrize('early_delivery', [False, True])
def test_semantic_close_sends_only_control_even_with_early_delivery(tmp_path, early_delivery):
    provider = FixedProvider(lambda *_: AgentDecision(
        type='message', message='편안히 쉬세요.', reason='end_conversation'))
    factory = RuntimeFactory(provider, str(tmp_path / 'speech.sqlite3'))
    worker = DialogueWorker(factory, 'speaker', publish_before_save=early_delivery)
    try:
        assert worker.submit('last-turn', '눈이 감기네. 우리 내일 이어서 하면 좋겠다.')
        reply, = collect(worker, 1)
        signals = []
        assert worker.publish_reply(reply, lambda _: pytest.fail('unexpected final speech'),
                                    end_dialogue=lambda r: signals.append(r['utterance_id']) or True)
        assert signals == ['last-turn']
        assert worker.drain() == []
    finally:
        worker.close()


@pytest.mark.parametrize('early_delivery', [False, True])
def test_standalone_thanks_is_spoken_and_accepts_the_next_turn_in_the_same_room(tmp_path, early_delivery):
    provider = FixedProvider(lambda *_: AgentDecision('message', '세 개예요.', reason='general_knowledge'))
    worker = DialogueWorker(RuntimeFactory(provider, str(tmp_path/'speech.sqlite3')), 'speaker',
                            publish_before_save=early_delivery)
    try:
        delivered = []
        for uid, utterance in [('question', '세모에는 변이 몇 개야?'), ('thanks', '고마워.'), ('next', '네모는?')]:
            assert worker.submit(uid, utterance)
            reply, = collect(worker, 1)
            assert worker.publish_reply(reply, lambda text: delivered.append((reply['conversation_id'], text)) or True,
                                        end_dialogue=lambda _: pytest.fail('ordinary thanks closed the dialogue'))
        assert len({room for room, _ in delivered}) == 1
        assert delivered[1][1] == '천만에요.'
        assert [call[0].utterance for call in provider.calls] == ['세모에는 변이 몇 개야?', '네모는?']
    finally:
        worker.close()


def test_publishes_before_durable_result_and_keeps_next_turn_order(tmp_path):
    path = str(tmp_path / 'speech.sqlite3')
    factory = RuntimeFactory(database_path=path)
    worker = DialogueWorker(factory, 'speaker', publish_before_save=True)
    release = threading.Event()
    enqueuing = threading.Event()
    try:
        wait_until(lambda: worker.ready)
        after_turn = factory.runtime.story_memory.after_turn

        def delayed_enqueue(*args):
            enqueuing.set()
            assert release.wait(5)
            return after_turn(*args)

        with patch.object(factory.runtime.story_memory, 'after_turn', delayed_enqueue):
            assert worker.submit('first', '오늘 어때?')
            reply = collect(worker, 1)[0]
            assert reply._delivery_ticket is not None
            observed = []

            def publish(text):
                with sqlite3.connect(path, timeout=0) as outside:
                    row = outside.execute('SELECT status,assistant_content FROM conversation_turns').fetchone()
                    assert row == ('pending', None)
                    # Another process cannot erase a source between the final
                    # validation and this publication.
                    with pytest.raises(sqlite3.OperationalError):
                        outside.execute('BEGIN IMMEDIATE')
                observed.append(text)
                return True

            assert worker.publish_reply(reply, publish)
            assert enqueuing.wait(5)
            assert worker.submit('next', '다음 질문')
            assert len(factory.provider.calls) == 1
            release.set()
            next_reply = collect(worker, 1)[0]
            assert worker.publish_reply(next_reply, lambda text: observed.append(text) or True)
            wait_until(lambda: worker.has_capacity() and len(factory.completed) == 2)
            assert len(observed) == 2
            assert '이전: 오늘 어때?' in observed[1]
            assert worker.drain() == []
            with sqlite3.connect(path) as outside:
                assert outside.execute("SELECT COUNT(*) FROM conversation_turns WHERE status='completed'").fetchone()[0] == 2
    finally:
        release.set()
        worker.close()


def test_late_drained_ticket_expires_and_only_saved_reply_can_publish(tmp_path):
    factory = RuntimeFactory(database_path=str(tmp_path / 'speech.sqlite3'))
    worker = DialogueWorker(factory, 'speaker', publish_before_save=True)
    try:
        assert worker.submit('first', '안녕')
        early = collect(worker, 1)[0]
        assert early._delivery_ticket is not None
        wait_until(lambda: bool(factory.completed))
        published = []
        assert worker.publish_reply(early, lambda text: published.append(text) or True) is None
        saved = collect(worker, 1)[0]
        assert saved._delivery_ticket is None
        assert worker.publish_reply(saved, lambda text: published.append(text) or True)
        assert len(published) == 1
        assert worker.has_capacity()
    finally:
        worker.close()


def test_suspended_early_reply_never_publishes_or_leaks_capacity():
    factory = RuntimeFactory()
    worker = DialogueWorker(factory, 'speaker', capacity=1, publish_before_save=True)
    try:
        assert worker.submit('old', '안녕')
        early = collect(worker, 1)[0]
        worker.suspend()
        worker.resume()
        wait_until(lambda: bool(factory.completed))
        assert worker.publish_reply(early, lambda text: pytest.fail('stale speech')) is None
        assert worker.drain() == []
        assert worker.has_capacity()
    finally:
        worker.close()


def test_fact_revocation_during_inference_prevents_early_delivery(tmp_path):
    release = threading.Event()

    def respond(request, history):
        assert release.wait(5)
        return AgentDecision(type='message', message='오래된 기억을 사용한 답변')

    factory = RuntimeFactory(FixedProvider(respond), str(tmp_path / 'speech.sqlite3'))
    worker = DialogueWorker(factory, 'speaker', publish_before_save=True)
    try:
        assert worker.submit('first', '안녕')
        assert factory.provider.entered.wait(5)
        memory = SQLiteMemoryStore(factory.database_path)
        try:
            memory.invalidate_answers('speaker')
        finally:
            memory.close()
        release.set()
        reply = collect(worker, 1)[0]
        assert reply._delivery_ticket is None
        assert reply['text'] == MEMORY_CHANGED_RESPONSE
    finally:
        release.set()
        worker.close()


def test_save_failure_after_publication_does_not_speak_twice(tmp_path, caplog):
    factory = RuntimeFactory(database_path=str(tmp_path / 'speech.sqlite3'))
    worker = DialogueWorker(factory, 'speaker', publish_before_save=True)
    try:
        wait_until(lambda: worker.ready)
        complete = factory.runtime.conversation_store.complete_turn

        def fail_after_handoff(*args, **kwargs):
            callback = kwargs['before_commit']

            def fail():
                callback()
                raise RuntimeError('simulated disk failure')

            kwargs['before_commit'] = fail
            return complete(*args, **kwargs)

        with patch.object(factory.runtime.conversation_store, 'complete_turn', fail_after_handoff):
            assert worker.submit('first', '안녕')
            reply = collect(worker, 1)[0]
            published = []
            assert worker.publish_reply(reply, lambda text: published.append(text) or True)
            wait_until(lambda: worker._running is False)
            assert len(published) == 1
            assert worker.drain() == []
            assert 'speech_result_save_failed' in caplog.text
    finally:
        worker.close()


def test_memory_management_receipt_waits_for_save(tmp_path):
    factory = RuntimeFactory(database_path=str(tmp_path / 'speech.sqlite3'))
    worker = DialogueWorker(factory, 'speaker', publish_before_save=True)
    try:
        assert worker.submit('first', '개인화 켜줘')
        reply = collect(worker, 1)[0]
        assert reply._delivery_ticket is None
        assert factory.completed
    finally:
        worker.close()
