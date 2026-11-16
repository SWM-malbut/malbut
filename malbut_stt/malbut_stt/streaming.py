"""Collect consecutive utterances from one continuous mono PCM16 input stream."""

import math
from collections import deque
from typing import Callable

from malbut_stt.audio import CapturePCM, CaptureResult, CaptureSettings, UtteranceCollector


class StreamingUtteranceCollector:
    """Emit speech onset and complete recordings without reopening the input.

    The caller supplies microphone audio after any required echo cancellation;
    a voice activity detector alone does not distinguish TTS from user speech.
    """

    def __init__(self, is_speech: Callable[[bytes, int], bool], *,
                 settings: CaptureSettings | None = None,
                 early_endpoint_s: float | None = None,
                 endpoint_min_silence_s: float = 1.0,
                 partial_interval_s: float | None = None,
                 start_qualifier=None) -> None:
        self.settings = settings or CaptureSettings(silence_timeout_s=2.0)
        if early_endpoint_s is not None and (
            isinstance(early_endpoint_s, bool) or not math.isfinite(early_endpoint_s)
            or not 0 < early_endpoint_s < self.settings.silence_timeout_s
        ):
            raise ValueError('early endpoint must precede the fallback silence timeout')
        self.early_endpoint_s = early_endpoint_s
        self.endpoint_min_silence_s = endpoint_min_silence_s
        self._next_turn_timing = None
        if partial_interval_s is not None and (
            isinstance(partial_interval_s, bool) or not math.isfinite(partial_interval_s)
            or partial_interval_s <= 0
        ):
            raise ValueError('partial interval must be positive')
        self.partial_interval_s = partial_interval_s
        self.is_speech = is_speech
        self.start_qualifier = start_qualifier
        self.pending = bytearray()
        self.pending_vad = bytearray()
        self._pending_start_blocked = False
        self.discarding = False
        self.quiet_frames = 0
        self._revision = 0
        self._extended_pre_roll_frames = None
        self.collector = self._new_collector()
        self._candidate_revision = None

    def _new_collector(self) -> UtteranceCollector:
        self._revision += 1
        self._partial_frames = 0
        self._ordinary_pre_roll_at = None
        if self.start_qualifier is not None:
            self.start_qualifier.reset()
        collector = UtteranceCollector(16000, self.is_speech, self.settings,
                                      start_qualifier=self.start_qualifier)
        if self._extended_pre_roll_frames is not None:
            collector.pre_roll = deque(maxlen=self._extended_pre_roll_frames)
        collector.revision = self._revision
        return collector

    def reset(self, *, pre_roll_s=None) -> None:
        """Drop buffered audio and select the pre-roll for the new capture owner."""
        self._extended_pre_roll_frames = (math.ceil(pre_roll_s / 0.02)
                                          if pre_roll_s is not None else None)
        self.pending.clear()
        self.pending_vad.clear()
        self._pending_start_blocked = False
        self.discarding = False
        self.quiet_frames = 0
        self.collector = self._new_collector()
        self._candidate_revision = None

    def end_extended_pre_roll(self):
        """Keep the reply's buffered onset until all its samples have aged out."""
        if self._extended_pre_roll_frames is not None:
            self._ordinary_pre_roll_at = self.collector.wait_frames + self._extended_pre_roll_frames
            self._extended_pre_roll_frames = None

    def set_next_turn_timing(self, minimum_s, settings, predecode_s):
        """Stage validated command timings until the next unvoiced boundary."""
        self._next_turn_timing = (minimum_s, settings, predecode_s)

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

    def feed(self, pcm: bytes, *, start_blocked: bool = False) -> list[CaptureResult]:
        """Accept arbitrary recorder chunks and preserve their frame boundaries.

        A qualified VAD onset emits ``speech_started`` before each ``complete``
        or ``too_long`` result. Short isolated noise creates no event.
        An overlong utterance is discarded until its terminating silence, so
        its tail cannot become a new command. Idle silence creates no event.
        ``start_blocked`` marks samples captured while a new utterance could
        not be accepted; any such frame in the qualified onset marks its event.
        """
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        if pcm:
            self._pending_start_blocked |= start_blocked
        self.pending.extend(pcm)
        self.pending_vad.extend(getattr(pcm, 'vad_pcm', pcm))
        events = []
        frame_bytes = 640  # 20 ms of 16 kHz mono PCM16.
        consumed = 0
        while len(self.pending) - consumed >= frame_bytes:
            vad_frame = bytes(self.pending_vad[consumed:consumed + frame_bytes])
            frame = CapturePCM(self.pending[consumed:consumed + frame_bytes], vad_frame)
            consumed += frame_bytes
            frame_blocked = self._pending_start_blocked
            self._pending_start_blocked = (
                len(self.pending) > consumed and start_blocked)
            if self.discarding:
                if self.is_speech(vad_frame, 16000):
                    self.quiet_frames = 0
                else:
                    self.quiet_frames += 1
                if self.quiet_frames * 0.02 >= self.settings.silence_timeout_s:
                    self.discarding = False
                    self.quiet_frames = 0
                    self.collector = self._new_collector()
                continue

            if (self._next_turn_timing is not None and not self.collector.started
                    and not self.collector.candidate_frames):
                self.endpoint_min_silence_s, self.settings, self.early_endpoint_s = (
                    self._next_turn_timing)
                self.collector.settings = self.settings
                self._next_turn_timing = None
            if (self._ordinary_pre_roll_at is not None
                    and self.collector.wait_frames + 1 >= self._ordinary_pre_roll_at):
                self.collector.pre_roll = deque(self.collector.pre_roll,
                                                maxlen=math.ceil(self.settings.pre_roll_s / 0.02))
                self._ordinary_pre_roll_at = None
            started = self.collector.started
            result = self.collector.feed(frame, start_blocked=frame_blocked)
            self._revision = self.collector.revision
            if not started and self.collector.started:
                events.append(CaptureResult(
                    'speech_started', start_blocked=self.collector.start_blocked))
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
            if result.status in ('too_long', 'buffer_overflow'):
                self.discarding = True
                self.quiet_frames = self.collector.silent_frames
            self.collector = self._new_collector()
            self._candidate_revision = None
        del self.pending[:consumed]
        del self.pending_vad[:consumed]
        return events
