"""Initial device recovery preserves source; no sound hardware is accessed."""
from contextlib import contextmanager
from threading import Event, Thread
import time
from unittest.mock import patch
import numpy as np
import pytest
from malbut_tts.audio import StreamingPlayer
from test_audio import fake_device, wait_until


@contextmanager
def running(tmp_path, *, latency=.035, early=False, traced=True):
    api, devices = fake_device()
    api.OutputStream.latency = latency
    clock = [1.]
    states = []
    original_start = api.OutputStream.start
    def start(device):
        original_start(device)
        if early:
            device.step()
    api.OutputStream.start = start
    with patch.dict('sys.modules', {'sounddevice': api}), patch(
            'malbut_tts.audio.monotonic', lambda: clock[0]):
        player = StreamingPlayer(states.append, Event())
        if traced:
            player.enable_output_trace(tmp_path, 'pid', 'rid', '')
        player.write(np.arange(1, 17, dtype=np.float32), 24000)
        try:
            wait_until(lambda: states or player._done.is_set())
            yield player, devices[0], clock, states
        finally:
            try:
                player.close()
            except RuntimeError:
                pass


def test_no_source_before_start_returns_then_one_real_latency(tmp_path):
    with running(tmp_path, early=True) as (p, d, now, states):
        assert d.output == [0.] * 4 and p._offset == 0
        now[0] = 1.034; d.step()
        assert d.output == [0.] * 8 and p._offset == 0
        now[0] = 1.035; d.step()
        assert d.output[-4:] == [1., 2., 3., 4.]
        assert not p._startup_pending and p.underruns == 0
        assert states == ['playing'] and d.options['latency'] == 'high'


def test_repeated_early_device_faults_consume_only_zeroes(tmp_path):
    with running(tmp_path) as (p, d, now, _):
        for t in [1., 1.002, 1.005, 1.024]:
            now[0] = t; d.step(underflow=True)
            assert p._offset == 0 and d.output[-4:] == [0.] * 4
        now[0] = 1.058; d.step()
        assert p._offset == 0
        now[0] = 1.060; d.step()
        assert d.output[-4:] == [1., 2., 3., 4.] and p.underruns == 4
        assert p._trace.starvations == 0


def test_normal_fast_callback_batches_do_not_restart_clock(tmp_path):
    with running(tmp_path) as (p, d, now, _):
        for t in [1., 1.0001, 1.0002, 1.0003, 1.0004, 1.002]:
            now[0] = t; d.step()
        now[0] = 1.035; d.step()
        assert p._offset == 4 and d.output[-4:] == [1., 2., 3., 4.]


def test_device_fault_after_first_source_never_rewinds_or_reprimes(tmp_path):
    with running(tmp_path) as (p, d, now, _):
        now[0] = 1.035; d.step()
        now[0] = 1.036; d.step(underflow=True)
        assert d.output == list(map(float, range(1, 9)))
        assert p._offset == 8 and not p._startup_pending and p.underruns == 1
        assert p._trace.device_underflows == 1 and p._trace.starvations == 0


@pytest.mark.parametrize('callback', [True, False])
def test_second_250ms_limit_fails_without_consuming_source(tmp_path, callback):
    with running(tmp_path) as (p, d, now, _):
        p._startup_retried = True
        for t in [1., 1.05, 1.10, 1.15, 1.20, 1.24]:
            now[0] = t; d.step(underflow=True)
        now[0] = 1.251
        if callback:
            d.step()
        wait_until(lambda: p._done.is_set())
        assert p._error is not None and not p._complete
        assert p._offset == 0 and p._trace.consumed == 0
        assert '250 ms' in str(p._error)
        assert d.closed


def test_stop_while_priming_aborts_without_source(tmp_path):
    with running(tmp_path) as (p, d, now, _):
        d.step(); p.stop()
        assert p._trace.consumed == 0 and d.closed and d.aborted
        assert not p._complete


def test_pause_before_source_rearms_pending_gate_on_resume(tmp_path):
    with running(tmp_path) as (p, d, now, states):
        now[0] = 1.01
        assert p.pause(); d.step(); d.drain()
        wait_until(lambda: states[-1] == 'paused')
        now[0] = 2.
        assert p.resume()
        wait_until(lambda: states == ['playing', 'paused', 'playing'])
        d.step(); assert p._trace.consumed == 0
        now[0] = 2.036; d.step()
        assert d.output[-4:] == [1., 2., 3., 4.]


def test_pause_after_source_does_not_add_another_startup_wait(tmp_path):
    with running(tmp_path) as (p, d, now, states):
        now[0] = 1.035; d.step()
        assert p.pause(); d.step(); d.drain()
        wait_until(lambda: states[-1] == 'paused')
        now[0] = 2.; assert p.resume()
        wait_until(lambda: states == ['playing', 'paused', 'playing'])
        d.step()
        assert d.output[-4:] == [5., 6., 7., 8.]


def test_accepted_pause_wins_over_owner_timeout_before_drain_callback(tmp_path):
    with running(tmp_path) as (p, d, now, states):
        now[0] = 1.24; assert p.pause()
        now[0] = 1.30
        time.sleep(.065)  # Allow the existing 50 ms owner poll to inspect the pending pause.
        assert p._error is None and not p._done.is_set()
        d.step(); d.drain()
        wait_until(lambda: states[-1] == 'paused')
        assert p._trace.consumed == 0
        now[0] = 2.; assert p.resume()
        wait_until(lambda: states == ['playing', 'paused', 'playing'])
        d.step(); assert p._trace.consumed == 0
        now[0] = 2.036; d.step()
        assert d.output[-4:] == [1., 2., 3., 4.]


def test_finite_clip_drains_full_source_after_zero_priming(tmp_path):
    with running(tmp_path) as (p, d, now, _):
        outcome = []
        thread = Thread(target=lambda: (p.finish(), outcome.append('finished')))
        thread.start(); wait_until(lambda: p._input_done)
        d.step(frames=32)
        assert p._trace.consumed == 0 and not d.draining
        now[0] = 1.035; d.step(frames=32); d.drain(); thread.join(2)
        assert outcome == ['finished'] and p._complete
        assert d.output == [0.] * 32 + list(map(float, range(1, 17))) + [0.] * 16


def test_duck_during_priming_applies_only_to_source_at_release(tmp_path):
    with running(tmp_path) as (p, d, now, _):
        assert p.set_ducked(True); d.step()
        now[0] = 1.035; d.step()
        np.testing.assert_allclose(d.output[-4:], np.arange(1, 5) * .05)
        assert p._offset == 4


@pytest.mark.parametrize('latency', [0., float('nan'), float('inf')])
def test_unusable_device_latency_fails_before_start(tmp_path, latency):
    with running(tmp_path, latency=latency) as (p, d, _, states):
        assert p._error is not None and p._trace.consumed == 0
        assert d.starts == 0 and d.closed and states == []
