"""Local Piper KSS speech with the selected v2.1 Korean normalization."""

import json
from pathlib import Path
from threading import RLock

import numpy as np

from malbut_tts.audio import PlaybackCancelled


class PiperSynthesizer:
    """Produce sentence PCM on CPU; never download or fall back to an API.

    Stop is checked before and after each sentence inference. An ONNX call
    already in progress finishes before cancellation is observed.
    """

    backend = 'piper'
    sentence_streaming = True
    synthesis_streaming = False

    def __init__(self, model_path, *, threads=6):
        if not isinstance(model_path, (str, Path)) or not str(model_path).strip():
            raise ValueError('Piper model_path must be a local .onnx file')
        if type(threads) is not int or not 1 <= threads <= 64:
            raise ValueError('Piper threads must be an integer from 1 through 64')
        self._path = Path(model_path).expanduser()
        self._threads = threads
        self._voice = None
        self._lock = RLock()

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event.is_set():
            raise PlaybackCancelled('Speech synthesis was cancelled.')

    def load(self):
        with self._lock:
            if self._voice is not None:
                return
            config_path = Path(str(self._path) + '.json')
            if not self._path.is_file() or not config_path.is_file():
                raise FileNotFoundError('Piper requires a local ONNX file and its .onnx.json')
            import onnxruntime as ort
            from piper import PiperVoice
            from piper.config import PiperConfig, SynthesisConfig
            from malbut_tts.korean_normalization import KoreanNormalizer

            options = ort.SessionOptions()
            options.intra_op_num_threads = self._threads
            options.inter_op_num_threads = 1
            options.add_session_config_entry('session.intra_op.allow_spinning', '0')
            options.add_session_config_entry('session.inter_op.allow_spinning', '0')
            session = ort.InferenceSession(
                str(self._path), sess_options=options, providers=['CPUExecutionProvider'])
            if session.get_providers() != ['CPUExecutionProvider']:
                raise RuntimeError('Piper requires CPUExecutionProvider only')
            config = PiperConfig.from_dict(json.loads(config_path.read_text(encoding='utf-8')))
            self._normalizer = KoreanNormalizer()
            self._synthesis = SynthesisConfig()
            self.sample_rate = config.sample_rate
            self._voice = PiperVoice(session=session, config=config)

    def generate(self, text, cancel_event):
        self._check_cancel(cancel_event)
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ValueError('Piper text must contain 1 through 4096 characters')
        self.load()
        with self._lock:
            self._check_cancel(cancel_event)
            # Normalize the whole request before sentence splitting so number
            # labels and multiword lexicon entries retain their context.
            processed = self._normalizer(text)
            chunks = iter(self._voice.synthesize(processed, self._synthesis))
            emitted = False
            try:
                while True:
                    self._check_cancel(cancel_event)
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        break
                    self._check_cancel(cancel_event)
                    pcm = np.array(chunk.audio_float_array, dtype=np.float32, copy=True)
                    if pcm.ndim != 1 or not pcm.size or not np.isfinite(pcm).all():
                        raise RuntimeError('Piper generated invalid PCM')
                    emitted = True
                    yield pcm, self.sample_rate
                if not emitted:
                    raise RuntimeError('Piper generated no audio')
            finally:
                close = getattr(chunks, 'close', None)
                if close is not None:
                    close()
