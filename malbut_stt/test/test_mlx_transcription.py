"""Exercise the optional MLX adapter without Apple hardware, inference, or network."""

from concurrent.futures import ThreadPoolExecutor
import sys
from threading import get_ident
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.mlx_transcription import MlxWhisperTranscriber
from malbut_stt.wake import LocalWakeRecognizer


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    path = tmp_path / 'mlx-small'
    path.mkdir()
    (path / 'config.json').write_text('{}')
    (path / 'weights.npz').write_bytes(b'fake-local-weights')
    state = SimpleNamespace(path=path, events=[], requests=[], segments=['  앞 ', '뒤.  '])
    state.loaded = SimpleNamespace(encoder=SimpleNamespace(conv1=SimpleNamespace(
        weight=SimpleNamespace(dtype='float16'))))
    state.loaded.state = {'encoder': state.loaded.encoder}

    def load(path, dtype):
        state.events.append(('load', path, dtype))
        return state.loaded

    def transcribe(audio, **options):
        state.events.append('decode')
        state.requests.append((audio, options))
        return {'segments': [{'text': text} for text in state.segments]}

    mx = SimpleNamespace(
        float16='float16', gpu='gpu',
        set_default_device=lambda device: state.events.append(('device', device)),
        eval=lambda tree: state.events.append(('eval', tree)),
        synchronize=lambda: state.events.append('sync'),
    )
    module = SimpleNamespace(ModelHolder=SimpleNamespace(get_model=load), transcribe=transcribe)
    monkeypatch.setitem(sys.modules, 'mlx', SimpleNamespace(core=mx))
    monkeypatch.setitem(sys.modules, 'mlx.core', mx)
    monkeypatch.setitem(sys.modules, 'mlx_whisper', SimpleNamespace(transcribe=module))
    monkeypatch.setitem(sys.modules, 'mlx_whisper.transcribe', module)
    for forbidden in ('faster_whisper', 'openai', 'pvrecorder', 'rclpy', 'huggingface_hub'):
        monkeypatch.setitem(sys.modules, forbidden, None)
    monkeypatch.setattr('malbut_stt.mlx_transcription.platform.system', lambda: 'Darwin')
    monkeypatch.setattr('malbut_stt.mlx_transcription.platform.machine', lambda: 'arm64')
    return state


def test_model_is_preloaded_and_synchronized_before_returning(runtime):
    transcriber = MlxWhisperTranscriber(runtime.path)
    assert runtime.events == [
        ('device', 'gpu'), ('load', str(runtime.path), 'float16'),
        ('eval', runtime.loaded.state), 'sync',
    ]
    assert runtime.requests == []
    assert transcriber.backend == 'mlx' and transcriber.model.compute_type == 'float16'


def test_lazy_nonparameter_buffers_are_ready_for_first_background_decode(runtime, monkeypatch):
    class LazyBuffer:
        def __init__(self):
            self.owner = get_ident()
            self.ready = False

        def evaluate(self):
            if not self.ready and self.owner != get_ident():
                raise RuntimeError('There is no Stream(gpu, 1) in current thread.')
            self.ready = True

    positional, mask = LazyBuffer(), LazyBuffer()
    runtime.loaded.state = {
        'encoder': {'_positional_embedding': positional}, 'decoder': {'_mask': mask},
    }

    def evaluate_tree(tree):
        if isinstance(tree, dict):
            for value in tree.values():
                evaluate_tree(value)
        elif isinstance(tree, LazyBuffer):
            tree.evaluate()

    monkeypatch.setattr(sys.modules['mlx.core'], 'eval', evaluate_tree)
    transcriber = MlxWhisperTranscriber(runtime.path)

    def decode(*_args, **_kwargs):
        positional.evaluate()
        mask.evaluate()
        return {'segments': [{'text': '제이크야.'}]}

    transcriber.model.decode = decode
    wake = LocalWakeRecognizer.from_transcriber(transcriber)
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(wake.transcribe, b'\x01\x00' * 320, 16000).result() == '제이크야.'
    assert runtime.requests == []  # Loading did not run an ASR warmup.


def test_pcm_samples_all_segments_and_serial_wake_sharing_are_preserved(runtime):
    transcriber = MlxWhisperTranscriber(runtime.path)
    wake = LocalWakeRecognizer.from_transcriber(transcriber)
    assert wake.model is transcriber.model
    pcm = np.array([-32768, -1, 0, 1, 32767], dtype='<i2').tobytes()
    assert transcriber.transcribe(pcm, 16000) == '앞 뒤.'
    runtime.segments = ['제이크야']
    assert wake.transcribe(pcm, 16000) == '제이크야'
    runtime.segments = ['  다음\n문장  ']
    assert transcriber.transcribe(pcm, 16000) == '다음\n문장'
    assert sum(isinstance(event, tuple) and event[0] == 'load'
               for event in runtime.events) == 1
    assert [options['initial_prompt'] for _, options in runtime.requests] == [
        None, '로봇 이름은 제이크입니다.', None,
    ]
    for audio, options in runtime.requests:
        assert audio.dtype == np.float32
        np.testing.assert_array_equal(audio, [-1, -1/32768, 0, 1/32768, 32767/32768])
        assert options | {'initial_prompt': None} == {
            'path_or_hf_repo': str(runtime.path), 'language': 'ko', 'task': 'transcribe',
            'condition_on_previous_text': False, 'initial_prompt': None,
            'fp16': True, 'best_of': 5, 'verbose': None,
        }
    assert runtime.events[4:] == ['sync', 'decode', 'sync'] * 3


@pytest.mark.parametrize('pcm', [b'', bytes(640), bytes(64000)])
def test_zero_pcm_skips_numpy_and_mlx_decode(runtime, monkeypatch, pcm):
    transcriber = MlxWhisperTranscriber(runtime.path)
    monkeypatch.setitem(sys.modules, 'numpy', None)
    assert transcriber.transcribe(pcm, 16000) == ''
    assert LocalWakeRecognizer.from_transcriber(transcriber).transcribe(pcm, 16000) == ''
    assert runtime.requests == [] and runtime.events[-1] == 'sync'


@pytest.mark.parametrize('missing', ['directory', 'config.json', 'weights.npz'])
def test_missing_local_assets_fail_before_import_or_hosted_fallback(runtime, monkeypatch, missing):
    path = runtime.path
    if missing == 'directory':
        path = path / 'absent'
    else:
        (path / missing).unlink()
    monkeypatch.setitem(sys.modules, 'mlx', None)
    monkeypatch.setitem(sys.modules, 'mlx_whisper', None)
    with pytest.raises(ValueError, match='local'):
        MlxWhisperTranscriber(path)
    assert runtime.events == []


def test_removed_model_directory_cannot_be_interpreted_as_a_hub_id(runtime):
    transcriber = MlxWhisperTranscriber(runtime.path)
    (runtime.path / 'weights.npz').unlink()
    with pytest.raises(ValueError, match='local'):
        transcriber.transcribe(b'\x01\x00' * 320, 16000)
    assert runtime.requests == []


@pytest.mark.parametrize('system, machine', [('Linux', 'aarch64'), ('Darwin', 'x86_64')])
def test_unsupported_platform_fails_before_import_or_inference(runtime, monkeypatch,
                                                               system, machine):
    monkeypatch.setattr('malbut_stt.mlx_transcription.platform.system', lambda: system)
    monkeypatch.setattr('malbut_stt.mlx_transcription.platform.machine', lambda: machine)
    monkeypatch.setitem(sys.modules, 'mlx', None)
    with pytest.raises(RuntimeError, match='Apple Silicon macOS'):
        MlxWhisperTranscriber(runtime.path)
    assert runtime.events == []


def test_incompatible_cached_weight_dtype_does_not_claim_fp16(runtime):
    runtime.loaded.encoder.conv1.weight.dtype = 'float32'
    with pytest.raises(ValueError, match='fp16 model weights'):
        MlxWhisperTranscriber(runtime.path)
    assert runtime.requests == []


def test_decode_failure_still_synchronizes_and_does_not_fall_back_to_api(runtime):
    transcriber = MlxWhisperTranscriber(runtime.path)

    def fail(*_args, **_kwargs):
        raise RuntimeError('native failure')

    transcriber.model.decode = fail
    with pytest.raises(RuntimeError):
        transcriber.transcribe(b'\x01\x00' * 320, 16000)
    assert runtime.events[-2:] == ['sync', 'sync']


@pytest.mark.parametrize('pcm, rate', [(b'\x00', 16000), (b'\x01\x00', 8000)])
def test_invalid_pcm_never_reaches_mlx(runtime, pcm, rate):
    transcriber = MlxWhisperTranscriber(runtime.path)
    with pytest.raises(ValueError):
        transcriber.transcribe(pcm, rate)
    assert runtime.requests == []
