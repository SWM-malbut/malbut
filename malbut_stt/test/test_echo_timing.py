"""Timing evidence must neither change audio freshness nor block fail-closed AEC."""

import ctypes
import json
import os
from queue import Empty, Full, Queue
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.echo import pulse, recorder, timing


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_disabled_trace_does_not_open_file_or_start_writer(monkeypatch):
    monkeypatch.delenv('MALBUT_AEC_TRACE_PATH', raising=False)
    monkeypatch.setattr(timing.os, 'open', lambda *a, **k: pytest.fail('unexpected file I/O'))
    trace = timing.TimingTrace('test')
    trace.record('sample', {'parent_age_s': 0.1}, force=True)
    trace.close()
    assert not trace.enabled
    assert trace.writer is None


def test_trace_contains_source_proof_and_scalars_but_not_pcm(monkeypatch, tmp_path):
    path = tmp_path / 'timing.jsonl'
    monkeypatch.setenv('MALBUT_AEC_TRACE_PATH', str(path))
    trace = timing.TimingTrace('worker_aec')
    trace.record('worker_chunk', dict(mic_seq=7, captured_at_s=12.0,
        reference_hold=125, processor_active=True,
        priming_historical_frames=80, priming_backlog_frames=4, pcm=b'sensitive audio',
        secret='not exported', bad_s=float('nan'), object_s=object()), force=True)
    trace.close()
    rows = read_rows(path)
    assert [r['event'] for r in rows] == ['trace_open', 'worker_chunk', 'trace_closed']
    assert set(rows[0]['metrics']['source_sha256']) == {
        'timing.py', 'pulse.py', 'worker.py', 'recorder.py', 'scheduling.py'}
    assert all(len(v) == 64 for v in rows[0]['metrics']['source_sha256'].values())
    assert rows[1]['metrics'] == dict(mic_seq=7, captured_at_s=12.0,
        reference_hold=125, processor_active=True,
        priming_historical_frames=80, priming_backlog_frames=4)
    assert rows[-1]['metrics']['dropped_records'] == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert all(len(line.encode()) <= trace.MAX_LINE_BYTES for line in path.read_text().splitlines())


def test_trace_refuses_symlink_without_affecting_capture(monkeypatch, tmp_path):
    original = tmp_path / 'existing'
    original.write_text('unchanged')
    link = tmp_path / 'trace'
    link.symlink_to(original)
    monkeypatch.setenv('MALBUT_AEC_TRACE_PATH', str(link))
    trace = timing.TimingTrace('test')
    assert not trace.enabled
    trace.record('parent_failure', {'parent_age_s': 1}, force=True)
    trace.close()
    assert original.read_text() == 'unchanged'


def test_writer_start_failure_does_not_mask_audio_cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv('MALBUT_AEC_TRACE_PATH', str(tmp_path / 'timing'))
    def fail_start(self):
        raise RuntimeError('thread resource unavailable')
    monkeypatch.setattr(timing.Thread, 'start', fail_start)
    trace = timing.TimingTrace('test')
    assert not trace.enabled
    trace.close()  # Joining an unstarted writer must not escape cleanup.


def test_sampling_limits_hot_path_and_force_keeps_failure(monkeypatch, tmp_path):
    path = tmp_path / 'timing'
    monkeypatch.setenv('MALBUT_AEC_TRACE_PATH', str(path))
    trace = timing.TimingTrace('test')
    now = [10.0]
    monkeypatch.setattr(timing.time, 'monotonic', lambda: now[0])
    for _ in range(100):
        trace.record('normal', {'parent_age_s': .1})
    now[0] = 10.1
    for _ in range(100):
        trace.record('slow', {'parent_age_s': .3}, slow=True)
    now[0] = 10.31
    trace.record('slow', {'parent_age_s': .3}, slow=True)
    trace.record('parent_failure', {'parent_age_s': .401}, force=True)
    trace.close()
    events = [x['event'] for x in read_rows(path)]
    assert events.count('normal') == 1
    assert events.count('slow') == 2
    assert events.count('parent_failure') == 1


def test_full_diagnostic_queue_preserves_failure_without_waiting(monkeypatch, tmp_path):
    monkeypatch.delenv('MALBUT_AEC_TRACE_PATH', raising=False)
    trace = timing.TimingTrace('test')
    trace.fd = os.open(tmp_path / 'file', os.O_WRONLY | os.O_CREAT, 0o600)
    trace.instance = 'test'
    trace.pending = Queue(maxsize=1)
    try:
        trace.record('sample', {'parent_age_s': .1}, force=True)
        trace.record('parent_failure', {'parent_age_s': .5}, force=True)
        assert trace.dropped == 1
        assert json.loads(trace.pending.get_nowait())['event'] == 'parent_failure'
    finally:
        trace._disable()


def test_close_records_its_own_eviction_when_diagnostic_queue_is_full(monkeypatch, tmp_path):
    monkeypatch.delenv('MALBUT_AEC_TRACE_PATH', raising=False)
    trace = timing.TimingTrace('test')
    trace.fd = os.open(tmp_path / 'file', os.O_WRONLY | os.O_CREAT, 0o600)
    trace.instance = 'test'
    trace.pending = Queue(maxsize=1)
    trace.record('sample', {'parent_age_s': .1}, force=True)
    trace.close()
    terminal = json.loads(trace.pending.get_nowait())
    assert terminal['event'] == 'trace_closed'
    assert terminal['metrics']['dropped_records'] == 1


def test_forced_terminal_survives_writer_draining_between_full_and_evict(monkeypatch, tmp_path):
    monkeypatch.delenv('MALBUT_AEC_TRACE_PATH', raising=False)
    trace = timing.TimingTrace('test')
    trace.fd = os.open(tmp_path / 'file', os.O_WRONLY | os.O_CREAT, 0o600)
    trace.instance = 'test'
    class DrainedQueue:
        first = True
        value = None
        def put_nowait(self, value):
            if self.first:
                self.first = False
                raise Full()
            self.value = value
        def get_nowait(self):
            raise Empty()  # Writer consumed the formerly full queue.
    trace.pending = DrainedQueue()
    trace.close()
    terminal = json.loads(trace.pending.value)
    assert terminal['event'] == 'trace_closed'
    assert terminal['metrics']['dropped_records'] == 0


def test_diagnostic_budget_keeps_failure_and_close_slots(monkeypatch, tmp_path):
    path = tmp_path / 'timing'
    monkeypatch.setenv('MALBUT_AEC_TRACE_PATH', str(path))
    trace = timing.TimingTrace('test')
    trace.MAX_RECORDS = 4  # open + one ordinary sample + failure + close
    trace.record('sample', {'parent_age_s': .1})
    trace.last_sample = float('-inf')
    trace.record('sample', {'parent_age_s': .1})
    trace.record('parent_failure', {'parent_age_s': .5}, force=True)
    trace.close()
    assert [r['event'] for r in read_rows(path)] == [
        'trace_open', 'sample', 'parent_failure', 'trace_closed']


def test_pulse_timing_preserves_pcm_timestamp_and_separate_intervals(monkeypatch):
    cap = pulse.Capture.__new__(pulse.Capture)
    cap.trace_timing = True
    cap.last_timing = None
    cap.error = ctypes.c_int()
    cap.handle = None
    cap.buf = np.array([16384, -16384], dtype='<i2')
    cap.lib = SimpleNamespace(pa_simple_read=lambda *a: 0,
                             pa_simple_get_latency=lambda *a: 125000)
    ticks = iter([10.0, 10.032, 10.033, 10.035, 10.036])
    monkeypatch.setattr(pulse.time, 'monotonic', lambda: next(ticks))
    samples, captured_at, latency = cap.read()
    assert samples.tolist() == [.5, -.5]
    assert captured_at == pytest.approx(10.036 - .125)
    assert latency == .125
    assert cap.last_timing['capture_read_s'] == pytest.approx(.032)
    assert cap.last_timing['latency_query_s'] == pytest.approx(.002)
    assert cap.last_timing['timestamp_anchor_s'] == 10.036


def test_pulse_trace_off_keeps_one_timestamp_call(monkeypatch):
    cap = pulse.Capture.__new__(pulse.Capture)
    cap.trace_timing = False
    cap.last_timing = None
    cap.error = ctypes.c_int()
    cap.handle = None
    cap.buf = np.zeros(512, dtype='<i2')
    cap.lib = SimpleNamespace(pa_simple_read=lambda *a: 0,
                             pa_simple_get_latency=lambda *a: 100000)
    calls = []
    monkeypatch.setattr(pulse.time, 'monotonic', lambda: calls.append(True) or 5.)
    _, at, latency = cap.read()
    assert (at, latency) == (4.9, .1)
    assert calls == [True]
    assert cap.last_timing is None


@pytest.mark.parametrize('metadata', [None, {'mic_seq': 3, 'timestamp_anchor_s': 1.25,
    'pulse_latency_s': .05, 'worker_publish_begin_s': 1.3}])
@pytest.mark.parametrize('age,accepted', [(0.399, True), (0.401, False)])
def test_metadata_cannot_bypass_existing_age_guard(monkeypatch, tmp_path, metadata, age, accepted):
    path = tmp_path / 'timing'
    monkeypatch.setenv('MALBUT_AEC_TRACE_PATH', str(path))
    reads = []
    raw = SimpleNamespace(start=lambda: reads.append('raw-start'), read=lambda: reads.append('raw-read'))
    r = recorder.EchoCancelledRecorder({}, lambda: raw)
    r.aec_ready = True
    r._queue = Queue()
    payload = (b'\x01\x00' * 512, bytes(1024))
    message = ('audio', payload, 1.2)
    if metadata is not None:
        message += (metadata,)
    r._queue.put(message)
    monkeypatch.setattr(recorder, 'monotonic', lambda: 1.2 + age)
    result = r.read()
    r._trace.close()
    assert r.aec_ready is accepted
    assert reads == []  # No raw PCM before the dialogue owner acknowledges.
    assert r.last_timing['captured_at_s'] == 1.2
    assert r.last_timing['parent_age_s'] == pytest.approx(age)
    assert r.last_timing['metadata_present'] is (metadata is not None)
    if metadata is not None:
        assert r.last_timing['pulse_latency_s'] + r.last_timing['after_pulse_anchor_s'] == pytest.approx(age)
    if accepted:
        assert bytes(result) == payload[0]
        assert result.vad_pcm == payload[1]
        assert r.failure_timing is None
    else:
        assert result == [0] * 512
        assert r.failure == 'RuntimeError: AEC capture backlog'
        assert r.failure_timing == r.last_timing
        failure = next(row for row in read_rows(path) if row['event'] == 'parent_failure')
        assert failure['metrics']['parent_age_s'] == pytest.approx(age)
        assert failure['metrics']['failure_reason'] == 'capture_backlog'


def test_optional_metadata_does_not_change_worker_protocol_validation():
    assert timing.unpack_message(('audio', b'x', 1.0)) == ('audio', b'x', 1.0, None)
    assert timing.unpack_message(('audio', b'x', 1.0, {'mic_seq': 4}))[3] == {'mic_seq': 4}
    with pytest.raises(ValueError, match='worker message'):
        timing.unpack_message(('audio', b'x', 1.0, b'not metadata'))


def test_diagnostic_queue_depth_failure_is_not_an_audio_error():
    class Unsupported:
        def qsize(self):
            raise ValueError('queue unavailable')
    assert timing.queue_depth(Unsupported()) is None
