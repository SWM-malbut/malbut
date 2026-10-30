"""Optional local turn guards; no downloads, remote inference, or microphone I/O.

Production capture uses SileroStreamVad. The optional reachy_start mode adds
Reachy's native-window onset qualifier, without Smart Turn or an endpoint hold.
Smart Turn may veto a lexical early endpoint; it never extends the fallback.
"""

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from time import perf_counter

from malbut_stt.silero_vad import load_pinned_model


SMART_TURN_SHA256 = '2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f'
SMART_TURN_FILENAME = 'smart-turn-v3.2-cpu.onnx'


class ReachyStartFilter:
    """Continuous 512-sample inference, consumed on the existing 20 ms clock.

    The pinned Malbut Silero weights produced identical probabilities to the
    Reachy weights in the local 2449-frame comparison. State reset and onset
    rules follow Reachy's local_vad.py (b731ea1). The capture clock may observe
    confirmation up to one 20 ms frame later than the native 32 ms clock.
    """

    def __init__(self, model_path=None, *, model=None):
        self.model = model if model is not None else load_pinned_model(model_path)
        self.reset()

    def reset(self):
        self.model.reset()
        self.pending = bytearray()
        self.confirmed = False
        self.speech_samples = self.quiet_samples = self.idle_samples = 0

    def __call__(self, pcm, sample_rate):
        if sample_rate != 16000 or len(pcm) != 640:
            raise ValueError('Reachy start filter requires 20 ms mono PCM16 at 16 kHz')
        self.pending.extend(pcm)
        while len(self.pending) >= 1024:
            frame = bytes(self.pending[:1024])
            del self.pending[:1024]
            probability = self.model.probability(frame)
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError('Invalid Silero probability')
            if probability >= .5:
                self.idle_samples = self.quiet_samples = 0
                self.speech_samples += 512
                if self.speech_samples >= 2560:
                    self.confirmed = True
            elif self.confirmed:
                self.quiet_samples += 512
                if self.quiet_samples >= 6400:
                    self.confirmed = False
                    self.quiet_samples = self.speech_samples = self.idle_samples = 0
                    self.model.reset()
            else:
                self.speech_samples = 0
                self.idle_samples += 512
                if self.idle_samples >= 8000:
                    self.idle_samples = 0
                    self.model.reset()
        return self.confirmed


@dataclass(frozen=True)
class TurnPrediction:
    complete: bool
    probability: float
    inference_ms: float


class SmartTurnGuard:
    """Pinned v3.2 CPU inference on the latest eight seconds of observed PCM.

    Feature settings match the tested Hugging Face speech-to-speech analyzer:
    left zero padding, Whisper 8 s features, do_normalize=True, threshold >0.5.
    """

    def __init__(self, model_path):
        path = Path(model_path).expanduser()
        if hashlib.sha256(path.read_bytes()).hexdigest() != SMART_TURN_SHA256:
            raise ValueError('Smart Turn model checksum mismatch')
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(path), sess_options=options,
                                           providers=['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name
        self.features = WhisperFeatureExtractor(chunk_length=8)
        self.check(bytes(32000))  # Warm kernels before capture starts.

    def check(self, pcm):
        if not pcm or len(pcm) % 2:
            raise ValueError('Smart Turn requires nonempty mono PCM16')
        import numpy as np

        started = perf_counter()
        audio = np.frombuffer(pcm[-256000:], dtype='<i2').astype(np.float32) / 32768
        audio = np.pad(audio, (128000-len(audio), 0))
        features = self.features(audio, sampling_rate=16000, return_tensors='np',
                                 padding='max_length', max_length=128000,
                                 truncation=True, do_normalize=True)
        outputs = self.session.run(None, {
            self.input_name: np.asarray(features.input_features, dtype=np.float32)})
        probability = float(np.asarray(outputs[0]).reshape(-1)[0])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('Invalid Smart Turn probability')
        return TurnPrediction(probability > .5, probability,
                              (perf_counter()-started)*1000)


def load_guards(mode, model_directory):
    if mode == 'legacy':
        return None, None
    if mode == 'reachy_start':
        directory = Path(model_directory).expanduser()
        return ReachyStartFilter(directory/'silero-v6.2.3.onnx'), None
    if mode != 'hybrid':
        raise ValueError('turn_detection must be legacy or hybrid or reachy_start')
    directory = Path(model_directory).expanduser()
    return None, SmartTurnGuard(directory/SMART_TURN_FILENAME)
