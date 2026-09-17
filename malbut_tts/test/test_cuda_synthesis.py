"""Test buffered CUDA synthesis without importing Torch or opening hardware."""

from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from malbut_tts.audio import PlaybackCancelled
from malbut_tts.cuda_synthesis import CudaSynthesizer
from malbut_tts.runtime import SpeechRuntime


class FakeTalker:
    """Model the real per-forward hooks and batch-one generation result."""

    def __init__(self):
        self.config = SimpleNamespace(codec_eos_token_id=99)
        self.hooks = []
        self.sequences = np.array([[1, 2, 99]])
        self.before_forward = lambda: None
        self.on_generate = lambda: None
        self.forward_count = 0

    def register_forward_pre_hook(self, callback):
        self.hooks.append(callback)
        return SimpleNamespace(remove=lambda: self.hooks.remove(callback))

    def generate(self, **kwargs):
        self.before_forward()
        for callback in self.hooks:
            callback(self, ())
        self.forward_count += 1
        self.on_generate()
        return SimpleNamespace(sequences=self.sequences)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    talker = FakeTalker()
    state = SimpleNamespace(
        talker=talker, cancel=Event(), path=tmp_path,
        audio=np.arange(9, dtype=np.float64) / 10, rate=8,
        after_generate=lambda: None,
    )

    def generate(**kwargs):
        talker.generate(max_new_tokens=kwargs['max_new_tokens'])
        state.after_generate()
        return [state.audio], state.rate

    state.model = SimpleNamespace(
        device=SimpleNamespace(type='cuda'),
        model=SimpleNamespace(talker=talker, eval=Mock()),
        generate_custom_voice=Mock(side_effect=generate),
    )
    state.loader = Mock(return_value=state.model)
    state.torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=Mock(return_value=True)),
        float16=object(), float32=object(),
    )
    modules = {
        'torch': state.torch,
        'qwen_tts': SimpleNamespace(Qwen3TTSModel=SimpleNamespace(
            from_pretrained=state.loader)),
    }
    state.import_module = Mock(side_effect=modules.__getitem__)
    monkeypatch.setattr(
        'malbut_tts.cuda_synthesis.importlib.import_module', state.import_module)
    state.engine = CudaSynthesizer(tmp_path)
    return state


def assert_restored(rig):
    assert rig.talker.hooks == []
    assert 'generate' not in vars(rig.talker)


def test_lazy_local_cuda_model_reuse_and_exact_whole_text(rig):
    rig.import_module.assert_not_called()
    text = '  안녕하세요.\n다음 문장도 그대로 읽어 주세요.  '
    chunks = list(rig.engine.generate(text, rig.cancel))
    assert [len(audio) for audio, _ in chunks] == [4, 4, 1]
    assert all(audio.dtype == np.float32 and rate == 8 for audio, rate in chunks)
    np.testing.assert_array_equal(np.concatenate([a for a, _ in chunks]),
                                  rig.audio.astype(np.float32))
    rig.model.generate_custom_voice.assert_called_once_with(
        text=text, speaker='Sohee', language='Korean', instruct=None,
        non_streaming_mode=True, max_new_tokens=1024,
    )
    assert rig.engine.synthesis_streaming is False
    list(rig.engine.generate('새 요청', rig.cancel))
    rig.loader.assert_called_once_with(
        str(rig.path.resolve()), device_map='cuda:0',
        dtype=rig.torch.float32, attn_implementation='sdpa', local_files_only=True,
    )
    rig.model.model.eval.assert_called_once_with()
    assert_restored(rig)


def test_explicit_fp16_device_and_generation_limit(rig):
    engine = CudaSynthesizer(rig.path, dtype='float16', device='cuda:1',
                             max_new_tokens=128, chunk_seconds=0.25)
    assert len(list(engine.generate('시험', rig.cancel))) == 5
    assert rig.loader.call_args.kwargs['dtype'] is rig.torch.float16
    assert rig.loader.call_args.kwargs['device_map'] == 'cuda:1'
    assert rig.model.generate_custom_voice.call_args.kwargs['max_new_tokens'] == 128


@pytest.mark.parametrize('options', [
    {'dtype': 'bfloat16'}, {'dtype': 'auto'}, {'device': 'cpu'},
    {'device': 'auto'}, {'device': 'cuda:-1'},
    {'max_new_tokens': 0}, {'max_new_tokens': True}, {'max_new_tokens': 4097},
    {'chunk_seconds': 0}, {'chunk_seconds': float('nan')},
    {'chunk_seconds': float('inf')}, {'chunk_seconds': True},
    {'speaker': ''}, {'language': None},
])
def test_invalid_configuration_fails_before_import(rig, options):
    with pytest.raises(ValueError):
        CudaSynthesizer(rig.path, **options)
    rig.import_module.assert_not_called()


def test_missing_local_directory_never_loads_or_downloads(rig):
    engine = CudaSynthesizer(rig.path / 'missing')
    with pytest.raises(FileNotFoundError):
        next(engine.generate('시험', rig.cancel))
    rig.import_module.assert_not_called()


def test_cuda_absence_has_no_fallback(rig):
    rig.torch.cuda.is_available.return_value = False
    with pytest.raises(RuntimeError, match='available CUDA GPU'):
        next(rig.engine.generate('시험', rig.cancel))
    rig.loader.assert_not_called()
    rig.import_module.assert_called_once_with('torch')


def test_loader_cannot_silently_return_cpu_model(rig):
    rig.model.device.type = 'cpu'
    with pytest.raises(RuntimeError, match='not loaded on CUDA'):
        next(rig.engine.generate('시험', rig.cancel))
    assert rig.engine._model is None
    rig.model.generate_custom_voice.assert_not_called()


def test_cancel_before_start_performs_no_loading(rig):
    rig.cancel.set()
    with pytest.raises(PlaybackCancelled):
        next(rig.engine.generate('시험', rig.cancel))
    rig.import_module.assert_not_called()


def test_cancel_during_load_skips_generation(rig):
    def load(*args, **kwargs):
        rig.cancel.set()
        return rig.model

    rig.loader.side_effect = load
    with pytest.raises(PlaybackCancelled):
        next(rig.engine.generate('시험', rig.cancel))
    rig.model.generate_custom_voice.assert_not_called()
    assert_restored(rig)


def test_cooperative_cancel_interrupts_next_talker_forward(rig):
    rig.talker.before_forward = rig.cancel.set
    with pytest.raises(PlaybackCancelled):
        next(rig.engine.generate('시험', rig.cancel))
    assert rig.talker.forward_count == 0
    assert_restored(rig)


def test_cancel_after_talker_discards_result_before_decode(rig):
    rig.talker.on_generate = rig.cancel.set
    with pytest.raises(PlaybackCancelled):
        next(rig.engine.generate('시험', rig.cancel))
    assert_restored(rig)


def test_cancel_during_waveform_decode_discards_all_pcm(rig):
    rig.after_generate = rig.cancel.set
    with pytest.raises(PlaybackCancelled):
        next(rig.engine.generate('시험', rig.cancel))
    assert_restored(rig)


def test_cancel_between_buffered_chunks_prevents_next_yield(rig):
    iterator = rig.engine.generate('시험', rig.cancel)
    next(iterator)
    rig.cancel.set()
    with pytest.raises(PlaybackCancelled):
        next(iterator)
    assert_restored(rig)


def test_cancel_after_last_chunk_is_not_normal_generator_completion(rig):
    rig.audio = np.array([0.1])
    iterator = rig.engine.generate('시험', rig.cancel)
    next(iterator)
    rig.cancel.set()
    with pytest.raises(PlaybackCancelled):
        next(iterator)


def test_token_cap_without_eos_cannot_publish_truncated_audio(rig):
    rig.talker.sequences = np.array([[1, 2, 3]])
    with pytest.raises(RuntimeError, match='generation limit without EOS'):
        next(rig.engine.generate('시험', rig.cancel))
    assert_restored(rig)


@pytest.mark.parametrize('sequences', [None, np.array([]), np.array([99]),
                                       np.empty((1, 0)), np.array([[99], [99]])])
def test_unverifiable_generation_fails_closed(rig, sequences):
    rig.talker.sequences = sequences
    with pytest.raises(RuntimeError, match='verify Qwen codec'):
        next(rig.engine.generate('시험', rig.cancel))
    assert_restored(rig)


def test_bypassed_talker_cannot_claim_verified_completion(rig):
    rig.model.generate_custom_voice.side_effect = None
    rig.model.generate_custom_voice.return_value = ([rig.audio], rig.rate)
    with pytest.raises(RuntimeError, match='verify Qwen codec'):
        next(rig.engine.generate('시험', rig.cancel))
    assert_restored(rig)


def test_model_exception_restores_hook_and_original_generate(rig):
    rig.model.generate_custom_voice.side_effect = RuntimeError('model failure')
    with pytest.raises(RuntimeError, match='CUDA TTS generation failed: RuntimeError'):
        next(rig.engine.generate('시험', rig.cancel))
    assert_restored(rig)


@pytest.mark.parametrize('source', ['import', 'load', 'generate'])
def test_backend_exception_never_exposes_private_utterance(rig, source):
    sentinel = 'PRIVATE_UTTERANCE_SENTINEL'
    error = RuntimeError(f'Backend failed while processing {sentinel}')
    if source == 'import':
        rig.import_module.side_effect = error
    elif source == 'load':
        rig.loader.side_effect = error
    else:
        rig.model.generate_custom_voice.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        next(rig.engine.generate(sentinel, rig.cancel))
    assert sentinel not in str(caught.value)
    assert str(caught.value).startswith('CUDA TTS ')
    assert caught.value.__suppress_context__
    assert_restored(rig)


def test_runtime_cancel_during_generation_then_same_engine_next_request(rig):
    """An actual runtime drops cancelled PCM and reuses a clean model afterward."""
    started, proceed = Event(), Event()
    statuses, players = [], []
    original_generate = rig.model.generate_custom_voice.side_effect
    calls = 0

    def generate(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert proceed.wait(3), 'Test did not release cancelled generation'
        return original_generate(**kwargs)

    rig.model.generate_custom_voice.side_effect = generate

    class Player:
        def __init__(self, on_state, cancel_event):
            self.on_state = on_state
            self.cancel = cancel_event
            self.audio = []
            self.finished = False
            players.append(self)

        def write(self, audio, rate):
            assert not self.cancel.is_set()
            if not self.audio:
                self.on_state('playing')
            self.audio.append(audio.copy())

        def finish(self):
            self.finished = True

        def stop(self):
            self.cancel.set()

        def close(self):
            pass

    logger = Mock()
    runtime = SpeechRuntime(rig.engine, Player,
                            lambda pid, state: statuses.append((pid, state)),
                            logger=logger)
    try:
        first = runtime.submit('취소할 답변')
        assert started.wait(3), 'Runtime did not start generation'
        assert runtime.control(first, 'stop')
        second = runtime.submit('다음 답변')
        proceed.set()
        deadline = monotonic() + 3
        while (second, 'finished') not in statuses and monotonic() < deadline:
            sleep(0.002)
        assert statuses == [(first, 'stopped'), (second, 'playing'),
                            (second, 'finished')]
        assert len(players) == 2 and players[0].audio == []
        assert not players[0].finished and players[1].finished
        np.testing.assert_array_equal(np.concatenate(players[1].audio),
                                      rig.audio.astype(np.float32))
        rig.loader.assert_called_once()
        assert_restored(rig)
        logger.error.assert_not_called()
    finally:
        proceed.set()
        runtime.close()


def test_runtime_error_logger_receives_content_free_backend_error(rig):
    sentinel = 'PRIVATE_RUNTIME_SENTINEL'
    rig.model.generate_custom_voice.side_effect = RuntimeError(sentinel)
    terminal = Event()
    logger = Mock()
    player = SimpleNamespace(close=Mock(), stop=Mock())
    runtime = SpeechRuntime(
        rig.engine, lambda **_: player,
        lambda pid, state: terminal.set() if state == 'failed' else None,
        logger=logger,
    )
    try:
        runtime.submit(sentinel)
        assert terminal.wait(3), 'Runtime did not report synthesis failure'
        assert logger.error.call_count == 1
        assert sentinel not in str(logger.error.call_args)
        assert 'CUDA TTS generation failed: RuntimeError' in str(logger.error.call_args)
    finally:
        runtime.close()


def test_existing_instance_generate_override_is_restored(rig):
    original = rig.talker.generate
    rig.talker.generate = original
    list(rig.engine.generate('시험', rig.cancel))
    assert vars(rig.talker)['generate'] is original
    assert rig.talker.hooks == []


@pytest.mark.parametrize('audio', [[], [[0.1]], [float('nan')],
                                  [float('inf')], ['not-pcm']])
def test_invalid_pcm_never_reaches_player(rig, audio):
    rig.audio = audio
    with pytest.raises(RuntimeError, match='invalid PCM'):
        next(rig.engine.generate('시험', rig.cancel))
    assert_restored(rig)


@pytest.mark.parametrize('rate', [0, -1, 8.5, float('nan'), float('inf'),
                                 True, '24000', None])
def test_invalid_sample_rate_fails(rig, rate):
    rig.rate = rate
    with pytest.raises(RuntimeError, match='invalid sample rate'):
        next(rig.engine.generate('시험', rig.cancel))


def test_generator_close_releases_request_without_leaving_hooks(rig):
    iterator = rig.engine.generate('시험', rig.cancel)
    next(iterator)
    iterator.close()
    assert_restored(rig)
    assert len(list(rig.engine.generate('다음 요청', rig.cancel))) == 3
