"""Collect one bounded utterance after the wake phrase, without disk I/O."""

from collections import deque
from dataclasses import dataclass
import math
from typing import Callable, Optional


@dataclass(frozen=True)
class CaptureSettings:
    """Audio durations in seconds; the VAD frame is always 20 milliseconds."""

    start_timeout_s: float = 5.0
    silence_timeout_s: float = 1.0
    max_utterance_s: float = 20.0
    pre_roll_s: float = 0.3

    def __post_init__(self) -> None:
        """Reject invalid durations before listening starts."""
        for value in (
            self.start_timeout_s, self.silence_timeout_s,
            self.max_utterance_s, self.pre_roll_s,
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('capture durations must be finite and positive')
        if self.silence_timeout_s >= self.max_utterance_s:
            raise ValueError('silence timeout must be shorter than utterance limit')
        if self.pre_roll_s > self.start_timeout_s:
            raise ValueError('pre-roll must not exceed speech start timeout')


@dataclass(frozen=True)
class CaptureResult:
    """Return either a complete PCM utterance or a reason for discarding it."""

    status: str
    pcm: bytes = b''


class UtteranceCollector:
    """Reframe recorder chunks for VAD and retain only post-wake audio."""

    def __init__(
        self,
        sample_rate: int,
        is_speech: Callable[[bytes, int], bool],
        settings: CaptureSettings,
    ) -> None:
        """Create a fresh collector for one wake detection."""
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError('sample rate is unsupported by WebRTC VAD')
        self.sample_rate = sample_rate
        self.is_speech = is_speech
        self.settings = settings
        self.frame_bytes = sample_rate // 50 * 2
        self.pending = bytearray()
        self.pre_roll = deque(maxlen=max(1, math.ceil(settings.pre_roll_s / 0.02)))
        self.audio = bytearray()
        self.wait_frames = 0
        self.speech_frames = 0
        self.silent_frames = 0
        self.started = False
        self.result: Optional[CaptureResult] = None

    def feed(self, pcm: bytes) -> Optional[CaptureResult]:
        """Accept PCM16 little-endian chunks and finalize at most once."""
        if self.result is not None:
            raise RuntimeError('utterance is already finalized')
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        self.pending.extend(pcm)
        while len(self.pending) >= self.frame_bytes:
            frame = bytes(self.pending[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            speech = self.is_speech(frame, self.sample_rate)
            if not self.started:
                self.wait_frames += 1
                self.pre_roll.append(frame)
                if speech:
                    self.started = True
                    self.audio.extend(b''.join(self.pre_roll))
                    self.pre_roll.clear()
                    self.speech_frames = 1
                elif self.wait_frames >= math.ceil(self.settings.start_timeout_s / 0.02):
                    self.result = CaptureResult('no_speech')
            else:
                self.audio.extend(frame)
                self.speech_frames += 1
                self.silent_frames = 0 if speech else self.silent_frames + 1
                if self.speech_frames > math.floor(self.settings.max_utterance_s / 0.02):
                    self.result = CaptureResult('too_long')
                elif self.silent_frames >= math.ceil(self.settings.silence_timeout_s / 0.02):
                    self.result = CaptureResult('complete', bytes(self.audio))
            if self.result is not None:
                self.pending.clear()
                self.pre_roll.clear()
                self.audio.clear()
                return self.result
        return None
