"""Check request ordering, cancellation, failures, and completion boundaries."""

from queue import Queue
from threading import Condition, Event

import pytest

from malbut_tts.runtime import DIALOGUE, NOTIFICATION, SpeechRuntime


class FakeSynthesizer:
    """Yield one distinguishable audio chunk for each original text."""

    def __init__(self):
        self.texts = []

    def generate(self, text, cancel_event):
        self.texts.append(text)
        if text == 'synthesis failure':
            raise ValueError('synthesis failed')
        if text != 'empty audio':
            yield text, 24000


class FakePlayer:
    """Hold device drain until the test explicitly releases it."""

    def __init__(self, on_state, cancel_event):
        self.on_state = on_state
        self.cancel = cancel_event
        self.audio = []
        self.drain = Event()
        self.finishing = Event()
        self.closed = Event()
        self.fail = False

    def write(self, audio, sample_rate):
        if self.cancel.is_set():
            raise RuntimeError('cancelled')
        self.audio.append((audio, sample_rate))
        self.on_state('playing')

    def finish(self):
        self.finishing.set()
        assert self.drain.wait(3), 'test did not release device drain'
        if self.fail:
            raise RuntimeError('device failed')

    def pause(self):
        self.on_state('paused')
        return True

    def resume(self):
        self.on_state('playing')
        return True

    def stop(self):
        self.cancel.set()
        self.drain.set()

    def close(self):
        self.closed.set()


class Harness:
    """Wait on observable events instead of guessing worker timing."""

    def __init__(self, synth=None):
        self.synth = synth or FakeSynthesizer()
        self.players = Queue()
        self.events = []
        self.condition = Condition()
        self.runtime = SpeechRuntime(
            self.synth, self.make_player, self.status,
        )

    def make_player(self, **kwargs):
        player = FakePlayer(**kwargs)
        self.players.put(player)
        return player

    def status(self, playback_id, state):
        with self.condition:
            self.events.append((playback_id, state))
            self.condition.notify_all()

    def wait(self, playback_id, state):
        with self.condition:
            assert self.condition.wait_for(
                lambda: (playback_id, state) in self.events, timeout=3,
            ), self.events

    def active(self, playback_id):
        player = self.players.get(timeout=3)
        self.wait(playback_id, 'playing')
        return player


@pytest.fixture
def h():
    """Always release an active fake device, including after assertion errors."""
    harness = Harness()
    try:
        yield harness
    finally:
        harness.runtime.close()


def test_priority_and_fifo_preserve_current_and_all_pending_requests(h):
    """An active notification finishes before queued dialogue, then notices."""
    first = h.runtime.submit('active notice', NOTIFICATION)
    player = h.active(first)
    pending = [
        h.runtime.submit('notice 1', NOTIFICATION),
        h.runtime.submit('dialogue 1', DIALOGUE),
        h.runtime.submit('dialogue 2', DIALOGUE),
        h.runtime.submit('notice 2', NOTIFICATION),
    ]
    assert h.synth.texts == ['active notice']
    player.drain.set()
    h.wait(first, 'finished')
    for playback_id in (pending[1], pending[2], pending[0], pending[3]):
        player = h.active(playback_id)
        player.drain.set()
        h.wait(playback_id, 'finished')
    assert h.synth.texts == [
        'active notice', 'dialogue 1', 'dialogue 2', 'notice 1', 'notice 2',
    ]
    assert len(set([first] + pending)) == 5


def test_finished_waits_for_full_device_drain_and_is_emitted_once(h):
    """Synthesis exhaustion alone must never start the STT silence timer."""
    pid = h.runtime.submit(' 문장 하나.\n문장 둘. ')
    player = h.active(pid)
    assert player.finishing.wait(3)
    assert h.events == [(pid, 'playing')]
    assert player.audio == [(' 문장 하나.\n문장 둘. ', 24000)]
    player.drain.set()
    h.wait(pid, 'finished')
    assert player.closed.is_set()
    player.on_state('playing')  # A stale device callback cannot revive it.
    assert h.events == [(pid, 'playing'), (pid, 'finished')]
    assert not h.runtime.control(pid, 'resume')
    assert not h.runtime.control(pid, 'stop')


def test_paused_request_blocks_even_higher_priority_and_resumes_same_id(h):
    """Pause retains the active job, its buffer, and all waiting jobs."""
    pid = h.runtime.submit('notice', NOTIFICATION)
    player = h.active(pid)
    assert h.runtime.control(pid, 'pause')
    h.wait(pid, 'paused')
    queued = h.runtime.submit('answer', DIALOGUE)
    assert not h.runtime.control(pid, 'pause')
    assert not h.runtime.control(queued, 'resume')
    assert h.players.empty()
    assert h.synth.texts == ['notice']
    assert h.runtime.control(pid, 'resume')
    assert h.events[:3] == [
        (pid, 'playing'), (pid, 'paused'), (pid, 'playing'),
    ]
    player.drain.set()
    h.wait(pid, 'finished')
    h.active(queued).drain.set()
    h.wait(queued, 'finished')


def test_stop_drops_active_audio_and_keeps_other_pending_requests(h):
    """Stop is terminal for its ID and never emits a normal completion."""
    pid = h.runtime.submit('old')
    player = h.active(pid)
    queued = h.runtime.submit('next')
    assert h.runtime.control(pid, 'pause')
    assert h.runtime.control(pid, 'stop')
    h.wait(pid, 'stopped')
    assert player.closed.is_set()
    assert not h.runtime.control(pid, 'resume')
    assert (pid, 'finished') not in h.events
    h.active(queued).drain.set()
    h.wait(queued, 'finished')


@pytest.mark.parametrize('text', ['synthesis failure', 'empty audio'])
def test_synthesis_failure_is_terminal_and_next_request_runs(h, text):
    """One failed request does not stall the request queue."""
    failed = h.runtime.submit(text)
    next_id = h.runtime.submit('next')
    h.wait(failed, 'failed')
    assert h.players.get(timeout=3).closed.is_set()
    h.active(next_id).drain.set()
    h.wait(next_id, 'finished')
    assert (failed, 'finished') not in h.events


def test_device_failure_is_not_finished(h):
    """A failed output stream does not produce a successful playback event."""
    pid = h.runtime.submit('answer')
    player = h.active(pid)
    player.fail = True
    player.drain.set()
    h.wait(pid, 'failed')
    assert (pid, 'finished') not in h.events


def test_stop_device_failure_keeps_service_response_and_reports_failed(h):
    """A failed device abort is not a successful stop or a lost response."""
    pid = h.runtime.submit('answer')
    player = h.active(pid)

    def failed_close():
        raise RuntimeError('device close failed')

    def failed_stop():
        player.cancel.set()
        player.drain.set()
        failed_close()

    player.stop = failed_stop
    player.close = failed_close
    assert h.runtime.control(pid, 'stop')
    h.wait(pid, 'failed')
    assert (pid, 'stopped') not in h.events
    assert (pid, 'finished') not in h.events


def test_blank_invalid_kind_and_invalid_controls_do_not_change_state(h):
    """Ignore unusable input and reject a control that cannot be performed."""
    for text in ('', ' \n\t', None):
        assert h.runtime.submit(text) is None
    for kind in (-1, 2, 'dialogue', True):
        assert h.runtime.submit('hello', kind) is None
    assert not h.runtime.control('missing', 'stop')
    pid = h.runtime.submit('answer')
    player = h.active(pid)
    for command in ('resume', 'invalid'):
        assert not h.runtime.control(pid, command)
    assert not h.runtime.control('missing', 'pause')
    assert h.events == [(pid, 'playing')]
    player.drain.set()
    h.wait(pid, 'finished')


def test_stop_during_generation_discards_late_chunks_and_closes_iterator():
    """A noninterruptible model call can return late without playing audio."""
    entered = Event()
    release = Event()
    closed = Event()

    class SlowSynthesizer:
        def generate(self, text, cancel_event):
            try:
                entered.set()
                assert release.wait(3)
                yield 'late audio', 24000
            finally:
                closed.set()

    h = Harness(SlowSynthesizer())
    try:
        pid = h.runtime.submit('old')
        assert entered.wait(3)
        assert not h.runtime.control(pid, 'pause')
        assert h.runtime.control(pid, 'stop')
        release.set()
        h.wait(pid, 'stopped')
        player = h.players.get(timeout=3)
        assert player.audio == []
        assert closed.is_set()
        assert h.events == [(pid, 'stopped')]
    finally:
        release.set()
        h.runtime.close()


def test_validation_is_rechecked_before_first_audio(h):
    """The notebook Agent bridge can revoke an answer before it is spoken."""
    def validate():
        raise ValueError('answer no longer valid')

    pid = h.runtime.submit('answer', validate=validate)
    h.wait(pid, 'failed')
    assert h.players.get(timeout=3).audio == []
    assert h.events == [(pid, 'failed')]


def test_close_cancels_active_and_drops_pending(h):
    """Shutdown cannot start another waiting voice or accept another job."""
    pid = h.runtime.submit('active')
    h.active(pid)
    h.runtime.submit('pending')
    h.runtime.close()
    assert h.events[-1] == (pid, 'stopped')
    assert h.synth.texts == ['active']
    assert h.runtime.submit('too late') is None
