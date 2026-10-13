"""Optional CPU runner for Silero v6 raw frame probabilities (no segment gate)."""

import math
import hashlib
from pathlib import Path


MODEL_SHA256 = '1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3'


def load_pinned_model(path):
    """Load only the locally provisioned model used by observation/evaluation."""
    path = Path(path).expanduser()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != MODEL_SHA256:
        raise ValueError('Silero model checksum mismatch')
    return SileroVad(path)


class SileroVad:
    """Follow v6.2.3 OnnxWrapper's 512 + 64 sample recurrent protocol at 16kHz.

    Upstream revision: 5cd7945676eb32225748052e2e6a0580e4686a08.
    Imports are lazy so ordinary STT needs no ONNX runtime.
    """

    def __init__(self, path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(path), sess_options=options, providers=['CPUExecutionProvider'])
        if {item.name for item in self.session.get_inputs()} != {'input', 'state', 'sr'}:
            raise ValueError('expected a Silero recurrent ONNX model with input/state/sr')
        self.reset()

    def reset(self):
        import numpy as np

        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def probability(self, pcm):
        import numpy as np

        if len(pcm) != 1024:
            raise ValueError('Silero requires 512 PCM16 samples at 16kHz')
        block = np.frombuffer(pcm, dtype='<i2').astype(np.float32).reshape(1, 512) / 32768
        inputs = np.concatenate((self.context, block), axis=1)
        result, state = self.session.run(None, dict(
            input=inputs, state=self.state, sr=np.array(16000, dtype=np.int64)))
        probability = float(result.reshape(-1)[0])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('Silero returned an invalid probability')
        self.state, self.context = state, inputs[:, -64:]
        return probability
