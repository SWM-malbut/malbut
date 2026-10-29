"""Preserve unfiltered speech after the playback reference has drained."""
import numpy as np


class QuietAsrRecovery:
    def __init__(self):
        self.raw_delay = np.zeros(512, np.float32)
        self.quiet_samples = 0
        self.blend = 0.0

    def process(self, spatial, raw, reference):
        output = np.empty_like(spatial)
        for start in range(0, len(spatial), 128):
            end = start + 128
            dry = self.raw_delay[:128].copy() * 32
            self.raw_delay[:-128] = self.raw_delay[128:]
            self.raw_delay[-128:] = raw[start:end]
            far = reference[start:end]
            if float(far @ far) / 128 > 1e-8:
                self.quiet_samples = 0
                self.blend = 0.0
            else:
                self.quiet_samples += 128
            target = min(1.0, max(0, self.quiet_samples - 4000) / 512)
            if self.blend == target == 0.0:
                output[start:end] = spatial[start:end]
            elif self.blend == target == 1.0:
                output[start:end] = dry
            else:
                blend = np.linspace(self.blend, target, 128, endpoint=False)
                output[start:end] = spatial[start:end] * (1 - blend) + dry * blend
            self.blend = target
        return output.clip(-.98, .98)
