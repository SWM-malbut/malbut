"""Check request ordering, cancellation, failures, and completion boundaries."""

from queue import Queue
from threading import Condition, Event

import pytest

from malbut_tts.runtime import (
    CONFIRMATION, DIALOGUE, MAX_FINALIZED_REQUEST_IDS, MAX_RETIRED_PLAYBACK_IDS,
    NOTIFICATION, SpeechRuntime,
)


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

    def __init__(self, synth=None, **runtime_options):
        self.synth = synth or FakeSynthesizer()
        self.players = Queue()
        self.events = []
        self.interim_flags = []
        self.condition = Condition()
        self.runtime = SpeechRuntime(
            self.synth, self.make_player, self.status,
            **runtime_options,
        )

    def make_player(self, **kwargs):
        player = FakePlayer(**kwargs)
        self.players.put(player)
        return player

    def status(self, playback_id, state, interim):
        with self.condition:
            self.events.append((playback_id, state))
            self.interim_flags.append(interim)
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


def test_confirmation_preempts_old_playback_and_queued_dialogue(h):
    first = h.runtime.submit('ordinary answer')
    h.active(first)
    waiting = h.runtime.submit('queued ordinary answer')
    question = h.runtime.submit('넘어지셨나요?', CONFIRMATION, playback_id='question-1')
    assert question == 'question-1'
    h.wait(first, 'stopped')
    h.wait(waiting, 'stopped')
    h.active(question).drain.set()
    h.wait(question, 'finished')
    assert h.synth.texts == ['ordinary answer', '넘어지셨나요?']
    assert (first, 'finished') not in h.events
    assert (waiting, 'playing') not in h.events


def test_cancel_pending_known_playback_id_never_synthesizes_it(h):
    first = h.runtime.submit('ordinary answer')
    player = h.active(first)
    waiting = h.runtime.submit('queued answer', playback_id='known-id')
    assert h.runtime.control(waiting, 'stop')
    h.wait(waiting, 'stopped')
    player.drain.set()
    h.wait(first, 'finished')
    assert h.synth.texts == ['ordinary answer']
    assert not h.runtime.control(waiting, 'stop')


def test_duplicate_live_playback_id_does_not_create_competing_request(h):
    first = h.runtime.submit('ordinary answer', playback_id='same-id')
    h.active(first)
    assert h.runtime.submit('duplicate', playback_id='same-id') is None


def test_final_removes_only_waiting_interims_for_its_request(h):
    active = h.runtime.submit('unrelated active')
    player = h.active(active)
    progress = h.runtime.submit('old progress', request_id='weather', interim=True)
    other_progress = h.runtime.submit('other progress', request_id='other', interim=True)
    other_final = h.runtime.submit('other final', request_id='another')
    final = h.runtime.submit('weather failed', request_id='weather')
    repeated_final = h.runtime.submit('weather failed', request_id='weather')
    h.wait(progress, 'stopped')
    assert not player.cancel.is_set()
    player.drain.set()
    h.wait(active, 'finished')
    for pid in (other_progress, other_final, final, repeated_final):
        h.active(pid).drain.set()
        h.wait(pid, 'finished')
    assert h.synth.texts == [
        'unrelated active', 'other progress', 'other final',
        'weather failed', 'weather failed',
    ]
    assert h.events.count((progress, 'stopped')) == 1


@pytest.mark.parametrize('interim', [False, True])
def test_new_correlated_request_replaces_waiting_progress_even_when_queue_is_full(interim):
    h = Harness(max_pending_requests=1)
    try:
        active = h.runtime.submit('active')
        player = h.active(active)
        old = h.runtime.submit('old progress', request_id='request', interim=True)
        new = h.runtime.submit('new text', request_id='request', interim=interim)
        h.wait(old, 'stopped')
        assert (new, 'failed') not in h.events
        player.drain.set()
        h.wait(active, 'finished')
        h.active(new).drain.set()
        h.wait(new, 'finished')
        assert h.synth.texts == ['active', 'new text']
        assert h.events.count((old, 'stopped')) == 1
    finally:
        h.runtime.close()


@pytest.mark.parametrize('paused', [False, True])
def test_final_preserves_its_progress_after_playback_started(h, paused):
    progress = h.runtime.submit('playing progress', request_id='request', interim=True)
    player = h.active(progress)
    if paused:
        assert h.runtime.control(progress, 'pause')
    waiting = h.runtime.submit('waiting progress', request_id='request', interim=True)
    final = h.runtime.submit('final', request_id='request')
    h.wait(waiting, 'stopped')
    assert not player.cancel.is_set()
    assert (progress, 'stopped') not in h.events
    if paused:
        assert h.runtime.control(progress, 'resume')
    player.drain.set()
    h.wait(progress, 'finished')
    h.active(final).drain.set()
    h.wait(final, 'finished')
    assert h.synth.texts == ['playing progress', 'final']


@pytest.mark.parametrize('interim', [False, True])
@pytest.mark.parametrize('same_request', [False, True])
def test_replacement_only_cancels_matching_progress_during_synthesis(interim, same_request):
    entered, release = Event(), Event()

    class SlowProgress(FakeSynthesizer):
        def generate(self, text, cancel_event):
            if text == 'old progress':
                entered.set()
                assert release.wait(3)
            yield from super().generate(text, cancel_event)

    h = Harness(SlowProgress())
    try:
        old = h.runtime.submit('old progress', request_id='request', interim=True)
        assert entered.wait(3)
        old_player = h.players.get(timeout=3)
        new = h.runtime.submit('new text', interim=interim,
                               request_id='request' if same_request else 'other')
        canceled = same_request and not interim
        assert old_player.cancel.is_set() is canceled
        release.set()
        if canceled:
            h.wait(old, 'stopped')
            assert old_player.audio == []
        else:
            h.wait(old, 'playing')
            old_player.drain.set()
            h.wait(old, 'finished')
        h.active(new).drain.set()
        h.wait(new, 'finished')
        old_events = [(old, 'stopped')] if canceled else [
            (old, 'playing'), (old, 'finished'),
        ]
        assert h.events == old_events + [(new, 'playing'), (new, 'finished')]
    finally:
        release.set()
        h.runtime.close()


def test_final_cancels_progress_before_player_factory_returns():
    entered, release = Event(), Event()
    h = Harness()

    def delayed_player(**kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return h.make_player(**kwargs)

    h.runtime._player_factory = delayed_player
    try:
        progress = h.runtime.submit('progress', request_id='request', interim=True)
        assert entered.wait(3)
        final = h.runtime.submit('final', request_id='request')
        release.set()
        h.wait(progress, 'stopped')
        assert h.players.get(timeout=3).audio == []
        h.active(final).drain.set()
        h.wait(final, 'finished')
        assert h.synth.texts == ['final']
        assert h.events == [(progress, 'stopped'), (final, 'playing'), (final, 'finished')]
    finally:
        release.set()
        h.runtime.close()


def test_rejected_final_does_not_cancel_generating_progress_or_retire_request():
    entered, release = Event(), Event()

    class SlowProgress(FakeSynthesizer):
        def generate(self, text, cancel_event):
            if text == 'progress':
                entered.set()
                assert release.wait(3)
            yield from super().generate(text, cancel_event)

    h = Harness(SlowProgress(), max_pending_requests=1)
    try:
        progress = h.runtime.submit('progress', request_id='request', interim=True)
        assert entered.wait(3)
        player = h.players.get(timeout=3)
        queued = h.runtime.submit('other request', request_id='other')
        rejected = h.runtime.submit('rejected final', request_id='request')
        h.wait(rejected, 'failed')
        assert not player.cancel.is_set()
        release.set()
        h.wait(progress, 'playing')
        player.drain.set()
        h.wait(progress, 'finished')
        other_player = h.active(queued)
        late = h.runtime.submit('later progress', request_id='request', interim=True)
        other_player.drain.set()
        h.wait(queued, 'finished')
        h.active(late).drain.set()
        h.wait(late, 'finished')
        assert h.synth.texts == ['progress', 'other request', 'later progress']
    finally:
        release.set()
        h.runtime.close()


@pytest.mark.parametrize('invalid', [
    {'text': ''}, {'request_id': ' '}, {'request_id': None},
    {'request_id': 'x' * 257}, {'interim': 'false'},
    {'playback_id': 'active'}, {'playback_id': 'progress'},
])
def test_invalid_or_duplicate_final_preserves_waiting_progress(h, invalid):
    active = h.runtime.submit('active', playback_id='active')
    player = h.active(active)
    progress = h.runtime.submit('progress', request_id='request',
                                playback_id='progress', interim=True)
    options = {'text': 'final', 'request_id': 'request', **invalid}
    assert h.runtime.submit(**options) is None
    assert (progress, 'stopped') not in h.events
    player.drain.set()
    h.wait(active, 'finished')
    h.active(progress).drain.set()
    h.wait(progress, 'finished')
    later = h.runtime.submit('later progress', request_id='request', interim=True)
    h.active(later).drain.set()
    h.wait(later, 'finished')
    assert h.synth.texts == ['active', 'progress', 'later progress']


def test_request_id_accepts_the_full_utterance_id_length(h):
    final = h.runtime.submit('final', request_id='x' * 256)
    h.active(final).drain.set()
    h.wait(final, 'finished')
    late = h.runtime.submit('late progress', request_id='x' * 256, interim=True)
    h.wait(late, 'stopped')
    assert h.synth.texts == ['final']


def test_accepted_final_stops_late_progress_even_after_final_finishes(h):
    final = h.runtime.submit('final', request_id='request')
    player = h.active(final)
    late = h.runtime.submit('late progress', request_id='request',
                            interim=True, playback_id='late')
    h.wait(late, 'stopped')
    assert h.runtime.submit('duplicate late progress', request_id='request',
                            interim=True, playback_id='late') is None
    player.drain.set()
    h.wait(final, 'finished')
    later = h.runtime.submit('later progress', request_id='request', interim=True)
    h.wait(later, 'stopped')
    assert h.synth.texts == ['final']
    assert h.events.count((late, 'stopped')) == 1
    assert h.events.count((later, 'stopped')) == 1


def test_finalized_request_history_is_bounded(h):
    active = h.runtime.submit('active')
    h.active(active)
    for index in range(MAX_FINALIZED_REQUEST_IDS + 1):
        final = h.runtime.submit('final', request_id=f'request-{index}')
        assert h.runtime.control(final, 'stop')
    assert len(h.runtime._finalized_request_ids) == MAX_FINALIZED_REQUEST_IDS
    assert 'request-0' not in h.runtime._finalized_request_ids
    late = h.runtime.submit('late progress', interim=True,
                            request_id=f'request-{MAX_FINALIZED_REQUEST_IDS}')
    h.wait(late, 'stopped')
    assert h.synth.texts == ['active']


def test_requests_without_correlation_keep_all_progress_and_final(h):
    active = h.runtime.submit('active')
    player = h.active(active)
    pending = [
        h.runtime.submit('progress', interim=True),
        h.runtime.submit('progress', interim=True),
        h.runtime.submit('final'),
    ]
    player.drain.set()
    h.wait(active, 'finished')
    for pid in pending:
        h.active(pid).drain.set()
        h.wait(pid, 'finished')
    assert h.synth.texts == ['active', 'progress', 'progress', 'final']


def test_confirmation_still_preempts_with_a_finalized_request_id(h):
    final = h.runtime.submit('final', request_id='request')
    h.active(final)
    question = h.runtime.submit('question', CONFIRMATION,
                                request_id='request', interim=True)
    h.wait(final, 'stopped')
    h.active(question).drain.set()
    h.wait(question, 'finished')
    assert h.synth.texts == ['final', 'question']


def test_stop_all_clears_active_and_waiting_before_question_is_available(h):
    first = h.runtime.submit('ordinary answer')
    h.active(first)
    waiting = h.runtime.submit('queued ordinary answer')
    assert h.runtime.control('', 'stop_all')
    h.wait(first, 'stopped')
    h.wait(waiting, 'stopped')
    assert h.synth.texts == ['ordinary answer']
    assert h.runtime.control('', 'stop_all')
    question = h.runtime.submit('question', CONFIRMATION, playback_id='q1')
    h.active(question).drain.set()
    h.wait(question, 'finished')


def test_stop_before_confirmation_receipt_suppresses_late_audio_and_preemption(h):
    active = h.runtime.submit('진행 중인 다른 음성')
    player = h.active(active)
    assert h.runtime.control('confirmation-late', 'stop')
    assert h.runtime.control('confirmation-late', 'stop')
    assert ('confirmation-late', 'stopped') not in h.events
    assert h.runtime.submit(
        '이미 답한 질문', CONFIRMATION, playback_id='confirmation-late',
    ) == 'confirmation-late'
    h.wait('confirmation-late', 'stopped')
    assert not player.cancel.is_set()
    assert h.synth.texts == ['진행 중인 다른 음성']
    assert not h.runtime.control('confirmation-late', 'stop')
    assert h.runtime.submit('중복 질문', CONFIRMATION, playback_id='confirmation-late') is None
    assert h.events.count(('confirmation-late', 'stopped')) == 1
    player.drain.set()
    h.wait(active, 'finished')


def test_stop_reservations_are_bounded_and_reject_invalid_ids(h):
    for invalid in ('', '  ', None, 'x' * 201):
        assert not h.runtime.control(invalid, 'stop')
    for index in range(MAX_RETIRED_PLAYBACK_IDS + 1):
        assert h.runtime.control(f'future-{index}', 'stop')
    assert len(h.runtime._retired_ids) == MAX_RETIRED_PLAYBACK_IDS
    assert 'future-0' not in h.runtime._retired_ids
    latest = f'future-{MAX_RETIRED_PLAYBACK_IDS}'
    assert h.runtime.submit('늦은 질문', CONFIRMATION, playback_id=latest) == latest
    h.wait(latest, 'stopped')
    assert h.synth.texts == []


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


@pytest.mark.parametrize('interim', [False, True])
@pytest.mark.parametrize('terminal', ['finished', 'stopped', 'failed'])
def test_interim_flag_is_preserved_from_playing_through_terminal(h, interim, terminal):
    pid = h.runtime.submit('답변을 준비하고 있어요.', interim=interim)
    player = h.active(pid)
    assert h.interim_flags == [interim]
    if terminal == 'stopped':
        assert h.runtime.control(pid, 'stop')
    else:
        player.fail = terminal == 'failed'
        player.drain.set()
    h.wait(pid, terminal)
    assert h.events == [(pid, 'playing'), (pid, terminal)]
    assert h.interim_flags == [interim, interim]


def test_invalid_interim_flag_is_rejected_before_synthesis(h):
    for interim in (None, 0, 1, '', 'false', [], {}):
        assert h.runtime.submit('잘못된 요청', interim=interim) is None
    assert h.synth.texts == [] and h.events == []


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
    for kind in (-1, 3, 'dialogue', True):
        assert h.runtime.submit('hello', kind) is None
    assert not h.runtime.control('', 'stop')
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


def test_ten_thousand_waiting_requests_keep_only_default_capacity(h, caplog):
    """Flooding a paused player cannot accumulate 10,000 pending texts."""
    caplog.set_level('ERROR')
    active = h.runtime.submit('active')
    player = h.active(active)
    assert h.runtime.control(active, 'pause')
    submitted = [h.runtime.submit(f'request {index}') for index in range(10000)]
    with h.runtime._condition:
        retained = [entry[2].playback_id for entry in h.runtime._pending]
    assert set(retained) == set(submitted[:32])
    assert len(retained) == 32
    assert h.events.count((active, 'paused')) == 1
    assert h.synth.texts == ['active']
    assert not player.cancel.is_set()
    assert [(pid, state) for pid, state in h.events if state == 'failed'] == [
        (pid, 'failed') for pid in submitted[32:]
    ]


@pytest.mark.parametrize('options', [
    pytest.param({}, id='default'),
    pytest.param({'pending_timeout_s': 0.0}, id='explicit-zero'),
])
def test_default_or_disabled_expiry_preserves_long_waits_and_priority(options):
    """Long playback and pause preserve waiting speech until it can play."""
    now = [0.0]
    h = Harness(clock=lambda: now[0], **options)
    try:
        active = h.runtime.submit('active notice', NOTIFICATION)
        player = h.active(active)
        notice1 = h.runtime.submit('notice 1', NOTIFICATION)
        dialogue1 = h.runtime.submit('dialogue 1', DIALOGUE)
        # Admission runs expiry checks deterministically after long playback.
        with h.runtime._condition:
            now[0] += 120.0
            notice2 = h.runtime.submit('notice 2', NOTIFICATION)
            assert len(h.runtime._pending) == 3
        assert h.events == [(active, 'playing')]
        assert h.runtime.control(active, 'pause')
        with h.runtime._condition:
            now[0] += 600.0
            dialogue2 = h.runtime.submit('dialogue 2', DIALOGUE)
            assert len(h.runtime._pending) == 4
            h.runtime._condition.notify_all()
        assert h.events == [(active, 'playing'), (active, 'paused')]
        assert h.synth.texts == ['active notice']
        assert h.runtime.control(active, 'resume')
        player.drain.set()
        h.wait(active, 'finished')
        for pid in (dialogue1, dialogue2, notice1, notice2):
            h.active(pid).drain.set()
            h.wait(pid, 'finished')
        assert h.synth.texts == [
            'active notice', 'dialogue 1', 'dialogue 2', 'notice 1', 'notice 2',
        ]
        assert not any(state == 'failed' for _, state in h.events)
    finally:
        h.runtime.close()


def test_waiting_requests_expire_while_paused_without_new_submissions():
    """A paused active job cannot retain or later speak expired waiting text."""
    now = [100.0]
    h = Harness(clock=lambda: now[0], pending_timeout_s=5.0)
    try:
        active = h.runtime.submit('active')
        player = h.active(active)
        assert h.runtime.control(active, 'pause')
        pending = h.runtime.submit('stale answer')
        with h.runtime._condition:
            now[0] = 105.0
            h.runtime._condition.notify_all()
        h.wait(pending, 'failed')
        with h.runtime._condition:
            assert h.runtime._pending == []
        assert h.synth.texts == ['active']
        assert h.players.empty()
        assert h.runtime.control(active, 'resume')
        fresh = h.runtime.submit('fresh answer')
        player.drain.set()
        h.wait(active, 'finished')
        h.active(fresh).drain.set()
        h.wait(fresh, 'finished')
        assert h.events.count((pending, 'failed')) == 1
        assert h.synth.texts == ['active', 'fresh answer']
    finally:
        h.runtime.close()


def test_expired_requests_free_capacity_before_admission_and_keep_priority():
    """Expiry scans beyond heap priority and fresh admission can reuse slots."""
    now = [0.0]
    h = Harness(clock=lambda: now[0], max_pending_requests=3,
                pending_timeout_s=5.0)
    try:
        active = h.runtime.submit('active')
        player = h.active(active)
        expired = h.runtime.submit('old notice', NOTIFICATION)
        now[0] = 1.0
        notice = h.runtime.submit('fresh notice', NOTIFICATION)
        dialogue = h.runtime.submit('fresh dialogue', DIALOGUE)
        # Keep the worker/reaper out so admission itself must reclaim space.
        with h.runtime._condition:
            now[0] = 5.0
            admitted = h.runtime.submit('new dialogue', DIALOGUE)
            assert len(h.runtime._pending) == 3
        h.wait(expired, 'failed')
        player.drain.set()
        h.wait(active, 'finished')
        for pid in (dialogue, admitted, notice):
            h.active(pid).drain.set()
            h.wait(pid, 'finished')
        assert h.synth.texts == [
            'active', 'fresh dialogue', 'new dialogue', 'fresh notice',
        ]
        assert h.events.count((expired, 'failed')) == 1
    finally:
        h.runtime.close()


@pytest.mark.parametrize('option,value', [
    ('max_pending_requests', 0), ('max_pending_requests', -1),
    ('max_pending_requests', 1.5), ('max_pending_requests', True),
    ('pending_timeout_s', -1.0),
    ('pending_timeout_s', float('inf')), ('pending_timeout_s', float('nan')),
    ('pending_timeout_s', True), ('pending_timeout_s', False),
    ('pending_timeout_s', '30'),
])
def test_invalid_pending_limits_are_rejected_before_starting_workers(option, value):
    with pytest.raises(ValueError, match=option):
        SpeechRuntime(None, None, None, **{option: value})
