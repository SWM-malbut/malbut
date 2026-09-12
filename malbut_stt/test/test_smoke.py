"""Exercise the laptop entry point without ROS, hardware, or network I/O."""

import io
import json
import sys
from types import SimpleNamespace
from uuid import UUID
import wave

import pytest

from malbut_stt.smoke import main


@pytest.fixture
def runtime(monkeypatch):
    """Replace SDK boundaries while keeping the real pipeline and WAV adapter."""
    state = SimpleNamespace(
        frames=[[1] * 512] + [[0] * 512] * 32, recordings=None,
        text='  거실로 가줘.\n', failure=None, interrupt=None,
        events=[], requests=[], recorder_options=None, wake_options=None,
        wake_texts=['제이크야'], wake_requests=[],
        sample_rate=16000, fail_at=None, reads=0,
    )

    def fail(phase):
        if state.fail_at == phase:
            raise RuntimeError('PRIVATE-HARDWARE-DETAIL')

    class Recorder:
        def __init__(self, **kwargs):
            state.events.append('recorder')
            fail('opening_microphone')
            self.sample_rate = state.sample_rate
            state.recorder_options = kwargs
            self.frames = iter(state.recordings.pop(0) if state.recordings is not None
                               else state.frames)

        @staticmethod
        def get_available_devices():
            state.events.append('devices')
            return ['Laptop microphone', 'USB microphone']

        def start(self):
            state.events.append('start')
            fail('starting_microphone')

        def read(self):
            state.reads += 1
            fail('reading_microphone')
            if state.interrupt == 'read':
                raise KeyboardInterrupt
            samples = next(self.frames)
            if isinstance(samples, BaseException):
                raise samples
            return samples

        def stop(self):
            state.events.append('stop')

        def delete(self):
            state.events.append('delete')

    def create_transcription(**kwargs):
        assert state.events[-2:] == ['stop', 'delete']
        state.events.append('transcribe')
        state.requests.append(kwargs)
        if state.failure is not None:
            raise state.failure
        return SimpleNamespace(text=state.text)

    def create_client(**kwargs):
        assert kwargs['timeout'] == 30.0 and kwargs['max_retries'] == 0
        state.events.append('client')
        return SimpleNamespace(
            audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create_transcription)),
            close=lambda: state.events.append('client_close'),
        )

    def create_wake(path):
        state.events.append('wake')
        state.wake_options = path

        def transcribe(pcm, sample_rate):
            assert state.events[-2:] == ['stop', 'delete']
            assert sample_rate == 16000
            state.events.append('recognize_wake')
            state.wake_requests.append(pcm)
            fail('recognizing_wake')
            return state.wake_texts.pop(0)

        return SimpleNamespace(transcribe=transcribe)

    def enter():
        state.events.append('enter')
        if state.interrupt == 'input':
            raise KeyboardInterrupt
        return ''

    monkeypatch.setitem(sys.modules, 'rclpy', None)
    monkeypatch.setitem(sys.modules, 'pvrecorder', SimpleNamespace(PvRecorder=Recorder))
    monkeypatch.setitem(sys.modules, 'pvporcupine', None)
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    monkeypatch.setattr('malbut_stt.smoke.LocalWakeRecognizer', create_wake)
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=create_client))
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(
        Vad=lambda mode: SimpleNamespace(is_speech=lambda pcm, rate: pcm[:2] != b'\x00\x00'),
    ))
    monkeypatch.setattr('builtins.input', enter)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-openai')
    monkeypatch.delenv('PICOVOICE_ACCESS_KEY', raising=False)
    return state


def events(capsys):
    """Read the CLI's JSON stream separately from its interactive prompt."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_manual_once_preserves_text_and_closes_capture_before_api(runtime, monkeypatch, capsys):
    """A ROS-free run uses real capture/WAV logic and emits one final result."""
    assert main(['--manual', '--once', '--device-index', '1']) == 0
    output = events(capsys)
    assert [item['event'] for item in output] == [
        'ready', 'listening', 'transcribing', 'transcript',
    ]
    assert output[-1]['text'] == runtime.text
    assert str(UUID(output[-1]['utterance_id'])) == output[-1]['utterance_id']
    assert output[-1]['transcription_s'] >= 0
    assert runtime.recorder_options == {'frame_length': 512, 'device_index': 1}
    assert runtime.events == [
        'client', 'enter', 'recorder', 'start', 'stop', 'delete', 'transcribe', 'client_close',
    ]
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request['model'] == 'gpt-transcribe'
    assert request['extra_body'] == {'languages': ['ko']}
    with wave.open(io.BytesIO(request['file'][1]), 'rb') as recording:
        assert (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) == (
            1, 2, 16000,
        )
        pcm = recording.readframes(recording.getnframes())
    assert pcm.startswith(b'\x01\x00' * 512)
    assert b'\x07\x00' not in pcm


def test_manual_once_silence_never_calls_api(runtime, capsys):
    """One attempt with no speech ends locally after the capture timeout."""
    runtime.frames = [[0] * 512] * 157
    assert main(['--manual', '--once']) == 0
    assert events(capsys)[-1]['event'] == 'no_speech'
    assert runtime.requests == []
    assert runtime.events[-3:] == ['stop', 'delete', 'client_close']


def test_vad_diagnostic_starts_at_last_positive_frame(runtime, monkeypatch, capsys):
    """Measure from VAD processing, preserving the separate API duration."""
    runtime.frames = (
        [[1] * 320] + [[0] * 320] * 3
        + [[1] * 320] + [[0] * 320] * 50
    )
    clock = iter([10.0, 11.0, 12.0, 14.0])
    monkeypatch.setattr('malbut_stt.smoke.monotonic', lambda: next(clock))
    assert main(['--manual', '--once']) == 0
    transcript = events(capsys)[-1]
    assert transcript['event'] == 'transcript'
    assert transcript['transcription_s'] == 2.0
    assert transcript['vad_last_speech_to_text_s'] == 3.0


@pytest.mark.parametrize('previous_outcome', ['result', 'failure', 'wake'])
def test_new_listening_resets_vad_diagnostic_after_result_failure_or_wake(
    runtime, monkeypatch, capsys, previous_outcome,
):
    """A later callback without VAD evidence cannot reuse an earlier timestamp."""
    clock = iter([10.0] + ([] if previous_outcome == 'wake' else [12.0])
                 + ([14.0] if previous_outcome == 'result' else []) + [20.0, 22.0])
    monkeypatch.setattr('malbut_stt.smoke.monotonic', lambda: next(clock))

    def fake_pipeline(**callbacks):
        def run():
            callbacks['report']('waiting_for_wake' if previous_outcome == 'wake' else 'listening')
            assert callbacks['is_speech'](b'\x01\x00' * 320, 16000)
            if previous_outcome == 'wake':
                callbacks['report']('wake_detected')
            else:
                callbacks['report']('transcribing')
                if previous_outcome == 'failure':
                    callbacks['report']('transcription_failed:RuntimeError')
                else:
                    callbacks['publish']('previous', '이전 발화')
            callbacks['report']('listening')
            assert not callbacks['is_speech'](bytes(640), 16000)
            callbacks['report']('transcribing')
            callbacks['publish']('current', 'VAD 시각 없는 결과')

        return SimpleNamespace(run=run)

    monkeypatch.setattr('malbut_stt.smoke.SpeechPipeline', fake_pipeline)
    assert main(['--manual']) == (1 if previous_outcome == 'failure' else 0)
    transcripts = [item for item in events(capsys) if item['event'] == 'transcript']
    assert transcripts[-1]['utterance_id'] == 'current'
    assert transcripts[-1]['transcription_s'] == 2.0
    assert transcripts[-1]['vad_last_speech_to_text_s'] is None
    if previous_outcome == 'result':
        assert transcripts[0]['vad_last_speech_to_text_s'] == 4.0
    assert runtime.requests == []


def test_api_error_once_exits_without_retry_or_exception_body(runtime, capsys):
    """A failed API request cannot leak its body or start another capture."""
    runtime.failure = RuntimeError('PRIVATE-REQUEST-BODY')
    assert main(['--manual', '--once']) == 1
    output = capsys.readouterr()
    assert 'transcription_failed:RuntimeError' in output.out
    assert 'PRIVATE-REQUEST-BODY' not in output.out + output.err
    assert '"event": "transcript"' not in output.out
    assert len(runtime.requests) == runtime.events.count('recorder') == 1
    assert runtime.events[-1] == 'client_close'


@pytest.mark.parametrize('phase', ['input', 'read'])
def test_ctrl_c_releases_open_resources(runtime, phase):
    """Interrupting the prompt or microphone releases everything already opened."""
    runtime.interrupt = phase
    assert main(['--manual', '--once']) == 0
    assert runtime.requests == []
    expected = ['client', 'enter']
    if phase == 'read':
        expected += ['recorder', 'start', 'stop', 'delete']
    assert runtime.events == expected + ['client_close']


def model_args(tmp_path, wake_only=False):
    """Use a directory placeholder consumed only by the fake recognizer."""
    model = tmp_path / 'wake-model'
    model.mkdir(exist_ok=True)
    args = ['--model-path', str(model)]
    return args + ['--wake-only'] if wake_only else args


@pytest.mark.parametrize('missing', ['OPENAI_API_KEY', 'model'])
def test_missing_configuration_never_initializes_hardware(runtime, monkeypatch, tmp_path, missing):
    """Reject missing API credentials or a local model before SDK boundaries."""
    args = model_args(tmp_path)
    if missing == 'model':
        (tmp_path / 'wake-model').rmdir()
    else:
        monkeypatch.delenv(missing)
    with pytest.raises(SystemExit) as error:
        main(args + ['--once'])
    assert error.value.code == 2
    assert runtime.events == []


def test_list_devices_needs_no_keys_models_or_recognition_sdks(runtime, monkeypatch, capsys):
    """Device discovery does not initialize either recognition path."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    monkeypatch.setitem(sys.modules, 'webrtcvad', None)
    assert main(['--list-devices']) == 0
    assert events(capsys) == [
        {'event': 'device', 'index': 0, 'name': 'Laptop microphone'},
        {'event': 'device', 'index': 1, 'name': 'USB microphone'},
    ]
    assert runtime.events == ['devices']


def test_wake_once_sends_only_a_fresh_command_capture_to_api(runtime, tmp_path, capsys):
    """Local wake PCM is closed and discarded before the command microphone opens."""
    args = model_args(tmp_path)
    runtime.recordings = [
        [[2] * 512] + [[0] * 512] * 13,
        [[1] * 512] + [[0] * 512] * 32,
    ]
    assert main(args + ['--once']) == 0
    output = events(capsys)
    assert [item['event'] for item in output] == [
        'ready', 'waiting_for_wake', 'recognizing_wake', 'wake_detected',
        'listening', 'transcribing', 'transcript',
    ]
    assert output[0]['mode'] == 'wake'
    assert runtime.wake_options == str(tmp_path / 'wake-model')
    assert runtime.events == [
        'wake', 'client', 'recorder', 'start', 'stop', 'delete', 'recognize_wake',
        'recorder', 'start', 'stop', 'delete', 'transcribe', 'client_close',
    ]
    assert runtime.wake_requests[0].startswith(b'\x02\x00' * 512)
    with wave.open(io.BytesIO(runtime.requests[0]['file'][1]), 'rb') as audio:
        pcm = audio.readframes(audio.getnframes())
    assert pcm.startswith(b'\x01\x00' * 512)
    assert b'\x02\x00' not in pcm


def test_command_once_keeps_listening_after_rejected_wake(runtime, tmp_path, capsys):
    """The command attempt begins only after a complete valid wake phrase."""
    runtime.wake_texts = ['제이크야 거실로 가', '제이크야']
    runtime.recordings = [list(runtime.frames) for _ in range(3)]
    assert main(model_args(tmp_path) + ['--once']) == 0
    output = [item['event'] for item in events(capsys)]
    assert output.count('not_wake') == output.count('wake_detected') == 1
    assert output.count('waiting_for_wake') == 2
    assert output.count('listening') == output.count('transcript') == 1
    assert len(runtime.wake_requests) == 2 and len(runtime.requests) == 1
    assert runtime.events.count('recorder') == 3


@pytest.mark.parametrize('outcome', ['wake_detected', 'not_wake', 'wake_no_speech', 'wake_too_long'])
def test_wake_only_once_needs_no_openai_and_stops_after_first_attempt(
    runtime, monkeypatch, tmp_path, capsys, outcome,
):
    """Wake-only attempts never create an API client or a command recorder."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    if outcome == 'not_wake':
        runtime.wake_texts = ['제이크야 거실로 가']
    elif outcome == 'wake_no_speech':
        runtime.frames = [[0] * 512] * 157
    elif outcome == 'wake_too_long':
        runtime.frames = [[1] * 512] * 189
    assert main(model_args(tmp_path, wake_only=True) + ['--once', '--device-index', '1']) == 0
    output = events(capsys)
    assert output[0]['mode'] == 'wake_only'
    assert output[-1]['event'] == outcome
    assert 'listening' not in [item['event'] for item in output]
    assert runtime.recorder_options == {'frame_length': 512, 'device_index': 1}
    assert runtime.events.count('recorder') == 1
    assert runtime.events.count('stop') == runtime.events.count('delete') == 1
    assert 'client' not in runtime.events and runtime.requests == []
    assert len(runtime.wake_requests) == (1 if outcome in ('wake_detected', 'not_wake') else 0)


def test_wake_only_reopens_capture_after_detection_until_ctrl_c(runtime, monkeypatch, tmp_path, capsys):
    """Continuous local detection returns to wake capture without a command path."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    runtime.wake_texts = ['제이크야', '제이크야']
    runtime.recordings = [list(runtime.frames), list(runtime.frames), [KeyboardInterrupt()]]
    assert main(model_args(tmp_path, wake_only=True)) == 0
    output = [item['event'] for item in events(capsys)]
    assert output.count('wake_detected') == 2
    assert output.count('waiting_for_wake') == 3
    assert 'listening' not in output
    assert len(runtime.wake_requests) == 2 and runtime.requests == []
    assert runtime.events.count('recorder') == runtime.events.count('delete') == 3


@pytest.mark.parametrize('failure', [
    'sample_rate', 'opening_microphone', 'starting_microphone',
    'reading_microphone', 'recognizing_wake',
])
def test_wake_only_failure_closes_resources_without_details(runtime, tmp_path, capsys, failure):
    """A hardware or local inference failure stops without exposing its body."""
    if failure == 'sample_rate':
        runtime.sample_rate = 48000
    else:
        runtime.fail_at = failure
    assert main(model_args(tmp_path, wake_only=True) + ['--once']) == 1
    output = capsys.readouterr()
    assert 'PRIVATE-HARDWARE-DETAIL' not in output.out + output.err
    assert json.loads(output.out.splitlines()[-1]) == {
        'event': 'stopped',
        'phase': 'opening_microphone' if failure == 'sample_rate' else failure,
        'error': 'ValueError' if failure == 'sample_rate' else 'RuntimeError',
    }
    expected = ['wake', 'recorder']
    if failure not in ('sample_rate', 'opening_microphone'):
        expected.append('start')
    if failure in ('reading_microphone', 'recognizing_wake'):
        expected.append('stop')
    if failure != 'opening_microphone':
        expected.append('delete')
    if failure == 'recognizing_wake':
        expected.append('recognize_wake')
    assert runtime.events == expected
    assert runtime.requests == []


@pytest.mark.parametrize('invalid', ['manual', 'model'])
def test_wake_only_invalid_configuration_never_opens_hardware(runtime, tmp_path, invalid):
    """Reject mutually exclusive modes or an absent local model up front."""
    args = model_args(tmp_path, wake_only=True)
    if invalid == 'manual':
        args.append('--manual')
    else:
        (tmp_path / 'wake-model').rmdir()
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert runtime.events == []


@pytest.mark.parametrize('option', ['--keyword-path', '--language-model-path'])
def test_legacy_wake_options_are_not_accepted(runtime, option):
    """Removed vendor-specific options fail before any device or API opens."""
    with pytest.raises(SystemExit) as error:
        main(['--manual', option, 'unused'])
    assert error.value.code == 2
    assert runtime.events == []
