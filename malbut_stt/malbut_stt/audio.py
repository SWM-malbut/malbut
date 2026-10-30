"""Collect one bounded utterance after the wake phrase, without disk I/O."""

from collections import deque
from dataclasses import dataclass
import math
import os
from queue import Empty, Full, Queue
from threading import Event
from time import monotonic
from typing import Callable, Optional


class CapturePCM(bytes):
    """Aligned local ASR, VAD, and optional overlap-recovery waveforms."""

    def __new__(cls, pcm, vad_pcm, recovery_pcm=None):
        if len(pcm) != len(vad_pcm) or len(pcm) % 2:
            raise ValueError('Recognition and VAD PCM must have identical sample clocks')
        if recovery_pcm is not None and len(recovery_pcm) != len(pcm):
            raise ValueError('Recovery and recognition PCM must have identical sample clocks')
        value = super().__new__(cls, pcm)
        value.vad_pcm = bytes(vad_pcm)
        value.recovery_pcm = None if recovery_pcm is None else bytes(recovery_pcm)
        return value


@dataclass(frozen=True)
class CaptureSettings:
    """Audio durations in seconds; the VAD frame is always 20 milliseconds."""

    start_timeout_s: float = 5.0
    silence_timeout_s: float = 1.0
    max_utterance_s: Optional[float] = 20.0
    pre_roll_s: float = 0.3
    max_buffer_s: float = 60.0
    min_speech_s: float = 0.08

    def __post_init__(self) -> None:
        """Reject invalid durations before listening starts."""
        durations = (
            self.start_timeout_s, self.silence_timeout_s,
            self.pre_roll_s, self.max_buffer_s, self.min_speech_s,
        )
        if self.max_utterance_s is not None:
            durations += (self.max_utterance_s,)
        for value in durations:
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('capture durations must be finite and positive')
        if (self.max_utterance_s is not None
                and self.silence_timeout_s >= self.max_utterance_s):
            raise ValueError('silence timeout must be shorter than utterance limit')
        if self.silence_timeout_s >= self.max_buffer_s:
            raise ValueError('silence timeout must be shorter than audio buffer limit')
        if self.pre_roll_s > self.start_timeout_s:
            raise ValueError('pre-roll must not exceed speech start timeout')
        if self.pre_roll_s > self.max_buffer_s:
            raise ValueError('pre-roll must not exceed audio buffer limit')
        if self.min_speech_s > self.pre_roll_s:
            raise ValueError('minimum speech must not exceed pre-roll')


@dataclass(frozen=True)
class CaptureResult:
    """Return either a complete PCM utterance or a reason for discarding it."""

    status: str
    pcm: bytes = b''
    revision: int = 0
    silence_s: float = 0.0
    audio_start_s: float = 0.0
    start_blocked: bool = False
    voiced_s: float = 0.0


class UtteranceCollector:
    """Reframe recorder chunks for VAD and retain only post-wake audio."""

    def __init__(
        self,
        sample_rate: int,
        is_speech: Callable[[bytes, int], bool],
        settings: CaptureSettings,
        *, start_qualifier=None,
    ) -> None:
        """Create a fresh collector for one wake detection."""
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError('unsupported capture sample rate')
        self.sample_rate = sample_rate
        self.is_speech = is_speech
        # Stateful VADs must not reuse PCM/context from a previous capture.
        detector = getattr(is_speech, '__self__', is_speech)
        reset = getattr(detector, 'reset', None)
        if callable(reset):
            reset()
        self.settings = settings
        self.start_qualifier = start_qualifier
        self.frame_bytes = sample_rate // 50 * 2
        self.pending = bytearray()
        self.pending_vad = bytearray()
        self._pending_start_blocked = False
        self.pre_roll = deque(maxlen=max(1, math.ceil(settings.pre_roll_s / 0.02)))
        self.audio = bytearray()
        self.audio_start_samples = 0
        self.wait_frames = 0
        self.speech_frames = 0
        self.voiced_frames = 0
        self.silent_frames = 0
        self.candidate_frames = 0
        self.candidate_audio = bytearray()
        self._candidate_start_blocked = False
        self.start_blocked = False
        self.started = False
        self.result: Optional[CaptureResult] = None
        self.revision = 0

    def feed(self, pcm: bytes, *, start_blocked: bool = False) -> Optional[CaptureResult]:
        """Accept PCM16 and retain onset eligibility across frame/chunk boundaries."""
        if self.result is not None:
            raise RuntimeError('utterance is already finalized')
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        if pcm:
            self._pending_start_blocked |= start_blocked
        self.pending.extend(pcm)
        self.pending_vad.extend(getattr(pcm, 'vad_pcm', pcm))
        while len(self.pending) >= self.frame_bytes:
            frame = bytes(self.pending[:self.frame_bytes])
            vad_frame = bytes(self.pending_vad[:self.frame_bytes])
            del self.pending[:self.frame_bytes]
            del self.pending_vad[:self.frame_bytes]
            frame_blocked = self._pending_start_blocked
            # The old partial frame was consumed; remaining samples came from
            # this call and must not inherit that older frame's eligibility.
            self._pending_start_blocked = bool(self.pending) and start_blocked
            qualified = (self.start_qualifier(vad_frame, self.sample_rate)
                         if self.start_qualifier is not None else True)
            speech = self.is_speech(vad_frame, self.sample_rate)
            if not self.started:
                self.wait_frames += 1
                self.pre_roll.append(frame)
                if speech:
                    # Preserve pre-roll relative to the first voiced frame,
                    # including the onset while it earns enough VAD evidence.
                    if self.candidate_frames == 0:
                        self.candidate_audio.extend(b''.join(self.pre_roll))
                    else:
                        self.candidate_audio.extend(frame)
                    self._candidate_start_blocked |= frame_blocked
                    self.candidate_frames += 1
                    if (qualified and self.candidate_frames >= math.ceil(
                            self.settings.min_speech_s / 0.02)):
                        self.started = True
                        self.revision += self.candidate_frames
                        self.audio.extend(self.candidate_audio)
                        self.pre_roll.clear()
                        self.speech_frames = self.candidate_frames
                        self.voiced_frames = self.candidate_frames
                        self.start_blocked = self._candidate_start_blocked
                        self.candidate_audio.clear()
                        self.candidate_frames = 0
                        self._candidate_start_blocked = False
                    elif (self.start_qualifier is not None
                          and self.candidate_frames >= math.ceil(self.settings.pre_roll_s / 0.02)):
                        # A rejected WebRTC run must not retain unbounded PCM
                        # or prevent the idle timeout from ever firing.
                        self.candidate_frames = 0
                        self.candidate_audio.clear()
                        self._candidate_start_blocked = False
                else:
                    self.candidate_frames = 0
                    self.candidate_audio.clear()
                    self._candidate_start_blocked = False
                # Let an onset crossing the idle-window boundary finish its
                # short qualification, instead of losing a valid short word.
                if (not self.started and self.candidate_frames == 0
                        and self.wait_frames >= math.ceil(self.settings.start_timeout_s / 0.02)):
                    self.result = CaptureResult('no_speech')
            else:
                self.audio.extend(frame)
                self.speech_frames += 1
                self.silent_frames = 0 if speech else self.silent_frames + 1
                if speech:
                    self.revision += 1
                    self.voiced_frames += 1
            if self.started:
                if (self.settings.max_utterance_s is not None
                        and self.speech_frames > math.floor(
                            self.settings.max_utterance_s / 0.02)):
                    self.result = CaptureResult('too_long')
                elif len(self.audio) > self.settings.max_buffer_s * self.sample_rate * 2:
                    # This is unprocessed-audio backlog, not total speech duration.
                    self.result = CaptureResult('buffer_overflow')
                elif self.silent_frames >= math.ceil(self.settings.silence_timeout_s / 0.02):
                    self.result = self.snapshot('complete')
            if self.result is not None:
                self.pending.clear()
                self._pending_start_blocked = False
                self.pre_roll.clear()
                self.candidate_audio.clear()
                self.candidate_frames = 0
                self._candidate_start_blocked = False
                self.audio.clear()
                return self.result
        return None

    @property
    def audio_start_s(self) -> float:
        """Absolute start time of the PCM still retained for this utterance."""
        return self.audio_start_samples / self.sample_rate

    def snapshot(self, status: str) -> CaptureResult:
        """Associate an immutable audio snapshot with its last voiced frame."""
        return CaptureResult(status, bytes(self.audio), self.revision,
                             self.silent_frames * 0.02,
                             self.audio_start_s, self.start_blocked,
                             self.voiced_frames * 0.02)

    def discard_before(self, audio_start_s: float) -> None:
        """Release PCM only up to a successfully transcribed stable boundary."""
        if not math.isfinite(audio_start_s) or audio_start_s < 0:
            raise ValueError('audio boundary must be finite and nonnegative')
        start = round(audio_start_s * self.sample_rate)
        if start <= self.audio_start_samples:
            return
        count = start - self.audio_start_samples
        if count * 2 > len(self.audio):
            raise ValueError('audio boundary exceeds retained recording')
        del self.audio[:count * 2]
        self.audio_start_samples = start


class MicrophoneOverflow(RuntimeError):
    """Audio was lost, but the input stream can still be read."""


class MicrophoneTimeout(RuntimeError):
    """The input device stopped delivering frames."""


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
        self._frames = Queue(maxsize=32)
        self._cancelled = Event()
        self._overflow = Event()

        # Use the same device IDs as `python -m sounddevice`.
        self._device = None if device_index == -1 else device_index
        # Robot Bringup pins XFM in PULSE_SOURCE for both consumers. Standalone
        # capture and explicit non-XFM device selections keep their old behavior.
        shared_source = os.environ.get('MALBUT_SHARED_MICROPHONE', '')
        if device_index == 0 and shared_source:
            if os.environ.get('PULSE_SOURCE') != shared_source:
                raise RuntimeError('Shared microphone source does not match PULSE_SOURCE')
            self._device = 'pulse'

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
            callback=self._capture,
        )

    def _capture(self, data, frames, time_info, status):
        # PortAudio owns its callback thread. Python readers never enter a
        # blocking native read that can race with stop/close after USB removal.
        if self._cancelled.is_set():
            return
        if status.input_overflow:
            self._overflow.set()
        try:
            self._frames.put_nowait(bytes(data))
        except Full:
            self._overflow.set()

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

        deadline = monotonic() + 5.0
        while not self._cancelled.is_set():
            if self._overflow.is_set():
                self._overflow.clear()
                while True:
                    try:
                        self._frames.get_nowait()
                    except Empty:
                        break
                raise MicrophoneOverflow('microphone input overflow')
            try:
                data = self._frames.get(timeout=0.05)
                if self._overflow.is_set():
                    continue
                break
            except Empty:
                if monotonic() >= deadline:
                    raise MicrophoneTimeout('microphone input timeout')
        else:
            raise RuntimeError('microphone capture cancelled')

        samples = np.frombuffer(data, dtype='<i2')
        if samples.size != self._frame_length:
            raise RuntimeError('microphone returned an incomplete frame')

        return samples.tolist()

    def cancel_read(self):
        """Release Python readers without touching the native audio stream."""
        self._cancelled.set()

    def stop(self):
        self.cancel_read()
        self._stream.stop()

    def delete(self):
        self._stream.close()

    @staticmethod
    def get_available_devices():
        """List all device names so enumeration preserves PortAudio device IDs."""
        import sounddevice as sd

        return [info['name'] for info in sd.query_devices()]
