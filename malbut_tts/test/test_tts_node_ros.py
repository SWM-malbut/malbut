"""Real ROS Topic and Service verification without a model or speaker."""

from threading import Event, Thread
import time

import pytest


rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from malbut_interfaces.msg import (  # noqa: E402
    SpeechPlaybackStatus, SpeechRequest,
)
from malbut_interfaces.srv import ControlSpeechPlayback  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import (  # noqa: E402
    DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy,
)

from malbut_tts import node as tts_node  # noqa: E402
from malbut_tts.runtime import SpeechRuntime  # noqa: E402


class ControlledRuntime:
    """Keep service acceptance separate from test-triggered audio events."""

    def __init__(self, on_status):
        self.on_status = on_status
        self.submitted = []
        self.accepted = []
        self.closed = False

    def submit(self, text, request_type=0):
        self.submitted.append((text, request_type))
        return 'ros-playback-1'

    def control(self, playback_id, command):
        if playback_id != 'ros-playback-1' or command != 'pause':
            return False
        self.accepted.append((playback_id, command))
        return True

    def close(self):
        self.closed = True


def test_generated_request_kind_service_and_worker_status_round_trip():
    """Generated ROS types carry kind, acceptance, and whole-request states."""
    rclpy.init()
    executor = SingleThreadedExecutor()
    tts = peer = None
    received = []

    def spin_until(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            executor.spin_once(timeout_sec=0.01)
        raise AssertionError('TTS Topic or Service did not make progress')

    try:
        assert SpeechRequest().request_type == SpeechRequest.DIALOGUE == 0
        assert SpeechRequest.NOTIFICATION == 1
        tts = tts_node.create_tts_node(ControlledRuntime)
        peer = Node('tts_adapter_test_peer')
        executor.add_node(tts)
        executor.add_node(peer)
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        requests = peer.create_publisher(
            SpeechRequest, tts_node.RESPONSE_TOPIC, qos,
        )
        peer.create_subscription(
            SpeechPlaybackStatus, tts_node.STATUS_TOPIC, received.append, qos,
        )
        client = peer.create_client(
            ControlSpeechPlayback, tts_node.CONTROL_SERVICE,
        )
        assert client.wait_for_service(timeout_sec=5)
        spin_until(lambda: requests.get_subscription_count() == 1)
        spin_until(lambda: tts._status_publisher.get_subscription_count() == 1)
        requests.publish(SpeechRequest(
            text=' 순찰을 마쳤어요.\n',
            request_type=SpeechRequest.NOTIFICATION,
        ))
        requests.publish(SpeechRequest(
            text='대화 답변이에요.', request_type=SpeechRequest.DIALOGUE,
        ))
        spin_until(lambda: len(tts._runtime.submitted) == 2)
        assert tts._runtime.submitted == [
            (' 순찰을 마쳤어요.\n', SpeechRequest.NOTIFICATION),
            ('대화 답변이에요.', SpeechRequest.DIALOGUE),
        ]

        control = client.call_async(ControlSpeechPlayback.Request(
            playback_id='ros-playback-1', command='pause',
        ))
        spin_until(control.done)
        assert control.result().accepted
        assert received == []
        invalid = client.call_async(ControlSpeechPlayback.Request(
            playback_id='missing', command='resume',
        ))
        spin_until(invalid.done)
        assert not invalid.result().accepted

        worker = Thread(target=lambda: [
            tts._runtime.on_status('ros-playback-1', state)
            for state in ('playing', 'paused', 'playing', 'finished')
        ])
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        spin_until(lambda: len(received) == 4)
        assert [(message.playback_id, message.state)
                for message in received] == [
            ('ros-playback-1', state)
            for state in ('playing', 'paused', 'playing', 'finished')
        ]
        runtime = tts._runtime
        executor.remove_node(tts)
        tts.destroy_node()
        tts = None
        assert runtime.closed
    finally:
        if tts is not None:
            tts.destroy_node()
        if peer is not None:
            peer.destroy_node()
        executor.shutdown(timeout_sec=0)
        if rclpy.ok():
            rclpy.shutdown()


class ChunkSynthesizer:
    """Yield two distinguishable chunks for each full original request."""

    def __init__(self):
        self.texts = []

    def generate(self, text, cancel_event):
        self.texts.append(text)
        for index in range(2):
            if not cancel_event.is_set():
                yield (text, index), 24000


class DrainControlledPlayer:
    """Acknowledge controls and device drain only when the test allows them."""

    def __init__(self, on_state, cancel_event):
        self.on_state = on_state
        self.cancel = cancel_event
        self.audio = []
        self.finishing = Event()
        self.drain = Event()
        self.closed = Event()

    def write(self, audio, sample_rate):
        assert not self.cancel.is_set()
        self.audio.append((audio, sample_rate))
        if len(self.audio) == 1:
            self.on_state('playing')

    def finish(self):
        self.finishing.set()
        assert self.drain.wait(30), 'Test did not release the device drain'

    def pause(self):
        return True

    def resume(self):
        return True

    def stop(self):
        self.cancel.set()
        self.drain.set()

    def close(self):
        self.closed.set()


def test_real_runtime_priority_controls_and_device_drain_through_ros():
    """DDS input drives real queueing and control with per-request statuses."""
    rclpy.init()
    executor = SingleThreadedExecutor()
    tts = peer = None
    synth = ChunkSynthesizer()
    players, submitted, received = [], [], []

    def make_player(**kwargs):
        player = DrainControlledPlayer(**kwargs)
        players.append(player)
        return player

    def runtime_factory(on_status):
        runtime = SpeechRuntime(synth, make_player, on_status)
        real_submit = runtime.submit

        def record_submit(text, request_type=0):
            playback_id = real_submit(text, request_type)
            submitted.append((text, playback_id))
            return playback_id

        runtime.submit = record_submit
        return runtime

    def spin_until(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            executor.spin_once(timeout_sec=0.01)
        raise AssertionError('Real TTS runtime did not progress through ROS')

    def states(playback_id):
        return [message.state for message in received
                if message.playback_id == playback_id]

    def control(playback_id, command):
        future = client.call_async(ControlSpeechPlayback.Request(
            playback_id=playback_id, command=command,
        ))
        spin_until(future.done)
        return future.result().accepted

    try:
        tts = tts_node.create_tts_node(runtime_factory)
        peer = Node('tts_runtime_test_peer')
        executor.add_node(tts)
        executor.add_node(peer)
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        requests = peer.create_publisher(
            SpeechRequest, tts_node.RESPONSE_TOPIC, qos,
        )
        peer.create_subscription(
            SpeechPlaybackStatus, tts_node.STATUS_TOPIC, received.append, qos,
        )
        client = peer.create_client(
            ControlSpeechPlayback, tts_node.CONTROL_SERVICE,
        )
        assert client.wait_for_service(timeout_sec=5)
        spin_until(lambda: requests.get_subscription_count() == 1)
        spin_until(lambda: tts._status_publisher.get_subscription_count() == 1)

        requests.publish(SpeechRequest(
            text='재생 중인 알림', request_type=SpeechRequest.NOTIFICATION,
        ))
        spin_until(lambda: len(submitted) == 1)
        first_id = submitted[0][1]
        spin_until(lambda: states(first_id) == ['playing'])
        first_player = players[0]
        spin_until(first_player.finishing.is_set)
        assert states(first_id) == ['playing']
        assert not control('missing-id', 'pause')
        assert not control(first_id, 'resume')
        assert not control(first_id, 'unknown')
        assert control(first_id, 'pause')
        assert states(first_id) == ['playing']
        assert not control(first_id, 'pause')
        first_player.on_state('paused')
        spin_until(lambda: states(first_id) == ['playing', 'paused'])

        for text, request_type in (
            ('알림 하나', SpeechRequest.NOTIFICATION),
            (' 답변 하나.\n두 번째 문장. ', SpeechRequest.DIALOGUE),
            ('답변 둘', SpeechRequest.DIALOGUE),
            ('알림 둘', SpeechRequest.NOTIFICATION),
        ):
            requests.publish(SpeechRequest(
                text=text, request_type=request_type,
            ))
        spin_until(lambda: len(submitted) == 5)
        ids = dict(submitted)
        assert len(set(ids.values())) == 5
        assert synth.texts == ['재생 중인 알림']
        assert len(players) == 1
        assert not control(ids['답변 둘'], 'resume')
        assert not control(first_id, 'pause')
        assert control(first_id, 'resume')
        assert states(first_id) == ['playing', 'paused']
        first_player.on_state('playing')
        spin_until(lambda: states(first_id) == [
            'playing', 'paused', 'playing',
        ])
        assert control(first_id, 'stop')
        spin_until(lambda: states(first_id)[-1] == 'stopped')
        assert states(first_id) == ['playing', 'paused', 'playing', 'stopped']
        assert first_player.closed.is_set()
        assert not control(first_id, 'resume')
        assert not control(first_id, 'stop')

        expected = [
            '재생 중인 알림', ' 답변 하나.\n두 번째 문장. ',
            '답변 둘', '알림 하나', '알림 둘',
        ]
        for index, text in enumerate(expected[1:], 1):
            playback_id = ids[text]
            spin_until(lambda: states(playback_id) == ['playing'])
            player = players[index]
            spin_until(player.finishing.is_set)
            assert synth.texts == expected[:index + 1]
            assert player.audio == [
                ((text, 0), 24000), ((text, 1), 24000),
            ]
            assert states(playback_id) == ['playing']
            player.drain.set()
            spin_until(lambda: states(playback_id) == ['playing', 'finished'])
            assert player.closed.is_set()
            player.on_state('playing')
            assert not control(playback_id, 'stop')

        assert synth.texts == expected
        terminal = [(message.playback_id, message.state)
                    for message in received
                    if message.state in ('finished', 'stopped', 'failed')]
        assert terminal == [(first_id, 'stopped')] + [
            (ids[text], 'finished') for text in expected[1:]
        ]
        assert len(received) == 12
    finally:
        if tts is not None:
            tts.destroy_node()
        if peer is not None:
            peer.destroy_node()
        executor.shutdown(timeout_sec=0)
        if rclpy.ok():
            rclpy.shutdown()
