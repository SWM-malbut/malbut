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
    max_utterance_s: Optional[float] = 20.0
    pre_roll_s: float = 0.3

    def __post_init__(self) -> None:
        """Reject invalid durations before listening starts."""
        durations = (
            self.start_timeout_s, self.silence_timeout_s,
            self.pre_roll_s,
        )
        if self.max_utterance_s is not None:
            durations += (self.max_utterance_s,)
        for value in durations:
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('capture durations must be finite and positive')
        if (self.max_utterance_s is not None
                and self.silence_timeout_s >= self.max_utterance_s):
            raise ValueError('silence timeout must be shorter than utterance limit')
        if self.pre_roll_s > self.start_timeout_s:
            raise ValueError('pre-roll must not exceed speech start timeout')


@dataclass(frozen=True)
class CaptureResult:
    """Return either a complete PCM utterance or a reason for discarding it."""

    status: str
    pcm: bytes = b''
    revision: int = 0
    silence_s: float = 0.0


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
        self.revision = 0

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
                    self.revision += 1
                    self.audio.extend(b''.join(self.pre_roll))
                    self.pre_roll.clear()
                    self.speech_frames = 1
                elif self.wait_frames >= math.ceil(self.settings.start_timeout_s / 0.02):
                    self.result = CaptureResult('no_speech')
            else:
                self.audio.extend(frame)
                self.speech_frames += 1
                self.silent_frames = 0 if speech else self.silent_frames + 1
                if speech:
                    self.revision += 1
                if (self.settings.max_utterance_s is not None
                        and self.speech_frames > math.floor(
                            self.settings.max_utterance_s / 0.02)):
                    self.result = CaptureResult('too_long')
                elif self.silent_frames >= math.ceil(self.settings.silence_timeout_s / 0.02):
                    self.result = self.snapshot('complete')
            if self.result is not None:
                self.pending.clear()
                self.pre_roll.clear()
                self.audio.clear()
                return self.result
        return None

    def snapshot(self, status: str) -> CaptureResult:
        """Associate an immutable audio snapshot with its last voiced frame."""
        return CaptureResult(status, bytes(self.audio), self.revision,
                             self.silent_frames * 0.02)

class SoundDeviceRecorder:
    """PvRecorder-compatible microphone wrapper using PortAudio/sounddevice."""

    def __init__(self, frame_length=512, device_index=-1):
        if type(frame_length) is not int or frame_length <= 0:
            raise ValueError('frame_length must be a positive integer')
        if type(device_index) is not int or device_index < -1:
            raise ValueError('device_index must be -1 or a microphone index')

        import sounddevice as sd

        self._sd = sd
        self._frame_length = frame_length
        self._sample_rate = 16000

        # Keep the old PvRecorder semantics:
        # -1 = default microphone
        #  0..N = index among input-capable devices only.
        if device_index == -1:
            self._device = None
        else:
            input_devices = [
                index
                for index, info in enumerate(sd.query_devices())
                if info['max_input_channels'] > 0
            ]
            if device_index >= len(input_devices):
                raise ValueError('microphone device index is out of range')
            self._device = input_devices[device_index]

        sd.check_input_settings(
            device=self._device,
            channels=1,
            dtype='int16',
            samplerate=self._sample_rate,
        )

        self._stream = sd.RawInputStream(
            device=self._device,
            samplerate=self._sample_rate,
            channels=1,
            dtype='int16',
            blocksize=self._frame_length,
        )

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def selected_device(self):
        return self._sd.query_devices(self._device, 'input')['name']

    def start(self):
        self._stream.start()

    def read(self):
        import numpy as np

        data, overflowed = self._stream.read(self._frame_length)
        if overflowed:
            raise RuntimeError('microphone input overflow')

        samples = np.frombuffer(data, dtype='<i2')
        if samples.size != self._frame_length:
            raise RuntimeError('microphone returned an incomplete frame')

        return samples.tolist()

    def stop(self):
        self._stream.stop()

    def delete(self):
        self._stream.close()

    @staticmethod
    def get_available_devices():
        import sounddevice as sd

        return [
            info['name']
            for info in sd.query_devices()
            if info['max_input_channels'] > 0
        ]