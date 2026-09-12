"""Verify local wake boundaries without optional SDKs, keys, or hardware."""

import json
import struct
import sys
from types import SimpleNamespace
import unicodedata

import pytest

from malbut_stt.local_wake import is_wake_phrase, main


@pytest.mark.parametrize('text, expected', [
    ('제이크야', True),
    (' \t제이크야!\n', True),
    ('“제이 크야?”', True),
    (unicodedata.normalize('NFD', '제이크야'), True),
    ('제이크', False),
    ('Jake', False),
    ('안녕 제이크야', False),
    ('제이크야 거실로 가', False),
    ('제이크야 제이크야', False),
    ('제이크야2', False),
    ('', False),
    (' \n!?', False),
])
def test_only_the_whole_normalized_phrase_is_accepted(text, expected):
    """Punctuation and spaces cannot turn aliases or sentences into a wake."""
    assert is_wake_phrase(text) is expected


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """Keep the real collector while replacing model, array, and device edges."""
    model_path = tmp_path / 'local-model'
    model_path.mkdir()
    (model_path / 'tokenizer.json').touch()
    state = SimpleNamespace(
        args=['--model-path', str(model_path)],
        frames=[[1] * 512] + [[0] * 512] * 13,
        segments=['제이크야'], events=[], requests=[],
        model_options=None, recorder_options=None,
        failure=None, interrupt=None, sample_rate=16000,
    )

    def fail(phase):
        if state.failure == phase:
            raise RuntimeError('PRIVATE-RUNTIME-DETAIL')

    def transcribe(audio, **kwargs):
        state.events.append('transcribe')
        state.requests.append((audio, kwargs))
        fail('transcribe')

        def segments():
            fail('segments')
            for text in state.segments:
                yield SimpleNamespace(text=text)

        return segments(), SimpleNamespace()

    def create_model(path, **kwargs):
        state.events.append('model')
        state.model_options = (path, kwargs)
        fail('model')
        return SimpleNamespace(transcribe=transcribe)

    class Recorder:
        def __init__(self, **kwargs):
            state.events.append('recorder')
            fail('recorder')
            self.sample_rate = state.sample_rate
            state.recorder_options = kwargs
            self.frames = iter(state.frames)

        def start(self):
            state.events.append('start')
            fail('start')

        def read(self):
            fail('read')
            if state.interrupt == 'read':
                raise KeyboardInterrupt
            return next(self.frames)

        def stop(self):
            state.events.append('stop')
            fail('stop')

        def delete(self):
            state.events.append('delete')
            fail('delete')

    class Samples(list):
        def astype(self, dtype):
            assert dtype == 'float32'
            return self

        def __truediv__(self, divisor):
            return [sample / divisor for sample in self]

    def frombuffer(pcm, dtype):
        assert dtype == '<i2'
        return Samples(struct.unpack('<' + 'h' * (len(pcm) // 2), pcm))

    def is_speech(pcm, rate):
        fail('vad')
        assert rate == 16000 and len(pcm) == 640
        return pcm[:2] != b'\x00\x00'

    def enter():
        state.events.append('enter')
        if state.interrupt == 'input':
            raise KeyboardInterrupt
        return ''

    for name in ('OPENAI_API_KEY', 'PICOVOICE_ACCESS_KEY'):
        monkeypatch.delenv(name, raising=False)
    for name in ('openai', 'pvporcupine', 'rclpy'):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=create_model))
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace(
        frombuffer=frombuffer, float32='float32',
    ))
    monkeypatch.setitem(sys.modules, 'pvrecorder', SimpleNamespace(PvRecorder=Recorder))
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(
        Vad=lambda mode: SimpleNamespace(is_speech=is_speech),
    ))
    monkeypatch.setattr('builtins.input', enter)
    return state


def events(capsys):
    """Separate structured results from the microphone prompt."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize('segments, outcome', [
    (['제이크야!'], 'wake_detected'),
    (['제이크야', ' 거실로 가'], 'not_wake'),
    (['안녕'], 'not_wake'),
    ([], 'not_wake'),
])
def test_wav_needs_only_a_local_model_and_checks_all_segments(
    runtime, monkeypatch, tmp_path, capsys, segments, outcome,
):
    """File recognition never imports microphone SDKs or opens a recorder."""
    for name in ('numpy', 'pvrecorder', 'webrtcvad'):
        monkeypatch.setitem(sys.modules, name, None)
    recording = tmp_path / 'sample.wav'
    recording.touch()  # Decoding belongs to the replaced model boundary.
    runtime.segments = segments
    assert main(runtime.args + ['--wav', str(recording)]) == 0
    output = events(capsys)
    assert len(output) == 1 and output[0]['event'] == outcome
    assert output[0]['text'] == ''.join(segments).strip()
    assert runtime.events == ['model', 'transcribe']
    assert runtime.requests[0][0] == str(recording)
    path, options = runtime.model_options
    assert path == runtime.args[1]
    assert options['local_files_only'] is True
    assert options['device'] == 'cpu' and options['compute_type'] == 'int8'


@pytest.mark.parametrize('segments, outcome', [
    (['제이크야'], 'wake_detected'),
    (['제이크야 거실로 가'], 'not_wake'),
    ([], 'not_wake'),
])
def test_once_preserves_first_frame_and_closes_before_recognition(
    runtime, capsys, segments, outcome,
):
    """One completed attempt ends even when its whole transcript is rejected."""
    runtime.segments = segments
    assert main(runtime.args + ['--once', '--device-index', '2']) == 0
    assert [item['event'] for item in events(capsys)] == ['waiting_for_wake', outcome]
    assert runtime.events == [
        'model', 'enter', 'recorder', 'start', 'stop', 'delete', 'transcribe',
    ]
    assert runtime.recorder_options == {'frame_length': 512, 'device_index': 2}
    assert len(runtime.requests) == 1
    audio, options = runtime.requests[0]
    assert audio[:512] == [1 / 32768.0] * 512
    assert audio[512:] == [0.0] * (len(audio) - 512)
    assert len(audio) == 320 * 22  # Two VAD speech frames, then 0.4 s silence.
    assert options['language'] == 'ko'
    assert options['condition_on_previous_text'] is False


@pytest.mark.parametrize('frames, outcome', [
    ([[0] * 512] * 157, 'no_speech'),
    ([[1] * 512] * 189, 'too_long'),
])
def test_once_discards_silence_and_overlong_audio_without_asr(runtime, capsys, frames, outcome):
    """A failed capture consumes the first attempt and cannot start another."""
    runtime.frames = frames
    assert main(runtime.args + ['--once']) == 0
    assert [item['event'] for item in events(capsys)] == ['waiting_for_wake', outcome]
    assert runtime.requests == []
    assert runtime.events == ['model', 'enter', 'recorder', 'start', 'stop', 'delete']


@pytest.mark.parametrize('interrupt', ['input', 'read'])
def test_ctrl_c_releases_open_microphone_without_recognition(runtime, interrupt):
    """Stopping before or during recording releases each opened resource."""
    runtime.interrupt = interrupt
    assert main(runtime.args + ['--once']) == 0
    assert runtime.requests == []
    expected = ['model', 'enter']
    if interrupt == 'read':
        expected += ['recorder', 'start', 'stop', 'delete']
    assert runtime.events == expected


@pytest.mark.parametrize('failure', [
    'model', 'recorder', 'sample_rate', 'start', 'read', 'vad',
    'stop', 'delete', 'transcribe', 'segments',
])
def test_errors_release_resources_without_retry_or_exception_details(runtime, capsys, failure):
    """Device and delayed ASR errors exit once, including cleanup failures."""
    runtime.failure = failure
    if failure == 'sample_rate':
        runtime.sample_rate = 48000
    assert main(runtime.args + ['--once']) == 1
    output = capsys.readouterr()
    assert 'PRIVATE-RUNTIME-DETAIL' not in output.out + output.err
    assert 'wake_detected' not in output.out
    stopped = json.loads(output.out.splitlines()[-1])
    assert stopped['event'] == 'stopped'
    assert stopped['error'] == ('ValueError' if failure == 'sample_rate' else 'RuntimeError')
    assert runtime.events.count('recorder') <= 1
    if failure not in ('model', 'recorder'):
        assert runtime.events.count('delete') == 1
    if failure not in ('model', 'recorder', 'sample_rate', 'start'):
        assert runtime.events.count('stop') == 1
    if failure in ('transcribe', 'segments'):
        assert runtime.events[-3:] == ['stop', 'delete', 'transcribe']
        assert len(runtime.requests) == 1
    else:
        assert runtime.requests == []


@pytest.mark.parametrize('missing', ['model', 'wav'])
def test_missing_paths_fail_before_loading_model_or_opening_hardware(runtime, tmp_path, missing):
    """Reject missing local inputs without falling back to a downloaded model."""
    args = ['--model-path', str(tmp_path / 'absent-model')]
    if missing == 'wav':
        args = runtime.args + ['--wav', str(tmp_path / 'absent.wav')]
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert runtime.events == []


def test_missing_tokenizer_stops_before_sdk_or_microphone_initialization(runtime, tmp_path, capsys):
    """Missing local tokenizer data cannot trigger a hosted tokenizer fallback."""
    (tmp_path / 'local-model' / 'tokenizer.json').unlink()
    assert main(runtime.args + ['--once']) == 1
    assert events(capsys) == [{
        'event': 'stopped', 'phase': 'loading_local_model', 'error': 'ValueError',
    }]
    assert runtime.events == []
    assert runtime.requests == []
