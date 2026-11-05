"""Real ROS message contract; synthetic provider, no API or audio device."""

import time

import pytest

rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')
messages = pytest.importorskip('malbut_interfaces.msg')
if not hasattr(messages, 'SpeechPreparation'):
    pytest.skip('Rebuild the optional speech-preparation interfaces', allow_module_level=True)

from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from malbut_agent_server.config import Settings  # noqa: E402
from malbut_agent_server.ros_communication import create_communication_node  # noqa: E402
from test_speech_dialogue import RuntimeFactory  # noqa: E402
from test_speech_prefetch import PreviewProvider, TEXT  # noqa: E402


@pytest.fixture
def ros_prefetch(tmp_path, monkeypatch, request):
    monkeypatch.setenv('ROS_DOMAIN_ID', '194')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    monkeypatch.setattr('malbut_agent_server.speech_scene.RosSpeechScene', lambda *_: None)
    option = getattr(request, 'param', True)
    overridden = isinstance(option, tuple)
    enabled = option[1] if overridden else option
    rclpy.init(args=(['--ros-args', '-p', f'speech_prefetch_enabled:={str(enabled).lower()}']
                    if overridden else []))
    executor = SingleThreadedExecutor()
    provider = PreviewProvider(blocked=True)
    agent = observer = None
    try:
        agent = create_communication_node(
            speech_db_path=str(tmp_path / 'receipts.sqlite3'),
            dialogue_settings=Settings(speech_prefetch_enabled=not enabled if overridden else enabled),
            dialogue_factory=RuntimeFactory(provider),
        )
        observer = Node('speech_prefetch_test_observer')
        prepares, finals = [], []
        observer.create_subscription(messages.SpeechPreparation, '/malbut/tts/prepare', prepares.append, 10)
        observer.create_subscription(messages.SpeechRequest, '/malbut/speech/response', finals.append, 10)
        executor.add_node(agent)
        executor.add_node(observer)

        def spin_until(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate():
                    return
                executor.spin_once(timeout_sec=.01)
            raise AssertionError('ROS fixture did not reach expected state')

        spin_until(lambda: agent.dialogue.ready and agent._speech.get_subscription_count() == 1
                   and (not enabled or agent._preparation.get_subscription_count() == 1))
        yield agent, provider, prepares, finals, spin_until
    finally:
        provider.release.set()
        for node in (agent, observer):
            if node is not None:
                executor.remove_node(node)
                node.destroy_node()
        executor.shutdown(timeout_sec=2)
        if rclpy.ok():
            rclpy.shutdown()


@pytest.mark.parametrize('ros_prefetch', [True, False, ('override', True), ('override', False)], indirect=True)
def test_typed_quiet_prepare_then_one_existing_final_request(ros_prefetch):
    agent, provider, prepares, finals, spin_until = ros_prefetch
    enabled = agent._preparation is not None
    assert agent.get_parameter('speech_prefetch_enabled').value is enabled
    assert agent.describe_parameter('speech_prefetch_enabled').read_only
    assert agent.dialogue.submit('synthetic-utterance', '안녕')
    spin_until(lambda: bool(prepares) if enabled else provider.prepared.is_set())
    assert finals == []
    if enabled:
        assert prepares[0].operation == messages.SpeechPreparation.PREPARE
        assert prepares[0].text == '안녕하세요. 다'
    provider.release.set()
    spin_until(lambda: bool(finals))
    assert len(finals) == 1
    assert finals[0].text == TEXT and finals[0].request_id == 'synthetic-utterance'
    assert finals[0].interim is False and finals[0].audio_id == ''
    if enabled:
        assert finals[0].prepared_id == prepares[0].prepared_id
        assert finals[0].generation_id == prepares[0].generation_id
    else:
        assert prepares == []
        assert finals[0].prepared_id == finals[0].generation_id == ''


def test_shutdown_cancels_quiet_hint_before_waiting_for_running_provider(ros_prefetch):
    agent, provider, prepares, finals, spin_until = ros_prefetch
    assert agent.dialogue.submit('synthetic-utterance', '안녕')
    spin_until(lambda: bool(prepares))
    agent.begin_shutdown()
    spin_until(lambda: len(prepares) == 2)
    assert prepares[1].operation == messages.SpeechPreparation.CANCEL
    assert prepares[1].prepared_id == prepares[0].prepared_id
    assert not provider.release.is_set()
    assert finals == []
