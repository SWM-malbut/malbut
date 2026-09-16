"""Collect consecutive utterances from one continuous mono PCM16 input stream."""

import math
from typing import Callable

from malbut_stt.audio import CaptureResult, CaptureSettings, UtteranceCollector


class StreamingUtteranceCollector:
    """Emit speech onset and complete recordings without reopening the input.

    The caller supplies microphone audio after any required echo cancellation;
    a voice activity detector alone does not distinguish TTS from user speech.
    """

    def __init__(self, is_speech: Callable[[bytes, int], bool], *,
                 settings: CaptureSettings | None = None,
                 early_endpoint_s: float | None = None,
                 partial_interval_s: float | None = None) -> None:
        self.settings = settings or CaptureSettings(silence_timeout_s=2.0)
        if early_endpoint_s is not None and (
            isinstance(early_endpoint_s, bool) or not math.isfinite(early_endpoint_s)
            or not 0 < early_endpoint_s < self.settings.silence_timeout_s
        ):
            raise ValueError('early endpoint must precede the fallback silence timeout')
        self.early_endpoint_s = early_endpoint_s
        if partial_interval_s is not None and (
            isinstance(partial_interval_s, bool) or not math.isfinite(partial_interval_s)
            or partial_interval_s <= 0
        ):
            raise ValueError('partial interval must be positive')
        self.partial_interval_s = partial_interval_s
        self.is_speech = is_speech
        self.pending = bytearray()
        self.discarding = False
        self.quiet_frames = 0
        self._revision = 0
        self.collector = self._new_collector()
        self._candidate_revision = None

    def _new_collector(self) -> UtteranceCollector:
        self._revision += 1
        self._partial_frames = 0
        collector = UtteranceCollector(16000, self.is_speech, self.settings)
        collector.revision = self._revision
        return collector

    def reset(self) -> None:
        """Drop buffered audio after a device discontinuity or session reset."""
        self.pending.clear()
        self.discarding = False
        self.quiet_frames = 0
        self.collector = self._new_collector()
        self._candidate_revision = None

    def endpoint_is_current(self, revision: int) -> bool:
        """A resumed voice, reset or completed recording invalidates a candidate."""
        return (self.early_endpoint_s is not None and self.collector.started
                and self.collector.revision == revision and not self.discarding
                and self.collector.silent_frames * 0.02 >= self.early_endpoint_s)

    def finish_endpoint(self, revision: int) -> CaptureResult | None:
        """Finalize a current complete-sentence decision, never an earlier pause."""
        if not self.endpoint_is_current(revision):
            return None
        result = self.collector.snapshot('complete')
        self.collector = self._new_collector()
        self._candidate_revision = None
        return result

    def feed(self, pcm: bytes) -> list[CaptureResult]:
        """Accept arbitrary recorder chunks and preserve their frame boundaries.

        ``speech_started`` precedes each ``complete`` or ``too_long`` result.
        An overlong utterance is discarded until its terminating silence, so
        its tail cannot become a new command. Idle silence creates no event.
        """
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        self.pending.extend(pcm)
        events = []
        frame_bytes = 640  # 20 ms of 16 kHz mono PCM16.
        consumed = 0
        while len(self.pending) - consumed >= frame_bytes:
            frame = bytes(self.pending[consumed:consumed + frame_bytes])
            consumed += frame_bytes
            if self.discarding:
                if self.is_speech(frame, 16000):
                    self.quiet_frames = 0
                else:
                    self.quiet_frames += 1
                if self.quiet_frames * 0.02 >= self.settings.silence_timeout_s:
                    self.discarding = False
                    self.quiet_frames = 0
                    self.collector = self._new_collector()
                continue

            started = self.collector.started
            result = self.collector.feed(frame)
            self._revision = self.collector.revision
            if not started and self.collector.started:
                events.append(CaptureResult('speech_started'))
            if result is None:
                if (self.endpoint_is_current(self.collector.revision)
                        and self._candidate_revision != self.collector.revision):
                    self._candidate_revision = self.collector.revision
                    events.append(self.collector.snapshot('endpoint_check'))
                elif (self.partial_interval_s is not None and self.collector.started
                      and self.collector.silent_frames == 0
                      and self.collector.speech_frames - self._partial_frames
                      >= math.ceil(self.partial_interval_s / 0.02)):
                    self._partial_frames = self.collector.speech_frames
                    events.append(self.collector.snapshot('partial_check'))
                continue
            if result.status != 'no_speech':
                events.append(result)
            if result.status == 'too_long':
                self.discarding = True
                self.quiet_frames = self.collector.silent_frames
            self.collector = self._new_collector()
            self._candidate_revision = None
        del self.pending[:consumed]
        return events
