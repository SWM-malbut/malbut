"""Skip repeated inference on digital silence created by echo suppression."""


class SuppressedSilenceVad:
    def __init__(self, vad):
        self.vad = vad
        self.quiet_samples = 0

    def reset(self):
        """Preserve the collector's reset contract between dialogue captures."""
        self.quiet_samples = 0
        self.vad.reset()

    def is_speech(self, pcm, sample_rate):
        if sample_rate != 16000 or not pcm or len(pcm) % 2:
            raise ValueError('AEC VAD requires nonempty PCM16 at 16 kHz')
        if any(pcm):
            self.quiet_samples = 0
            return self.vad.is_speech(pcm, sample_rate)
        previous = self.quiet_samples
        self.quiet_samples = min(8000, previous + len(pcm) // 2)
        if self.quiet_samples >= 8000:
            if previous < 8000:
                self.vad.reset()
        else:
            self.vad.is_speech(pcm, sample_rate)
        # A recurrent VAD probability can linger after the echo has vanished.
        return False
