"""Exercise the actual entry point with fake ROS, microphone, and cloud libraries."""

import io
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID
import wave

import pytest

from malbut_stt.node import main


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """Install in-process boundaries; never open hardware or an HTTP connection."""
    model = tmp_path / 'local-model'
    model.mkdir()
    state = SimpleNamespace(
        ok=False, calls={}, logs=[], published=[], closed=[], failure=None,
        late_response=False, late_wake=False, recorders=[],
        parameters={'wake_model_path': str(model)},
        recordings=[
            [[7] * 512] + [[0] * 512] * 13,
            [[1] * 512] + [[0] * 512] * 32,
        ],
    )

    def fail(phase):
        if state.failure == phase:
            raise RuntimeError('hidden credential or audio content')

    def init(args):
        state.calls['ros_args'] = args
        state.ok = True

    def shutdown():
        state.ok = False
        state.closed.append('ros')

    def publish(message):
        state.published.append(message)
        state.ok = False

    logger = SimpleNamespace(**{
        level: lambda message, level=level: state.logs.append((level, message))
        for level in ('info', 'warning', 'error')
    })

    class Node:
        """Expose only the ROS operations used by the STT entry point."""

        def __init__(self, name):
            state.calls['node_name'] = name

        def declare_parameter(self, name, default):
            state.calls.setdefault('defaults', {})[name] = default
            return SimpleNamespace(value=state.parameters.get(name, default))

        def get_logger(self):
            return logger

        def create_publisher(self, message_type, topic, qos):
            state.calls['publisher'] = (message_type, topic, qos)
            return SimpleNamespace(publish=publish)

        def destroy_node(self):
            state.closed.append('node')

    class Recorder:
        """Supply deterministic PCM and track device lifetime."""

        sample_rate = 16000

        def __init__(self, **kwargs):
            state.calls.setdefault('recorders', []).append(kwargs)
            fail('opening_microphone')
            self.frames = iter(state.recordings[len(state.recorders)])
            self.active = False
            self.deleted = False
            state.recorders.append(self)

        def start(self):
            fail('starting_microphone')
            self.active = True

        def read(self):
            fail('reading_microphone')
            return next(self.frames)

        def stop(self):
            self.active = False
            state.closed.append('recorder_stop')

        def delete(self):
            self.deleted = True
            state.closed.append('recorder')

    def transcribe_wake(pcm, sample_rate):
        assert len(state.recorders) == 1
        assert state.recorders[0].deleted and not state.recorders[0].active
        state.calls['wake_transcription'] = (pcm, sample_rate)
        fail('recognizing_wake')
        if state.late_wake:
            state.ok = False
        return '제이크야'

    def create_wake(model_path):
        state.calls['wake'] = model_path
        fail('initializing_wake')
        return SimpleNamespace(transcribe=transcribe_wake)

    def create_transcription(**kwargs):
        state.calls['transcription'] = kwargs
        assert len(state.recorders) == 2
        assert all(recorder.deleted and not recorder.active for recorder in state.recorders)
        if state.late_response:
            state.ok = False
        return SimpleNamespace(text='  원문 그대로\n')

    def create_client(**kwargs):
        state.calls['client'] = kwargs
        fail('creating_api_client')
        return SimpleNamespace(
            audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create_transcription)),
            close=lambda: state.closed.append('api_client'),
        )

    def create_vad(mode):
        state.calls['vad_mode'] = mode
        return SimpleNamespace(is_speech=lambda frame, _: frame[:2] != b'\x00\x00')

    modules = {
        'rclpy': SimpleNamespace(init=init, ok=lambda: state.ok, shutdown=shutdown),
        'rclpy.node': SimpleNamespace(Node=Node),
        'rclpy.qos': SimpleNamespace(
            QoSProfile=SimpleNamespace,
            HistoryPolicy=SimpleNamespace(KEEP_LAST='keep_last'),
            ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
            DurabilityPolicy=SimpleNamespace(VOLATILE='volatile'),
        ),
        'malbut_interfaces.msg': SimpleNamespace(SpeechTranscript=SimpleNamespace),
        'pvporcupine': None,
        'pvrecorder': SimpleNamespace(PvRecorder=Recorder),
        'webrtcvad': SimpleNamespace(Vad=create_vad),
        'openai': SimpleNamespace(OpenAI=create_client),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr('malbut_stt.wake.LocalWakeRecognizer', create_wake)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-openai')
    monkeypatch.delenv('PICOVOICE_ACCESS_KEY', raising=False)
    return state


def test_entrypoint_wires_defaults_qos_and_closes_runtime(runtime):
    """Verify the real pipeline wiring, final publication, and resource cleanup."""
    assert main(['--ros-args']) == 0
    assert runtime.calls['ros_args'] == ['--ros-args']
    assert runtime.calls['node_name'] == 'malbut_stt'
    defaults = runtime.calls['defaults']
    assert defaults['start_timeout_s'] == 5.0
    assert defaults['silence_timeout_s'] == 1.0
    assert defaults['max_utterance_s'] == 20.0
    assert defaults['pre_roll_s'] == 0.3
    assert defaults['wake_model_path'] == ''
    assert 'keyword_path' not in defaults and 'language_model_path' not in defaults
    assert runtime.calls['vad_mode'] == 2
    assert runtime.calls['recorders'] == [{'frame_length': 512, 'device_index': -1}] * 2
    assert runtime.calls['wake'] == Path(runtime.parameters['wake_model_path'])
    assert runtime.calls['client'] == {
        'api_key': 'test-only-openai', 'base_url': 'https://api.openai.com/v1',
        'timeout': 30.0, 'max_retries': 0,
    }
    _, topic, qos = runtime.calls['publisher']
    assert topic == '/malbut/speech/transcript'
    assert vars(qos) == {
        'history': 'keep_last', 'depth': 10,
        'reliability': 'reliable', 'durability': 'volatile',
    }
    assert runtime.calls['transcription']['model'] == 'gpt-transcribe'
    assert runtime.calls['transcription']['extra_body'] == {'languages': ['ko']}
    wake_pcm, rate = runtime.calls['wake_transcription']
    assert rate == 16000
    assert wake_pcm.startswith(b'\x07\x00' * 512)
    with wave.open(io.BytesIO(runtime.calls['transcription']['file'][1]), 'rb') as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        command_pcm = audio.readframes(audio.getnframes())
    assert command_pcm.startswith(b'\x01\x00' * 512)
    assert b'\x07\x00' not in command_pcm
    assert ('info', 'wake_detected') in runtime.logs
    assert ('info', 'listening') in runtime.logs
    assert len(runtime.published) == 1
    assert runtime.published[0].text == '  원문 그대로\n'
    assert str(UUID(runtime.published[0].utterance_id)) == runtime.published[0].utterance_id
    assert runtime.closed == ['recorder_stop', 'recorder'] * 2 + ['api_client', 'node']


@pytest.mark.parametrize('phase, released, ready', [
    ('initializing_wake', [], False),
    ('creating_api_client', [], False),
    ('opening_microphone', ['api_client'], False),
    ('starting_microphone', ['recorder', 'api_client'], False),
    ('reading_microphone', ['recorder_stop', 'recorder', 'api_client'], True),
    ('recognizing_wake', ['recorder_stop', 'recorder', 'api_client'], True),
])
def test_runtime_failure_identifies_phase_without_exception_content(
    runtime, phase, released, ready,
):
    """Failure reports tell which boundary failed and close earlier resources."""
    runtime.failure = phase
    assert main() == 1
    assert ('error', 'STT stopped during ' + phase + ': RuntimeError') in runtime.logs
    assert (('info', 'waiting_for_wake') in runtime.logs) is ready
    assert all('hidden' not in message for _, message in runtime.logs)
    assert runtime.closed == released + ['node', 'ros']
    assert runtime.published == []
    assert 'transcription' not in runtime.calls


def test_entrypoint_suppresses_response_after_ros_shutdown(runtime):
    """A late transcription result cannot be published by the actual wiring."""
    runtime.late_response = True
    assert main() == 0
    assert 'transcription' in runtime.calls
    assert runtime.published == []
    assert not any(message.startswith('published:') for _, message in runtime.logs)
    assert runtime.closed == ['recorder_stop', 'recorder'] * 2 + ['api_client', 'node']


def test_shutdown_during_local_recognition_never_opens_command_microphone(runtime):
    """A late local wake result cannot start command capture or upload audio."""
    runtime.late_wake = True
    assert main() == 0
    assert 'wake_transcription' in runtime.calls
    assert 'transcription' not in runtime.calls
    assert len(runtime.recorders) == 1
    assert runtime.published == []
    assert runtime.closed == ['recorder_stop', 'recorder', 'api_client', 'node']


@pytest.mark.parametrize('missing', ['key', 'model', 'model_directory'])
def test_missing_configuration_never_initializes_model_or_hardware(
    runtime, monkeypatch, tmp_path, missing,
):
    """Fail before model loading, device opening, or API client creation."""
    if missing == 'key':
        monkeypatch.delenv('OPENAI_API_KEY')
    elif missing == 'model':
        runtime.parameters['wake_model_path'] = ''
    else:
        model_file = tmp_path / 'not-a-directory'
        model_file.touch()
        runtime.parameters['wake_model_path'] = str(model_file)
    assert main() == 1
    assert not {'wake', 'client', 'recorders'} & runtime.calls.keys()
    assert runtime.closed == ['node', 'ros']
    assert runtime.published == []


def test_shutdown_between_result_check_and_publication_is_not_reported_as_sent(
    runtime, monkeypatch,
):
    """The publisher's shutdown guard must not produce a false published log."""
    ros = sys.modules['rclpy']
    original_ok = ros.ok

    def shutdown_before_publish():
        was_ok = original_ok()
        if 'transcription' in runtime.calls and was_ok:
            runtime.ok = False
        return was_ok

    monkeypatch.setattr(ros, 'ok', shutdown_before_publish)
    assert main() == 0
    assert 'transcription' in runtime.calls
    assert runtime.published == []
    assert not any(message.startswith('published:') for _, message in runtime.logs)
