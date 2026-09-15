"""Exercise local sentence recognition without models, devices, or network I/O."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.transcription import LocalWhisperTranscriber
from malbut_stt.wake import LocalWakeRecognizer


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """Replace the model boundary and make the API SDK unavailable."""
    state = SimpleNamespace(loads=[], requests=[], segments=['  거실로 ', '가 줘.\n'])
    state.path = tmp_path / 'model'
    state.path.mkdir()
    (state.path / 'tokenizer.json').write_text('{}')

    class Model:
        def __init__(self, path, **kwargs):
            state.loads.append((path, kwargs))

        def transcribe(self, audio, **kwargs):
            state.requests.append((audio, kwargs))
            return (SimpleNamespace(text=text) for text in state.segments), None

    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    monkeypatch.setitem(sys.modules, 'openai', None)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    return state


def test_local_sentence_uses_complete_pcm_and_preserves_internal_text(runtime):
    """Normalize PCM16 amplitude and emit every segment without a robot-name bias."""
    transcriber = LocalWhisperTranscriber(str(runtime.path))
    pcm = np.array([-32768, -1, 0, 1, 32767], dtype='<i2').tobytes()
    assert transcriber.transcribe(pcm, 16000) == '거실로 가 줘.'
    assert runtime.loads == [(str(runtime.path), {
        'device': 'cpu', 'compute_type': 'int8', 'cpu_threads': 6,
        'local_files_only': True,
    })]
    audio, options = runtime.requests[0]
    assert audio.dtype == np.float32
    np.testing.assert_array_equal(audio, [-1.0, -1 / 32768, 0.0, 1 / 32768, 32767 / 32768])
    assert options == {
        'language': 'ko', 'beam_size': 1, 'condition_on_previous_text': False,
        'initial_prompt': None,
    }


def test_model_is_reused_and_wake_prompt_does_not_carry_into_command(runtime):
    """The same model accepts a wake hint without retaining it for the next turn."""
    transcriber = LocalWhisperTranscriber(str(runtime.path))
    transcriber.transcribe(b'\x01\x00' * 320, 16000, initial_prompt='로봇 이름은 제이크입니다.')
    runtime.segments = ['  ROS 2를\n실행해 줘.  ']
    assert transcriber.transcribe(b'\x01\x00' * 320, 16000) == 'ROS 2를\n실행해 줘.'
    assert len(runtime.loads) == 1
    assert [options['initial_prompt'] for _, options in runtime.requests] == [
        '로봇 이름은 제이크입니다.', None,
    ]


def test_wake_wrapper_shares_the_loaded_model_without_loading_or_importing_again(
    runtime, monkeypatch,
):
    """One model services wake and normal text with a distinct prompt on every call."""
    transcriber = LocalWhisperTranscriber(str(runtime.path))
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    wake = LocalWakeRecognizer.from_transcriber(transcriber)
    assert wake.model is transcriber.model
    pcm = b'\x01\x00' * 320
    transcriber.transcribe(pcm, 16000)
    runtime.segments = ['제이크야']
    assert wake.transcribe(pcm, 16000) == '제이크야'
    runtime.segments = ['이름 힌트 없는 문장']
    assert transcriber.transcribe(pcm, 16000) == '이름 힌트 없는 문장'
    assert len(runtime.loads) == 1
    assert [options['initial_prompt'] for _, options in runtime.requests] == [
        None, '로봇 이름은 제이크입니다.', None,
    ]


def test_cuda_settings_are_explicit_and_still_load_only_local_files(runtime):
    """A Jetson caller can select its compute settings without changing the backend."""
    LocalWhisperTranscriber(str(runtime.path), device='cuda', compute_type='int8_float16')
    assert runtime.loads == [(str(runtime.path), {
        'device': 'cuda', 'compute_type': 'int8_float16', 'cpu_threads': 6,
        'local_files_only': True,
    })]


@pytest.mark.parametrize('missing', ['directory', 'tokenizer', 'tokenizer_file'])
def test_incomplete_model_is_rejected_before_model_loading(runtime, missing):
    """A missing tokenizer cannot trigger faster-whisper's remote tokenizer fallback."""
    (runtime.path / 'tokenizer.json').unlink()
    if missing == 'directory':
        runtime.path.rmdir()
    elif missing == 'tokenizer_file':
        (runtime.path / 'tokenizer.json').mkdir()
    with pytest.raises(ValueError):
        LocalWhisperTranscriber(str(runtime.path))
    assert runtime.loads == []


@pytest.mark.parametrize('pcm, sample_rate', [(bytes(640), 48000), (b'\x01', 16000)])
def test_invalid_pcm_is_rejected_before_inference(runtime, pcm, sample_rate):
    """Only complete little-endian PCM16 samples at 16 kHz reach Whisper."""
    transcriber = LocalWhisperTranscriber(str(runtime.path))
    with pytest.raises(ValueError):
        transcriber.transcribe(pcm, sample_rate)
    assert runtime.requests == []


def test_empty_pcm_returns_no_transcript_without_inference(runtime):
    """An empty input must not generate invented speech."""
    transcriber = LocalWhisperTranscriber(str(runtime.path))
    assert transcriber.transcribe(b'', 16000) == ''
    assert runtime.requests == []


@pytest.mark.parametrize('recognizer', [LocalWhisperTranscriber, LocalWakeRecognizer])
@pytest.mark.parametrize('pcm', [b'', bytes(640), bytes(64000)])
def test_exact_zero_pcm_never_imports_numpy_or_runs_inference(
    runtime, monkeypatch, recognizer, pcm,
):
    engine = recognizer(str(runtime.path))
    monkeypatch.setitem(sys.modules, 'numpy', None)
    assert engine.transcribe(pcm, 16000) == ''
    assert runtime.requests == []


@pytest.mark.parametrize('recognizer', [LocalWhisperTranscriber, LocalWakeRecognizer])
@pytest.mark.parametrize('sample', [1, -1])
def test_one_nonzero_pcm_sample_is_preserved_without_a_volume_threshold(
    runtime, recognizer, sample,
):
    engine = recognizer(str(runtime.path))
    pcm = bytes(320) + np.array([sample], dtype='<i2').tobytes() + bytes(318)
    engine.transcribe(pcm, 16000)
    assert len(runtime.requests) == 1
    audio = runtime.requests[0][0]
    assert np.count_nonzero(audio) == 1 and audio[160] == sample / 32768.0


@pytest.mark.parametrize('pcm', [b'\x00', b'\x01'])
def test_wake_rejects_incomplete_pcm_samples_before_silence_check(runtime, pcm):
    engine = LocalWakeRecognizer(str(runtime.path))
    with pytest.raises(ValueError, match='whole samples'):
        engine.transcribe(pcm, 16000)
    assert runtime.requests == []


@pytest.mark.parametrize('recognizer', [LocalWhisperTranscriber, LocalWakeRecognizer])
def test_float32_is_explicit_local_only_and_keeps_thread_and_beam_defaults(runtime, recognizer):
    engine = recognizer(str(runtime.path), compute_type='float32')
    engine.transcribe(b'\x01\x00' * 320, 16000)
    assert runtime.loads[0][1] == {
        'device': 'cpu', 'compute_type': 'float32', 'cpu_threads': 6, 'local_files_only': True,
    }
    assert runtime.requests[0][1]['beam_size'] == 1
