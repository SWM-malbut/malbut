"""Check whole-sentence buffering without loading a model or audio device."""

from threading import Event
from unittest.mock import Mock

import numpy as np
import pytest

from malbut_tts.audio import PlaybackCancelled
from malbut_tts.sentence_synthesis import SentenceSynthesizer


class ScriptedSynthesizer:
    """Record exact model inputs and close every scripted model iterator."""

    def __init__(self, script=None):
        self.texts = []
        self.closed = []
        self.load = Mock()
        self.script = script or self.default_script

    @staticmethod
    def default_script(index, cancel_event):
        yield [index + 1], 24000

    def generate(self, text, cancel_event):
        index = len(self.texts)
        self.texts.append(text)
        try:
            yield from self.script(index, cancel_event)
        finally:
            self.closed.append(index)


def test_sentence_generation_is_lazy_ordered_and_preserves_original_text():
    backend = ScriptedSynthesizer()
    engine = SentenceSynthesizer(backend, max_chars=32)
    original = '  첫 번째 문장입니다.\n두 번째 문장입니다!  마지막 문장입니다?  '
    iterator = engine.generate(original, Event())
    assert backend.texts == []
    first, rate = next(iterator)
    assert len(backend.texts) == 1
    assert backend.closed == [0]
    assert first.tolist() == [1]
    assert first.dtype == np.float32 and first.flags.c_contiguous
    assert rate == 24000
    remaining = list(iterator)
    assert len(remaining) == 2
    assert ''.join(backend.texts) == original
    assert all(segment.strip() for segment in backend.texts)
    assert backend.closed == [0, 1, 2]
    assert [audio.tolist() for audio, _ in remaining] == [[2], [3]]
    assert engine.sentence_streaming is True
    assert engine.synthesis_streaming is False
    engine.load()
    backend.load.assert_called_once_with()


def test_fragments_are_copied_and_combined_only_after_sentence_completion():
    source = np.array([1, 2], dtype=np.float64)

    def script(index, cancel_event):
        yield source, 24000
        source[:] = 9
        yield [3, 4], 24000

    backend = ScriptedSynthesizer(script)
    result = list(SentenceSynthesizer(backend).generate('One sentence.', Event()))
    assert len(result) == 1
    np.testing.assert_array_equal(result[0][0], [1, 2, 3, 4])
    assert backend.closed == [0]


@pytest.mark.parametrize('rate', [0, -1, 24000.5, True, '24000', None,
                                  float('nan'), float('inf')])
def test_invalid_sample_rates_are_rejected_before_sentence_yield(rate):
    def script(index, cancel_event):
        yield [1], rate

    backend = ScriptedSynthesizer(script)
    with pytest.raises(RuntimeError, match='invalid sample rate'):
        next(SentenceSynthesizer(backend).generate('One sentence.', Event()))
    assert backend.closed == [0]


@pytest.mark.parametrize('pcm', [[], [[1]], [float('nan')], [float('inf')],
                                 ['not PCM'], None])
def test_invalid_pcm_is_rejected_before_sentence_yield(pcm):
    def script(index, cancel_event):
        yield pcm, 24000

    backend = ScriptedSynthesizer(script)
    with pytest.raises(RuntimeError, match='invalid PCM'):
        next(SentenceSynthesizer(backend).generate('One sentence.', Event()))
    assert backend.closed == [0]


@pytest.mark.parametrize('between_sentences', [False, True])
def test_rate_changes_fail_within_and_between_sentences(between_sentences):
    def script(index, cancel_event):
        yield [1], 24000 if index == 0 else 16000
        if not between_sentences:
            yield [2], 16000

    backend = ScriptedSynthesizer(script)
    iterator = SentenceSynthesizer(backend).generate('First. Second.', Event())
    if between_sentences:
        assert next(iterator)[1] == 24000
    with pytest.raises(RuntimeError, match='sample rate changed'):
        next(iterator)
    assert backend.closed == ([0, 1] if between_sentences else [0])


def test_no_audio_is_a_failure_and_does_not_skip_to_the_next_sentence():
    def script(index, cancel_event):
        yield from ()

    backend = ScriptedSynthesizer(script)
    with pytest.raises(RuntimeError, match='no audio'):
        next(SentenceSynthesizer(backend).generate('First. Second.', Event()))
    assert len(backend.texts) == 1
    assert backend.closed == [0]


@pytest.mark.parametrize('failure_index', [0, 1])
def test_late_model_failure_discards_current_sentence_and_never_retries(failure_index):
    def script(index, cancel_event):
        yield [index + 1], 24000
        if index == failure_index:
            raise RuntimeError('late model failure')

    backend = ScriptedSynthesizer(script)
    iterator = SentenceSynthesizer(backend).generate('First. Second. Third.', Event())
    if failure_index:
        assert next(iterator)[0].tolist() == [1]
    with pytest.raises(RuntimeError, match='late model failure'):
        next(iterator)
    assert len(backend.texts) == failure_index + 1
    assert backend.closed == list(range(failure_index + 1))


def test_cancel_before_start_does_not_invoke_the_model():
    backend = ScriptedSynthesizer()
    cancel = Event()
    cancel.set()
    with pytest.raises(PlaybackCancelled):
        next(SentenceSynthesizer(backend).generate('First.', cancel))
    assert backend.texts == []


def test_cancel_between_sentences_does_not_invoke_the_next_model_call():
    backend = ScriptedSynthesizer()
    cancel = Event()
    iterator = SentenceSynthesizer(backend).generate('First. Second.', cancel)
    next(iterator)
    cancel.set()
    with pytest.raises(PlaybackCancelled):
        next(iterator)
    assert len(backend.texts) == 1
    assert backend.closed == [0]


@pytest.mark.parametrize('cancel_after_yield', [False, True])
def test_cancellation_during_generation_discards_the_whole_current_sentence(cancel_after_yield):
    def script(index, cancel_event):
        if not cancel_after_yield:
            cancel_event.set()
        yield [1], 24000
        cancel_event.set()

    backend = ScriptedSynthesizer(script)
    with pytest.raises(PlaybackCancelled):
        next(SentenceSynthesizer(backend).generate('First. Second.', Event()))
    assert backend.closed == [0]
    assert len(backend.texts) == 1


def test_closing_sentence_iterator_does_not_start_another_segment():
    backend = ScriptedSynthesizer()
    iterator = SentenceSynthesizer(backend).generate('First. Second.', Event())
    next(iterator)
    iterator.close()
    assert len(backend.texts) == 1
    assert backend.closed == [0]


@pytest.mark.parametrize('max_chars', [True, 15, 513, 80.0, '80'])
def test_invalid_sentence_limits_fail_at_configuration(max_chars):
    with pytest.raises(ValueError, match='max_chars'):
        SentenceSynthesizer(ScriptedSynthesizer(), max_chars=max_chars)
