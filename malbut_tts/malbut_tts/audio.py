"""Play bounded PCM streams with controls acknowledged after device actions."""

from collections import deque
import importlib
from threading import Condition, Event, Thread, current_thread

import numpy as np


class PlaybackCancelled(RuntimeError):
    """Signal that this playback has been cancelled."""


class StreamingPlayer:
    """Own one output stream, retaining unplayed PCM across a drained pause."""

    def __init__(self, on_state, cancel_event, device=None):
        """Start the device owner; sounddevice itself is imported on demand."""
        self._on_state = on_state
        self._cancel = cancel_event
        self._device = device
        self._condition = Condition()
        self._pending = deque()
        self._offset = 0
        self._rate = None
        self._input_done = False
        self._pause_requested = False
        self._paused = False
        self._started = False
        self._drained = False
        self._drain_reason = None
        self._error = None
        self._complete = False
        self._underruns = 0
        self._closing = Event()
        self._done = Event()
        self._sd = None
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def underruns(self):
        """Count callbacks with missing PCM or reported device underflow."""
        with self._condition:
            return self._underruns

    def _check(self):
        if self._error is not None:
            raise RuntimeError('Audio playback failed.') from self._error
        if self._cancel.is_set():
            raise PlaybackCancelled('Audio playback was cancelled.')
        if self._closing.is_set():
            raise RuntimeError('Audio playback was closed.')

    def wait_for_capacity(self, *, max_pending=2):
        """Reserve producer time only when another complete sentence can fit.

        Called by the single runtime producer *before* synthesis. The queue
        includes the currently playing partial sentence. There is no second
        writer, so its size can only decrease until that producer writes.
        """
        if type(max_pending) is not int or not 1 <= max_pending <= 32:
            raise ValueError('max_pending must be an integer from 1 through 32')
        with self._condition:
            while True:
                self._check()
                if self._input_done:
                    raise RuntimeError('Cannot write after finishing playback.')
                if len(self._pending) < max_pending:
                    return
                self._condition.wait(0.05)

    def write(self, audio, sample_rate):
        """Queue PCM, waiting for buffer space while remaining cancellable."""
        chunk = np.array(audio, dtype=np.float32, order='C', copy=True)
        if chunk.ndim != 1 or not chunk.size or not np.isfinite(chunk).all():
            raise ValueError('Expected nonempty, finite, one-dimensional PCM.')
        rate = int(sample_rate)
        if rate <= 0 or rate != sample_rate:
            raise ValueError('Expected a positive integer sample rate.')
        with self._condition:
            self._check()
            if self._input_done:
                raise RuntimeError('Cannot write after finishing playback.')
            if self._rate is not None and self._rate != rate:
                raise ValueError('The sample rate changed during playback.')
            self._rate = rate
            while len(self._pending) >= 32:
                self._check()
                self._condition.wait(0.05)
            self._check()
            self._pending.append(chunk)
            self._condition.notify_all()

    def finish(self):
        """Wait through any pauses until every output buffer has drained."""
        with self._condition:
            self._check()
            if self._rate is None:
                raise RuntimeError('No audio was generated.')
            self._input_done = True
            self._condition.notify_all()
        while not self._done.wait(0.05):
            with self._condition:
                self._check()
        with self._condition:
            self._check()
            if not self._complete:
                raise RuntimeError('Audio did not finish playing.')

    def pause(self):
        """Accept a pause which takes effect after submitted PCM drains."""
        with self._condition:
            if (not self._started or self._pause_requested
                    or self._done.is_set() or self._cancel.is_set()
                    or self._closing.is_set()
                    or self._error is not None
                    or self._drain_reason == 'finished'):
                return False
            self._pause_requested = True
            self._condition.notify_all()
            return True

    def resume(self):
        """Accept resumption of a confirmed pause at the retained offset."""
        with self._condition:
            if (not self._paused or not self._pause_requested
                    or self._done.is_set() or self._cancel.is_set()
                    or self._closing.is_set()
                    or self._error is not None):
                return False
            self._pause_requested = False
            self._condition.notify_all()
            return True

    def stop(self):
        """Cancel and wait until output has been aborted and closed."""
        self._cancel.set()
        self.close()

    def close(self):
        """Release output without turning a synthesis failure into a stop."""
        if not self._done.is_set():
            self._closing.set()
            with self._condition:
                self._pending.clear()
                self._condition.notify_all()
            if current_thread() is not self._thread:
                self._done.wait()
        with self._condition:
            if self._error is not None:
                raise RuntimeError('Could not stop audio cleanly.') \
                    from self._error

    def _callback(self, outdata, frames, timing, status):
        del timing
        outdata.fill(0)
        underflow = bool(getattr(status, 'output_underflow', False))
        try:
            with self._condition:
                if self._cancel.is_set() or self._closing.is_set():
                    self._drain_reason = 'cancelled'
                    raise self._sd.CallbackAbort()
                if self._input_done and not self._pending:
                    self._drain_reason = 'finished'
                    raise self._sd.CallbackStop()
                if self._pause_requested:
                    self._drain_reason = 'paused'
                    raise self._sd.CallbackStop()
                filled = 0
                while filled < frames and self._pending:
                    chunk = self._pending[0]
                    count = min(frames - filled, len(chunk) - self._offset)
                    outdata[filled:filled + count, 0] = chunk[
                        self._offset:self._offset + count]
                    filled += count
                    self._offset += count
                    if self._offset == len(chunk):
                        self._pending.popleft()
                        self._offset = 0
                if filled < frames and not self._input_done:
                    underflow = True
                self._condition.notify_all()
                if self._input_done and not self._pending:
                    self._drain_reason = 'finished'
                    raise self._sd.CallbackStop()
        except (self._sd.CallbackStop, self._sd.CallbackAbort):
            raise
        except BaseException as error:
            with self._condition:
                self._error = error
                self._condition.notify_all()
            raise self._sd.CallbackAbort() from error
        finally:
            if underflow:
                with self._condition:
                    self._underruns += 1

    def _finished_callback(self):
        with self._condition:
            self._drained = True
            self._condition.notify_all()

    def _run(self):
        stream = None
        inactive_seen = False
        try:
            while True:
                action = None
                with self._condition:
                    if (self._cancel.is_set() or self._closing.is_set()
                            or self._error is not None):
                        break
                    if stream is None and self._pending:
                        action = 'create'
                    elif self._drained:
                        if self._drain_reason == 'finished':
                            self._complete = True
                            break
                        if self._drain_reason != 'paused':
                            raise RuntimeError('Output stopped unexpectedly.')
                        if not self._paused:
                            self._paused = True
                            action = 'paused'
                        elif not self._pause_requested:
                            self._paused = False
                            self._drained = False
                            self._drain_reason = None
                            action = 'start'
                    elif self._started:
                        if not stream.active:
                            if inactive_seen:
                                raise RuntimeError(
                                    'Output became inactive without drain.')
                            inactive_seen = True
                        else:
                            inactive_seen = False
                    if action is None:
                        self._condition.wait(0.05)
                        continue
                if action == 'create':
                    self._sd = importlib.import_module('sounddevice')
                    stream = self._sd.OutputStream(
                        samplerate=self._rate, channels=1, dtype='float32',
                        blocksize=0, latency='low', device=self._device,
                        callback=self._callback,
                        finished_callback=self._finished_callback,
                    )
                    action = 'start'
                if action == 'start':
                    if self._cancel.is_set() or self._closing.is_set():
                        break
                    stream.start()
                    inactive_seen = False
                    with self._condition:
                        self._started = True
                    if (not self._cancel.is_set()
                            and not self._closing.is_set()):
                        self._on_state('playing')
                elif (action == 'paused' and not self._cancel.is_set()
                      and not self._closing.is_set()):
                    # CallbackStop makes a stream inactive, but PortAudio
                    # requires stop() before that stream can start again.
                    stream.stop(ignore_errors=False)
                    self._on_state('paused')
        except BaseException as error:
            with self._condition:
                self._error = error
        finally:
            try:
                if stream is not None:
                    try:
                        if not self._complete and not stream.stopped:
                            stream.abort(ignore_errors=False)
                    finally:
                        stream.close(ignore_errors=False)
            except BaseException as error:
                with self._condition:
                    self._error = error
                    self._complete = False
            finally:
                with self._condition:
                    self._pending.clear()
                    self._done.set()
                    self._condition.notify_all()
