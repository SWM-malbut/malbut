"""Fixed placement residual spectrum gate for VAD only; ASR stays continuous."""
import numpy as np

class SpatialEchoGate:
    def __init__(self, leakage):
        self.leakage = np.asarray(leakage)
        self.window = np.sqrt(np.hanning(512))
        self.history = np.zeros((512, 2))
        self.power = np.zeros(257)
        self.noise = np.zeros(257)
        self.ola = np.zeros(512)
        self.denominator = np.zeros(512)

    def process(self, near, raw):
        self.history[:-128] = self.history[128:]
        self.history[-128:] = np.column_stack((near, raw))
        spectrum = np.fft.rfft(self.history * self.window[:, None], axis=0)
        self.power = .8 * self.power + .2 * abs(spectrum[:, 0]) ** 2
        self.noise = .8 * self.noise + .2 * abs(spectrum[:, 1]) ** 2 * self.leakage
        gain = np.clip(1 - 2 * self.noise / (self.power + 1e-12), 0, 1)
        frame = np.fft.irfft(spectrum[:, 0] * gain, n=512)
        original = self.history[:, 0] * self.window
        admitted = frame @ frame >= .5 * (original @ original)
        if not admitted:
            frame.fill(0)
        self.ola += frame * self.window
        self.denominator += self.window ** 2
        output = self.ola[:128] / np.maximum(self.denominator[:128], 1e-8)
        if not admitted:
            output.fill(0)
        self.ola[:-128] = self.ola[128:]
        self.ola[-128:] = 0
        self.denominator[:-128] = self.denominator[128:]
        self.denominator[-128:] = 0
        return output.astype('f4')
