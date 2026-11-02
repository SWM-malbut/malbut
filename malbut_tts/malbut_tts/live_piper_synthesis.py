"""Bound first-audio latency and configure the speaking rate of live Piper replies."""

import math

import numpy as np

from malbut_tts.piper_synthesis import PiperSynthesizer
from malbut_tts.sentences import iter_speech_segments


class LivePiperSynthesizer(PiperSynthesizer):
    """Normalize once, then synthesize bounded segments lazily at the selected rate."""

    def __init__(self, model_path, *, threads=3, speed=0.9, max_chars=40):
        if (type(speed) not in (int, float) or not math.isfinite(speed)
                or not 0.5 <= speed <= 2.0):
            raise ValueError('Piper speed must be finite and between 0.5 and 2.0')
        if type(max_chars) is not int or not 16 <= max_chars <= 512:
            raise ValueError('Piper max_chars must be an integer from 16 through 512')
        super().__init__(model_path, threads=threads)
        self._speed = speed
        self._max_chars = max_chars

    def load(self):
        with self._lock:
            if self._voice is not None:
                return
            super().load()
            from piper.config import SynthesisConfig

            # Use the voice's own timing as the baseline; keep pitch/sample rate.
            self._synthesis = SynthesisConfig(
                length_scale=self._voice.config.length_scale / self._speed)

    def generate(self, text, cancel_event):
        self._check_cancel(cancel_event)
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ValueError('Piper text must contain 1 through 4096 characters')
        self.load()
        with self._lock:
            self._check_cancel(cancel_event)
            # Normalize the whole request before sentence splitting so number
            # labels and multiword lexicon entries retain their context.
            processed = self._normalizer(text)
            emitted = False
            for segment in iter_speech_segments(processed, max_chars=self._max_chars):
                self._check_cancel(cancel_event)
                chunks = iter(self._voice.synthesize(segment, self._synthesis))
                segment_emitted = False
                try:
                    while True:
                        self._check_cancel(cancel_event)
                        try:
                            chunk = next(chunks)
                        except StopIteration:
                            break
                        self._check_cancel(cancel_event)
                        pcm = np.array(chunk.audio_float_array, dtype=np.float32, copy=True)
                        if pcm.ndim != 1 or not pcm.size or not np.isfinite(pcm).all():
                            raise RuntimeError('Piper generated invalid PCM')
                        emitted = segment_emitted = True
                        yield pcm, self.sample_rate
                    if not segment_emitted:
                        raise RuntimeError('Piper generated no audio for segment')
                finally:
                    close = getattr(chunks, 'close', None)
                    if close is not None:
                        close()
            if not emitted:
                raise RuntimeError('Piper generated no audio')
