"""Optional, fail-closed AEC recorder with an isolated, bounded audio worker.

The dialogue owner must acknowledge the switch to half-duplex before this
recorder releases any unfiltered microphone samples. A wedged native capture
or model can therefore be terminated without wedging ROS or opening raw barge-in.
"""

from multiprocessing import get_context
from queue import Empty
from threading import Event, Lock
from time import monotonic


class EchoCancelledRecorder:
    sample_rate = 16000

    def __init__(self, config, fallback_factory, *, startup_timeout_s=25.0):
        self.config = dict(config)
        self.fallback_factory = fallback_factory
        self.startup_timeout_s = startup_timeout_s
        self.aec_ready = False
        self.failure = None
        self.calibration = None
        self._fallback = None
        self._acknowledged = Event()
        self._closed = Event()
        self._process = None
        self._queue = None
        self._stop = None
        self._worker_lock = Lock()
        self._fallback_lock = Lock()

    def start(self):
        from .worker import run
        context = get_context('spawn')
        self._queue = context.Queue(maxsize=8)
        self._stop = context.Event()
        self._process = context.Process(
            target=run, args=(self.config, self._queue, self._stop),
            name='speech-aec', daemon=True)
        try:
            self._process.start()
            deadline = monotonic() + self.startup_timeout_s
            while monotonic() < deadline and not self._closed.is_set():
                try:
                    kind, payload, _ = self._queue.get(timeout=0.1)
                except Empty:
                    if not self._process.is_alive():
                        raise RuntimeError('AEC worker exited during startup')
                    continue
                if kind != 'ready':
                    raise RuntimeError(str(payload))
                self.calibration = payload
                self.aec_ready = True
                return
            raise TimeoutError('AEC startup timed out')
        except Exception as error:
            self._fail(error)

    def _fail(self, error):
        self.aec_ready = False  # Never expose raw PCM before the owner's gate closes.
        self.failure = type(error).__name__ + ': ' + str(error)
        self._stop_worker()

    def acknowledge_half_duplex(self):
        if not self.aec_ready:
            self._acknowledged.set()

    def read(self):
        if self._closed.is_set():
            return [0] * 512
        if self.aec_ready:
            try:
                kind, payload, captured_at = self._queue.get(timeout=0.5)
                if kind != 'audio':
                    raise RuntimeError(str(payload))
                if monotonic() - captured_at > 0.4:
                    raise RuntimeError('AEC capture backlog')
                from malbut_stt.audio import CapturePCM
                return CapturePCM(*payload)
            except Exception as error:
                if self._closed.is_set():
                    return [0] * 512
                if isinstance(error, Empty):
                    error = TimeoutError('AEC worker stopped delivering audio')
                self._fail(error)
        if not self._acknowledged.is_set():
            self._closed.wait(0.032)
            return [0] * 512
        with self._fallback_lock:
            if self._closed.is_set():
                return [0] * 512
            if self._fallback is None:
                self._fallback = self.fallback_factory()
                self._fallback.start()
        return self._fallback.read()

    def _stop_worker(self):
        with self._worker_lock:
            self._stop_worker_locked()

    def _stop_worker_locked(self):
        if self._stop is not None:
            self._stop.set()
        process = self._process
        if process is not None and process.pid is not None:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)

    def stop(self):
        self._closed.set()
        self._stop_worker()
        with self._fallback_lock:
            if self._fallback is not None:
                self._fallback.stop()

    def delete(self):
        self.stop()
        if self._fallback is not None:
            self._fallback.delete()
            self._fallback = None
        if self._queue is not None:
            self._queue.close()
            self._queue.cancel_join_thread()
            self._queue = None


class IsolatedMicrophoneRecorder:
    """Half-duplex fallback whose native read also stays out of the ROS process."""

    sample_rate = 16000

    def __init__(self, source):
        self._capture = EchoCancelledRecorder(
            {'raw_source': source}, lambda: None, startup_timeout_s=5.0)

    def start(self):
        self._capture.start()
        if not self._capture.aec_ready:
            raise RuntimeError('Fallback microphone failed: ' + str(self._capture.failure))

    def read(self):
        pcm = self._capture.read()
        if not self._capture.aec_ready and not self._capture._closed.is_set():
            raise RuntimeError('Fallback microphone failed: ' + str(self._capture.failure))
        return pcm

    def stop(self):
        self._capture.stop()

    def delete(self):
        self._capture.delete()
