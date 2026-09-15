"""Exercise device drain and cancellation without opening audio hardware."""

from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from malbut_tts.audio import PlaybackCancelled, StreamingPlayer


def wait_until(predicate):
    """Wait briefly for a device-owner operation or fail the test."""
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('The device operation did not complete.')
        time.sleep(0.002)


def fake_device():
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
    ), instances


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
        stream.step()
        self.assertTrue(stream.draining)
        self.assertEqual(self.player.underruns, 3)
        stream.drain()
        wait_until(lambda: bool(outcome))
        self.assertEqual(outcome, ['finished'])
        self.assertEqual(self.player.underruns, 3)
        with self.assertRaises(AttributeError):
            self.player.underruns = 0

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
