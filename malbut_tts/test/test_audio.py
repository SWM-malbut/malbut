"""Exercise device drain and cancellation without opening audio hardware."""

from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pytest

from malbut_tts.audio import PlaybackCancelled, StreamingPlayer


def wait_until(predicate):
    """Wait briefly for a device-owner operation or fail the test."""
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('The device operation did not complete.')
        time.sleep(0.002)


def fake_device(high_latency=0.0348):
    """Return a manually advanced callback device with explicit drain."""
    instances = []

    class CallbackStop(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    class OutputStream:
        def __init__(self, *, callback, finished_callback, **kwargs):
            self.callback = callback
            self.finished_callback = finished_callback
            self.options = kwargs
            self.output = []
            self.starts = 0
            self.draining = False
            self.active = False
            self.stopped = True
            self.closed = False
            self.aborted = False
            instances.append(self)

        def start(self):
            if not self.stopped:
                return
            self.starts += 1
            self.active = True
            self.stopped = False
            self.draining = False

        def step(self, frames=4, underflow=False):
            if not self.active or self.draining:
                raise AssertionError('Device callback is not active.')
            block = np.empty((frames, 1), dtype=np.float32)
            try:
                status = SimpleNamespace(output_underflow=underflow)
                self.callback(block, frames, None, status)
            except CallbackStop:
                self.draining = True
            except CallbackAbort:
                self.drain()
                return
            self.output.extend(block[:, 0].tolist())

        def drain(self):
            self.active = False
            self.finished_callback()

        def stop(self, ignore_errors=False):
            self.stopped = True

        def abort(self, ignore_errors=False):
            self.aborted = True
            self.stopped = True
            self.drain()

        def close(self, ignore_errors=False):
            self.closed = True

    return SimpleNamespace(
        OutputStream=OutputStream, CallbackStop=CallbackStop,
        CallbackAbort=CallbackAbort,
        query_devices=lambda device, kind: {'default_high_output_latency': high_latency},
    ), instances


@pytest.mark.parametrize('high_latency, expected', [(0.0348, 0.1), (0.1, 0.1), (0.2, 0.2)])
@pytest.mark.parametrize('device', [None, 17])
def test_output_latency_floor_preserves_device_high_latency(high_latency, expected, device):
    """Allow at least 100 ms without reducing the selected device's high latency."""
    api, instances = fake_device(high_latency)
    api.query_devices = lambda selected, kind: {
        'default_high_output_latency': high_latency if selected == device else 0.4,
    }
    with patch.dict('sys.modules', {'sounddevice': api}):
        player = StreamingPlayer(lambda state: None, Event(), device=device)
        try:
            player.write([1, 2, 3, 4], 24000)
            wait_until(lambda: instances and instances[0].starts == 1)
            stream = instances[0]
            requested = stream.options['latency']
            assert (high_latency if requested == 'high' else requested) == expected
            assert stream.options['device'] == device
            assert stream.options['blocksize'] == 0
        finally:
            player.stop()
            player.close()


class StreamingPlayerTests(unittest.TestCase):
    """Verify the observable PCM and playback-state contract."""

    def setUp(self):
        """Install a fake device and create a cancellable output player."""
        api, self.instances = fake_device()
        mocked = patch.dict('sys.modules', {'sounddevice': api})
        mocked.start()
        self.addCleanup(mocked.stop)
        self.states = []
        self.cancel = Event()
        self.player = StreamingPlayer(self.states.append, self.cancel)
        self.addCleanup(self.cleanup_player)

    def cleanup_player(self):
        """Release devices after assertions, including expected failures."""
        try:
            self.player.close()
        except RuntimeError:
            pass

    def start(self, values):
        """Queue the first audio and wait for the output stream to start."""
        self.player.write(values, 24000)
        wait_until(lambda: self.states == ['playing'])
        return self.instances[0]

    def finish_in_thread(self):
        """Wait for complete output without blocking the fake device driver."""
        outcome = []

        def finish():
            try:
                self.player.finish()
                outcome.append('finished')
            except Exception as error:
                outcome.append(error)

        thread = Thread(target=finish)
        thread.start()
        self.addCleanup(thread.join, 2)
        wait_until(lambda: self.player._input_done or bool(outcome))
        return outcome

    def test_starts_before_generation_ends_and_waits_for_device_drain(self):
        """PCM plays while more input is possible, and drain gates success."""
        stream = self.start([1, 2, 3, 4])
        self.assertEqual(stream.options['latency'], 0.1)
        stream.step()
        self.player.write([5, 6, 7, 8], 24000)
        outcome = self.finish_in_thread()
        stream.step()
        self.assertTrue(stream.draining)
        self.assertEqual(outcome, [])
        stream.drain()
        wait_until(lambda: bool(outcome))
        self.assertEqual(outcome, ['finished'])
        self.assertEqual(stream.output, list(range(1, 9)))
        self.assertTrue(stream.closed)
        self.assertFalse(stream.aborted or self.cancel.is_set())
        self.assertEqual(self.states, ['playing'])

    def test_pause_drains_then_resumes_exact_pcm_without_other_audio(self):
        """A partial chunk survives pause and finished waits across it."""
        stream = self.start(np.arange(1, 13, dtype=np.float32))
        stream.step()
        self.assertTrue(self.player.pause())
        self.assertFalse(self.player.pause())
        self.assertFalse(self.player.resume())
        stream.step()
        self.assertEqual(self.states, ['playing'])
        self.assertTrue(stream.draining)
        stream.drain()
        wait_until(lambda: self.states == ['playing', 'paused'])
        self.player.write([13, 14, 15, 16], 24000)
        outcome = self.finish_in_thread()
        self.assertEqual(outcome, [])
        self.assertEqual(stream.starts, 1)
        self.assertTrue(self.player.resume())
        self.assertFalse(self.player.resume())
        wait_until(lambda: self.states == ['playing', 'paused', 'playing'])
        for _ in range(3):
            stream.step()
        self.assertTrue(stream.draining)
        self.assertEqual(outcome, [])
        stream.drain()
        wait_until(lambda: bool(outcome))
        self.assertEqual(outcome, ['finished'])
        self.assertEqual([x for x in stream.output if x], list(range(1, 17)))
        self.assertEqual(stream.starts, 2)
        self.assertEqual(self.player.underruns, 0)

    def test_underruns_count_starvation_or_status_but_not_end_padding(self):
        """Distinguish starvation from intentional final buffer padding."""
        stream = self.start([1, 2, 3, 4])
        stream.step(frames=8)
        self.assertEqual(self.player.underruns, 1)
        self.player.write([5, 6, 7, 8], 24000)
        stream.step(frames=8, underflow=True)
        self.assertEqual(self.player.underruns, 2)
        self.player.write([9, 10, 11, 12], 24000)
        stream.step(underflow=True)
        self.assertEqual(self.player.underruns, 3)
        self.player.write([13, 14], 24000)
        outcome = self.finish_in_thread()
        stream.step(frames=12)
        self.assertTrue(stream.draining)
        self.assertEqual(self.player.underruns, 3)
        stream.drain()
        wait_until(lambda: bool(outcome))
        self.assertEqual(outcome, ['finished'])
        self.assertEqual(self.player.underruns, 3)
        with self.assertRaises(AttributeError):
            self.player.underruns = 0

    def test_starvation_waits_for_400ms_before_resuming_without_losing_pcm(self):
        """Small arrivals accumulate after starvation instead of stuttering."""
        stream = self.start([1, 2])
        stream.step()
        for value in (3, 4, 5):
            self.player.write(np.full(2400, value), 24000)
            stream.step(frames=2400)
            self.assertFalse(any(stream.output[-2400:]))
        self.player.write(np.full(2400, 6), 24000)
        stream.step(frames=9600)
        np.testing.assert_array_equal(
            stream.output[-9600:], np.repeat([3, 4, 5, 6], 2400))
        self.assertEqual(stream.starts, 1)
        self.assertEqual(self.states, ['playing'])
        outcome = self.finish_in_thread()
        stream.step()
        stream.drain()
        wait_until(lambda: bool(outcome))
        self.assertEqual(outcome, ['finished'])

    def test_rebuffering_preserves_pause_and_flushes_short_final_audio(self):
        """Pause still drains; finished input needs no minimum refill size."""
        stream = self.start([1, 2])
        stream.step()
        self.player.write([3, 4], 24000)
        self.assertTrue(self.player.pause())
        stream.step()
        stream.drain()
        wait_until(lambda: self.states[-1] == 'paused')
        outcome = self.finish_in_thread()
        self.assertEqual(outcome, [])
        self.assertTrue(self.player.resume())
        wait_until(lambda: self.states[-1] == 'playing')
        stream.step()
        self.assertTrue(stream.draining)
        stream.drain()
        wait_until(lambda: bool(outcome))
        self.assertEqual(outcome, ['finished'])
        self.assertEqual([x for x in stream.output if x], [1, 2, 3, 4])

    def test_full_short_sentence_queue_can_drain_during_rebuffering(self):
        """Refilling must not deadlock the local sentence producer's limit."""
        stream = self.start([1, 2])
        stream.step()
        self.player.write([3, 4], 24000)
        self.player.write([5, 6], 24000)
        available = Event()

        def reserve():
            self.player.wait_for_capacity(max_pending=2)
            available.set()

        thread = Thread(target=reserve)
        thread.start()
        self.addCleanup(thread.join, 2)
        wait_until(lambda: not self.player._rebuffering)
        stream.step()
        self.assertTrue(available.wait(1))
        self.assertEqual(stream.output[-4:], [3, 4, 5, 6])

    def test_stop_during_rebuffering_discards_queued_audio(self):
        """Cancellation never waits for the refill threshold."""
        stream = self.start([1, 2])
        stream.step()
        self.player.write([3, 4], 24000)
        self.player.stop()
        self.assertTrue(stream.aborted and stream.closed)
        self.assertEqual([x for x in stream.output if x], [1, 2])

    def test_cancellation_during_startup_discards_pcm_before_playing_state(self):
        """A final answer can cancel buffered progress before PLAYING arrives."""
        entered, release = Event(), Event()
        api, instances = fake_device()
        original_start = api.OutputStream.start

        def delayed_start(stream):
            original_start(stream)
            entered.set()
            assert release.wait(2)

        api.OutputStream.start = delayed_start
        with patch.dict('sys.modules', {'sounddevice': api}):
            try:
                self.player.write([1, 2, 3, 4], 24000)
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.states, [])
                self.cancel.set()
                stream = instances[0]
                stream.step()
                self.assertEqual(stream.output, [])
                release.set()
                wait_until(lambda: stream.closed)
                self.assertEqual(self.states, [])
                self.assertTrue(stream.aborted)
                self.assertEqual(list(self.player._pending), [])
            finally:
                release.set()

    def test_stop_unblocks_full_buffer_and_rejects_late_pcm(self):
        """Stopping a paused request discards pending and late audio."""
        stream = self.start([1, 2, 3, 4])
        self.assertTrue(self.player.pause())
        stream.step()
        stream.drain()
        wait_until(lambda: self.states[-1] == 'paused')
        for _ in range(31):
            self.player.write([5], 24000)
        entered = Event()
        outcome = []

        def blocked_write():
            entered.set()
            try:
                self.player.write([6], 24000)
            except Exception as error:
                outcome.append(error)

        thread = Thread(target=blocked_write)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(outcome, [])
        self.player.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], PlaybackCancelled)
        self.assertTrue(stream.stopped and stream.closed)
        self.assertFalse(self.player.resume())
        with self.assertRaises(PlaybackCancelled):
            self.player.write([7], 24000)
        self.assertFalse(any(stream.output))

    def test_external_cancellation_unblocks_finish_while_paused(self):
        """A shared cancellation event also stops the device owner."""
        stream = self.start([1, 2, 3, 4])
        self.player.pause()
        stream.step()
        stream.drain()
        wait_until(lambda: self.states[-1] == 'paused')
        outcome = self.finish_in_thread()
        self.cancel.set()
        wait_until(lambda: bool(outcome) and stream.closed)
        self.assertIsInstance(outcome[0], PlaybackCancelled)
        self.assertTrue(stream.stopped)

    def test_unexpected_device_finish_is_failure(self):
        """An unrequested device shutdown cannot count as successful drain."""
        stream = self.start([1, 2, 3, 4])
        stream.drain()
        wait_until(lambda: stream.closed)
        with self.assertRaisesRegex(RuntimeError, 'playback failed'):
            self.player.finish()
        self.assertFalse(self.cancel.is_set())
        self.assertTrue(stream.aborted)
        with self.assertRaisesRegex(RuntimeError, 'stop audio cleanly'):
            self.player.close()

    def test_inactive_device_without_finished_callback_does_not_hang(self):
        """Detect a lost device even when its finished callback is missing."""
        stream = self.start([1, 2, 3, 4])
        outcome = self.finish_in_thread()
        stream.active = False
        wait_until(lambda: bool(outcome) and stream.closed)
        self.assertIsInstance(outcome[0], RuntimeError)
        self.assertNotIsInstance(outcome[0], PlaybackCancelled)
        self.assertFalse(self.cancel.is_set())

    def test_stop_cleanup_error_remains_visible_after_thread_exits(self):
        """A failed device close cannot later be reported as a clean stop."""
        stream = self.start([1, 2, 3, 4])

        def failed_close(ignore_errors=False):
            raise RuntimeError('The device could not be closed.')

        stream.close = failed_close
        with self.assertRaisesRegex(RuntimeError, 'stop audio cleanly'):
            self.player.stop()
        with self.assertRaisesRegex(RuntimeError, 'stop audio cleanly'):
            self.player.close()

    def test_close_after_synthesis_failure_does_not_mark_user_stop(self):
        """Cleanup must preserve the runtime's distinction between outcomes."""
        stream = self.start([1, 2, 3, 4])
        self.player.close()
        self.assertFalse(self.cancel.is_set())
        self.assertTrue(stream.aborted and stream.closed)

    def test_invalid_pcm_is_rejected(self):
        """Malformed PCM and changed sample rates never enter output."""
        for values in ([], [[1]], [float('nan')]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.player.write(values, 24000)
        self.start([1])
        with self.assertRaisesRegex(ValueError, 'sample rate changed'):
            self.player.write([2], 16000)


def test_duck_changes_pcm_without_consuming_or_mutating_queued_audio():
    api, devices = fake_device()
    clock = [1.]
    states, cancel = [], Event()
    with patch.dict('sys.modules', {'sounddevice': api}), patch(
            'malbut_tts.audio.monotonic', lambda: clock[0]):
        player = StreamingPlayer(states.append, cancel)
        original = np.ones(24000, dtype=np.float32)
        try:
            player.write(original, 24000)
            wait_until(lambda: states == ['playing'])
            device = devices[0]
            assert player.set_ducked(True)
            device.step()
            assert device.output == [.25]*4
            assert player.set_ducked(False)
            device.step()
            assert device.output[4:] == [1.]*4
            assert player.set_ducked(True)
            clock[0] += .301  # Missing restore message still expires safely.
            device.step()
            assert device.output[8:] == [1.]*4
            np.testing.assert_array_equal(original, np.ones(24000))
            assert states == ['playing']
        finally:
            player.close()


def test_transient_portaudio_open_error_retries_and_plays_all_pcm():
    """Temporary USB/PulseAudio sink absence during open recovers without dropping PCM."""
    api, instances = fake_device()

    class PortAudioError(RuntimeError):
        pass

    api.PortAudioError = PortAudioError
    real_stream = api.OutputStream
    attempts = []

    def flaky_stream(**kwargs):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PortAudioError('Error opening OutputStream: No such entity')
        return real_stream(**kwargs)

    api.OutputStream = flaky_stream
    states, cancel = [], Event()
    with patch.dict('sys.modules', {'sounddevice': api}):
        player = StreamingPlayer(
            states.append, cancel, device='pulse',
            open_retry_timeout_s=1.0, open_retry_interval_s=0.01,
        )
        try:
            player.write([1, 2, 3, 4], 24000)
            wait_until(lambda: states == ['playing'])
            assert len(attempts) == 3
            stream = instances[0]
            outcome = []
            thread = Thread(target=lambda: outcome.append(player.finish()))
            thread.start()
            wait_until(lambda: player._input_done)
            stream.step()
            stream.drain()
            thread.join(2)
            assert not thread.is_alive()
            assert outcome == [None]
            assert stream.output == [1.0, 2.0, 3.0, 4.0]
        finally:
            player.close()


def test_transient_portaudio_start_error_closes_stream_and_retries():
    """A half-open stream that fails start() is closed before retrying."""
    api, instances = fake_device()

    class PortAudioError(RuntimeError):
        pass

    api.PortAudioError = PortAudioError
    original_start = api.OutputStream.start
    starts = []

    def flaky_start(stream):
        starts.append(stream)
        if len(starts) == 1:
            raise PortAudioError('device unavailable during start')
        original_start(stream)

    api.OutputStream.start = flaky_start
    states, cancel = [], Event()
    with patch.dict('sys.modules', {'sounddevice': api}):
        player = StreamingPlayer(
            states.append, cancel, device='pulse',
            open_retry_timeout_s=1.0, open_retry_interval_s=0.01,
        )
        try:
            player.write([1, 2, 3, 4], 24000)
            wait_until(lambda: states == ['playing'])
            assert len(instances) == 2
            assert instances[0].closed
            assert instances[1].active
        finally:
            player.stop()


def test_persistent_portaudio_open_error_fails_after_timeout_with_cause():
    """Permanent speaker loss stops retrying at the deadline and preserves the cause."""
    api, _ = fake_device()

    class PortAudioError(RuntimeError):
        pass

    api.PortAudioError = PortAudioError
    attempts = []

    def failing_stream(**kwargs):
        attempts.append(1)
        raise PortAudioError('No such entity')

    api.OutputStream = failing_stream
    states, cancel = [], Event()
    with patch.dict('sys.modules', {'sounddevice': api}):
        player = StreamingPlayer(
            states.append, cancel, device='pulse',
            open_retry_timeout_s=0.05, open_retry_interval_s=0.01,
        )
        player.write([1, 2, 3, 4], 24000)
        with pytest.raises(RuntimeError, match='playback failed') as exc_info:
            player.finish()
        assert isinstance(exc_info.value.__cause__, PortAudioError)
        assert len(attempts) >= 2
        assert states == []
        with pytest.raises(RuntimeError, match='stop audio cleanly') as close_exc:
            player.close()
        assert isinstance(close_exc.value.__cause__, PortAudioError)


def test_cancellation_during_open_retry_exits_promptly_without_error():
    """Stopping a request while waiting for USB audio never blocks for the full retry window."""
    api, _ = fake_device()

    class PortAudioError(RuntimeError):
        pass

    api.PortAudioError = PortAudioError
    entered = Event()

    def failing_stream(**kwargs):
        entered.set()
        raise PortAudioError('No such entity')

    api.OutputStream = failing_stream
    cancel = Event()
    with patch.dict('sys.modules', {'sounddevice': api}):
        player = StreamingPlayer(
            lambda state: None, cancel, device='pulse',
            open_retry_timeout_s=10.0, open_retry_interval_s=0.5,
        )
        player.write([1, 2, 3, 4], 24000)
        assert entered.wait(2)
        started = time.monotonic()
        player.stop()
        assert time.monotonic() - started < 1.0


def test_non_portaudio_open_error_fails_immediately_without_retry():
    """Configuration or programming errors never enter the transient device retry loop."""
    api, _ = fake_device()

    class PortAudioError(RuntimeError):
        pass

    api.PortAudioError = PortAudioError
    attempts = []

    def bad_stream(**kwargs):
        attempts.append(1)
        raise ValueError('invalid device specification')

    api.OutputStream = bad_stream
    with patch.dict('sys.modules', {'sounddevice': api}):
        player = StreamingPlayer(
            lambda state: None, Event(), device='bad',
            open_retry_timeout_s=5.0, open_retry_interval_s=0.1,
        )
        player.write([1, 2, 3, 4], 24000)
        with pytest.raises(RuntimeError, match='playback failed') as exc_info:
            player.finish()
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert len(attempts) == 1
        with pytest.raises(RuntimeError, match='stop audio cleanly'):
            player.close()
