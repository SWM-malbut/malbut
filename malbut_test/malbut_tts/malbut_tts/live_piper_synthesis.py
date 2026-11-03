"""Bound first-audio latency and configure the speaking rate of live Piper replies."""

import math
from dataclasses import dataclass

import numpy as np

from malbut_tts.piper_synthesis import PiperSynthesizer
from malbut_tts.sentences import iter_speech_segments


@dataclass(frozen=True)
class SpeechPlan:
    text: str
    segments: tuple
    fingerprint: tuple


@dataclass(frozen=True)
class PreparedSpeech:
    plan: SpeechPlan
    chunks: tuple  # Immutable float32 bytes and sample rate, first segment only.


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

    def _fingerprint(self):
        # Plans never cross a process/session boundary. Object identities bind
        # the loaded model and normalizer; values bind mutable synthesis knobs.
        return (str(self._path), id(self._voice), id(self._normalizer),
                repr(self._synthesis), self._threads, self._speed,
                self._max_chars, self.sample_rate, id(iter_speech_segments))

    def plan(self, text, cancel_event):
        self._check_cancel(cancel_event)
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ValueError('Piper text must contain 1 through 4096 characters')
        self.load()
        with self._lock:
            self._check_cancel(cancel_event)
            # Normalize the whole request before sentence splitting so number
            # labels and multiword lexicon entries retain their context.
            processed = self._normalizer(text)
            segments = tuple(iter_speech_segments(processed, max_chars=self._max_chars))
            if not segments:
                raise RuntimeError('Piper generated no audio')
            return SpeechPlan(text, segments, self._fingerprint())

    def generate(self, text, cancel_event):
        yield from self.generate_plan(self.plan(text, cancel_event), cancel_event)

    def generate_plan(self, plan, cancel_event, *, start=0, stop=None):
        with self._lock:
            self._check_cancel(cancel_event)
            if plan.fingerprint != self._fingerprint():
                raise RuntimeError('Piper speech plan is stale')
            emitted = False
            for segment in plan.segments[start:stop]:
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

    def prepare_first(self, text, cancel_event, *, max_bytes):
        plan = self.plan(text, cancel_event)
        chunks = []
        size = 0
        stream = self.generate_plan(plan, cancel_event, stop=1)
        try:
            for pcm, rate in stream:
                size += pcm.nbytes
                if size > max_bytes:
                    raise ValueError('Prepared PCM exceeds its memory bound')
                chunks.append((pcm.tobytes(), rate))
        finally:
            # A generator owns an RLock: close on this same synthesis thread.
            stream.close()
        return PreparedSpeech(plan, tuple(chunks))

    def resume_prepared(self, text, cancel_event, prepared):
        """Normalize final text once; return iterator and an exact-match hit."""
        plan = self.plan(text, cancel_event)
        hit = (isinstance(prepared, PreparedSpeech)
               and text.startswith(prepared.plan.text)
               and plan.fingerprint == prepared.plan.fingerprint
               and plan.segments[0] == prepared.plan.segments[0])
        if not hit:
            return self.generate_plan(plan, cancel_event), False

        def stream():
            for pcm, rate in prepared.chunks:
                self._check_cancel(cancel_event)
                yield np.frombuffer(pcm, dtype=np.float32), rate
            if len(plan.segments) > 1:
                yield from self.generate_plan(plan, cancel_event, start=1)
        return stream(), True
