"""Play bounded PCM streams with controls acknowledged after device actions."""

from collections import deque
import hashlib
import importlib
import json
from pathlib import Path
from threading import Condition, Event, Thread, current_thread
from time import monotonic

import numpy as np


class PlaybackCancelled(RuntimeError):
    """Signal that this playback has been cancelled."""


class _OutputTrace:
    """Bounded test-only memory capture; files are written after device close."""

    columns = ('entered_at', 'recorded_at', 'current_time', 'output_dac_time', 'frames', 'filled',
               'source_begin', 'source_end', 'gain', 'queue_chunks', 'queue_samples',
               'output_underflow', 'software_starvation', 'priming_output', 'rebuffering', 'input_done',
               'drain_reason', 'source_accepted_samples', 'startup_priming')

    def __init__(self, directory, playback_id, request_id, audio_id):
        self.path = Path(directory) / hashlib.sha256(playback_id.encode()).hexdigest()[:24]
        self.info = dict(playback_id=playback_id, request_id=request_id, audio_id=audio_id,
                         seconds_cap=30, callback_cap=16384, starts=[])
        self.rows = np.empty((16384, len(self.columns)), dtype='<f8')
        self.row_count = self.source_count = self.output_count = self.consumed = 0
        self.device_underflows = self.starvations = 0
        self.source = self.output = None
        self.invalid = False
        self.flushed = False

    def write(self, chunk, rate):
        if self.source is None:
            self.source = np.empty(rate * 30, dtype='<f4')
            self.output = np.empty(rate * 30, dtype='<f4')
            self.info['sample_rate'] = rate
        n = max(0, min(len(chunk), len(self.source) - self.source_count))
        self.source[self.source_count:self.source_count + n] = chunk[:n]
        self.source_count += len(chunk)
        self.invalid |= n != len(chunk)

    def snapshot(self, player):
        # Snapshot while the original output-fill lock is still held.
        return (player._gain, len(player._pending),
                max(0, sum(map(len, player._pending)) - player._offset),
                player._rebuffering, player._input_done, self.source_count)

    def callback(self, outdata, frames, filled, timing, status, snapshot, entered_at,
                 reason, starved, startup_priming=False):
        # Immutable fill-lock metadata; no file, logging or hash work here.
        gain, queue_chunks, queue_samples, rebuffering, input_done, accepted = snapshot
        device_underflow = bool(getattr(status, 'output_underflow', False))
        self.device_underflows += device_underflow
        self.starvations += starved
        n = max(0, min(frames, len(self.output) - self.output_count))
        self.output[self.output_count:self.output_count + n] = outdata[:n, 0]
        self.output_count += frames
        if self.row_count < len(self.rows):
            self.rows[self.row_count] = (
                entered_at, monotonic(), getattr(timing, 'currentTime', float('nan')),
                getattr(timing, 'outputBufferDacTime', float('nan')), frames, filled,
                self.consumed, self.consumed + filled, gain, queue_chunks, queue_samples,
                device_underflow, starved, bool(getattr(status, 'priming_output', False)),
                rebuffering, input_done, reason, accepted, startup_priming)
        self.row_count += 1
        self.consumed += filled
        self.invalid |= n != frames or self.row_count > len(self.rows)

    def device(self, stream, api, requested):
        # Device queries and metadata allocation happen outside the callback.
        self.info['requested_device'] = requested
        try:
            index = stream.device
            device = dict(api.query_devices(index))
            self.info.update(resolved_device=index, device=device,
                             hostapi=dict(api.query_hostapis(device['hostapi'])),
                             stream_latency=stream.latency, stream_sample_rate=stream.samplerate)
        except Exception as error:
            self.info['device_query_error'] = type(error).__name__ + ': ' + str(error)

    def flush(self, state, underruns):
        if self.flushed:
            return
        self.flushed = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        files = {}
        for suffix, data in (
                ('source.f32', np.empty(0, '<f4') if self.source is None else self.source[:self.source_count]),
                ('callback.f32', np.empty(0, '<f4') if self.output is None else self.output[:self.output_count]),
                ('callbacks.f64', self.rows[:self.row_count])):
            path = self.path.with_suffix('.' + suffix)
            with path.open('xb') as stream:
                stream.write(data.tobytes())
            files[suffix] = dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.info.update(state=state, valid=not self.invalid, truncated=self.invalid,
                         source_samples=self.source_count, callback_samples=self.output_count,
                         source_consumed_samples=self.consumed, callback_count=self.row_count,
                         source_saved_samples=0 if self.source is None else min(self.source_count, len(self.source)),
                         callback_saved_samples=0 if self.output is None else min(self.output_count, len(self.output)),
                         callback_saved_count=min(self.row_count, len(self.rows)),
                         callback_columns=self.columns, underruns=underruns, files=files,
                         device_underflow_callbacks=self.device_underflows,
                         software_starvation_callbacks=self.starvations,
                         native_alsa_recoveries='Not counted here; correlate native process log separately',
                         scope='Accepted source PCM and callback output; not proof of DAC or acoustic delivery')
        with self.path.with_suffix('.json').open('x') as stream:
            json.dump(self.info, stream, indent=2)
            stream.write('\n')


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
        self._rebuffering = False
        self._pause_requested = False
        self._paused = False
        self._gain = 1.0
        self._gain_until = 0.0
        self._started = False
        self._drained = False
        self._drain_reason = None
        self._error = None
        self._complete = False
        self._underruns = 0
        self._closing = Event()
        self._done = Event()
        self._sd = None
        self._trace = None
        self._startup_pending = True
        self._startup_retry_requested = False
        self._startup_retried = False
        self._startup_latency = None
        self._startup_deadline = None
        self._startup_ready_at = None
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def enable_output_trace(self, directory, playback_id, request_id, audio_id):
        with self._condition:
            if self._pending or self._started:
                raise RuntimeError('Output trace must begin before the first PCM write')
            self._trace = _OutputTrace(directory, playback_id, request_id, audio_id)

    def flush_output_trace(self, state):
        if not self._done.is_set():
            raise RuntimeError('Output trace requires a closed device')
        if self._trace is not None:
            self._trace.flush(state, self.underruns)

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
                # A full queue of short local sentences must still drain;
                # otherwise the producer cannot reach finish().
                self._rebuffering = False
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
                self._rebuffering = False
                self._condition.wait(0.05)
            self._check()
            self._pending.append(chunk)
            if self._trace is not None:
                self._trace.write(chunk, rate)
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

    def set_ducked(self, ducked):
        """Change upcoming PCM only; preserve queued samples and playback position."""
        with self._condition:
            if (self._done.is_set() or self._cancel.is_set()
                    or self._closing.is_set() or self._error is not None):
                return False
            self._gain = 0.05 if ducked else 1.0
            # Lost/reordered restore requests must never leave playback quiet.
            self._gain_until = monotonic() + 0.3 if ducked else 0.0
            return True

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
            self._gain = 1.0
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
        entered_at = monotonic() if self._trace is not None else None
        outdata.fill(0)
        filled = 0
        snapshot = None
        reason = 0
        starved = False
        startup_priming = False
        underflow = bool(getattr(status, 'output_underflow', False))
        try:
            with self._condition:
                if self._trace is not None:
                    snapshot = self._trace.snapshot(self)
                if self._cancel.is_set() or self._closing.is_set():
                    self._drain_reason = 'cancelled'
                    reason = 3
                    raise self._sd.CallbackAbort()
                if self._input_done and not self._pending:
                    self._drain_reason = 'finished'
                    reason = 1
                    raise self._sd.CallbackStop()
                if self._pause_requested:
                    self._drain_reason = 'paused'
                    reason = 2
                    raise self._sd.CallbackStop()
                if self._startup_retry_requested:
                    startup_priming = True
                    reason = 5
                    raise self._sd.CallbackAbort()
                if self._startup_pending:
                    startup_priming = True
                    now = monotonic()
                    if now >= self._startup_deadline:
                        if not self._startup_retried:
                            self._startup_retry_requested = True
                            self._condition.notify_all()
                            reason = 5
                            raise self._sd.CallbackAbort()
                        raise RuntimeError('Output startup did not settle within 250 ms.')
                    if underflow and self._startup_ready_at is not None:
                        self._startup_ready_at = now + self._startup_latency
                    if self._startup_ready_at is None or now < self._startup_ready_at:
                        # Device recovery must consume silence, not the speech prefix.
                        return
                    startup_priming = False
                if self._rebuffering:
                    buffered = sum(len(chunk) for chunk in self._pending) - self._offset
                    # Match the initial 400 ms cushion after actual starvation.
                    # Keep the stream open and leave queued PCM untouched.
                    if not self._input_done and buffered < max(frames, int(self._rate * 0.4)):
                        underflow = True
                        starved = True
                        return
                    self._rebuffering = False
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
                    starved = True
                    self._rebuffering = True
                if filled:
                    self._startup_pending = False
                if monotonic() >= self._gain_until:
                    self._gain = 1.0
                outdata[:filled] *= self._gain
                if self._trace is not None:
                    snapshot = self._trace.snapshot(self)
                self._condition.notify_all()
                if self._input_done and not self._pending:
                    self._drain_reason = 'finished'
                    reason = 1
                    raise self._sd.CallbackStop()
        except (self._sd.CallbackStop, self._sd.CallbackAbort):
            raise
        except BaseException as error:
            reason = 4
            with self._condition:
                self._error = error
                self._condition.notify_all()
            raise self._sd.CallbackAbort() from error
        finally:
            if underflow or self._trace is not None:
                with self._condition:
                    if underflow:
                        self._underruns += 1
                    if self._trace is not None:
                        self._trace.callback(outdata, frames, filled, timing, status,
                                             snapshot, entered_at, reason, starved,
                                             startup_priming)

    def _finished_callback(self):
        with self._condition:
            self._drained = True
            self._condition.notify_all()

    def _run(self):
        stream = None
        inactive_seen = False
        retry_start = False
        try:
            while True:
                action = None
                with self._condition:
                    if (self._cancel.is_set() or self._closing.is_set()
                            or self._error is not None):
                        break
                    if self._startup_retry_requested:
                        action = 'retry'
                    elif stream is None and self._pending:
                        if self._pause_requested:
                            if not self._paused:
                                self._paused = True
                                action = 'paused'
                        else:
                            self._paused = False
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
                        if (self._startup_pending
                                and not self._pause_requested
                                and monotonic() >= self._startup_deadline):
                            if self._startup_retried:
                                raise RuntimeError('Output startup did not settle within 250 ms.')
                            self._startup_retry_requested = True
                            action = 'retry'
                        elif not stream.active:
                            if inactive_seen:
                                raise RuntimeError(
                                    'Output became inactive without drain.')
                            inactive_seen = True
                        else:
                            inactive_seen = False
                    if action is None:
                        self._condition.wait(0.05)
                        continue
                if action == 'retry':
                    # Keep old callbacks in zero/abort mode until close has joined them.
                    failed_stream, stream = stream, None
                    try:
                        if not failed_stream.stopped:
                            failed_stream.abort(ignore_errors=False)
                    finally:
                        failed_stream.close(ignore_errors=False)
                    with self._condition:
                        self._startup_retried = True
                        self._startup_retry_requested = False
                        self._drained = False
                        self._drain_reason = None
                        self._startup_ready_at = None
                        self._startup_deadline = None
                    if self._trace is not None:
                        self._trace.info['startup_retry'] = dict(
                            cause='startup_unsettled_250ms_before_source', closed_at=monotonic())
                    inactive_seen = False
                    retry_start = True
                    continue
                if action == 'create':
                    self._sd = importlib.import_module('sounddevice')
                    stream = self._sd.OutputStream(
                        samplerate=self._rate, channels=1, dtype='float32',
                        blocksize=0, latency='high', device=self._device,
                        callback=self._callback,
                        finished_callback=self._finished_callback,
                    )
                    self._startup_latency = float(stream.latency)
                    if not np.isfinite(self._startup_latency) or self._startup_latency <= 0:
                        raise RuntimeError('Output device did not report a positive latency.')
                    if self._trace is not None:
                        self._trace.device(stream, self._sd, self._device)
                    action = 'start'
                if action == 'start' and retry_start:
                    with self._condition:
                        if self._pause_requested:
                            self._paused = self._drained = True
                            self._drain_reason = 'paused'
                            action = 'paused'
                        else:
                            # Commit the new start with initial-start pause eligibility.
                            self._started = False
                if action == 'start':
                    if self._cancel.is_set() or self._closing.is_set():
                        break
                    start_at = monotonic() if self._trace is not None else None
                    with self._condition:
                        if self._startup_pending:
                            self._startup_deadline = monotonic() + 0.25
                            self._startup_ready_at = None
                    stream.start()
                    if self._trace is not None:
                        starts = self._trace.info['starts']
                        if len(starts) < 32:
                            starts.append(dict(entered_at=start_at, returned_at=monotonic(),
                                               startup_attempt=2 if self._startup_retried else 1,
                                               internal_retry=retry_start,
                                               stream_latency=self._startup_latency))
                        else:
                            self._trace.invalid = True
                    inactive_seen = False
                    with self._condition:
                        if self._startup_pending:
                            self._startup_ready_at = monotonic() + self._startup_latency
                        self._started = True
                    if (not retry_start and not self._cancel.is_set()
                            and not self._closing.is_set()):
                        self._on_state('playing')
                    retry_start = False
                elif (action == 'paused' and not self._cancel.is_set()
                      and not self._closing.is_set()):
                    # CallbackStop makes a stream inactive, but PortAudio
                    # requires stop() before that stream can start again.
                    if stream is not None and not stream.stopped:
                        stream.stop(ignore_errors=False)
                    retry_start = False
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
