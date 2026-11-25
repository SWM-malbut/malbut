"""Installed Silero admission immediately before each actual Whisper model call.

Only the command transcriber's model is wrapped; wake recognition keeps its
original model. A request attached to a wake phrase is checked before publication.
Each command input is checked independently, including cropped
suffixes and recovery decodes. The observer queue is never consulted and
accepted audio is passed on unchanged.
"""

import hashlib
import math
from pathlib import Path
from time import monotonic

from malbut_stt.noise_guard import frame_rms_dbfs
from malbut_stt.silero_vad import MODEL_SHA256, load_pinned_model


ENVIRONMENT_KEY = 'MALBUT_STT_SILERO_GATE_MODEL'


class SileroNonSpeech(RuntimeError):
    """This input did not contain enough consecutive Silero-positive audio."""


class SileroGateError(RuntimeError):
    """Admission could not be checked; do not bypass the gate."""


class SileroGateCancelled(RuntimeError):
    """The owner cancelled or closed the model while checking admission."""


class SileroGate:
    """Block non-speech before Whisper using Silero probability and optional RMS floor."""

    def __init__(self, model_path, *, threshold=0.5, min_speech_ms=80, min_rms_dbfs=None):
        if (type(threshold) not in (int, float) or not math.isfinite(threshold)
                or not 0.0 < threshold < 1.0):
            raise ValueError('Silero gate threshold must be strictly between 0 and 1')
        if type(min_speech_ms) is not int or not 20 <= min_speech_ms <= 2000:
            raise ValueError('Silero gate min_speech_ms must be an integer between 20 and 2000')
        if min_rms_dbfs is not None and (
                type(min_rms_dbfs) not in (int, float) or not math.isfinite(min_rms_dbfs)
                or not -120.0 <= min_rms_dbfs <= 0.0):
            raise ValueError('Silero gate min_rms_dbfs must be finite between -120 and 0')
        self.threshold = float(threshold)
        self.min_speech_ms = min_speech_ms
        self.min_speech_samples = round(min_speech_ms * 16)
        self.min_rms_dbfs = None if min_rms_dbfs is None else float(min_rms_dbfs)
        self.model = load_pinned_model(model_path)

    @property
    def metadata(self):
        info = dict(model='silero-v6.2.3', model_sha256=MODEL_SHA256,
                    threshold=self.threshold, min_speech_ms=self.min_speech_ms,
                    sample_rate=16000, frame_samples=512, mode='block_before_whisper')
        if self.min_rms_dbfs is not None:
            info['min_rms_dbfs'] = self.min_rms_dbfs
        return info

    def check(self, pcm, *, cancelled=lambda: False):
        if len(pcm) % 2:
            raise ValueError('Silero gate requires whole PCM16 samples')
        started = monotonic()
        self.model.reset()
        longest = run = scanned = 0
        peak = 0.0
        for offset in range(0, len(pcm), 1024):
            if cancelled():
                raise SileroGateCancelled('silero_gate_cancelled')
            block = pcm[offset:offset + 1024]
            probability = self.model.probability(block.ljust(1024, b'\0'))
            scanned += len(block) // 2
            voiced = probability >= self.threshold
            if voiced and self.min_rms_dbfs is not None and block:
                voiced = frame_rms_dbfs(block) >= self.min_rms_dbfs
            run = run + len(block) // 2 if voiced else 0
            longest = max(longest, run)
            peak = max(peak, probability)
            # Padding never counts as speech. Stop once admission is decided.
            if longest >= self.min_speech_samples:
                break
        if cancelled():
            raise SileroGateCancelled('silero_gate_cancelled')
        allowed = longest >= self.min_speech_samples
        return dict(self.metadata, allowed=allowed,
                    reason='speech_detected' if allowed else 'silero_non_speech',
                    sample_count=len(pcm) // 2, scanned_samples=scanned,
                    pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                    peak_probability=peak, longest_speech_s=longest / 16000,
                    elapsed_s=monotonic() - started)

    def wrap_model(self, model, diagnostics=None):
        return _GatedModel(model, self, diagnostics)

    def require_speech(self, pcm, diagnostics=None, *, cancelled=lambda: False):
        """Validate PCM even when reusing an already decoded wake-and-request clip."""
        def event(name, **fields):
            if diagnostics is not None:
                diagnostics.event(name, **fields)

        try:
            decision = self.check(pcm, cancelled=cancelled)
        except SileroGateCancelled:
            event('silero_gate_cancelled', reason='cancelled_before_whisper')
            raise
        except Exception as error:
            event('silero_gate_error', error=type(error).__name__, allowed=False)
            raise SileroGateError('silero_gate_unavailable') from error
        event('silero_gate', **decision)
        if cancelled():
            raise SileroGateCancelled('silero_gate_cancelled')
        if not decision['allowed']:
            raise SileroNonSpeech('silero_non_speech')


class _GatedModel:
    def __init__(self, model, gate, diagnostics):
        self._model, self._gate, self._diagnostics = model, gate, diagnostics
        self._cancel_epoch = 0
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._model, name)

    def _event(self, name, **fields):
        if self._diagnostics is not None:
            self._diagnostics.event(name, **fields)

    def transcribe(self, audio, **options):
        import numpy as np

        epoch = self._cancel_epoch

        def cancelled():
            return self._closed or epoch != self._cancel_epoch

        try:
            samples = np.asarray(audio)
            if samples.ndim != 1 or not np.isfinite(samples).all():
                raise ValueError('Silero gate requires finite mono audio')
            pcm = np.rint(np.clip(samples * 32768, -32768, 32767)).astype('<i2')
            if not np.array_equal(samples, pcm.astype(np.float32) / 32768):
                raise ValueError('Silero gate input must be lossless PCM16')
        except Exception as error:
            self._event('silero_gate_error', error=type(error).__name__, allowed=False)
            raise SileroGateError('silero_gate_unavailable') from error
        self._gate.require_speech(pcm.tobytes(), self._diagnostics, cancelled=cancelled)
        return self._model.transcribe(audio, **options)

    def cancel(self):
        self._cancel_epoch += 1
        cancel = getattr(self._model, 'cancel', None)
        if callable(cancel):
            cancel()

    def close(self):
        self._closed = True
        self.cancel()
        close = getattr(self._model, 'close', None)
        if callable(close):
            close()


def from_environment(environ, *, threshold=0.5, min_speech_ms=80, min_rms_dbfs=None):
    """Use the installed model by default; an explicit empty override disables it."""
    def _create(path):
        if threshold == 0.5 and min_speech_ms == 80 and min_rms_dbfs is None:
            return SileroGate(path)
        return SileroGate(path, threshold=threshold, min_speech_ms=min_speech_ms,
                          min_rms_dbfs=min_rms_dbfs)

    if ENVIRONMENT_KEY in environ:
        path = environ[ENVIRONMENT_KEY].strip()
        return _create(path) if path else None
    cache = Path(environ.get('XDG_CACHE_HOME') or Path.home() / '.cache').expanduser()
    path = cache / 'malbut_speech/models/silero-v6.2.3.onnx'
    # Installation opts this robot in. Missing weights never trigger a download;
    # present but invalid weights/runtime must fail instead of bypassing the gate.
    return _create(str(path)) if path.is_file() else None
