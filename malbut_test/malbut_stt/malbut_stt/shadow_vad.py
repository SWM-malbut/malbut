"""Bounded, optional microphone observation. Never controls STT decisions.

Both shadow detectors see continuous PCM before speaker/busy gates. Production
WebRTC still runs separately and is recorded as vad_frame by IncidentRecorder.
Only scores, frame boundaries and hashes are added to the existing event sink;
this module never writes audio files or opens a microphone.
"""

import hashlib
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic

from malbut_stt.silero_vad import SileroVad


MODEL_SHA256 = '1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3'
ENVIRONMENT_KEY = 'MALBUT_STT_SHADOW_VAD_MODEL'


class ShadowDiagnostics:
    """Forward existing diagnostics unchanged; enqueue copies from one producer."""

    def __init__(self, diagnostics, model_path, *, vad_mode=2, queue_size=64,
                 on_error=None, detector_factory=None):
        self.diagnostics = diagnostics
        self.model_path = Path(model_path).expanduser()
        self.vad_mode = vad_mode
        if type(queue_size) is not int or not 1 <= queue_size <= 256:
            raise ValueError('shadow queue_size must be between 1 and 256')
        self._queue = Queue(maxsize=queue_size)
        self._stop = Event()
        self._closed = False
        self._sample_index = 0
        self._dropped_chunks = 0
        self._processed_chunks = 0
        self._on_error = on_error
        self._factory = detector_factory or self._load_detectors
        self._thread = Thread(target=self._run, name='stt-shadow-vad', daemon=True)
        self._thread.start()

    def __getattr__(self, name):
        return getattr(self.diagnostics, name)

    def _load_detectors(self):
        # Pin the protocol and weights; do not download anything at node startup.
        digest = hashlib.sha256()
        with self.model_path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != MODEL_SHA256:
            raise ValueError('shadow Silero model checksum mismatch')
        import webrtcvad

        return SileroVad(self.model_path), lambda: webrtcvad.Vad(self.vad_mode)

    def _emit(self, name, **fields):
        if not self._stop.is_set():
            self.diagnostics.event(name, observation_only=True, **fields)
            return True
        return False

    def audio(self, pcm, **metadata):
        original = getattr(self.diagnostics, 'audio', None)
        if callable(original):
            original(pcm, **metadata)
        if self._stop.is_set():
            return
        # Capture supplies one 512-sample chunk. Bound each queued object too.
        try:
            if len(pcm) != 1024:
                raise ValueError('shadow observation expects 512 PCM16 samples')
            start = self._sample_index
            self._sample_index += 512
            item = (start, bytes(pcm), dict(metadata), monotonic())
            self._queue.put_nowait(item)
        except Full:
            self._dropped_chunks += 1
        except Exception as error:
            self._fail(error)

    def _fail(self, error):
        try:
            self._emit('shadow_vad_error', error=type(error).__name__,
                       dropped_chunks_total=self._dropped_chunks)
        except Exception:
            pass
        self._stop.set()
        if self._on_error is not None:
            try:
                self._on_error(type(error).__name__)
            except Exception:
                pass

    def _run(self):
        try:
            silero, make_webrtc = self._factory()
            webrtc = make_webrtc()
            self._emit('shadow_vad_started', model='silero-v6.2.3',
                       model_sha256=MODEL_SHA256, sample_rate=16000,
                       silero_threshold=0.5, silero_frame_samples=512,
                       webrtc_mode=self.vad_mode, webrtc_frame_samples=320,
                       input_stage='microphone_before_gates', queue_capacity=self._queue.maxsize)
            expected, previous_at, overflow = 0, None, False
            pending, pending_start = b'', 0
            while not self._stop.is_set():
                try:
                    start, pcm, metadata, enqueued_at = self._queue.get(timeout=.1)
                except Empty:
                    continue
                at = metadata.get('captured_at', enqueued_at)
                reason = ('queue_gap' if start != expected else
                          'microphone_overflow' if metadata.get('overflow') and not overflow else
                          'capture_time_gap' if previous_at is not None and
                          (at < previous_at or at - previous_at > .25) else None)
                if reason:
                    silero.reset()
                    webrtc = make_webrtc()
                    pending, pending_start = b'', start
                    self._emit('shadow_vad_gap', reason=reason, sample_index=start,
                               missing_samples=max(0, start - expected),
                               captured_at=at, dropped_chunks_total=self._dropped_chunks,
                               detectors_reset=True)
                expected, previous_at, overflow = start + 512, at, bool(metadata.get('overflow'))
                started = monotonic()
                probability = silero.probability(pcm)
                pending += pcm
                frames = []
                while len(pending) >= 640:
                    frame, pending = pending[:640], pending[640:]
                    frames.append(dict(start_sample=pending_start,
                                       end_sample=pending_start + 320,
                                       speech=bool(webrtc.is_speech(frame, 16000)),
                                       pcm_sha256=hashlib.sha256(frame).hexdigest()))
                    pending_start += 320
                if self._emit('shadow_vad', sample_index=start, samples=512,
                              pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                              model_sha256=MODEL_SHA256, silero_threshold=.5,
                              webrtc_mode=self.vad_mode,
                              silero_probability=probability, silero_speech=probability >= .5,
                              webrtc_frames=frames, input_stage='microphone_before_gates',
                              source=metadata, queue_wait_s=started - enqueued_at,
                              inference_s=monotonic() - started,
                              dropped_chunks_total=self._dropped_chunks):
                    self._processed_chunks += 1
        except Exception as error:
            self._fail(error)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=2)
        # Capture has already stopped. Any unprocessed tail is explicitly marked.
        try:
            self.diagnostics.event('shadow_vad_stopped', observation_only=True,
                                   dropped_chunks_total=self._dropped_chunks,
                                   observed_chunks=self._sample_index // 512,
                                   processed_chunks=self._processed_chunks,
                                   unprocessed_chunks=(self._sample_index // 512
                                                       - self._dropped_chunks
                                                       - self._processed_chunks),
                                   pending_chunks=self._queue.qsize(),
                                   worker_still_running=self._thread.is_alive())
        finally:
            close = getattr(self.diagnostics, 'close', None)
            if callable(close):
                close()


def from_environment(environ, diagnostics, *, vad_mode=2, on_error=None):
    path = environ.get(ENVIRONMENT_KEY, '').strip()
    if not path:
        return diagnostics
    if diagnostics is None:
        if on_error:
            on_error('diagnostics_required')
        return diagnostics
    try:
        return ShadowDiagnostics(diagnostics, path, vad_mode=vad_mode, on_error=on_error)
    except Exception as error:
        if on_error:
            on_error(type(error).__name__)
        return diagnostics
