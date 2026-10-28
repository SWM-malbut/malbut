"""Calibrated AEC3 plus a small residual echo model, with a quiet-path bypass."""

import math
from pathlib import Path

import numpy as np
from pywebrtc_audio import AudioProcessor

from .neural import DTLN

RATE = 16000
HOP = 128
REFERENCE_LEAD_S = 0.04  # Leave a short causal tail for AEC3 to learn.


def calibrate(microphone, reference, anchor_difference=0.0):
    mic = np.asarray(microphone, dtype=np.float32)
    ref = np.asarray(reference, dtype=np.float32)
    if mic.ndim != 1 or ref.ndim != 1 or min(len(mic), len(ref)) < RATE * 2:
        raise ValueError('Calibration requires at least two seconds of mono audio')
    if not np.isfinite(mic).all() or not np.isfinite(ref).all():
        raise ValueError('Invalid calibration PCM')
    if not math.isfinite(anchor_difference):
        raise ValueError('Invalid capture clocks')
    size = 1 << int(np.ceil(np.log2(len(mic) + len(ref))))
    correlation = np.fft.irfft(
        np.fft.rfft(mic, size) * np.conj(np.fft.rfft(ref, size)), size)
    lag = 1600 + int(np.argmax(abs(correlation[1600:16000])))
    confidence = float(abs(correlation[lag]) / np.sqrt(
        np.sum(mic * mic) * np.sum(ref * ref) + 1e-12))
    delay = lag / RATE + anchor_difference - REFERENCE_LEAD_S
    if confidence <= 0.1 or not 0.05 < delay < 0.9:
        raise ValueError('Echo reference calibration is unreliable '
                         f'(confidence={confidence:.3f}, delay_s={delay:.3f})')
    gain = float(np.clip(np.sqrt(np.mean(mic * mic) / (
        np.mean(ref * ref) + 1e-12)), 1, 8))
    return dict(lag_samples=lag, reference_delay_s=delay, confidence=confidence,
                microphone_gain=gain, reference_lead_s=REFERENCE_LEAD_S)


class EchoProcessor:
    """Own one stream's adaptive filters; never share between microphones."""

    def __init__(self, gain, neural_models):
        if not math.isfinite(gain) or not 1 <= gain <= 8:
            raise ValueError('Invalid microphone normalization')
        self.gain = gain
        self.aec = AudioProcessor(
            sample_rate=RATE, echo_cancellation=True, noise_suppression=True,
            high_pass_filter=True, auto_gain_control=False, ns_level=2)
        self.neural = DTLN(Path(neural_models))
        self.mic = np.empty(0, np.float32)
        self.ref = np.empty(0, np.float32)
        self.output = np.zeros(HOP, np.float32)
        # AEC3 reframing adds 8 ms; the neural overlap adds another 24 ms.
        self.dry = np.zeros(512, np.float32)
        self.far_delay = np.zeros(HOP, np.float32)
        self.envelope = 0.0
        self.reference_hold = 0
        self.active = False
        self.asr_output = np.empty(0, np.float32)
        self.last_voice = np.zeros(HOP, np.float32)
        self.voice_delay = np.zeros(384, np.float32)

    def _step(self, near, far):
        dry = self.dry[:HOP].copy()
        self.dry[:-HOP] = self.dry[HOP:]
        self.dry[-HOP:] = near
        self.envelope = max(float(dry @ dry) / HOP, self.envelope * 0.92)
        self.reference_hold = (125 if float(far @ far) / HOP > 1e-8
                               else max(0, self.reference_hold - 1))
        if not self.reference_hold:
            if self.active:
                self.mic = np.empty(0, np.float32)
                self.ref = np.empty(0, np.float32)
                self.output = np.zeros(HOP, np.float32)
            self.active = False
            self.far_delay = far.copy()
            self.last_voice = dry.copy()
            return dry
        self.mic = np.concatenate([self.mic, near])
        self.ref = np.concatenate([self.ref, far])
        while len(self.mic) >= 160:
            clean = self.aec.process(
                (self.mic[:160] / self.gain).astype(np.float32), self.ref[:160].copy())
            self.output = np.concatenate([self.output, clean * self.gain])
            self.mic, self.ref = self.mic[160:], self.ref[160:]
        out, self.output = self.output[:HOP].copy(), self.output[HOP:]
        if len(out) != HOP:
            raise RuntimeError('AEC output clock discontinuity')
        if not self.active:
            self.neural.reset()
            self.voice_delay.fill(0)
        voice = self.voice_delay[:HOP].copy()
        self.voice_delay[:-HOP] = self.voice_delay[HOP:]
        self.voice_delay[-HOP:] = out
        out = self.neural.process(out, self.far_delay)
        self.last_voice = voice.copy()
        self.active = True
        self.far_delay = far.copy()
        # Suppress only residuals at least 20 dB below the recent input.
        if float(out @ out) / HOP < self.envelope * 0.0630957344:
            voice.fill(0)
        return voice

    def process_array(self, near, far):
        near, far = (np.asarray(x, dtype=np.float32) for x in (near, far))
        if (near.ndim != 1 or near.shape != far.shape or len(near) % HOP
                or not np.isfinite(near).all() or not np.isfinite(far).all()):
            raise ValueError('AEC requires matching finite frames in 128-sample hops')
        out = np.empty_like(near)
        self.asr_output = np.empty_like(near)
        for start in range(0, len(near), HOP):
            out[start:start + HOP] = self._step(near[start:start + HOP], far[start:start + HOP])
            self.asr_output[start:start + HOP] = self.last_voice
        if not np.isfinite(out).all() or not np.isfinite(self.asr_output).all():
            raise RuntimeError('AEC produced invalid PCM')
        return out

    def process(self, near, far):
        if len(near) != HOP or len(far) != HOP:
            raise ValueError('AEC requires 128-sample frames')
        return self.process_array(near, far)
