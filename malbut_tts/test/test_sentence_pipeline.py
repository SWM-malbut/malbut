"""Exercise sentence backpressure through the real runtime and fake device."""

import sys
from threading import Event, Thread

import numpy as np
import pytest

from malbut_tts.audio import PlaybackCancelled, StreamingPlayer
from malbut_tts.runtime import SpeechRuntime
from malbut_tts.sentence_synthesis import SentenceSynthesizer
from test_audio import fake_device, wait_until


class RecordingSynthesizer:
    """Produce distinguishable full sentences, optionally holding/failing one."""

    def __init__(self, hold_index=None, fail_index=None):
        self.texts = []
        self.closed = []
        self.hold_index = hold_index
        self.fail_index = fail_index
        self.held = Event()
        self.release = Event()
        self.third_started = Event()

    def generate(self, text, cancel_event):
        index = len(self.texts)
        self.texts.append(text)
        if index == 2:
            self.third_started.set()
        try:
            if index == self.hold_index:
                self.held.set()
                assert self.release.wait(3), 'test did not release synthesis'
            yield np.arange(index * 8 + 1, index * 8 + 9), 24000
            if index == self.fail_index:
                raise RuntimeError('late sentence failure')
        finally:
            self.closed.append(index)


class ObservedPlayer(StreamingPlayer):
    """Keep synchronization evidence without changing queue/device behavior."""

    def __init__(self, **kwargs):
        self.capacity_blocked = Event()
        self.capacity_limits = []
        self.writes = []
        self.queue_sizes = []
        super().__init__(**kwargs)

    def wait_for_capacity(self, *, max_pending=2):
        self.capacity_limits.append(max_pending)
        with self._condition:
            if len(self._pending) >= max_pending:
                self.capacity_blocked.set()
        super().wait_for_capacity(max_pending=max_pending)

    def write(self, audio, sample_rate):
        super().write(audio, sample_rate)
        with self._condition:
            self.queue_sizes.append(len(self._pending))
        self.writes.append(np.array(audio, copy=True))


class Pipeline:
    def __init__(self, backend, streams):
        self.backend = backend
        self.streams = streams
        self.players = []
        self.events = []
        self.runtime = SpeechRuntime(
            SentenceSynthesizer(backend), self.make_player,
            lambda pid, state: self.events.append((pid, state)),
        )

    def make_player(self, **kwargs):
        player = ObservedPlayer(**kwargs)
        self.players.append(player)
        return player

    def wait(self, pid, state):
        wait_until(lambda: (pid, state) in self.events)

    def active(self, pid, index=0):
        self.wait(pid, 'playing')
        return self.players[index], self.streams[index]

    def finish(self, pid, player, stream):
        """Consume available PCM and explicitly acknowledge the final drain."""
        while not stream.draining:
            wait_until(lambda: bool(player._pending) or player._input_done)
            stream.step()
        assert (pid, 'finished') not in self.events
        stream.drain()
        self.wait(pid, 'finished')
        assert stream.closed


@pytest.fixture
def make_pipeline(monkeypatch):
    api, streams = fake_device()
    monkeypatch.setitem(sys.modules, 'sounddevice', api)
    pipelines = []

    def make(backend=None):
        pipeline = Pipeline(backend or RecordingSynthesizer(), streams)
        pipelines.append(pipeline)
        return pipeline

    yield make
    for pipeline in pipelines:
        pipeline.backend.release.set()
        pipeline.runtime.close()
        assert not pipeline.runtime._worker.is_alive()
        for player in pipeline.players:
            player._thread.join(2)
            assert not player._thread.is_alive(), 'audio thread survived cleanup'
        assert all(stream.closed for stream in pipeline.streams)


def test_first_audio_plays_while_later_sentence_is_still_generating(make_pipeline):
    backend = RecordingSynthesizer(hold_index=1)
    pipeline = make_pipeline(backend)
    pid = pipeline.runtime.submit('First sentence. Second sentence.')
    assert backend.held.wait(2)
    player, stream = pipeline.active(pid)
    assert not backend.release.is_set()
    assert backend.closed == [0]
    stream.step()
    assert stream.output == [1, 2, 3, 4]
    assert pipeline.events == [(pid, 'playing')]
    backend.release.set()
    pipeline.finish(pid, player, stream)
    assert pipeline.events == [(pid, 'playing'), (pid, 'finished')]
    assert backend.closed == [0, 1]


def test_backpressure_precedes_third_generation_and_counts_partial_sentence(make_pipeline):
    pipeline = make_pipeline()
    pid = pipeline.runtime.submit('First. Second. Third.')
    player, stream = pipeline.active(pid)
    assert player.capacity_blocked.wait(2)
    assert len(pipeline.backend.texts) == 2
    assert len(player._pending) == 2
    stream.step()  # Half the first sentence is still retained in the queue.
    assert player._offset == 4
    assert len(player._pending) == 2
    assert not pipeline.backend.third_started.wait(0.06)
    stream.step()
    assert pipeline.backend.third_started.wait(2)
    wait_until(lambda: len(player.writes) == 3)
    assert len(player._pending) == 2
    pipeline.finish(pid, player, stream)
    assert [value for value in stream.output if value] == list(range(1, 25))
    assert max(player.queue_sizes) <= 2
    assert set(player.capacity_limits) == {2}
    assert pipeline.events == [(pid, 'playing'), (pid, 'finished')]


def test_fifo_requests_use_one_id_and_terminal_for_all_their_sentences(make_pipeline):
    pipeline = make_pipeline()
    originals = ['  First.\nSecond! ', 'Third? Fourth.', 'Fifth. Sixth.']
    ids = [pipeline.runtime.submit(original) for original in originals]
    for index, pid in enumerate(ids):
        player, stream = pipeline.active(pid, index)
        assert len(pipeline.players) == index + 1
        pipeline.finish(pid, player, stream)
    assert ''.join(pipeline.backend.texts) == ''.join(originals)
    assert len(pipeline.backend.texts) == 6
    assert len(set(ids)) == 3
    assert pipeline.events == [
        event for pid in ids for event in [(pid, 'playing'), (pid, 'finished')]
    ]
    assert all(len(player.writes) == 2 for player in pipeline.players)


def test_pause_keeps_fill_bounded_and_resumes_retained_pcm_on_same_id(make_pipeline):
    pipeline = make_pipeline()
    pid = pipeline.runtime.submit('First. Second. Third.')
    player, stream = pipeline.active(pid)
    assert player.capacity_blocked.wait(2)
    stream.step()
    assert pipeline.runtime.control(pid, 'pause')
    stream.step()
    stream.drain()
    pipeline.wait(pid, 'paused')
    queued = pipeline.runtime.submit('Next request.')
    assert len(player._pending) == 2
    assert player._offset == 4
    assert not pipeline.backend.third_started.wait(0.06)
    assert len(pipeline.players) == 1
    assert pipeline.runtime.control(pid, 'resume')
    wait_until(lambda: stream.starts == 2)
    pipeline.finish(pid, player, stream)
    assert [value for value in stream.output if value] == list(range(1, 25))
    assert max(player.queue_sizes) <= 2
    next_player, next_stream = pipeline.active(queued, 1)
    pipeline.finish(queued, next_player, next_stream)
    assert [event for event in pipeline.events if event[0] == pid] == [
        (pid, 'playing'), (pid, 'paused'), (pid, 'playing'), (pid, 'finished'),
    ]


def test_stop_while_full_discards_pending_sentences_and_next_request_recovers(make_pipeline):
    pipeline = make_pipeline()
    pid = pipeline.runtime.submit('First. Second. Must not generate.')
    player, stream = pipeline.active(pid)
    assert player.capacity_blocked.wait(2)
    queued = pipeline.runtime.submit('Recovery.')
    assert pipeline.runtime.control(pid, 'stop')
    pipeline.wait(pid, 'stopped')
    assert not player._pending
    assert stream.closed and stream.aborted
    next_player, next_stream = pipeline.active(queued, 1)
    pipeline.finish(queued, next_player, next_stream)
    assert [text.strip() for text in pipeline.backend.texts] == [
        'First.', 'Second.', 'Recovery.',
    ]
    assert pipeline.backend.closed == [0, 1, 2]
    assert (pid, 'finished') not in pipeline.events


def test_stop_during_later_inference_drops_late_audio_and_recovers(make_pipeline):
    backend = RecordingSynthesizer(hold_index=1)
    pipeline = make_pipeline(backend)
    pid = pipeline.runtime.submit('First. Held second. Must not generate.')
    player, stream = pipeline.active(pid)
    assert backend.held.wait(2)
    stream.step()
    queued = pipeline.runtime.submit('Recovery.')
    assert pipeline.runtime.control(pid, 'stop')
    backend.release.set()
    pipeline.wait(pid, 'stopped')
    assert len(player.writes) == 1
    assert backend.closed[:2] == [0, 1]
    next_player, next_stream = pipeline.active(queued, 1)
    pipeline.finish(queued, next_player, next_stream)
    assert [text.strip() for text in backend.texts] == [
        'First.', 'Held second.', 'Recovery.',
    ]
    assert (pid, 'finished') not in pipeline.events


def test_late_second_sentence_failure_is_failed_not_finished_and_queue_recovers(make_pipeline):
    backend = RecordingSynthesizer(hold_index=1, fail_index=1)
    pipeline = make_pipeline(backend)
    pid = pipeline.runtime.submit('First. Broken second. Must not generate.')
    player, stream = pipeline.active(pid)
    assert backend.held.wait(2)
    stream.step()
    queued = pipeline.runtime.submit('Recovery.')
    backend.release.set()
    pipeline.wait(pid, 'failed')
    assert len(player.writes) == 1
    assert stream.closed and stream.aborted
    assert backend.closed[:2] == [0, 1]
    next_player, next_stream = pipeline.active(queued, 1)
    pipeline.finish(queued, next_player, next_stream)
    assert [text.strip() for text in backend.texts] == [
        'First.', 'Broken second.', 'Recovery.',
    ]
    assert (pid, 'finished') not in pipeline.events
    assert (pid, 'stopped') not in pipeline.events


@pytest.mark.parametrize('terminal', ['cancel', 'close', 'error', 'input_done'])
def test_capacity_wait_checks_terminal_conditions_even_with_empty_queue(terminal):
    cancel = Event()
    player = StreamingPlayer(lambda state: None, cancel)
    try:
        with player._condition:
            if terminal == 'cancel':
                cancel.set()
            elif terminal == 'close':
                player._closing.set()
            elif terminal == 'error':
                player._error = RuntimeError('device failure')
            else:
                player._input_done = True
        expected = PlaybackCancelled if terminal == 'cancel' else RuntimeError
        with pytest.raises(expected):
            player.wait_for_capacity(max_pending=2)
    finally:
        try:
            player.close()
        except RuntimeError:
            assert terminal == 'error'
        player._thread.join(2)
        assert not player._thread.is_alive()


def test_external_cancel_unblocks_capacity_wait_without_requesting_more_audio(monkeypatch):
    api, streams = fake_device()
    monkeypatch.setitem(sys.modules, 'sounddevice', api)
    cancel = Event()
    player = StreamingPlayer(lambda state: None, cancel)
    outcome = []
    entered = Event()

    def wait_for_capacity():
        entered.set()
        try:
            player.wait_for_capacity(max_pending=2)
        except Exception as error:
            outcome.append(error)

    waiter = Thread(target=wait_for_capacity)
    try:
        player.write([1, 2], 24000)
        player.write([3, 4], 24000)
        waiter.start()
        assert entered.wait(2)
        assert not outcome
        cancel.set()
        waiter.join(2)
        assert not waiter.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], PlaybackCancelled)
    finally:
        player.close()
        if waiter.ident is not None:
            waiter.join(2)
        player._thread.join(2)
        assert not player._thread.is_alive()
        assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('limit', [True, 0, 33, 2.0, '2'])
def test_capacity_wait_rejects_invalid_queue_limits(limit):
    player = StreamingPlayer(lambda state: None, Event())
    try:
        with pytest.raises(ValueError, match='max_pending'):
            player.wait_for_capacity(max_pending=limit)
    finally:
        player.close()
        player._thread.join(2)
        assert not player._thread.is_alive()
