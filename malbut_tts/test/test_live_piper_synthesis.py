"""Live reply timing, bounded synthesis, cancellation and PCM validation."""

import sys
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_tts.audio import PlaybackCancelled
from malbut_tts.backends import create_synthesizer
from malbut_tts.live_piper_synthesis import LivePiperSynthesizer


def prepared(voice):
    engine = LivePiperSynthesizer('/local/voice.onnx')
    engine._voice = voice
    engine._normalizer = lambda text: text.replace('15분', '십오 분')
    engine._synthesis = object()
    engine.sample_rate = 22050
    return engine


@pytest.mark.parametrize('configured_threads,expected_threads', [(None, 3), (4, 4)])
@pytest.mark.parametrize('speed', [0.9, 1.0, 1.5])
def test_cpu_load_and_local_configuration(
        tmp_path, monkeypatch, configured_threads, expected_threads, speed):
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
        PiperConfig=SimpleNamespace(from_dict=lambda _: SimpleNamespace(
            sample_rate=22050, length_scale=1.1)),
        SynthesisConfig=SimpleNamespace))
    for module in ('torch', 'qwen_tts', 'openai', 'malbut_tts.api_synthesis'):
        monkeypatch.setitem(sys.modules, module, None)
    options = dict(piper_speed=speed)
    if configured_threads is not None:
        options['piper_threads'] = configured_threads
    engine = create_synthesizer(path, **options)
    engine.load()
    engine.load()
    assert len(sessions) == 1
    assert sessions[0][1]['providers'] == ['CPUExecutionProvider']
    assert sessions[0][1]['sess_options'].intra_op_num_threads == expected_threads
    assert engine.sample_rate == 22050
    assert engine._synthesis.length_scale == pytest.approx(1.1 / speed)


def test_missing_model_fails_before_optional_imports(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'piper', None)
    with pytest.raises(FileNotFoundError):
        LivePiperSynthesizer(tmp_path / 'missing.onnx').load()


def test_cancelled_request_does_not_load_model():
    event = Event()
    event.set()
    with pytest.raises(PlaybackCancelled):
        next(LivePiperSynthesizer('/missing.onnx').generate('안녕하세요.', event))


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

    assert len(chunks) == len(seen) > 1
    assert ''.join(seen) == engine._normalizer(text.replace('**', ''))
    assert all(len(segment) <= 40 and '*' not in segment for segment in seen)
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


def test_long_first_sentence_yields_bounded_audio_before_synthesizing_the_rest():
    seen = []
    normalized = []

    def synthesize(text, _):
        seen.append(text)
        yield SimpleNamespace(audio_float_array=np.ones(100))

    engine = prepared(SimpleNamespace(synthesize=synthesize))
    text = '오늘은 날씨가 맑으니 가벼운 겉옷을 챙기고 가까운 공원을 천천히 산책하면 좋겠어요.'
    engine._normalizer = lambda value: normalized.append(value) or value
    stream = engine.generate(text, Event())
    next(stream)
    assert len(seen) == 1 and 0 < len(seen[0]) <= 40
    assert len(seen[0]) < len(text)
    list(stream)
    assert normalized == [text] and ''.join(seen) == text


def test_cancellation_does_not_start_the_next_segment():
    seen = []

    def synthesize(text, _):
        seen.append(text)
        yield SimpleNamespace(audio_float_array=np.ones(100))

    event = Event()
    stream = prepared(SimpleNamespace(synthesize=synthesize)).generate('반가워요. 다음 문장이에요.', event)
    next(stream)
    event.set()
    with pytest.raises(PlaybackCancelled):
        next(stream)
    assert seen == ['반가워요. ']


def test_empty_later_segment_is_an_error_not_silent_truncation():
    def synthesize(text, _):
        if text.startswith('반가워요'):
            yield SimpleNamespace(audio_float_array=np.ones(100))

    stream = prepared(SimpleNamespace(synthesize=synthesize)).generate('반가워요. 다음 문장이에요.', Event())
    next(stream)
    with pytest.raises(RuntimeError, match='no audio for segment'):
        next(stream)


@pytest.mark.parametrize('options', [
    {'speed': 0.49}, {'speed': 2.01}, {'speed': True}, {'speed': None},
    {'speed': float('nan')}, {'speed': float('inf')}, {'speed': '1.2'},
    {'max_chars': 15}, {'max_chars': 513}, {'max_chars': True}, {'max_chars': 40.0},
])
def test_invalid_timing_options_fail_before_model_loading(options):
    with pytest.raises(ValueError):
        LivePiperSynthesizer('/missing.onnx', **options)
