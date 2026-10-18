"""CPU selection, cancellation and PCM validation at the Piper boundary."""

import sys
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_tts.audio import PlaybackCancelled
from malbut_tts.backends import create_synthesizer
from malbut_tts.piper_synthesis import PiperSynthesizer


def prepared(voice):
    engine = PiperSynthesizer('/local/voice.onnx')
    engine._voice = voice
    engine._normalizer = lambda text: text.replace('15분', '십오 분')
    engine._synthesis = object()
    engine.sample_rate = 22050
    return engine


def test_cpu_load_and_local_configuration(tmp_path, monkeypatch):
    path = tmp_path / 'voice.onnx'
    path.write_bytes(b'model')
    path.with_suffix('.onnx.json').write_text('{}')
    sessions = []

    class Options:
        def add_session_config_entry(self, *_):
            pass

    def session(path, **kwargs):
        sessions.append((path, kwargs))
        return SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider'])

    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(
        SessionOptions=Options, InferenceSession=session))
    monkeypatch.setitem(sys.modules, 'piper', SimpleNamespace(
        PiperVoice=lambda **kwargs: SimpleNamespace(**kwargs)))
    monkeypatch.setitem(sys.modules, 'piper.config', SimpleNamespace(
        PiperConfig=SimpleNamespace(from_dict=lambda _: SimpleNamespace(sample_rate=22050)),
        SynthesisConfig=object))
    for module in ('torch', 'qwen_tts', 'openai', 'malbut_tts.api_synthesis'):
        monkeypatch.setitem(sys.modules, module, None)
    engine = create_synthesizer(path, backend='piper', piper_threads=4)
    engine.load()
    engine.load()
    assert len(sessions) == 1
    assert sessions[0][1]['providers'] == ['CPUExecutionProvider']
    assert sessions[0][1]['sess_options'].intra_op_num_threads == 4
    assert engine.sample_rate == 22050


def test_missing_model_fails_before_optional_imports(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'piper', None)
    with pytest.raises(FileNotFoundError):
        PiperSynthesizer(tmp_path / 'missing.onnx').load()


def test_cancelled_request_does_not_load_model():
    event = Event()
    event.set()
    with pytest.raises(PlaybackCancelled):
        next(PiperSynthesizer('/missing.onnx').generate('안녕하세요.', event))


def test_normalization_and_stop_between_sentences():
    seen = []
    closed = []

    def synthesize(text, _):
        seen.append(text)
        try:
            yield SimpleNamespace(audio_float_array=np.ones(100))
            raise AssertionError('Cancelled requests must not synthesize another sentence')
        finally:
            closed.append(True)

    event = Event()
    stream = prepared(SimpleNamespace(synthesize=synthesize)).generate('15분 뒤입니다.', event)
    pcm, rate = next(stream)
    assert seen == ['십오 분 뒤입니다.']
    assert pcm.dtype == np.float32 and rate == 22050
    event.set()
    with pytest.raises(PlaybackCancelled):
        next(stream)
    assert closed == [True]


def test_model_names_reach_piper_without_markdown_stars():
    from malbut_tts.korean_normalization import KoreanNormalizer

    seen = []

    def synthesize(text, _):
        seen.append(text)
        yield SimpleNamespace(audio_float_array=np.ones(100))

    engine = prepared(SimpleNamespace(synthesize=synthesize))
    engine._normalizer = KoreanNormalizer()
    text = (
        '네, 있어요. 국내 모델로는 네이버 **HyperCLOVA X**, '
        'LG AI연구원의 **EXAONE**, SK텔레콤의 **A.X** 계열이 있고, '
        '카카오의 **Kanana**도 공개 범위가 확대되고 있습니다.'
    )
    chunks = list(engine.generate(text, Event()))

    assert len(chunks) == 1
    assert seen == [engine._normalizer(text.replace('**', ''))]
    assert '*' not in seen[0]
    assert '**HyperCLOVA X**' in text  # The original dialogue remains unchanged.


@pytest.mark.parametrize('audio', [[], [float('nan')], [[1., 2.]]])
def test_invalid_audio_is_rejected(audio):
    engine = prepared(SimpleNamespace(synthesize=lambda *_: iter([
        SimpleNamespace(audio_float_array=audio)])))
    with pytest.raises(RuntimeError, match='invalid PCM'):
        list(engine.generate('안녕', Event()))


def test_empty_synthesis_is_not_a_success():
    engine = prepared(SimpleNamespace(synthesize=lambda *_: iter([])))
    with pytest.raises(RuntimeError, match='no audio'):
        list(engine.generate('안녕', Event()))
