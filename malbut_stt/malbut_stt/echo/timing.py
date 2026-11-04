"""Opt-in, content-free timing evidence for the isolated AEC capture path.

Nothing is opened unless MALBUT_AEC_TRACE_PATH is set. One append writes one
bounded JSON line; capture threads only attach numbers to their existing queue
messages. Observability failures must never affect audio or its age guard.
"""

import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
import time


class TimingTrace:
    MAX_RECORDS = 2048
    MAX_BYTES = 2 * 1024 * 1024
    MAX_LINE_BYTES = 4096

    def __init__(self, role):
        self.role = role
        self.fd = None
        self.records = self.bytes = 0
        self.last_sample = self.last_slow = float('-inf')
        self.closed = False
        self.dropped = 0
        self.pending = Queue(maxsize=32)
        self.finish = Event()
        self.writer = None
        path = os.environ.get('MALBUT_AEC_TRACE_PATH', '')
        if not path:
            return
        try:
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                              | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            self.instance = f'{os.getpid()}-{time.monotonic_ns()}'
            self.writer = Thread(target=self._flush, name='aec-timing-trace', daemon=True)
            self.writer.start()
            sources = {}
            for name in ('timing.py', 'pulse.py', 'worker.py', 'recorder.py', 'scheduling.py'):
                sources[name] = hashlib.sha256(
                    Path(__file__).with_name(name).read_bytes()).hexdigest()
            self._write('trace_open', {
                'clock': 'same_host_monotonic', 'source_sha256': sources,
                'pcm_retained': False, 'sample_period_s': 1.0,
                'slow_sample_period_s': 0.2, 'slow_age_s': 0.2,
            }, force=True)
        except Exception:
            self._disable()

    @property
    def enabled(self):
        return self.fd is not None and not self.closed

    def _disable(self):
        self.finish.set()
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def record(self, event, metrics, *, slow=False, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        if not force:
            if now - self.last_sample >= 1.0:
                self.last_sample = now
            elif slow and now - self.last_slow >= 0.2:
                self.last_slow = now
            else:
                return
        # Callers supply timing scalars only. Explicitly reject PCM, arbitrary
        # objects and non-finite values rather than serializing their repr.
        safe = {}
        for key, value in metrics.items():
            if not (key.endswith(('_s', '_ns', '_seq', '_index', '_depth'))
                    or key in ('ref_offset', 'frame_samples', 'exception_type',
                               'failure_reason', 'metadata_present',
                               'reference_hold', 'processor_active',
                               'priming_historical_frames', 'priming_backlog_frames')):
                continue
            if isinstance(value, bool) or value is None:
                safe[key] = value
            elif isinstance(value, (int, float)) and math.isfinite(value):
                safe[key] = value
            elif key in ('exception_type', 'failure_reason') and isinstance(value, str):
                safe[key] = value[:96]
        self._write(event, safe, force=force)

    def _write(self, event, metrics, *, force):
        if not self.enabled:
            return
        # Leave two bounded slots for failure/close after normal samples fill
        # the budget. Sampling and all limits are local to each process.
        reserve = 0 if force else 2
        if (self.records >= self.MAX_RECORDS - reserve
                or self.bytes >= self.MAX_BYTES - reserve * self.MAX_LINE_BYTES):
            return
        try:
            record = dict(schema_version=1, event=event, role=self.role,
                          pid=os.getpid(), instance=self.instance,
                          mono_ns=time.monotonic_ns(), metrics=metrics)
            raw = (json.dumps(record, separators=(',', ':'), allow_nan=False)
                   + '\n').encode('utf-8')
            if len(raw) > self.MAX_LINE_BYTES or self.bytes + len(raw) > self.MAX_BYTES:
                return
            try:
                self.pending.put_nowait(raw)
            except Full:
                if not force:
                    self.dropped += 1
                    return
                # A full diagnostic queue must not hide the failure that ends
                # the run. Evict an old sample, never wait on the writer.
                try:
                    self.pending.get_nowait()
                    self.dropped += 1
                except Empty:
                    pass  # The writer freed space after the failed put.
                if event == 'trace_closed':
                    record['metrics']['dropped_records'] = self.dropped
                    raw = (json.dumps(record, separators=(',', ':'), allow_nan=False)
                           + '\n').encode('utf-8')
                try:
                    self.pending.put_nowait(raw)
                except Full:
                    self.dropped += 1
                    return
            self.records += 1
            self.bytes += len(raw)
        except Exception:
            self._disable()

    def _flush(self):
        # File I/O never runs on the capture/processing/parent read path.
        try:
            while not self.finish.is_set() or not self.pending.empty():
                try:
                    raw = self.pending.get(timeout=0.05)
                except Empty:
                    continue
                if self.fd is None or os.write(self.fd, raw) != len(raw):
                    break
        except Exception:
            pass
        finally:
            self._disable()

    def close(self):
        if self.closed:
            return
        self._write('trace_closed', {'dropped_records': self.dropped}, force=True)
        self.closed = True
        self.finish.set()
        if self.writer is not None:
            try:
                self.writer.join(timeout=0.2)
            except Exception:
                self._disable()
        else:
            self._disable()


def queue_depth(queue):
    """Diagnostic only: qsize is approximate and may be unsupported."""
    try:
        return queue.qsize()
    except Exception:
        return None


def unpack_message(message):
    """Keep legacy worker tuples compatible with optional timing metadata."""
    if len(message) == 3:
        kind, payload, captured_at = message
        return kind, payload, captured_at, None
    if len(message) == 4 and isinstance(message[3], dict):
        return message
    raise ValueError('Invalid AEC worker message')
