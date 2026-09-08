"""Offline tests for receipt-only TTS communication."""

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from malbut_tts import receiver


class CapturingLogger:
    """Collect text receipt events without a ROS installation."""

    def __init__(self):
        self.events = []

    def info(self, message):
        self.events.append(('info', message))

    def warning(self, message):
        self.events.append(('warning', message))


def test_text_is_received_verbatim_and_json_escaped():
    """Whitespace, Korean, quotes, and newlines survive the logged value."""
    logger = CapturingLogger()
    text = ' 안녕하세요.\n"말벗"입니다.\t'
    assert receiver.receive_text(text, logger)
    level, message = logger.events[0]
    assert level == 'info'
    assert '\n' not in message
    assert json.loads(message) == {
        'event': 'tts_text_received', 'text': text,
    }


@pytest.mark.parametrize('text', ['', ' \t\n'])
def test_blank_text_is_ignored_with_a_warning(text):
    """Blank input produces no valid receipt event."""
    logger = CapturingLogger()
    assert not receiver.receive_text(text, logger)
    assert logger.events == [('warning', 'tts_text_ignored: blank response')]


def test_identical_text_is_not_used_as_a_duplicate_identifier():
    """Two messages with the same sentence produce two receipt events."""
    logger = CapturingLogger()
    assert receiver.receive_text('안녕', logger)
    assert receiver.receive_text('안녕', logger)
    assert len(logger.events) == 2
    assert all(level == 'info' for level, _ in logger.events)


def test_subscriber_uses_speech_request_and_agreed_qos(monkeypatch):
    """The callback receives SpeechRequest.text on the agreed Topic and QoS."""
    logger = CapturingLogger()
    subscription = {}

    class FakeNode:
        def __init__(self, name):
            self.name = name

        def get_logger(self):
            return logger

        def create_subscription(self, message_type, topic, callback, qos):
            subscription.update(
                message_type=message_type, topic=topic,
                callback=callback, qos=qos,
            )

    fake_speech_request = type('SpeechRequest', (), {})
    monkeypatch.setitem(sys.modules, 'rclpy', ModuleType('rclpy'))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(
        Node=FakeNode,
    ))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        DurabilityPolicy=SimpleNamespace(VOLATILE='volatile'),
        HistoryPolicy=SimpleNamespace(KEEP_LAST='keep_last'),
        ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
        QoSProfile=lambda **kwargs: kwargs,
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces',
                        ModuleType('malbut_interfaces'))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechRequest=fake_speech_request,
    ))

    node = receiver.create_receiver_node()
    assert node.name == 'malbut_tts_receiver'
    assert subscription['message_type'] is fake_speech_request
    assert subscription['topic'] == '/malbut/speech/response'
    assert subscription['qos'] == {
        'history': 'keep_last', 'depth': 10,
        'reliability': 'reliable', 'durability': 'volatile',
    }
    subscription['callback'](SimpleNamespace(text='받은 원문'))
    assert json.loads(logger.events[0][1])['text'] == '받은 원문'


def test_help_and_missing_ros_work_without_audio_or_api_setup(
    monkeypatch, capsys,
):
    """CLI help is usable without ROS and missing ROS is explained."""
    monkeypatch.setitem(sys.modules, 'rclpy', None)
    with pytest.raises(SystemExit) as exit_info:
        receiver.main(['--help'])
    assert exit_info.value.code == 0
    assert receiver.main([]) == 2
    assert 'rclpy is required' in capsys.readouterr().err


@pytest.mark.parametrize('ending', [
    'keyboard_interrupt', 'external_shutdown', 'message_import_error',
])
def test_lifecycle_closes_node_and_ros_context(monkeypatch, ending):
    """Interruption or a missing dependency cleans up acquired resources."""
    calls = []
    ros = ModuleType('rclpy')
    ros.init = lambda args: calls.append(('init', args))
    ros.ok = lambda: ending != 'external_shutdown'
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

    def create_node():
        if ending == 'message_import_error':
            raise ImportError('malbut_interfaces is unavailable')
        return FakeNode()

    def spin(node):
        calls.append('spin')
        if ending == 'external_shutdown':
            raise executors.ExternalShutdownException
        raise KeyboardInterrupt

    ros.spin = spin
    monkeypatch.setitem(sys.modules, 'rclpy', ros)
    monkeypatch.setitem(sys.modules, 'rclpy.executors', executors)
    monkeypatch.setattr(receiver, 'create_receiver_node', create_node)
    result = receiver.main([])
    if ending == 'message_import_error':
        assert result == 2
        assert calls == [('init', []), 'shutdown']
    else:
        assert result == 0
        expected = [('init', []), 'spin', 'destroy']
        if ending != 'external_shutdown':
            expected.append('shutdown')
        assert calls == expected
