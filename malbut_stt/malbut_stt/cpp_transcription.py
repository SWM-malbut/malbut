"""Resident whisper.cpp backend using explicitly supplied local model and library."""

import ctypes
import math
from pathlib import Path
from threading import Lock
from time import perf_counter
from types import SimpleNamespace

from malbut_stt.transcription import LocalWhisperTranscriber


class _DecodeWindow(ctypes.Structure):
    _fields_ = [(name, ctypes.c_double) for name in (
        'no_speech_prob', 'avg_logprobs', 'no_speech_threshold', 'logprob_threshold',
        'start_s', 'end_s', 'next_seek_s',
    )] + [(name, ctypes.c_int) for name in (
        'skipped_no_speech', 'single_timestamp_ending',
        'segment_start', 'segment_count', 'token_count',
    )]


def _load_library(path):
    library = ctypes.CDLL(str(path))
    signatures = {
        'mb_whisper_abi_version': ([], ctypes.c_int),
        'mb_whisper_create': ([ctypes.c_char_p, ctypes.c_int], ctypes.c_void_p),
        'mb_whisper_free': ([ctypes.c_void_p], None),
        'mb_whisper_reset_cancel': ([ctypes.c_void_p], None),
        'mb_whisper_cancel': ([ctypes.c_void_p], None),
        'mb_whisper_transcribe': ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                  ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                  ctypes.c_double], ctypes.c_int),
        'mb_whisper_segment_count': ([ctypes.c_void_p], ctypes.c_int),
        'mb_whisper_segment_text': ([ctypes.c_void_p, ctypes.c_int], ctypes.c_char_p),
        'mb_whisper_segment_start': ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int64),
        'mb_whisper_segment_end': ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int64),
        'mb_whisper_model_ftype': ([ctypes.c_void_p], ctypes.c_int),
        'mb_whisper_model_type': ([ctypes.c_void_p], ctypes.c_char_p),
        'mb_whisper_system_info': ([], ctypes.c_char_p),
    }
    try:
        for name, (arguments, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
    except AttributeError as error:
        raise ValueError('whisper.cpp requires rebuilding the packaged ABI 3 bridge') from error
    abi = library.mb_whisper_abi_version()
    if abi not in (3, 4):
        raise ValueError('whisper.cpp requires an ABI 3 or ABI 4 bridge')
    if abi == 4:
        try:
            for name, arguments in {
                'mb_whisper_trace_count': [ctypes.c_void_p],
                'mb_whisper_trace_truncated': [ctypes.c_void_p],
                'mb_whisper_trace_window': [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.POINTER(_DecodeWindow)],
            }.items():
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, ctypes.c_int
        except AttributeError as error:
            raise ValueError('whisper.cpp requires rebuilding the packaged ABI 4 bridge') from error
    return library


class _CppModel:
    """Keep native inference, result copying, and context destruction serialized."""

    def __init__(self, library, context, n_threads, decode_timeout_s):
        self.library, self.context, self.n_threads = library, context, n_threads
        self.decode_timeout_s = decode_timeout_s
        self._lock = Lock()
        self._state_lock = Lock()
        self._closing = False
        self.last_segments = []

    def _decode_info(self):
        if self.library.mb_whisper_abi_version() == 3:
            return dict(status='unavailable', reason='bridge_abi_3', windows=[])
        windows = []
        count = self.library.mb_whisper_trace_count(self.context)
        if not 0 <= count <= 64:
            raise ValueError('native trace window count exceeds ABI limit')
        for index in range(count):
            window = _DecodeWindow()
            if not self.library.mb_whisper_trace_window(self.context, index, ctypes.byref(window)):
                raise ValueError('native trace window unavailable')
            value, nonfinite = {}, {}
            for name, _ in window._fields_:
                field = getattr(window, name)
                if isinstance(field, float) and not math.isfinite(field):
                    nonfinite[name] = str(field)
                    field = None
                value[name] = field
            for name in ('skipped_no_speech', 'single_timestamp_ending'):
                value[name] = bool(value[name])
            if nonfinite:
                value['nonfinite_fields'] = nonfinite
            windows.append(value)
        return dict(status='observed', windows=windows,
                    windows_truncated=bool(self.library.mb_whisper_trace_truncated(self.context)))

    def transcribe(self, audio, *, language, beam_size, condition_on_previous_text,
                   initial_prompt):
        if language != 'ko' or beam_size != 1 or condition_on_previous_text:
            raise ValueError('whisper.cpp backend requires Korean greedy decoding without history')
        if initial_prompt is not None and not isinstance(initial_prompt, str):
            raise ValueError('initial_prompt must be text or None')
        import numpy as np

        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim != 1 or not np.isfinite(audio).all():
            raise ValueError('whisper.cpp requires finite mono float32 audio')
        audio = np.ascontiguousarray(audio)
        prompt = initial_prompt.encode('utf-8') if initial_prompt is not None else None
        with self._lock:
            with self._state_lock:
                if not self.context or self._closing:
                    raise RuntimeError('whisper.cpp context is closed')
                # Reset before releasing the state lock so a concurrent cancel
                # cannot get lost in the gap before the native call starts.
                self.library.mb_whisper_reset_cancel(self.context)
            self.last_segments = []
            if audio.size == 0 or not np.any(audio):
                return iter(()), SimpleNamespace(native_decode=dict(
                    status='not_run', reason='empty_input' if audio.size == 0 else 'exact_silence',
                    windows=[]))
            result = self.library.mb_whisper_transcribe(
                self.context, audio.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                audio.size, self.n_threads, prompt, self.decode_timeout_s,
            )
            # Copy under the inference lock: a later call must not overwrite
            # scores for an iterator that has not yet been consumed.
            try:
                native_decode = self._decode_info()
            except Exception as error:
                # Observation failure must not suppress a valid transcript.
                native_decode = dict(status='unavailable', reason='trace_read_failed',
                                     error=str(error)[:512], windows=[])
            native_decode['return_code'] = result
            if result != 0:
                if result == -100:
                    error = RuntimeError('whisper.cpp decode cancelled')
                elif result == -101:
                    error = TimeoutError('whisper.cpp decode deadline exceeded')
                else:
                    error = RuntimeError(f'whisper.cpp decode failed with code {result}')
                error.native_decode = native_decode
                raise error
            for index in range(self.library.mb_whisper_segment_count(self.context)):
                self.last_segments.append(SimpleNamespace(
                    text=self.library.mb_whisper_segment_text(self.context, index).decode('utf-8'),
                    start=self.library.mb_whisper_segment_start(self.context, index) / 100,
                    end=self.library.mb_whisper_segment_end(self.context, index) / 100,
                ))
            # Copy the list while holding the lock. A later call or close must
            # not invalidate the iterator consumed by the wake/stream wrapper.
            return iter(tuple(self.last_segments)), SimpleNamespace(native_decode=native_decode)

    def cancel(self):
        """Signal the current decode without waiting for its inference lock."""
        with self._state_lock:
            if self.context:
                self.library.mb_whisper_cancel(self.context)

    def close(self):
        """Cancel, then wait for native return before releasing the context."""
        with self._state_lock:
            self._closing = True
            if self.context:
                self.library.mb_whisper_cancel(self.context)
        with self._lock:
            with self._state_lock:
                if self.context:
                    self.library.mb_whisper_free(self.context)
                    self.context = None


class CppWhisperTranscriber(LocalWhisperTranscriber):
    """Share one native model between ordinary text, wake recognition, and previews."""

    backend = 'whisper.cpp'

    def __init__(self, model_path, library_path, *, use_gpu=True, n_threads=6,
                 decode_timeout_s=30.0):
        model_path = Path(model_path).expanduser().resolve()
        library_path = Path(library_path).expanduser().resolve()
        if not model_path.is_file() or not library_path.is_file():
            raise ValueError('whisper.cpp requires existing local model and bridge library files')
        if isinstance(n_threads, bool) or not isinstance(n_threads, int) or n_threads < 1:
            raise ValueError('n_threads must be a positive integer')
        if (isinstance(decode_timeout_s, bool)
                or not isinstance(decode_timeout_s, (int, float))
                or not math.isfinite(decode_timeout_s) or decode_timeout_s <= 0):
            raise ValueError('decode_timeout_s must be finite and positive')
        library = _load_library(library_path)
        started = perf_counter()
        context = library.mb_whisper_create(str(model_path).encode(), bool(use_gpu))
        if not context:
            raise RuntimeError('whisper.cpp failed to load the local model')
        self.model = _CppModel(library, context, n_threads, decode_timeout_s)
        self.load_s = perf_counter() - started
        try:
            self.metadata = {
                'backend': self.backend, 'model_path': str(model_path),
                'library_path': str(library_path), 'bridge_abi': library.mb_whisper_abi_version(),
                'native_decode_trace': library.mb_whisper_abi_version() == 4,
                'requested_use_gpu': bool(use_gpu), 'threads': n_threads,
                'decode_timeout_s': decode_timeout_s,
                'model_type': library.mb_whisper_model_type(context).decode(),
                'model_ftype': library.mb_whisper_model_ftype(context),
                'system_info': library.mb_whisper_system_info().decode(),
                'language': 'ko', 'sampling': 'greedy', 'best_of': 1, 'beam_size': 1,
                'temperature': 0.0, 'temperature_inc': 0.0, 'no_context': True,
                'initial_prompt_per_call': True, 'flash_attention': True,
                'load_s': self.load_s,
            }
        except Exception:
            self.model.close()
            raise

    def cancel(self):
        """Abort the current decode; subsequent calls can reuse the model."""
        self.model.cancel()

    def close(self):
        """Cancel active inference and release the model; repeated close is safe."""
        self.model.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
