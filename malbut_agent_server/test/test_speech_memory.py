"""Queued speech rechecks durable memory changes before text publication."""

import json
import sys
import time
from types import SimpleNamespace

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server import ros_communication
from malbut_agent_server.schemas import AgentDecision, ProviderResult
from malbut_agent_server.speech_dialogue import (
    DialogueWorker, MEMORY_CHANGED_RESPONSE,
)


OLD_REPLY = '반려견의 이름은 초코예요.'


class _FixedProvider:
    supports_memory = True

    def complete(self, *_args, **_kwargs):
        return ProviderResult(
            decision=AgentDecision(type='message', message=OLD_REPLY),
            provider='speech-memory-fixture', model='fixed', latency_ms=0,
            memory_supported=True,
        )


class _RuntimeFactory:
    def __init__(self, path, *, legacy_result=False):
        self.path = str(path)
        self.legacy_result = legacy_result
        self.guard_calls = 0
        self.closed = False

    def __call__(self):
        runtime = build_orchestrator(Settings(database_path=self.path))
        runtime.provider = _FixedProvider()
        self.record = runtime.memory_store.add('speaker', '반려견 이름은 초코')
        handle = runtime.handle
        close = runtime.memory_store.close

        def checked_handle(request):
            result = handle(request)
            if self.legacy_result:
                return SimpleNamespace(decision=result.decision)
            validator = result.memory_validator

            def check():
                assert not self.closed, 'Do not query a closed memory store'
                self.guard_calls += 1
                validator()

            result.memory_validator = check
            return result

        def checked_close():
            self.closed = True
            close()

        runtime.handle = checked_handle
        runtime.memory_store.close = checked_close
        return runtime

    def delete_from_another_connection(self):
        store = SQLiteMemoryStore(self.path)
        try:
            assert store.delete('speaker', self.record.id)
        finally:
            store.close()


def _wait_for_queued_reply(worker):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with worker._condition:
            if worker._results:
                return
        time.sleep(0.002)
    raise AssertionError('The reply did not enter the publication queue')


def _queue_reply(worker):
    assert worker.submit('speech-memory-1', '초코는 어떻게 지내?')
    _wait_for_queued_reply(worker)


def test_deleted_memory_replaces_answer_waiting_for_drain(tmp_path):
    """A committed answer becomes stale while waiting in the speech queue."""
    factory = _RuntimeFactory(tmp_path / 'memory.sqlite3')
    worker = DialogueWorker(factory, 'speaker')
    try:
        _queue_reply(worker)
        factory.delete_from_another_connection()
        reply = worker.drain()[0]
        assert reply['text'] == MEMORY_CHANGED_RESPONSE
        assert reply['kind'] == 'error'
        assert OLD_REPLY not in json.dumps(reply, ensure_ascii=False)
        assert set(reply) == {
            'utterance_id', 'text', 'kind', 'conversation_id',
        }
        spoken = []
        published = worker.publish_reply(
            reply, lambda text: spoken.append(text) or True,
        )
        assert type(published) is dict
        assert spoken == [MEMORY_CHANGED_RESPONSE]
        assert factory.guard_calls == 1
    finally:
        worker.close()


def test_unchanged_reply_is_checked_at_drain_and_publication(tmp_path):
    """A current answer retains its text through both freshness checks."""
    factory = _RuntimeFactory(tmp_path / 'memory.sqlite3')
    worker = DialogueWorker(factory, 'speaker')
    try:
        _queue_reply(worker)
        reply = worker.drain()[0]
        assert factory.guard_calls == 1
        spoken = []
        published = worker.publish_reply(
            reply, lambda text: spoken.append(text) or True,
        )
        assert spoken == [OLD_REPLY]
        assert factory.guard_calls == 2
        assert type(published) is dict
        assert '_memory_validator' not in json.dumps(published)
    finally:
        worker.close()


def test_drained_reply_cannot_query_or_publish_after_close(tmp_path):
    """Shutdown clears pending replies and closes stores before returning."""
    factory = _RuntimeFactory(tmp_path / 'memory.sqlite3')
    worker = DialogueWorker(factory, 'speaker')
    _queue_reply(worker)
    reply = worker.drain()[0]
    worker.close()
    spoken = []
    assert factory.closed
    assert worker.drain() == []
    assert worker.publish_reply(
        reply, lambda text: spoken.append(text) or True,
    ) is None
    assert factory.guard_calls == 1
    assert spoken == []


def test_legacy_fixture_result_needs_no_memory_validator(tmp_path):
    """Older fake runtimes may return only a normalized decision."""
    factory = _RuntimeFactory(
        tmp_path / 'memory.sqlite3', legacy_result=True,
    )
    worker = DialogueWorker(factory, 'speaker')
    try:
        _queue_reply(worker)
        reply = worker.drain()[0]
        assert reply['text'] == OLD_REPLY
        assert worker.publish_reply(reply, lambda _text: True) == dict(reply)
        assert factory.guard_calls == 0
    finally:
        worker.close()


def _fake_ros(monkeypatch, spoken, logs):
    class Node:
        def __init__(self, _name):
            self.context = SimpleNamespace(ok=lambda: True)

        def create_publisher(self, *_args):
            return SimpleNamespace(
                publish=lambda message: spoken.append(message.text),
            )

        def create_subscription(self, *_args):
            return None

        def create_timer(self, *_args):
            return None

        def get_logger(self):
            return SimpleNamespace(
                info=logs.append, error=logs.append, warning=logs.append,
            )

        def destroy_node(self):
            return True

    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=Node))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        DurabilityPolicy=SimpleNamespace(VOLATILE='volatile'),
        HistoryPolicy=SimpleNamespace(KEEP_LAST='keep-last'),
        ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
        QoSProfile=lambda **kwargs: kwargs,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechRequest=lambda **kwargs: SimpleNamespace(**kwargs),
        SpeechTranscript=SimpleNamespace,
    ))
    monkeypatch.setattr(
        'malbut_agent_server.manager_client.ManagerClient',
        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None),
    )


def test_node_rechecks_deletion_between_drain_and_say(tmp_path, monkeypatch):
    """The real node callback blocks stale text at a fake ROS publisher."""
    spoken, logs = [], []
    _fake_ros(monkeypatch, spoken, logs)
    factory = _RuntimeFactory(tmp_path / 'dialogue.sqlite3')
    node = ros_communication.create_communication_node(
        speech_db_path=str(tmp_path / 'receipts.sqlite3'),
        dialogue_settings=Settings(
            user_id='speaker', database_path=factory.path,
        ),
        dialogue_factory=factory,
    )
    try:
        _queue_reply(node.dialogue)
        drain = node.dialogue.drain

        def delete_after_drain():
            replies = drain()
            assert replies[0]['text'] == OLD_REPLY
            factory.delete_from_another_connection()
            return replies

        node.dialogue.drain = delete_after_drain
        node._drain_dialogue()
        assert spoken == [MEMORY_CHANGED_RESPONSE]
        assert factory.guard_calls == 2
        event = json.loads(logs[-1])
        assert event['event'] == 'dialogue_response_published'
        assert event['text'] == MEMORY_CHANGED_RESPONSE
        assert OLD_REPLY not in logs[-1]
        assert 'validator' not in logs[-1]
    finally:
        node.destroy_node()
