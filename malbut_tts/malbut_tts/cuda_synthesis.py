"""Experimental local Qwen CUDA synthesis, with buffered (not streamed) PCM.

The official qwen-tts 0.1.1 API returns a complete waveform. Chunking that
waveform only bounds output buffers; it does not reduce first-audio latency.
"""

from contextlib import contextmanager
import importlib
from math import isfinite
from numbers import Integral, Real
from pathlib import Path
import re
from threading import RLock

import numpy as np

from malbut_tts.audio import PlaybackCancelled


_MISSING = object()


class _ContractError(RuntimeError):
    """An adapter-owned diagnostic containing no model inputs or outputs."""


class CudaSynthesizer:
    """Keep one explicitly local model on CUDA and preserve each whole text.

    ``float32`` avoids assuming FP16 numerical stability on older GPUs, at
    the cost of more memory. There is no CPU, cloud, or automatic dtype fallback.
    The adapter targets the official qwen-tts 0.1.1 model structure and fails
    closed if normal codec EOS completion cannot be inspected.
    """

    backend = 'qwen_cuda'
    synthesis_streaming = False

    def __init__(self, model_path, speaker='Sohee', language='Korean', *,
                 dtype='float32', device='cuda:0', max_new_tokens=1024,
                 chunk_seconds=0.5):
        if dtype not in ('float16', 'float32'):
            raise ValueError('CUDA TTS dtype must be float16 or float32')
        if not isinstance(device, str) or not re.fullmatch(r'cuda(?::[0-9]+)?', device):
            raise ValueError('CUDA TTS requires an explicit CUDA device')
        if (type(max_new_tokens) is not int
                or not 2 <= max_new_tokens <= 4096):
            raise ValueError('max_new_tokens must be an integer from 2 through 4096')
        if (isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, Real)
                or not isfinite(chunk_seconds) or not 0 < chunk_seconds <= 5):
            raise ValueError('chunk_seconds must be positive and at most 5 seconds')
        for value, label in ((speaker, 'speaker'), (language, 'language')):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{label} must contain text')
        if not isinstance(model_path, (str, Path)) or not str(model_path).strip():
            raise ValueError('model_path must be an existing local directory')
        self._path = Path(model_path).expanduser()
        self._speaker = speaker
        self._language = language
        self._dtype = dtype
        self._device = device
        self._max_new_tokens = max_new_tokens
        self._chunk_seconds = float(chunk_seconds)
        self._model = None
        self._lock = RLock()

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event.is_set():
            raise PlaybackCancelled('Speech synthesis was cancelled.')

    @staticmethod
    @contextmanager
    def _backend_errors(operation):
        """Keep arbitrary third-party exception text out of runtime logs."""
        try:
            yield
        except (PlaybackCancelled, _ContractError):
            raise
        except Exception as error:
            raise RuntimeError(
                f'CUDA TTS {operation} failed: {type(error).__name__}') from None

    def load(self):
        """Lazily load existing local weights; never change device on failure."""
        with self._lock:
            if self._model is not None:
                return
            if not self._path.is_dir():
                raise FileNotFoundError(
                    f'Local CUDA TTS model directory does not exist: {self._path}')
            with self._backend_errors('model loading'):
                torch = importlib.import_module('torch')
                if not torch.cuda.is_available():
                    raise _ContractError('CUDA TTS requires an available CUDA GPU')
                qwen = importlib.import_module('qwen_tts')
                model = qwen.Qwen3TTSModel.from_pretrained(
                    str(self._path.resolve()), device_map=self._device,
                    dtype=getattr(torch, self._dtype), attn_implementation='sdpa',
                    local_files_only=True,
                )
                if getattr(getattr(model, 'device', None), 'type', None) != 'cuda':
                    raise _ContractError('CUDA TTS model was not loaded on CUDA')
                model.model.eval()
                self._model = model

    @contextmanager
    def _checked_generation(self, cancel_event):
        """Observe real EOS and check cancellation at talker forward boundaries.

        qwen-tts 0.1.1 discards arbitrary generation kwargs, including
        stopping_criteria. A scoped wrapper observes its actual talker result
        instead. No model source or global class is patched. Hooks cannot
        interrupt a CUDA kernel or an already-running waveform decoder.
        """
        talker = getattr(self._model.model, 'talker', None)
        generate = getattr(talker, 'generate', None)
        add_hook = getattr(talker, 'register_forward_pre_hook', None)
        eos = getattr(getattr(talker, 'config', None), 'codec_eos_token_id', None)
        if (not callable(generate) or not callable(add_hook)
                or isinstance(eos, bool) or not isinstance(eos, Integral)
                or eos < 0):
            raise _ContractError('Cannot verify Qwen codec generation completion')
        completion = {'seen': False}
        previous = vars(talker).get('generate', _MISSING)

        def check_forward(_module, _arguments):
            self._check_cancel(cancel_event)

        def checked_generate(*args, **kwargs):
            self._check_cancel(cancel_event)
            result = generate(*args, **kwargs)
            self._check_cancel(cancel_event)
            sequences = getattr(result, 'sequences', None)
            if (getattr(sequences, 'ndim', None) != 2
                    or sequences.shape[0] != 1 or sequences.shape[1] == 0):
                raise _ContractError('Cannot verify Qwen codec generation completion')
            # The official batch-one talker includes the sampled EOS in
            # sequences. No EOS means a token cap/other stop, not full speech.
            terminal = sequences[0, -1].item()
            if isinstance(terminal, bool) or not isinstance(terminal, Integral):
                raise _ContractError('Cannot verify Qwen codec generation completion')
            if terminal != eos:
                raise _ContractError('Speech reached the generation limit without EOS')
            completion['seen'] = True
            return result

        hook = add_hook(check_forward)
        try:
            talker.generate = checked_generate
            yield completion
        finally:
            try:
                if previous is _MISSING:
                    delattr(talker, 'generate')
                else:
                    talker.generate = previous
            finally:
                hook.remove()

    def generate(self, text, cancel_event):
        """Synthesize once, then yield validated buffered PCM with cancellation."""
        self._check_cancel(cancel_event)
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Speech text must contain text')
        with self._lock:
            self._check_cancel(cancel_event)
            self.load()
            self._check_cancel(cancel_event)
            with self._backend_errors('generation'):
                with self._checked_generation(cancel_event) as completion:
                    self._check_cancel(cancel_event)
                    wavs, sample_rate = self._model.generate_custom_voice(
                        text=text, speaker=self._speaker, language=self._language,
                        instruct=None, non_streaming_mode=True,
                        max_new_tokens=self._max_new_tokens,
                    )
                    self._check_cancel(cancel_event)
                    if not completion['seen']:
                        raise _ContractError('Cannot verify Qwen codec generation completion')
            if (not isinstance(wavs, (list, tuple)) or len(wavs) != 1):
                raise RuntimeError('The model must return one complete audio waveform')
            if (isinstance(sample_rate, bool) or not isinstance(sample_rate, Real)
                    or not isfinite(sample_rate) or sample_rate <= 0
                    or int(sample_rate) != sample_rate):
                raise RuntimeError('The model returned an invalid sample rate')
            rate = int(sample_rate)
            try:
                audio = np.ascontiguousarray(wavs[0], dtype=np.float32)
            except (TypeError, ValueError, OverflowError) as error:
                raise RuntimeError('The model generated invalid PCM') from error
            if audio.ndim != 1 or not audio.size or not np.isfinite(audio).all():
                raise RuntimeError('The model generated invalid PCM')
            chunk_size = max(1, int(rate * self._chunk_seconds))
            for offset in range(0, audio.size, chunk_size):
                self._check_cancel(cancel_event)
                yield audio[offset:offset + chunk_size], rate
            self._check_cancel(cancel_event)
