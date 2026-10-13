"""Opt-in Silero admission immediately before each actual Whisper model call.

One ASR worker serializes wake, incremental and final decodes. Each input is
checked independently, including cropped suffixes and recovery decodes. The
observer queue is never consulted and accepted audio is passed on unchanged.
"""

import hashlib
from time import monotonic

from malbut_stt.silero_vad import MODEL_SHA256, load_pinned_model


ENVIRONMENT_KEY = 'MALBUT_STT_SILERO_GATE_MODEL'


class SileroNonSpeech(RuntimeError):
    """This input did not contain enough consecutive Silero-positive audio."""


class SileroGateError(RuntimeError):
    """Admission could not be checked; do not bypass the gate."""


class SileroGateCancelled(RuntimeError):
    """The owner cancelled or closed the model while checking admission."""


class SileroGate:
    """Use the fixed, previously evaluated 0.5 / 80 ms clip admission rule."""

    def __init__(self, model_path):
        self.model = load_pinned_model(model_path)

    @property
    def metadata(self):
        return dict(model='silero-v6.2.3', model_sha256=MODEL_SHA256,
                    threshold=.5, min_speech_ms=80, sample_rate=16000,
                    frame_samples=512, mode='block_before_whisper')

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
            run = run + len(block) // 2 if probability >= .5 else 0
            longest = max(longest, run)
            peak = max(peak, probability)
            # Padding never counts as speech. Stop once admission is decided.
            if longest >= 1280:
                break
        if cancelled():
            raise SileroGateCancelled('silero_gate_cancelled')
        allowed = longest >= 1280
        return dict(self.metadata, allowed=allowed,
                    reason='speech_detected' if allowed else 'silero_non_speech',
                    sample_count=len(pcm) // 2, scanned_samples=scanned,
                    pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                    peak_probability=peak, longest_speech_s=longest / 16000,
                    elapsed_s=monotonic() - started)

    def wrap_model(self, model, diagnostics=None):
        return _GatedModel(model, self, diagnostics)


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
            decision = self._gate.check(pcm.tobytes(), cancelled=cancelled)
        except SileroGateCancelled:
            self._event('silero_gate_cancelled', reason='cancelled_before_whisper')
            raise
        except Exception as error:
            self._event('silero_gate_error', error=type(error).__name__, allowed=False)
            raise SileroGateError('silero_gate_unavailable') from error
        self._event('silero_gate', **decision)
        if cancelled():
            raise SileroGateCancelled('silero_gate_cancelled')
        if not decision['allowed']:
            raise SileroNonSpeech('silero_non_speech')
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


def from_environment(environ):
    """An explicit invalid setup fails startup instead of silently disabling."""
    path = environ.get(ENVIRONMENT_KEY, '').strip()
    return SileroGate(path) if path else None
