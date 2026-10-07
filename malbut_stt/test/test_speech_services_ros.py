"""Exercise real ROS service clients with audio and models replaced only at the boundary."""

import sys
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from malbut_interfaces.msg import (  # noqa: E402
    SpeechInputStatus, SpeechPlaybackStatus, SpeechTranscript,
)
from malbut_interfaces.srv import (  # noqa: E402
    ClassifySpeechAddressee, ControlSpeechPlayback, ControlWebTalk,
)
from rclpy.node import Node  # noqa: E402

from malbut_stt import node as stt_node, wake  # noqa: E402


@pytest.mark.parametrize('decision', ['addressed', 'not_addressed', 'unknown'])
def test_stt_service_round_trip_and_status_topic(monkeypatch, tmp_path, capsys, decision):
    """Use generated types and one executor without opening a microphone or model."""
    state = SimpleNamespace(clients=[], classifications=[], controls=[], transcripts=[],
                            decisions=[], statuses=[], input_statuses=[], web_talk=[],
                            closed=False)

    create_client = Node.create_client

    def record_client(self, *args, **kwargs):
        client = create_client(self, *args, **kwargs)
        state.clients.append(client)
        return client

    class AudioBoundary:
        def __init__(self, **callbacks):
            self.callbacks = callbacks
            self.session = SimpleNamespace(
                playback_id='playback-1', playback_state='playing', session_id='')
            self.pending_addressee = None
            self.phase = 'test_audio_boundary'
            self.capture_ready = Event()
            self.sent = False
            self.deadline = monotonic() + 8.0
            self.peer = None
            self.web_future = None
            self.web_active = True

        def start(self):
            self.peer = Node('speech_service_test_peer')
            self.peer.create_service(ClassifySpeechAddressee,
                                     '/malbut/speech/classify_addressee', self.classify)
            self.peer.create_service(ControlSpeechPlayback,
                                     '/malbut/speech/playback_control', self.control)
            self.web_client = self.peer.create_client(
                ControlWebTalk, '/malbut/speech/web_talk_control')
            self.status = self.peer.create_publisher(
                SpeechPlaybackStatus, '/malbut/speech/playback_status', 10)
            self.peer.create_subscription(SpeechTranscript, '/malbut/speech/transcript',
                                          state.transcripts.append, 10)
            self.peer.create_subscription(SpeechInputStatus, '/malbut/speech/input_status',
                                          state.input_statuses.append, 10)
            rclpy.get_global_executor().add_node(self.peer)

        def classify(self, request, response):
            state.classifications.append((request.utterance_id, request.playback_id, request.text))
            response.decision = decision
            return response

        def control(self, request, response):
            state.controls.append((request.playback_id, request.command))
            response.accepted = True
            return response

        def control_web_talk(self, lease_id, active, ttl_s):
            state.web_talk.append((lease_id, active, ttl_s))
            return True

        def poll(self):
            assert monotonic() < self.deadline, 'ROS service round trip timed out'
            # Model the first valid microphone frame without opening audio hardware.
            self.capture_ready.set()
            if (not self.sent and all(
                    client.service_is_ready() for client in state.clients)
                    and self.peer.count_publishers('/malbut/speech/transcript') == 1
                    and self.peer.count_publishers('/malbut/speech/input_status') == 1
                    and self.status.get_subscription_count() == 1):
                self.sent = True
                self.pending_addressee = ('utterance-1', 'playback-1', self.deadline)
                self.callbacks['publish_interruption']('utterance-1', 'playback-1', '잠깐만')
                for command in ('pause', 'resume', 'stop'):
                    self.callbacks['publish_control']('playback-1', command)
                self.callbacks['publish_transcript']('normal-1', '오늘 날씨 알려줘')
                for input_state in ('started', 'failed'):
                    self.callbacks['publish_input_status']('', 'missed-1', input_state)
            if (state.decisions and len(state.controls) == 3 and state.transcripts
                    and len(state.input_statuses) == 2):
                # Service acceptance must not manufacture an actual playback status.
                if not state.statuses:
                    assert self.session.playback_state == 'playing'
                    self.status.publish(SpeechPlaybackStatus(
                        playback_id='playback-1', state=SpeechPlaybackStatus.PAUSED,
                        interim=True))
                elif self.web_future is None:
                    self.web_future = self.web_client.call_async(ControlWebTalk.Request(
                        lease_id='web-1', active=self.web_active,
                        ttl_s=3.0 if self.web_active else 0.0))
                elif self.web_future.done():
                    assert self.web_future.result().accepted
                    if not self.web_active:
                        raise KeyboardInterrupt
                    self.web_active = False
                    self.web_future = None

        def on_addressee(self, uid, pid, result):
            state.decisions.append((uid, pid, result))
            self.pending_addressee = None

        def on_playback_status(self, pid, playback_state, *, interim=False, request_id=''):
            state.statuses.append((pid, playback_state, interim))
            self.session.playback_state = playback_state

        def close(self):
            state.closed = True
            if self.peer is not None:
                rclpy.get_global_executor().remove_node(self.peer)
                self.peer.destroy_node()

    monkeypatch.setattr(Node, 'create_client', record_client)
    monkeypatch.setattr(stt_node, 'DialoguePipeline', AudioBoundary)
    monkeypatch.setattr(stt_node, 'LocalWhisperTranscriber', lambda *_, **__: object())
    monkeypatch.setattr(wake, 'LocalWakeRecognizer', SimpleNamespace(
        from_transcriber=lambda _: object()))
    monkeypatch.setitem(sys.modules, 'pvrecorder', None)
    monkeypatch.setattr(stt_node, 'SoundDeviceRecorder', lambda **_: object())
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(
        Vad=lambda *_: SimpleNamespace(is_speech=lambda *_: False)))

    assert stt_node.main(['--ros-args', '-p', f'wake_model_path:={tmp_path}']) == 0
    assert capsys.readouterr().out.splitlines().count('malbut_speech_capture_ready') == 1
    assert state.classifications == [('utterance-1', 'playback-1', '잠깐만')]
    assert state.decisions == [('utterance-1', 'playback-1', decision)]
    assert state.controls == [('playback-1', value) for value in ('pause', 'resume', 'stop')]
    assert [(item.utterance_id, item.text) for item in state.transcripts] == [
        ('normal-1', '오늘 날씨 알려줘')]
    assert state.statuses and all(item == ('playback-1', 'paused', True)
                                  for item in state.statuses)
    assert [(item.session_id, item.utterance_id, item.state)
            for item in state.input_statuses] == [
        ('', 'missed-1', 'started'), ('', 'missed-1', 'failed')]
    assert state.closed and not rclpy.ok()
    assert state.web_talk == [
        ('startup-quarantine', True, 3.0), ('web-1', True, 3.0), ('web-1', False, 0.0)]
