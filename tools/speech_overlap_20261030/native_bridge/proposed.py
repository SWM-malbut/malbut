"""Causal, fixed-weight six-microphone filter with 24 ms overlap delay."""
import numpy as np

class SpatialFilter:
    def __init__(self, weights):
        if weights.shape != (257, 6) or not np.isfinite(weights).all():
            raise ValueError('Expected finite 257 by 6 spatial weights')
        self.weights = weights.conj()
        self.window = np.sqrt(np.hanning(512))
        self.history = np.zeros((512, 6))
        self.ola = np.zeros(512)
        self.denominator = np.zeros(512)

    def process(self, samples):
        if samples.shape != (128, 6):
            raise ValueError('Expected 128 frames from six microphones')
        self.history[:-128] = self.history[128:]
        self.history[-128:] = samples
        spectrum = np.fft.rfft(self.history * self.window[:, None], axis=0)
        self.ola += np.fft.irfft(np.sum(self.weights * spectrum, axis=1), n=512) * self.window
        self.denominator += self.window ** 2
        output = self.ola[:128] / np.maximum(self.denominator[:128], 1e-8)
        self.ola[:-128] = self.ola[128:]
        self.ola[-128:] = 0
        self.denominator[:-128] = self.denominator[128:]
        self.denominator[-128:] = 0
        return output.astype(np.float32)

    def process_block(self, samples):
        if samples.shape != (512, 6):
            raise ValueError('Expected 512 frames from six microphones')
        combined = np.concatenate((self.history[128:], samples))
        windows = np.lib.stride_tricks.sliding_window_view(combined, 512, axis=0)[::128].transpose(0, 2, 1)
        spectrum = np.fft.rfft(windows * self.window[None, :, None], axis=1)
        filtered = np.fft.irfft(np.sum(self.weights[None] * spectrum, axis=2), n=512, axis=1) * self.window[None]
        self.history[:] = samples
        output = np.empty(512, dtype=np.float32)
        for index in range(4):
            self.ola += filtered[index]
            self.denominator += self.window ** 2
            output[index*128:(index+1)*128] = self.ola[:128] / np.maximum(self.denominator[:128], 1e-8)
            self.ola[:-128] = self.ola[128:]
            self.ola[-128:] = 0
            self.denominator[:-128] = self.denominator[128:]
            self.denominator[-128:] = 0
        return output
