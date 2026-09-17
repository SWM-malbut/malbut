"""Real single-executor Service tests with fixed classification and no audio."""

from threading import Event, get_ident
import time
from types import SimpleNamespace

import pytest


rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from malbut_interfaces.msg import SpeechRequest  # noqa: E402
from malbut_interfaces.srv import ClassifySpeechAddressee  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402

from malbut_agent_server import ros_communication  # noqa: E402
from test_speech_dialogue import RuntimeFactory  # noqa: E402


def test_service_duplicate_and_shutdown_progress_on_single_executor(monkeypatch, tmp_path):
    """A blocked model must leave timers and duplicate requests runnable."""
    entered, release = Event(), Event()
    calls, spoken, ticks = [], [], []
    factory = RuntimeFactory()

    def classify(text, snapshot):
        calls.append((text, get_ident()))
        entered.set()
        assert release.wait(8)
        return ClassifySpeechAddressee.Response.NOT_ADDRESSED

    def runtime_factory():
        runtime = factory()
        runtime.speech_addressee = SimpleNamespace(classify=classify)
        return runtime

    monkeypatch.setattr('malbut_agent_server.manager_client.ManagerClient',
                        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None))
    rclpy.init()
    executor = SingleThreadedExecutor()
    agent = client_node = None

    def spin_until(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            executor.spin_once(timeout_sec=0.01)
        raise AssertionError('Service or timer did not progress on a single executor')

    try:
        agent = ros_communication.create_communication_node(
            speech_db_path=str(tmp_path / 'receipts.sqlite3'),
            dialogue_factory=runtime_factory,
        )
        client_node = Node('speech_addressee_service_test')
        executor.add_node(agent)
        executor.add_node(client_node)
        client_node.create_subscription(
            SpeechRequest, ros_communication.RESPONSE_TOPIC, spoken.append, 10,
        )
        agent.create_timer(0.01, lambda: ticks.append(True))
        client = client_node.create_client(
            ClassifySpeechAddressee, ros_communication.ADDRESSEE_SERVICE,
        )
        spin_until(client.service_is_ready)
        request = ClassifySpeechAddressee.Request(
            utterance_id='uid', playback_id='pid', text='  원문\n',
        )
        first, second = client.call_async(request), client.call_async(request)
        spin_until(lambda: entered.is_set() and agent._addressee_callbacks == 2)
        assert not first.done() and not second.done()
        before = len(ticks)
        spin_until(lambda: len(ticks) > before)
        release.set()
        spin_until(lambda: first.done() and second.done())
        assert first.result().decision == second.result().decision == (
            ClassifySpeechAddressee.Response.NOT_ADDRESSED
        )
        duplicate = client.call_async(request)
        spin_until(duplicate.done)
        assert duplicate.result().decision == ClassifySpeechAddressee.Response.NOT_ADDRESSED
        assert calls == [('  원문\n', calls[0][1])]
        assert calls[0][1] != get_ident()
        assert factory.handled == []
        assert agent._receipts.lookup('uid', '  원문\n') is None
        assert spoken == []

        conflict = client.call_async(ClassifySpeechAddressee.Request(
            utterance_id='uid', playback_id='pid', text='바뀐 원문',
        ))
        spin_until(conflict.done)
        assert conflict.result().decision == ClassifySpeechAddressee.Response.UNKNOWN
        assert len(calls) == 1

        entered.clear()
        release.clear()
        pending = client.call_async(ClassifySpeechAddressee.Request(
            utterance_id='shutdown', playback_id='pid', text='종료 중 발화',
        ))
        spin_until(lambda: entered.is_set() and agent._addressee_callbacks == 1)
        agent.begin_shutdown()
        spin_until(pending.done)
        assert pending.result().decision == ClassifySpeechAddressee.Response.UNKNOWN
        assert agent._addressee_callbacks == 0
        assert spoken == []
    finally:
        release.set()
        if agent is not None:
            agent.destroy_node()
        if client_node is not None:
            client_node.destroy_node()
        executor.shutdown(timeout_sec=0)
        if rclpy.ok():
            rclpy.shutdown()


@pytest.mark.parametrize('failed', [False, True])
def test_agent_startup_does_not_advertise_unusable_speech_inputs(monkeypatch, tmp_path, failed):
    """Exercise actual DDS discovery while the real dialogue worker is blocked."""
    entered, release = Event(), Event()
    factory = RuntimeFactory()

    def delayed_runtime():
        entered.set()
        assert release.wait(8)
        if failed:
            raise RuntimeError('test startup failure')
        return factory()

    monkeypatch.setattr('malbut_agent_server.manager_client.ManagerClient',
                        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None))
    rclpy.init()
    executor = SingleThreadedExecutor()
    agent = probe = None
    try:
        agent = ros_communication.create_communication_node(
            speech_db_path=str(tmp_path / 'receipts.sqlite3'),
            dialogue_factory=delayed_runtime,
        )
        probe = Node('speech_startup_discovery_test')
        executor.add_node(agent)
        executor.add_node(probe)
        client = probe.create_client(
            ClassifySpeechAddressee, ros_communication.ADDRESSEE_SERVICE)
        assert entered.wait(5)
        for _ in range(10):
            executor.spin_once(timeout_sec=0.02)
        assert not client.service_is_ready()
        assert probe.get_subscriptions_info_by_topic(ros_communication.TRANSCRIPT_TOPIC) == []
        release.set()
        deadline = time.monotonic() + 5
        if failed:
            with pytest.raises(RuntimeError, match='speech_dialogue_startup_failed'):
                while time.monotonic() < deadline:
                    executor.spin_once(timeout_sec=0.02)
            assert not client.service_is_ready()
            assert probe.get_subscriptions_info_by_topic(
                ros_communication.TRANSCRIPT_TOPIC) == []
        else:
            while time.monotonic() < deadline and not client.service_is_ready():
                executor.spin_once(timeout_sec=0.02)
            assert client.service_is_ready()
            assert agent.dialogue.ready
            assert probe.get_subscriptions_info_by_topic(ros_communication.TRANSCRIPT_TOPIC)
    finally:
        release.set()
        if agent is not None:
            agent.destroy_node()
        if probe is not None:
            probe.destroy_node()
        executor.shutdown(timeout_sec=0)
        if rclpy.ok():
            rclpy.shutdown()
