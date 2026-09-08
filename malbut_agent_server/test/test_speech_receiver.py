"""Transcript receipt, persistent deduplication, and ROS lifecycle tests."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import sys
import threading
from types import ModuleType

import pytest

from malbut_agent_server import speech_receiver
from malbut_agent_server.speech_receipts import SpeechReceiptStore


class CapturingLogger:
    """Collect receipt events without requiring a ROS installation."""

    def __init__(self):
        self.events = []

    def info(self, message):
        self.events.append(('info', message))

    def warning(self, message):
        self.events.append(('warning', message))

    def error(self, message):
        self.events.append(('error', message))


@pytest.fixture
def receipts(tmp_path):
    """Use a real, isolated SQLite file for each receipt test."""
    store = SpeechReceiptStore(str(tmp_path / 'receipts.sqlite3'))
    yield store
    store.close()


def test_receipts_preserve_identity_and_exact_text(receipts):
    """Duplicate content under a new ID is a new utterance."""
    assert receipts.receive('one', ' 안녕\n') == 'received'
    assert receipts.receive('one', ' 안녕\n') == 'duplicate'
    assert receipts.receive('one', '안녕') == 'conflict'
    assert receipts.receive('one', ' 안녕\n') == 'duplicate'
    assert receipts.receive('two', ' 안녕\n') == 'received'


@pytest.mark.parametrize('utterance_id,text', [
    ('', '안녕'), (' \n', '안녕'), ('one', ''), ('one', ' \t\n'),
])
def test_blank_inputs_never_consume_an_id(receipts, utterance_id, text):
    """Invalid content leaves its ID available for a valid transcript."""
    with pytest.raises(ValueError):
        receipts.receive(utterance_id, text)
    assert receipts.receive('one', '안녕') == 'received'


def test_receipts_survive_restart_without_storing_plaintext(tmp_path):
    """The file preserves only ID, text hash, and original receipt time."""
    path = tmp_path / 'receipts.sqlite3'
    text = 'private transcript 원문 보존 확인'
    first = SpeechReceiptStore(str(path))
    assert first.receive('one', text) == 'received'
    first.close()
    with sqlite3.connect(path) as connection:
        before = connection.execute('SELECT * FROM speech_receipts').fetchone()
    second = SpeechReceiptStore(str(path))
    try:
        assert second.receive('one', text) == 'duplicate'
        assert second.receive('one', 'changed') == 'conflict'
    finally:
        second.close()
    with sqlite3.connect(path) as connection:
        after = connection.execute('SELECT * FROM speech_receipts').fetchone()
    assert after == before
    assert after[0] == 'one'
    assert after[1] == hashlib.sha256(text.encode('utf-8')).hexdigest()
    assert after[2] > 0
    assert text.encode('utf-8') not in path.read_bytes()


def test_two_connections_accept_the_same_id_only_once(tmp_path):
    """SQLite serializes competing first receipts across connections."""
    path = str(tmp_path / 'receipts.sqlite3')
    barrier = threading.Barrier(2)

    def receive():
        store = SpeechReceiptStore(path)
        try:
            barrier.wait(timeout=5)
            return store.receive('one', '안녕')
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(receive) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == ['duplicate', 'received']


def test_receive_log_is_json_escaped_and_follows_commit(receipts, tmp_path):
    """A successful event preserves raw text and corresponds to a saved ID."""
    logger = CapturingLogger()
    text = '안녕\n"말벗"\t'
    outcome = speech_receiver.receive_transcript(
        receipts, 'one\n', text, logger,
    )
    assert outcome == 'received'
    level, message = logger.events[0]
    assert level == 'info'
    assert '\n' not in message
    assert json.loads(message) == {
        'event': 'speech_transcript', 'status': 'received',
        'utterance_id': 'one\n', 'text': text,
    }
    with sqlite3.connect(tmp_path / 'receipts.sqlite3') as connection:
        assert connection.execute(
            'SELECT utterance_id FROM speech_receipts',
        ).fetchall() == [('one\n',)]


def test_duplicate_and_conflict_do_not_log_a_new_transcript(receipts):
    """Repeated and conflicting IDs retain the first receipt."""
    logger = CapturingLogger()
    receipts.receive('one', 'first')
    for text in ('first', 'other'):
        speech_receiver.receive_transcript(receipts, 'one', text, logger)
    assert [level for level, _ in logger.events] == ['info', 'warning']
    events = [json.loads(message) for _, message in logger.events]
    assert [event['status'] for event in events] == ['duplicate', 'conflict']
    assert all('text' not in event for event in events)


def test_invalid_input_and_database_failure_are_not_success(receipts):
    """Storage failure cannot emit the success event or original speech."""
    logger = CapturingLogger()
    assert speech_receiver.receive_transcript(
        receipts, 'one', ' ', logger,
    ) == 'invalid'
    receipts.close()
    assert speech_receiver.receive_transcript(
        receipts, 'one', 'private', logger,
    ) == 'storage_error'
    assert [level for level, _ in logger.events] == ['warning', 'error']
    assert all('private' not in message for _, message in logger.events)


def test_help_and_missing_ros_do_not_start_any_provider(monkeypatch, capsys):
    """The optional CLI remains importable without ROS or model setup."""
    monkeypatch.setitem(sys.modules, 'rclpy', None)
    with pytest.raises(SystemExit) as exit_info:
        speech_receiver.main(['--help'])
    assert exit_info.value.code == 0
    assert speech_receiver.main([]) == 2
    assert 'rclpy is required' in capsys.readouterr().err


@pytest.mark.parametrize('startup_failure', [False, True])
def test_receiver_closes_resources_on_interrupt_or_startup_error(
    monkeypatch, tmp_path, startup_failure,
):
    """Shutdown closes the Node, ROS context, and receipt connection."""
    calls = []
    captured = []
    ros = ModuleType('rclpy')
    ros.init = lambda args: calls.append(('init', args))
    ros.ok = lambda: True
    ros.shutdown = lambda: calls.append('shutdown')
    executors = ModuleType('rclpy.executors')
    executors.ExternalShutdownException = type(
        'ExternalShutdown', (Exception,), {},
    )

    class FakeNode:
        def get_logger(self):
            return CapturingLogger()

        def destroy_node(self):
            calls.append('destroy')

    def create_node(store):
        captured.append(store)
        if startup_failure:
            raise ImportError('message has not been generated')
        return FakeNode()

    def spin(node):
        calls.append('spin')
        raise KeyboardInterrupt

    ros.spin = spin
    monkeypatch.setitem(sys.modules, 'rclpy', ros)
    monkeypatch.setitem(sys.modules, 'rclpy.executors', executors)
    monkeypatch.setattr(speech_receiver, 'create_receiver_node', create_node)
    result = speech_receiver.main([
        '--db-path', str(tmp_path / 'receipts.sqlite3'),
    ])
    assert result == (2 if startup_failure else 0)
    assert calls == ([('init', []), 'shutdown'] if startup_failure else [
        ('init', []), 'spin', 'destroy', 'shutdown',
    ])
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].receive('late', 'closed')
