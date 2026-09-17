"""Turn a buffered local synthesizer into a lazy sentence PCM producer."""

from math import isfinite
from numbers import Real

import numpy as np

from malbut_tts.audio import PlaybackCancelled
from malbut_tts.sentences import iter_speech_segments


class SentenceSynthesizer:
    """Synthesize one complete segment per pull; never replay a failed segment.

    SpeechRuntime reserves a slot in StreamingPlayer before pulling the next
    segment. A single model generates in order while the audio thread consumes
    previous PCM. Sentence boundaries are not separate playback IDs or terminals.
    This is sentence pipelining, not model token/audio streaming.
    """

    sentence_streaming = True
    synthesis_streaming = False

    def __init__(self, synthesizer, *, max_chars=80):
        if type(max_chars) is not int or not 16 <= max_chars <= 512:
            raise ValueError('max_chars must be an integer from 16 through 512')
        self._synthesizer = synthesizer
        self._max_chars = max_chars

    def load(self):
        self._synthesizer.load()

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event.is_set():
            raise PlaybackCancelled('Speech synthesis was cancelled.')

    def generate(self, text, cancel_event):
        """Yield full validated sentence PCM, preserving segment order and text."""
        self._check_cancel(cancel_event)
        expected_rate = None
        for segment in iter_speech_segments(text, max_chars=self._max_chars):
            self._check_cancel(cancel_event)
            iterator = iter(self._synthesizer.generate(segment, cancel_event))
            parts = []
            try:
                for audio, sample_rate in iterator:
                    self._check_cancel(cancel_event)
                    if (isinstance(sample_rate, bool) or not isinstance(sample_rate, Real)
                            or not isfinite(sample_rate) or sample_rate <= 0
                            or int(sample_rate) != sample_rate):
                        raise RuntimeError('The model returned an invalid sample rate')
                    rate = int(sample_rate)
                    if expected_rate is not None and expected_rate != rate:
                        raise RuntimeError('The sample rate changed during playback.')
                    expected_rate = rate
                    try:
                        part = np.array(audio, dtype=np.float32, order='C', copy=True)
                    except (TypeError, ValueError, OverflowError):
                        raise RuntimeError('The model generated invalid PCM') from None
                    if part.ndim != 1 or not part.size or not np.isfinite(part).all():
                        raise RuntimeError('The model generated invalid PCM')
                    parts.append(part)
                self._check_cancel(cancel_event)
                if not parts:
                    raise RuntimeError('TTS generated no audio')
                waveform = np.concatenate(parts)
            finally:
                close = getattr(iterator, 'close', None)
                if close is not None:
                    close()
            # Drop fragment copies before yielding a bounded whole sentence.
            parts.clear()
            self._check_cancel(cancel_event)
            yield waveform, expected_rate
        self._check_cancel(cancel_event)
