"""Silero-only capture VAD; local pinned weights, no model downloads."""

import math
import os
from pathlib import Path

from malbut_stt.silero_vad import load_pinned_model


def default_model_path():
    configured = os.environ.get('MALBUT_STT_SILERO_GATE_MODEL', '').strip()
    if configured:
        return Path(configured).expanduser()
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return cache / 'malbut_speech/models/silero-v6.2.3.onnx'


class SileroStreamVad:
    """Adapt 512-sample inference to the collector's 320-sample clock.

    Each decision uses only completed input windows. Between 32 ms windows,
    retain the last probability. The collector owns onset and silence timing;
    this adapter adds neither onset confirmation nor a 400 ms silence hold.
    One instance belongs to one capture stream, separate from ASR admission.
    """

    def __init__(self, model_path=None, *, model=None):
        self.model = model if model is not None else load_pinned_model(
            default_model_path() if model_path is None else model_path)
        self.reset()

    def reset(self):
        self.model.reset()
        self.pending = bytearray()
        self.probability = 0.0
        self.quiet_samples = 0

    def is_speech(self, pcm, sample_rate):
        if sample_rate != 16000 or not pcm or len(pcm) % 2:
            raise ValueError('Silero capture requires nonempty mono PCM16 at 16 kHz')
        self.pending.extend(pcm)
        while len(self.pending) >= 1024:
            frame = bytes(self.pending[:1024])
            del self.pending[:1024]
            probability = self.model.probability(frame)
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError('Invalid Silero probability')
            self.probability = probability
            if probability >= .5:
                self.quiet_samples = 0
            else:
                self.quiet_samples += 512
                if self.quiet_samples >= 8000:
                    self.model.reset()
                    self.quiet_samples = 0
        return self.probability >= .5
