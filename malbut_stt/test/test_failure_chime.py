"""Failed ordinary turns use local feedback without speaking or publishing text."""

from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline


VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


@pytest.fixture
def run():
    state = SimpleNamespace(
        now=0.0, chimes=[], transcripts=[], statuses=[], interruptions=[],
        controls=[], reports=[], partials=[],
    )
    pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=None, transcriber=None,
        is_speech=lambda frame, _: any(frame),
        publish_transcript=lambda *args: state.transcripts.append(args),
        publish_control=lambda *args: state.controls.append(args),
        publish_interruption=lambda *args: state.interruptions.append(args),
        publish_input_status=lambda *args: state.statuses.append(args),
        on_partial=lambda *args: state.partials.append(args),
        on_failure=lambda: state.chimes.append('failure'),
        report=state.reports.append, clock=lambda: state.now, input_has_aec=True,
        settings=CaptureSettings(silence_timeout_s=2.0),
    )
    state.pipeline = pipeline
    yield state
    pipeline.close()


def reply(state, job, text='', error=None):
    """Return one fake worker result through the same queue as local inference."""
    state.pipeline.results.put_nowait((*job[:3], text, error))
    state.pipeline.poll()


def wake_up(state):
    p = state.pipeline
    p.feed(VOICE * 4 + QUIET * 20)
    job = p.jobs.get_nowait()
    assert job[0] == 'wake'
    reply(state, job, '제이크야')
    assert p.session.active and p.session.session_id == ''


def final_command(state):
    """Finish capture in one chunk, without requesting an intermediate decode."""
    p = state.pipeline
    p.feed(VOICE * 4 + QUIET * 100)
    job = p.jobs.get_nowait()
    assert job[0] == 'command'
    return job


def assert_local_failure(state):
    p = state.pipeline
    assert state.chimes == ['failure']
    assert state.transcripts == state.interruptions == []
    assert not any(status[2] == 'failed' for status in state.statuses)
    assert not p.session.active and not p._busy
    assert p.session.utterance_id is None and p._reply_request_id is None
    assert p.pending_addressee is None and p.jobs.empty()
    assert p._command_start_deadline is None and p._retry_notice_deadline is None


def test_post_wake_silence_uses_existing_five_second_timeout_only_once(run):
    p = run.pipeline
    wake_up(run)
    assert p._command_start_deadline == pytest.approx(5.0)
    run.now = 4.99
    p.feed(QUIET * 750)
    assert p.session.active and run.chimes == []
    run.now = 5.0
    p.poll()
    assert_local_failure(run)
    assert run.statuses == []
    # Silence after the guard is ordinary idle input, with no repeated feedback.
    run.now = 6.0
    p.feed(QUIET * 750)
    p.poll()
    assert_local_failure(run)


@pytest.mark.parametrize('timeout_s', [.5, 1.01, 6.0])
def test_no_speech_honors_configured_start_timeout_clock_boundary(run, timeout_s):
    p = run.pipeline
    p.command_stream.settings = CaptureSettings(
        start_timeout_s=timeout_s, silence_timeout_s=2.0)
    wake_up(run)  # Rebuilds the collector from the configured capture settings.
    assert p._command_start_deadline == pytest.approx(timeout_s)
    run.now = timeout_s - .001
    p.poll()
    assert p.session.active and run.chimes == [] and p.jobs.empty()
    run.now = timeout_s
    p.poll()
    assert_local_failure(run)
    assert run.statuses == []


def test_no_speech_drops_later_speech_events_from_same_large_pcm_chunk(run):
    p = run.pipeline
    wake_up(run)
    run.now = 5.0
    p.feed(QUIET * 250 + VOICE * 4 + QUIET * 100)
    assert_local_failure(run)
    assert run.statuses == []
    assert not p.command_stream.collector.started and not p.wake_stream.collector.started
    run.now = 5.31
    wake_up(run)
    assert run.chimes == ['failure']


def test_busy_silence_cannot_advance_or_extend_command_start_deadline(run):
    p = run.pipeline
    wake_up(run)
    run.now = 4.99
    p.feed(QUIET * 250, busy_at_capture=True)
    assert p.session.active and p.session.utterance_id is None
    assert run.chimes == run.statuses == [] and p.jobs.empty()
    assert p._command_start_deadline == pytest.approx(5.0)
    run.now = 5.0
    p.feed(QUIET, busy_at_capture=True)
    assert_local_failure(run)


@pytest.mark.parametrize('trigger', ['poll', 'feed'])
def test_tts_response_deadline_emits_one_failure_and_discards_timeout_chunk(run, trigger):
    p = run.pipeline
    # Exercise the session timer without an independently owned wake deadline.
    p.session.activate()
    p.on_playback_status('answer', 'playing')
    run.now = 1.0
    p.on_playback_status('answer', 'finished')
    assert p.session.deadline == pytest.approx(6.0)
    run.now = 5.99
    p.poll()
    assert p.session.active and run.chimes == []
    run.now = 6.0
    if trigger == 'poll':
        p.poll()
    else:
        p.feed(VOICE * 4 + QUIET * 100)
    assert_local_failure(run)
    assert run.statuses == [] and 'session_ended:input_timeout' in run.reports
    run.now = 6.31
    p.poll()
    p.feed(QUIET * 750)
    assert_local_failure(run)


def test_qualified_speech_just_before_tts_deadline_keeps_its_final_result(run):
    p = run.pipeline
    p.session.activate()
    p.on_playback_status('answer', 'playing')
    run.now = 1.0
    p.on_playback_status('answer', 'finished')
    run.now = 5.99
    p.feed(VOICE * 4)
    uid = p.session.utterance_id
    assert uid is not None and p.session.deadline is None
    run.now = 6.01
    p.feed(QUIET * 100)
    job = p.jobs.get_nowait()
    assert job[0] == 'command' and job[2] == uid
    reply(run, job, '네')
    assert run.transcripts == [(uid, '네')] and run.chimes == []


@pytest.mark.parametrize('playback_state', ['playing', 'paused', 'finished'])
def test_playback_status_cannot_extend_independent_wake_deadline(run, playback_state):
    p = run.pipeline
    wake_up(run)
    p.on_playback_status('answer', 'playing')
    run.now = 1.0
    p.on_playback_status('answer', playback_state)
    assert p._command_start_deadline == pytest.approx(5.0)
    if playback_state == 'finished':
        assert p.session.deadline == pytest.approx(6.0)
    run.now = 4.99
    p.feed(QUIET * 250)
    assert p.session.active and run.chimes == []
    run.now = 5.0
    p.poll()
    assert_local_failure(run)
    p.poll()
    assert_local_failure(run)


def test_idle_silence_and_unqualified_noise_never_start_a_command(run):
    p = run.pipeline
    p.feed(QUIET * 750)
    assert run.chimes == [] and p.jobs.empty()
    wake_up(run)
    run.now = 4.99
    p.feed(QUIET * 100 + VOICE * 3 + QUIET * 147)
    assert p.session.active and run.chimes == [] and p.jobs.empty()
    run.now = 5.0
    p.poll()
    assert_local_failure(run)
    assert run.statuses == []


@pytest.mark.parametrize('text,error', [
    ('', None), (' \n\t', None), (None, None), ({'text': 'not a string'}, None),
    ('', 'RuntimeError'), ('recognizer stale text', 'RuntimeError'),
])
def test_final_recognition_failure_never_sends_agent_failure_or_transcript(run, text, error):
    wake_up(run)
    job = final_command(run)
    reply(run, job, text, error)
    assert_local_failure(run)
    assert run.statuses == [('', job[2], 'started')]
    # Repeated failures and a late nonempty success both belong to the old turn.
    reply(run, job, text, error)
    reply(run, job, '거실로 가 주세요.')
    assert_local_failure(run)


def test_empty_final_keeps_one_endpoint_cue_then_one_failure_cue(run):
    p = run.pipeline
    p.on_endpoint = lambda: run.chimes.append('endpoint')
    wake_up(run)
    job = final_command(run)
    assert run.chimes == ['endpoint'] and run.transcripts == []
    reply(run, job)
    assert run.chimes == ['endpoint', 'failure']
    assert not p.session.active and run.transcripts == run.interruptions == []
    assert run.statuses == [('', job[2], 'started')]
    reply(run, job)
    reply(run, job, '늦게 도착한 문장입니다.')
    assert run.chimes == ['endpoint', 'failure']
    assert run.transcripts == run.interruptions == []
    assert run.statuses == [('', job[2], 'started')]


@pytest.mark.parametrize('text', ['네', '아니', '오늘은 책을 읽었습니다'])
def test_normal_and_short_qualified_speech_are_delivered_without_failure_sound(run, text):
    wake_up(run)
    job = final_command(run)
    reply(run, job, text)
    assert run.transcripts == [(job[2], text)]
    assert run.chimes == [] and not run.pipeline.session.active
    assert run.pipeline._reply_request_id == job[2]


def test_unqualified_onset_cannot_extend_command_start_deadline(run):
    p = run.pipeline
    wake_up(run)
    run.now = 4.99
    p.feed(VOICE * 3)
    assert p.session.active and run.chimes == []
    run.now = 5.0
    p.feed(VOICE + QUIET * 100)
    assert_local_failure(run)
    assert run.statuses == []


def test_qualified_predeadline_capture_is_accepted_before_late_poll_timeout(run):
    p = run.pipeline
    wake_up(run)
    p.audio.put_nowait((p._audio_generation, 4.99, VOICE * 4, False))
    run.now = 5.1
    p.poll()
    assert p.session.active and p.session.utterance_id is not None
    assert p._command_start_deadline is None and run.chimes == []
    p.feed(QUIET * 100)
    job = p.jobs.get_nowait()
    reply(run, job, '네')
    assert run.transcripts == [(job[2], '네')] and run.chimes == []


def test_command_start_deadline_follows_wake_chime_and_echo_tail(run):
    p = run.pipeline

    def wake_sound():
        run.now += .18

    p.on_wake = wake_sound
    wake_up(run)
    assert p._chime_gate_until == pytest.approx(.48)
    assert p._command_start_deadline == pytest.approx(5.48)
    run.now = 5.479
    p.poll()
    assert p.session.active and run.chimes == []
    run.now = 5.48
    p.poll()
    assert_local_failure(run)


def test_failed_turn_accepts_a_new_wake_and_ignores_old_result_during_retry(run):
    wake_up(run)
    old = final_command(run)
    reply(run, old)
    assert_local_failure(run)
    run.now = .31
    wake_up(run)
    current = final_command(run)
    reply(run, old, 'late previous transcript')
    assert run.pipeline.session.active and run.pipeline._busy
    reply(run, current, '네')
    assert run.chimes == ['failure']
    assert run.transcripts == [(current[2], '네')]


@pytest.mark.parametrize('error', [None, 'RuntimeError'])
@pytest.mark.parametrize('final_text', ['', '거실에서 기다려 주세요.'])
def test_endpoint_failure_retries_final_audio_before_deciding_failure(run, error, final_text):
    p = run.pipeline
    wake_up(run)
    p.feed(VOICE * 4 + QUIET * 50)
    endpoint = p.jobs.get_nowait()
    assert endpoint[0] == 'endpoint'
    p.feed(QUIET * 50)
    assert p._endpoint_final is not None and run.chimes == []
    reply(run, endpoint, error=error)
    final = p.jobs.get_nowait()
    assert final[0] == 'command' and final[3] == VOICE * 4 + QUIET * 100
    assert run.chimes == [] and run.transcripts == [] and p.session.active
    reply(run, final, final_text)
    if final_text:
        assert run.transcripts == [(final[2], final_text)] and run.chimes == []
    else:
        assert_local_failure(run)
    reply(run, endpoint, '늦게 도착한 문장입니다.')
    assert run.chimes == ([] if final_text else ['failure'])
    assert run.transcripts == ([(final[2], final_text)] if final_text else [])


@pytest.mark.parametrize('error', [None, 'RuntimeError'])
def test_failed_inflight_partial_cannot_beep_until_final_retry_fails(run, error):
    p = run.pipeline
    p._stream_factory = lambda: object()
    p.command_stream.partial_interval_s = 2.0
    wake_up(run)
    p.feed(VOICE * 100)
    partial = p.jobs.get_nowait()
    assert partial[0] == 'partial'
    p.feed(QUIET * 100)
    final = p.jobs.get_nowait()
    assert final[0] == 'command' and final[3].final
    reply(run, partial, error=error)
    assert run.chimes == [] and p.session.active
    reply(run, final)
    assert_local_failure(run)
    reply(run, partial, '늦게 도착한 중간 결과입니다.')
    assert_local_failure(run)
    assert run.partials == []


@pytest.mark.parametrize('aec', [False, True])
def test_failure_sound_excludes_callback_echo_queued_audio_and_tail(run, aec):
    p = run.pipeline
    p.input_has_aec = aec
    wake_up(run)
    old_generation = p._audio_generation

    def sound():
        run.chimes.append('failure')
        assert p._chime_playing and not p.session.active
        p.feed(VOICE * 4 + QUIET * 20)
        assert p.jobs.empty() and not p.wake_stream.collector.started
        p.audio.put_nowait((p._audio_generation, run.now, VOICE, False))
        run.now += .16

    p.on_failure = sound
    run.now = 5.0
    p.feed(QUIET * 250)
    assert_local_failure(run)
    assert p.audio.empty() and not p._chime_playing
    assert p._chime_gate_until == pytest.approx(5.46)
    assert p._audio_generation != old_generation
    # Even a delayed queue delivery cannot reintroduce the beep as a wake.
    p.audio.put_nowait((old_generation, 5.5, VOICE * 4 + QUIET * 20, False))
    p.audio.put_nowait((p._audio_generation, 5.45, VOICE * 4 + QUIET * 20, False))
    run.now = 5.5
    p.poll()
    assert p.jobs.empty() and not p.wake_stream.collector.started
    wake_up(run)
    assert run.chimes == ['failure']


@pytest.mark.parametrize('missing', [False, True])
def test_unavailable_or_failed_sound_returns_to_wake_without_text_fallback(run, missing):
    p = run.pipeline
    wake_up(run)

    def broken_sound():
        run.chimes.append('attempt')
        raise RuntimeError('private device detail')

    p.on_failure = None if missing else broken_sound
    run.now = 5.0
    p.feed(QUIET * 250)
    assert not p.session.active and not p._chime_playing
    assert run.transcripts == run.statuses == []
    expected = 'failure_chime_unavailable' if missing else 'failure_chime_failed:RuntimeError'
    assert expected in run.reports
    assert not any('private device detail' in report for report in run.reports)
    run.now = 5.31
    wake_up(run)


@pytest.mark.parametrize('cancellation', ['terminate', 'close', 'web', 'confirmation'])
@pytest.mark.parametrize('kind', ['command', 'endpoint'])
def test_cancelled_turn_suppresses_late_failures_and_successes(run, cancellation, kind):
    p = run.pipeline
    wake_up(run)
    if kind == 'command':
        job = final_command(run)
    else:
        p.feed(VOICE * 4 + QUIET * 50)
        job = p.jobs.get_nowait()
        assert job[0] == 'endpoint'
    if cancellation == 'terminate':
        p._terminate('cancelled_for_test')
    elif cancellation == 'close':
        p.close()
    elif cancellation == 'web':
        assert p.control_web_talk('web-1', True, 10.0)
    else:
        assert p.start_session('fall-1')
    reply(run, job, error='RuntimeError')
    if cancellation == 'close':
        p.results.get_nowait()  # Closed pipelines deliberately do not consume results.
    reply(run, job, '늦게 도착한 문장입니다.')
    assert run.chimes == run.transcripts == run.interruptions == []
    assert not any(status[2] == 'failed' for status in run.statuses)
    if cancellation == 'confirmation':
        assert p.session_is_active('fall-1')


def test_web_lease_blocks_silence_feedback_and_requires_fresh_wake_after_release(run):
    p = run.pipeline
    wake_up(run)
    assert p.control_web_talk('web-1', True, 10.0)
    p.feed(QUIET * 500)
    assert run.chimes == [] and not p.session.active
    assert p.control_web_talk('web-1', False, 0.0)
    p.feed(QUIET * 500)
    assert run.chimes == []
    run.now = .31
    wake_up(run)
    run.now = 5.31
    p.feed(QUIET * 250)
    assert_local_failure(run)


def test_confirmation_keeps_silence_and_recognition_failure_policy(run):
    p = run.pipeline
    assert p.start_session('fall-1')
    p.feed(QUIET * 750)
    assert p.session_is_active('fall-1') and run.chimes == run.statuses == []
    failed = final_command(run)
    reply(run, failed)
    assert p.session_is_active('fall-1') and p.session.utterance_id is None
    assert run.chimes == run.transcripts == []
    assert run.statuses[-1] == ('fall-1', failed[2], 'failed')
    retry = final_command(run)
    reply(run, retry, '아니')
    assert run.transcripts == [(retry[2], '아니')]
    assert p.session_is_active('fall-1') and run.chimes == []
    assert p.stop_session('fall-1')
    reply(run, retry, error='RuntimeError')
    assert run.chimes == []


@pytest.mark.parametrize('waiting_for', ['inference', 'addressee'])
def test_idle_timeout_does_not_interrupt_pending_inference_or_addressee(run, waiting_for):
    p = run.pipeline
    wake_up(run)
    if waiting_for == 'addressee':
        p.on_playback_status('answer', 'playing')
    job = final_command(run)
    if waiting_for == 'addressee':
        reply(run, job, '잠깐 기다려 주세요.')
        assert p.pending_addressee is not None
    p.feed(QUIET * 500)
    assert p.session.active and run.chimes == []
    assert run.transcripts == []
    if waiting_for == 'inference':
        reply(run, job, '잠깐 기다려 주세요.')
        assert run.transcripts == [(job[2], '잠깐 기다려 주세요.')]
    else:
        assert p.pending_addressee is not None


def test_failed_interruption_does_not_send_failure_text_or_new_playback_controls(run):
    p = run.pipeline
    wake_up(run)
    p.on_playback_status('answer', 'playing')
    job = final_command(run)
    assert run.controls == [('answer', 'pause')]
    reply(run, job)
    assert_local_failure(run)
    assert run.controls == [('answer', 'pause')]
    assert p.session.playback_id == 'answer' and p.session.playback_state == 'playing'


def test_new_wake_after_failed_interruption_times_out_while_old_answer_stays_paused(run):
    p = run.pipeline
    wake_up(run)
    p.on_playback_status('answer', 'playing')
    p.feed(VOICE * 4)
    assert run.controls == [('answer', 'pause')]
    p.on_playback_status('answer', 'paused')
    p.feed(QUIET * 100)
    failed = p.jobs.get_nowait()
    assert failed[0] == 'command'
    reply(run, failed)
    assert_local_failure(run)
    assert p.session.playback_state == 'paused' and p.session.playback_id == 'answer'

    run.now = .31
    wake_up(run)
    assert p.session.playback_state == 'paused'
    assert p._command_start_deadline == pytest.approx(5.31)
    run.now = 5.30
    p.poll()
    assert p.session.active and run.chimes == ['failure']
    run.now = 5.31
    p.poll()
    assert run.chimes == ['failure', 'failure'] and not p.session.active
    assert p._command_start_deadline is None and p._reply_request_id is None
    assert run.transcripts == run.interruptions == []
    assert run.statuses == [('', failed[2], 'started')]
    assert run.controls == [('answer', 'pause')]
    assert p.session.playback_state == 'paused'
    p.poll()
    reply(run, failed)
    assert run.chimes == ['failure', 'failure']
