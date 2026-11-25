"""Front-end audio filtering, adaptive near-field VAD gating, and venue calibration."""

from __future__ import annotations

from collections import deque
import math
from typing import Any, Callable, Sequence

import numpy as np

from malbut_stt.audio import CapturePCM


_SILENCE_DBFS = -120.0
_FULL_SCALE = 32768.0
_WEBRTC_FRAME_SAMPLES = 160  # 10 ms at 16 kHz.


def _require_finite_number(value: Any, name: str, lo: float, hi: float,
                           *, strict_lo: bool = False, strict_hi: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    if (value <= lo if strict_lo else value < lo) or (value >= hi if strict_hi else value > hi):
        raise ValueError(f'{name} out of valid range [{lo}, {hi}]')
    return float(value)


def frame_rms_dbfs(pcm: bytes) -> float:
    """Return mono PCM16 RMS level in dBFS, clamped to -120.0 for exact silence."""
    if not pcm or len(pcm) % 2:
        raise ValueError('RMS measurement requires nonempty mono PCM16')
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / _FULL_SCALE
    mean_square = float(np.mean(samples * samples))
    if mean_square <= 0.0:
        return _SILENCE_DBFS
    return max(_SILENCE_DBFS, 10.0 * math.log10(mean_square))


def _float_to_pcm16(samples: np.ndarray) -> bytes:
    clipped = np.rint(np.clip(samples * _FULL_SCALE, -32768.0, 32767.0)).astype('<i2')
    return clipped.tobytes()


class HighPassFilter:
    """Streaming first-order high-pass IIR filter to strip low-frequency rumble.

    Implements ``y[n] = alpha * (y[n-1] + x[n] - x[n-1])`` with float64 state
    so chunked 20/32 ms calls match whole-utterance filtering.
    """

    def __init__(self, cutoff_hz: float, sample_rate: int = 16000) -> None:
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError('sample_rate must be a positive integer')
        cutoff = _require_finite_number(cutoff_hz, 'cutoff_hz', 0.0, sample_rate / 2.0,
                                        strict_lo=True, strict_hi=True)
        self.cutoff_hz = cutoff
        self.sample_rate = sample_rate
        self.alpha = 1.0 / (1.0 + 2.0 * math.pi * cutoff / sample_rate)
        self._prev_x = 0.0
        self._prev_y = 0.0

    def reset(self) -> None:
        """Clear filter delay state between independent streams."""
        self._prev_x = 0.0
        self._prev_y = 0.0

    def process_samples(self, samples: np.ndarray) -> np.ndarray:
        """Filter mono float samples while preserving state across calls."""
        x = np.asarray(samples, dtype=np.float64)
        if x.ndim != 1 or not np.isfinite(x).all():
            raise ValueError('high-pass filter requires finite 1-D audio')
        if x.size == 0:
            return np.empty(0, dtype=np.float32)
        out = np.empty(x.size, dtype=np.float64)
        alpha = self.alpha
        prev_x = self._prev_x
        prev_y = self._prev_y
        # Process in bounded blocks so alpha**k never underflows float64.
        block_size = 512
        for start in range(0, x.size, block_size):
            chunk = x[start:start + block_size]
            n = chunk.size
            diff = np.empty(n, dtype=np.float64)
            diff[0] = chunk[0] - prev_x
            if n > 1:
                diff[1:] = chunk[1:] - chunk[:-1]
            # Exact closed form for y[k] = alpha * (y[k-1] + diff[k]):
            # y[k] = alpha^(k+1) * (prev_y + sum_{j=0..k} diff[j] / alpha^j)
            exponents = np.arange(n, dtype=np.float64)
            powers = alpha ** exponents
            scaled_sum = np.cumsum(diff / powers)
            y_block = (alpha * powers) * (prev_y + scaled_sum)
            out[start:start + n] = y_block
            prev_x = float(chunk[-1])
            prev_y = float(y_block[-1])
        self._prev_x = prev_x
        self._prev_y = prev_y
        return out.astype(np.float32)

    def process_pcm(self, pcm: bytes) -> bytes:
        """Filter mono PCM16 bytes and return PCM16 bytes of equal length."""
        if len(pcm) % 2:
            raise ValueError('PCM16 input must contain whole samples')
        if not pcm:
            return b''
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / _FULL_SCALE
        return _float_to_pcm16(self.process_samples(samples))


class NoiseFloorTracker:
    """Rolling-quantile ambient noise-floor estimator shared across wake and command VAD.

    Tracks the upper-ambient percentile (default 75th percentile over the last
    3 seconds of non-speech frames) so spiky multi-talker venue babble neither
    collapses into brief valleys nor gets pulled upward by active near-field speech.
    """

    def __init__(
        self,
        *,
        initial_floor_dbfs: float = -55.0,
        min_floor_dbfs: float = -75.0,
        max_floor_dbfs: float = -15.0,
        window_frames: int = 150,
        quantile: float = 0.75,
        attack_alpha: float = 0.05,
        decay_alpha: float = 0.12,
        warmup_frames: int = 15,
    ) -> None:
        self.min_floor_dbfs = _require_finite_number(min_floor_dbfs, 'min_floor_dbfs', -120.0, 0.0)
        self.max_floor_dbfs = _require_finite_number(max_floor_dbfs, 'max_floor_dbfs', -120.0, 0.0)
        if self.min_floor_dbfs >= self.max_floor_dbfs:
            raise ValueError('min_floor_dbfs must be lower than max_floor_dbfs')
        self.floor_dbfs = float(np.clip(
            _require_finite_number(initial_floor_dbfs, 'initial_floor_dbfs', -120.0, 0.0),
            self.min_floor_dbfs, self.max_floor_dbfs,
        ))
        if type(window_frames) is not int or not 10 <= window_frames <= 1000:
            raise ValueError('window_frames must be an integer between 10 and 1000')
        self.window_frames = window_frames
        self.quantile = _require_finite_number(quantile, 'quantile', 0.1, 0.95)
        self.attack_alpha = _require_finite_number(
            attack_alpha, 'attack_alpha', 0.0, 1.0, strict_lo=True)
        self.decay_alpha = _require_finite_number(
            decay_alpha, 'decay_alpha', 0.0, 1.0, strict_lo=True)
        if type(warmup_frames) is not int or warmup_frames < 0:
            raise ValueError('warmup_frames must be a nonnegative integer')
        self.warmup_frames = warmup_frames
        self._observed = 0
        self._window: deque[float] = deque(maxlen=window_frames)

    def observe(self, rms_dbfs: float, *, is_active_speech: bool = False) -> float:
        """Update ambient floor from a frame level and return the current estimate."""
        if is_active_speech or not math.isfinite(rms_dbfs):
            return self.floor_dbfs
        # Exact digital silence (e.g. muted hardware/AEC zero frame) is not room ambience.
        if rms_dbfs <= -115.0:
            return self.floor_dbfs
        clamped = min(self.max_floor_dbfs, max(self.min_floor_dbfs, rms_dbfs))
        self._window.append(clamped)
        self._observed += 1
        ordered = sorted(self._window)
        # During initial warmup use conservative median; afterwards track the
        # configured ambient quantile so spiky babble peaks stay below onset.
        q = 0.5 if self._observed <= self.warmup_frames else self.quantile
        target = ordered[int((len(ordered) - 1) * q)]
        if self._observed <= self.warmup_frames:
            self.floor_dbfs = target
            return self.floor_dbfs
        alpha = self.decay_alpha if target < self.floor_dbfs else self.attack_alpha
        self.floor_dbfs = (1.0 - alpha) * self.floor_dbfs + alpha * target
        return self.floor_dbfs


class NoiseAwareVad:
    """Gate a streaming neural VAD with high-passed RMS and adaptive SNR hysteresis.

    Always advances the wrapped VAD so recurrent state stays synchronized.
    Resetting between utterances clears speech hysteresis and Silero state while
    preserving the shared ambient noise-floor estimate.
    """

    def __init__(
        self,
        vad: Any,
        *,
        min_rms_dbfs: float = -96.0,
        snr_margin_db: float = 0.0,
        sustain_margin_db: float = 4.0,
        hangover_frames: int = 8,
        onset_confirm_frames: int = 2,
        high_pass_hz: float = 100.0,
        floor_tracker: NoiseFloorTracker | None = None,
    ) -> None:
        self.vad = vad
        self.min_rms_dbfs = _require_finite_number(min_rms_dbfs, 'min_rms_dbfs', -120.0, 0.0)
        self.snr_margin_db = _require_finite_number(snr_margin_db, 'snr_margin_db', 0.0, 40.0)
        self.sustain_margin_db = _require_finite_number(
            sustain_margin_db, 'sustain_margin_db', 0.0, 20.0)
        if type(hangover_frames) is not int or not 0 <= hangover_frames <= 100:
            raise ValueError('hangover_frames must be an integer between 0 and 100')
        if type(onset_confirm_frames) is not int or not 1 <= onset_confirm_frames <= 10:
            raise ValueError('onset_confirm_frames must be an integer between 1 and 10')
        self.hangover_frames = hangover_frames
        self.onset_confirm_frames = onset_confirm_frames
        self.high_pass_hz = _require_finite_number(high_pass_hz, 'high_pass_hz', 0.0, 500.0)
        self.floor_tracker = floor_tracker if floor_tracker is not None else NoiseFloorTracker()
        self._hpf = HighPassFilter(self.high_pass_hz) if self.high_pass_hz > 0.0 else None
        self._in_speech = False
        self._hangover_remaining = 0
        self._onset_streak = 0
        self.last_rms_dbfs = _SILENCE_DBFS
        self.last_threshold_dbfs = _SILENCE_DBFS

    @property
    def window_has_signal(self) -> bool:
        return bool(getattr(self.vad, 'window_has_signal', False))

    @property
    def probability(self) -> float:
        return float(getattr(self.vad, 'probability', 0.0))

    @property
    def threshold(self) -> float:
        return float(getattr(self.vad, 'threshold', 0.5))

    @threshold.setter
    def threshold(self, value: float) -> None:
        if hasattr(self.vad, 'threshold'):
            self.vad.threshold = value

    def new_stream(self) -> NoiseAwareVad:
        """Create an independent VAD stream sharing the room noise-floor tracker."""
        child_vad = self.vad.new_stream() if hasattr(self.vad, 'new_stream') else self.vad
        return NoiseAwareVad(
            child_vad,
            min_rms_dbfs=self.min_rms_dbfs,
            snr_margin_db=self.snr_margin_db,
            sustain_margin_db=self.sustain_margin_db,
            hangover_frames=self.hangover_frames,
            onset_confirm_frames=self.onset_confirm_frames,
            high_pass_hz=self.high_pass_hz,
            floor_tracker=self.floor_tracker,
        )

    def reset(self) -> None:
        """Reset utterance-local VAD and hysteresis state; keep ambient noise floor."""
        reset = getattr(self.vad, 'reset', None)
        if callable(reset):
            reset()
        if self._hpf is not None:
            self._hpf.reset()
        self._in_speech = False
        self._hangover_remaining = 0
        self._onset_streak = 0

    def _active_thresholds(self) -> tuple[float, float]:
        floor = self.floor_tracker.floor_dbfs
        if self.snr_margin_db > 0.0:
            onset = max(self.min_rms_dbfs, floor + self.snr_margin_db)
            sustain_snr = max(0.0, self.snr_margin_db - self.sustain_margin_db)
            sustain_min = (self.min_rms_dbfs - self.sustain_margin_db
                           if self.min_rms_dbfs > -96.0 else self.min_rms_dbfs)
            sustain = max(sustain_min, floor + sustain_snr)
        else:
            onset = self.min_rms_dbfs
            sustain = (self.min_rms_dbfs - self.sustain_margin_db
                       if self.min_rms_dbfs > -96.0 else self.min_rms_dbfs)
        return onset, sustain

    def is_speech(self, pcm: bytes, sample_rate: int) -> bool:
        """Require both neural voice probability and near-field acoustic energy."""
        if sample_rate != 16000 or not pcm or len(pcm) % 2:
            raise ValueError('noise-aware VAD requires nonempty mono PCM16 at 16 kHz')
        # Always advance Silero so its internal recurrent state tracks the stream.
        silero_speech = bool(self.vad.is_speech(pcm, sample_rate))
        measured_pcm = self._hpf.process_pcm(pcm) if self._hpf is not None else pcm
        rms_dbfs = frame_rms_dbfs(measured_pcm)
        self.last_rms_dbfs = rms_dbfs

        # Fast-path when both energy gates are disabled.
        if self.min_rms_dbfs <= -96.0 and self.snr_margin_db <= 0.0:
            self.last_threshold_dbfs = -96.0
            self.floor_tracker.observe(rms_dbfs, is_active_speech=silero_speech)
            return silero_speech

        onset_dbfs, sustain_dbfs = self._active_thresholds()
        if self._in_speech:
            self.last_threshold_dbfs = sustain_dbfs
            if silero_speech and rms_dbfs >= sustain_dbfs:
                self._hangover_remaining = self.hangover_frames
                return True
            if silero_speech and self._hangover_remaining > 0:
                self._hangover_remaining -= 1
                return True
            self._in_speech = False
            self._hangover_remaining = 0
            self._onset_streak = 0
            # Update ambient floor only when frame is truly quiet/non-speech.
            self.floor_tracker.observe(rms_dbfs, is_active_speech=False)
            return False

        self.last_threshold_dbfs = onset_dbfs
        if silero_speech and rms_dbfs >= onset_dbfs:
            self._onset_streak += 1
            if self._onset_streak >= self.onset_confirm_frames:
                self._in_speech = True
                self._hangover_remaining = self.hangover_frames
                self._onset_streak = 0
                return True
            return False

        self._onset_streak = 0
        self.floor_tracker.observe(rms_dbfs, is_active_speech=False)
        return False


def _default_webrtc_processor(ns_level: int) -> Any:
    from pywebrtc_audio import AudioProcessor

    return AudioProcessor(
        sample_rate=16000,
        echo_cancellation=False,
        noise_suppression=True,
        high_pass_filter=True,
        auto_gain_control=False,
        ns_level=ns_level,
    )


class PreprocessedRecorder:
    """Wrap a 16 kHz microphone with optional HPF and sample-aligned WebRTC NS.

    When ``ns_mode='vad_only'``, returns ``CapturePCM(asr_pcm, vad_pcm)`` where
    ``asr_pcm`` stays free of nonlinear NS artifacts (optionally high-passed)
    while ``vad_pcm`` is noise-suppressed and aligned to the exact same sample
    clock (fixed 10 ms / 160-sample framing delay).
    """

    def __init__(
        self,
        inner: Any,
        *,
        high_pass_hz: float = 0.0,
        ns_level: int = 2,
        ns_mode: str = 'off',
        processor_factory: Callable[[int], Any] | None = None,
    ) -> None:
        if ns_mode not in ('off', 'vad_only', 'vad_and_asr'):
            raise ValueError('ns_mode must be off, vad_only, or vad_and_asr')
        if type(ns_level) is not int or ns_level not in (0, 1, 2, 3):
            raise ValueError('ns_level must be an integer between 0 and 3')
        self.high_pass_hz = _require_finite_number(high_pass_hz, 'high_pass_hz', 0.0, 500.0)
        self.ns_level = ns_level
        self.ns_mode = ns_mode
        self.inner = inner
        self._hpf = HighPassFilter(self.high_pass_hz) if self.high_pass_hz > 0.0 else None
        self._processor = None
        if self.ns_mode != 'off':
            factory = _default_webrtc_processor if processor_factory is None else processor_factory
            self._processor = factory(self.ns_level)
        self._reset_buffers()

    def _reset_buffers(self) -> None:
        if self._hpf is not None:
            self._hpf.reset()
        self._ns_in = np.empty(0, dtype=np.float32)
        # Fixed 160-sample (10 ms) alignment delay keeps dry and NS streams in lockstep.
        self._ns_out = np.zeros(_WEBRTC_FRAME_SAMPLES, dtype=np.float32)
        self._dry_delay = np.zeros(_WEBRTC_FRAME_SAMPLES, dtype=np.float32)

    @property
    def sample_rate(self) -> int:
        return int(self.inner.sample_rate)

    @property
    def selected_device(self) -> str:
        return str(getattr(self.inner, 'selected_device', ''))

    def start(self) -> None:
        self._reset_buffers()
        self.inner.start()

    def cancel_read(self) -> None:
        cancel = getattr(self.inner, 'cancel_read', None)
        if callable(cancel):
            cancel()

    def stop(self) -> None:
        self.inner.stop()

    def delete(self) -> None:
        self.inner.delete()

    def read(self) -> CapturePCM:
        """Read one chunk from the inner recorder and emit aligned CapturePCM."""
        raw = self.inner.read()
        if isinstance(raw, CapturePCM):
            raw_samples = np.frombuffer(bytes(raw), dtype='<i2').astype(np.float32) / _FULL_SCALE
        else:
            raw_samples = np.asarray(raw, dtype=np.float32) / _FULL_SCALE
        if raw_samples.ndim != 1 or not np.isfinite(raw_samples).all():
            raise ValueError('recorder returned invalid PCM samples')
        if raw_samples.size == 0:
            return CapturePCM(b'', b'')

        dry = self._hpf.process_samples(raw_samples) if self._hpf is not None else raw_samples
        if self._processor is None:
            pcm = _float_to_pcm16(dry)
            return CapturePCM(pcm, pcm)

        count = raw_samples.size
        # Feed raw (or HPF) audio to WebRTC NS in exact 160-sample (10 ms) blocks.
        self._ns_in = np.concatenate((self._ns_in, raw_samples))
        produced: list[np.ndarray] = []
        while self._ns_in.size >= _WEBRTC_FRAME_SAMPLES:
            block = self._ns_in[:_WEBRTC_FRAME_SAMPLES].copy()
            self._ns_in = self._ns_in[_WEBRTC_FRAME_SAMPLES:]
            cleaned = np.asarray(self._processor.process(block), dtype=np.float32).reshape(-1)
            if cleaned.size != _WEBRTC_FRAME_SAMPLES or not np.isfinite(cleaned).all():
                raise RuntimeError('noise suppressor returned an invalid 10 ms frame')
            produced.append(cleaned)
        if produced:
            self._ns_out = np.concatenate((self._ns_out, *produced))
        vad_samples = self._ns_out[:count]
        self._ns_out = self._ns_out[count:]
        if vad_samples.size != count:
            raise RuntimeError('noise suppressor clock discontinuity')

        aligned_dry = np.concatenate((self._dry_delay, dry))
        asr_samples = aligned_dry[:count]
        self._dry_delay = aligned_dry[count:]

        vad_pcm = _float_to_pcm16(vad_samples)
        asr_pcm = vad_pcm if self.ns_mode == 'vad_and_asr' else _float_to_pcm16(asr_samples)
        return CapturePCM(asr_pcm, vad_pcm)


def recommend_venue_settings(
    ambient_frames_dbfs: Sequence[float],
    speech_frames_dbfs: Sequence[float],
) -> dict[str, Any]:
    """Compute recommended noisy-venue STT parameters and hardware verdict.

    ``ambient_frames_dbfs`` should contain 20 ms high-passed frame RMS values
    recorded during venue silence; ``speech_frames_dbfs`` should contain frame
    RMS values recorded while the presenter speaks at demo distance.
    """
    ambient = np.asarray([v for v in ambient_frames_dbfs if math.isfinite(v) and v > -115.0],
                         dtype=np.float64)
    speech = np.asarray([v for v in speech_frames_dbfs if math.isfinite(v) and v > -115.0],
                        dtype=np.float64)
    if ambient.size < 10:
        raise ValueError('need at least 10 valid ambient frames to calibrate')
    if speech.size < 10:
        raise ValueError('need at least 10 valid speech frames to calibrate')

    amb_p50 = float(np.percentile(ambient, 50))
    amb_p75 = float(np.percentile(ambient, 75))
    amb_p90 = float(np.percentile(ambient, 90))
    amb_p95 = float(np.percentile(ambient, 95))
    # Calibration speech clips naturally include 50-75% inter-word pauses; use
    # p85/p90/p95 to measure active spoken syllables rather than pause frames.
    sp_p50 = float(np.percentile(speech, 50))
    sp_p75 = float(np.percentile(speech, 75))
    sp_p85 = float(np.percentile(speech, 85))
    sp_p90 = float(np.percentile(speech, 90))
    sp_p95 = float(np.percentile(speech, 95))

    snr_vs_p90 = sp_p90 - amb_p90
    snr_vs_p50 = sp_p90 - amb_p50
    # NoiseFloorTracker tracks ambient p75; measure headroom above ambient p75 -> p95.
    amb_tail_db = max(0.0, amb_p95 - amb_p75)

    if snr_vs_p90 >= 12.0:
        verdict = 'ok'
        verdict_detail = (
            'SNR is sufficient for the current microphone with noisy-venue STT guard.'
        )
        vad_threshold = 0.65
        ns_level = 2
        # Place onset above ambient p95 so babble spikes cannot stall endpoints.
        min_rms = round(min(sp_p90 - 5.0, amb_p95 + min(4.0, max(2.0, snr_vs_p90 * 0.25))), 1)
        snr_margin = round(min(12.0, max(6.0, amb_tail_db + min(4.0, snr_vs_p90 * 0.25))), 1)
    elif snr_vs_p90 >= 6.0:
        verdict = 'caution'
        verdict_detail = (
            'Marginal SNR: speak within 30 cm of the microphone or switch to a '
            'close-talk / wireless lavalier microphone for reliable demos.'
        )
        vad_threshold = 0.7
        ns_level = 3
        min_rms = round(min(sp_p90 - 3.5, amb_p95 + max(1.5, snr_vs_p90 * 0.2)), 1)
        snr_margin = round(min(10.0, max(5.0, amb_tail_db + max(1.5, snr_vs_p90 * 0.2))), 1)
    else:
        verdict = 'critical'
        verdict_detail = (
            'Ambient venue noise is nearly as loud as presenter speech at this '
            'microphone. Software gating alone cannot separate overlapping voices; '
            'use a close-talk or wireless lavalier microphone.'
        )
        vad_threshold = 0.75
        ns_level = 3
        min_rms = round(max(amb_p90 + 1.5, sp_p95 - 6.0), 1)
        snr_margin = round(max(4.0, amb_tail_db + 1.5), 1)

    min_rms = float(np.clip(min_rms, -60.0, -15.0))
    snr_margin = float(np.clip(snr_margin, 4.0, 15.0))
    sustain_margin = round(min(4.0, max(2.0, snr_margin * 0.45)), 1)

    parameters = {
        'followup_timeout_s': 0.0,
        'barge_in_mode': 'legacy',
        'short_speech_guard': True,
        'min_speech_s': 0.22,
        'wake_min_speech_s': 0.18,
        'max_utterance_s': 12.0,
        'endpoint_min_silence_s': 0.6,
        'silence_timeout_s': 1.5,
        'high_pass_hz': 100.0,
        'noise_suppression_mode': 'vad_only',
        'noise_suppression_level': ns_level,
        'vad_threshold': vad_threshold,
        'vad_min_rms_dbfs': min_rms,
        'vad_snr_margin_db': snr_margin,
        'vad_sustain_margin_db': sustain_margin,
        'vad_hangover_ms': 160,
    }
    return {
        'verdict': verdict,
        'verdict_detail': verdict_detail,
        'metrics': {
            'ambient_p50_dbfs': round(amb_p50, 2),
            'ambient_p90_dbfs': round(amb_p90, 2),
            'ambient_p95_dbfs': round(amb_p95, 2),
            'speech_p50_dbfs': round(sp_p50, 2),
            'speech_p75_dbfs': round(sp_p75, 2),
            'speech_p85_dbfs': round(sp_p85, 2),
            'speech_p90_dbfs': round(sp_p90, 2),
            'speech_p95_dbfs': round(sp_p95, 2),
            'snr_vs_ambient_p90_db': round(snr_vs_p90, 2),
            'snr_vs_ambient_p50_db': round(snr_vs_p50, 2),
        },
        'ros_parameters': parameters,
    }
