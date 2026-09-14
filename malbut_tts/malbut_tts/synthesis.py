"""Synthesize local Qwen CustomVoice PCM with the Apple Silicon MLX runtime."""

import importlib
from pathlib import Path

import numpy as np

from malbut_tts.audio import PlaybackCancelled


class MlxSynthesizer:
    """Load an existing local model once and stream each complete request."""

    def __init__(self, model_path, speaker='Sohee', language='Korean'):
        """Keep configuration without importing MLX or loading weights."""
        self._path = Path(model_path).expanduser()
        self._speaker = speaker
        self._language = language
        self._model = None
        self._mx = None

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event.is_set():
            raise PlaybackCancelled('Speech synthesis was cancelled.')

    def load(self):
        """Load the existing local model once, optionally before first input."""
        if self._model is None:
            if not self._path.is_dir():
                raise FileNotFoundError(
                    f'Local MLX model directory does not exist: {self._path}')
            self._mx = importlib.import_module('mlx.core')
            utils = importlib.import_module('mlx_audio.tts.utils')
            self._model = utils.load_model(str(self._path.resolve()))

    def generate(self, text, cancel_event):
        """Yield validated PCM chunks, discarding results of cancelled work."""
        self._check_cancel(cancel_event)
        self.load()
        self._check_cancel(cancel_event)
        iterator = self._model.generate_custom_voice(
            text=text, speaker=self._speaker, language=self._language,
            instruct=None, stream=True, streaming_interval=0.5,
            max_tokens=1024,
        )
        token_count = 0
        sample_rate = None
        received = False
        try:
            while True:
                self._check_cancel(cancel_event)
                try:
                    result = next(iterator)
                except StopIteration:
                    break
                self._check_cancel(cancel_event)
                self._mx.eval(result.audio)
                self._check_cancel(cancel_event)
                audio = np.ascontiguousarray(result.audio, dtype=np.float32)
                if (audio.ndim != 1 or not audio.size
                        or not np.isfinite(audio).all()):
                    raise RuntimeError('The model generated invalid PCM.')
                rate = int(result.sample_rate)
                if rate <= 0 or rate != result.sample_rate:
                    raise RuntimeError('The model returned an invalid rate.')
                if sample_rate is not None and rate != sample_rate:
                    raise RuntimeError('The model changed its sample rate.')
                sample_rate = rate
                token_count += result.token_count
                if token_count >= 1024:
                    raise RuntimeError('Speech reached the generation limit.')
                received = True
                yield audio, rate
            self._check_cancel(cancel_event)
            if not received:
                raise RuntimeError('The model generated no audio.')
        finally:
            iterator.close()
