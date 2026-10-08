"""Opt-in, bounded local evidence of the audio actually sent to a local model."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from threading import Lock, local
import time
from uuid import uuid4
import wave

from malbut_stt.confidence import confidence_scores


_RESULT_BYTES = 65536


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      default=vars).encode('utf-8')


class TranscriptionDiagnostics:
    """Save decode WAV/JSON pairs; never remove evidence or fail normal STT.

    The directory limit includes existing decode files. Each admitted WAV reserves
    64 KiB for its result, so concurrent/lazy decodes cannot exceed the limit.
    Models receive their original arguments and segments remain lazily consumed.
    """

    def __init__(self, directory, *, max_bytes=64 * 1024 * 1024):
        self.directory = Path(directory).expanduser()
        self.max_bytes = max_bytes
        self.error = None
        self._disabled = False
        self._local = local()
        self._lock = Lock()
        self._used = 0
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._used = sum(path.stat().st_size for path in self.directory.glob('decode-*')
                             if path.is_file())
            events = self.directory / 'events.jsonl'
            if events.exists():
                self._used += events.stat().st_size
        except Exception as error:
            self._fail(error)

    def _fail(self, error):
        self.error = f'{type(error).__name__}: {error}'
        self._disabled = True

    @contextmanager
    def context(self, **metadata):
        """Correlate model calls in this worker thread with its current ASR job."""
        previous = getattr(self._local, 'metadata', None)
        self._local.metadata = metadata
        try:
            yield
        finally:
            self._local.metadata = previous

    def wrap_model(self, model):
        return _DiagnosticModel(model, self)

    def event(self, name, **metadata):
        """Record pipeline boundaries using the same storage budget as decodes."""
        if self._disabled:
            return
        try:
            content = _json(dict(event=name, wall_ns=time.time_ns(),
                                 context=getattr(self._local, 'metadata', None) or {},
                                 **metadata)) + b'\n'
            if len(content) > _RESULT_BYTES:
                raise ValueError('diagnostic event exceeds result budget')
            with self._lock:
                if self._disabled:
                    return
                if self._used + len(content) > self.max_bytes:
                    self.error = 'diagnostic_limit_reached'
                    self._disabled = True
                    return
                self._used += len(content)
                with os.fdopen(os.open(self.directory / 'events.jsonl',
                                       os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'wb') as output:
                    output.write(content)
        except Exception as error:
            self._fail(error)

    def _begin(self, audio, options):
        if self._disabled:
            return None
        try:
            import numpy as np

            samples = np.asarray(audio)
            pcm = np.rint(np.clip(samples * 32768, -32768, 32767)).astype('<i2')
            if samples.ndim != 1 or not np.array_equal(samples, pcm.astype(np.float32) / 32768):
                raise ValueError('model audio cannot be saved losslessly as PCM16')
            raw = pcm.tobytes()
            name = 'decode-' + uuid4().hex
            record = dict(decode_id=name, wall_ns=time.time_ns(),
                          context=getattr(self._local, 'metadata', None) or {},
                          wav=name + '.wav', sample_rate=16000, sample_count=len(pcm),
                          pcm_sha256=hashlib.sha256(raw).hexdigest(), options=options,
                          segments=[], segments_truncated=False, error=None)
            if len(_json(record)) > _RESULT_BYTES // 2:
                raise ValueError('diagnostic context exceeds result budget')
            with self._lock:
                if self._disabled:
                    return None
                if self._used + len(raw) + 44 + _RESULT_BYTES > self.max_bytes:
                    self.error = 'diagnostic_limit_reached'
                    self._disabled = True
                    return None
                self._used += len(raw) + 44 + _RESULT_BYTES
                with self._open(name + '.wav') as output:
                    with wave.open(output, 'wb') as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(16000)
                        wav.writeframes(raw)
            return record
        except Exception as error:
            self._fail(error)
            return None

    def _open(self, name):
        return os.fdopen(os.open(self.directory / name,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')

    def _segment(self, record, segment):
        if record is None or record['segments_truncated']:
            return
        try:
            value = dict(segment._asdict() if hasattr(segment, '_asdict') else vars(segment))
            # Unknown confidence remains unknown; NaN/Inf must not disable evidence capture.
            for name, score in zip(('no_speech_prob', 'avg_logprob'), confidence_scores(segment)):
                if name in value:
                    value[name] = score
            # Reserve space for an error even when a lazy decoder yields then fails.
            if len(_json(record)) + len(_json(value)) > _RESULT_BYTES - 8192:
                record['segments_truncated'] = True
            else:
                record['segments'].append(json.loads(_json(value)))
        except Exception as error:
            record['segments_truncated'] = True
            self._fail(error)

    def _finish(self, record, error=None):
        if record is None:
            return
        try:
            record['finished_wall_ns'] = time.time_ns()
            if error is not None:
                record['error'] = dict(type=type(error).__name__, message=str(error)[:2048])
            content = _json(record)
            if len(content) > _RESULT_BYTES:
                raise ValueError('diagnostic result exceeds result budget')
            with self._lock:
                with self._open(record['decode_id'] + '.json') as output:
                    output.write(content)
                self._used -= _RESULT_BYTES - len(content)
        except Exception as failure:
            self._fail(failure)


class _DiagnosticModel:
    def __init__(self, model, diagnostics):
        self._model, self._diagnostics = model, diagnostics

    def __getattr__(self, name):
        return getattr(self._model, name)

    def report_confidence(self, event, **metadata):
        """Record score-only filtering decisions in the current decode/job context."""
        self._diagnostics.event(event, **metadata)
        report = getattr(self._model, 'report_confidence', None)
        if callable(report):
            report(event, **metadata)

    def transcribe(self, audio, *args, **options):
        record = self._diagnostics._begin(audio, options)
        try:
            segments, info = self._model.transcribe(audio, *args, **options)
        except Exception as error:
            self._diagnostics._finish(record, error)
            raise
        if record is None:
            return segments, info

        def observed():
            try:
                for segment in segments:
                    self._diagnostics._segment(record, segment)
                    yield segment
            except Exception as error:
                self._diagnostics._finish(record, error)
                raise
            else:
                self._diagnostics._finish(record)

        return observed(), info
