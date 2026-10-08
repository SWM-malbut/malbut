"""Bound ordinary requests and account for every accepted utterance without audio."""

from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline


VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


@pytest.fixture
def run():
    state = SimpleNamespace(now=0.0, events=[], transcripts=[], controls=[],
                            cancelled=[], stopped=[], statuses=[], reports=[])
    state.pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=None, transcriber=None,
        is_speech=lambda frame, _: any(frame),
        publish_transcript=lambda *args: state.transcripts.append(args),
        publish_control=lambda *args: state.controls.append(args),
        publish_interruption=lambda *_: None,
        publish_input_status=lambda *args: state.statuses.append(args),
        report=state.reports.append, clock=lambda: state.now, input_has_aec=True,
        stop_speech=state.stopped.append,
        settings=CaptureSettings(silence_timeout_s=2.0),
    )
    state.pipeline.on_lifecycle = state.events.append
    state.pipeline.cancel_request = state.cancelled.append
    state.pipeline.receipt_timeout_s = 5.0
    state.pipeline.reply_timeout_s = 20.0
    yield state
    state.pipeline.close()


def publish_turn(run, text='원문은 진단에 저장하지 않음'):
    p = run.pipeline
    p.session.activate()
    p.feed(VOICE * 4 + QUIET * 100)
    job = p.jobs.get_nowait()
    p.results.put_nowait((*job[:3], text, None))
    p.poll()
    return job[2]


def test_missing_receipt_cancels_only_that_request_and_reopens_wake(run):
    uid = publish_turn(run)
    run.now = 4.999
    run.pipeline.poll()
    assert run.pipeline._input_blocked(run.now)
    run.now = 5.0
    run.pipeline.poll()
    assert run.cancelled == [uid] and run.stopped == []
    assert run.pipeline._reply_request_id is None
    assert not run.pipeline._input_blocked(run.now)
    assert not run.pipeline.session.active
    assert run.statuses == [('', uid, 'started')]
    assert any(e['event'] == 'request_terminal' and e['reason'] == 'receipt_timeout'
               for e in run.events)


def test_acceptance_and_duplicate_receipts_cannot_extend_total_deadline(run):
    uid = publish_turn(run)
    run.now = 4.0
    run.pipeline.on_request_status(uid, 'accepted')
    run.now = 19.9
    run.pipeline.on_request_status(uid, 'accepted')
    run.pipeline.poll()
    assert run.pipeline._reply_request_id == uid
    run.now = 20.0
    run.pipeline.poll()
    assert run.cancelled == [uid]
    assert run.pipeline._reply_request_id is None
    assert any(e['reason'] == 'reply_timeout' for e in run.events
               if e['event'] == 'request_terminal')


@pytest.mark.parametrize('state', ['rejected', 'failed', 'cancelled'])
def test_matching_request_failure_is_terminal_without_agent_failure_input(run, state):
    uid = publish_turn(run)
    run.pipeline.on_request_status('unrelated', state)
    assert run.pipeline._reply_request_id == uid
    run.pipeline.on_request_status(uid, state)
    assert run.pipeline._reply_request_id is None
    assert run.statuses == [('', uid, 'started')]
    assert run.transcripts == [(uid, '원문은 진단에 저장하지 않음')]


def test_timed_out_old_playback_cannot_release_or_pause_new_request(run):
    old_uid = publish_turn(run)
    run.now = 5.0
    run.pipeline.poll()
    new_uid = publish_turn(run)
    run.pipeline.on_request_status(new_uid, 'accepted')
    run.pipeline.on_speech_request('late-old', request_id=old_uid)
    run.pipeline.on_playback_status('late-old', 'playing', request_id=old_uid)
    run.pipeline.on_playback_status('late-old', 'finished', request_id=old_uid)
    assert run.pipeline._reply_request_id == new_uid
    assert run.pipeline.session.playback_id != 'late-old'
    assert run.stopped and set(run.stopped) == {'late-old'}
    assert run.controls == []


def test_timeout_preserves_other_gates_and_never_stops_unrelated_playback(run):
    uid = publish_turn(run)
    run.pipeline.on_request_status(uid, 'accepted')
    run.pipeline.on_playback_status('external', 'playing', request_id='other')
    assert run.pipeline.control_web_talk('web', True, 15.0, quiet=False)
    run.now = 14.0
    assert run.pipeline.control_web_talk('web', True, 15.0, quiet=False)
    run.now = 20.0
    run.pipeline.poll()
    assert run.pipeline._reply_request_id is None
    assert run.pipeline._input_blocked(run.now)
    assert run.cancelled == [uid] and run.stopped == []


@pytest.mark.parametrize('failure', [('','empty_transcript'), (None,'transcription_failed:ValueError')])
def test_utterance_failure_has_one_correlated_diagnostic_terminal(run, failure):
    p = run.pipeline
    assert p.start_session('confirmation')
    p.feed(VOICE * 4 + QUIET * 100)
    job = p.jobs.get_nowait()
    text, reason = failure
    error = 'ValueError' if text is None else None
    p.results.put_nowait((*job[:3], text, error))
    p.poll()
    p._accept_result(*job[:3], text, error)
    terminals = [e for e in run.events if e['event'] == 'utterance_terminal']
    assert len(terminals) == 1
    assert terminals[0]['utterance_id'] == job[2]
    assert terminals[0]['session_id'] == 'confirmation'
    assert terminals[0]['reason'] == reason
    assert p.session_is_active('confirmation')
    assert not any('text' in e or 'pcm' in e for e in run.events)


def test_publication_closes_utterance_lifecycle_before_request_lifecycle(run):
    uid = publish_turn(run)
    run.pipeline.on_request_status(uid, 'accepted')
    run.pipeline.on_playback_status('answer', 'finished', request_id=uid)
    utterance = [e for e in run.events if e['event'] == 'utterance_terminal']
    request = [e for e in run.events if e['event'] == 'request_terminal']
    assert len(utterance) == len(request) == 1
    assert utterance[0]['utterance_id'] == request[0]['request_id'] == uid
    assert utterance[0]['reason'] == 'published'
    assert request[0]['reason'] == 'playback_finished'
    assert '원문은' not in str(run.events)


@pytest.mark.parametrize('reason', ['audio_queue_overflow', 'utterance_discarded:buffer_overflow',
                                  'session_replaced:proactive', 'web_talk_started'])
def test_cancellation_and_overflow_close_the_original_utterance_once(run, reason):
    p = run.pipeline
    assert p.start_session('old-session')
    p.feed(VOICE * 4)
    uid = p.session.utterance_id
    p._terminate(reason)
    p._terminate(reason)
    terminals = [e for e in run.events if e['event'] == 'utterance_terminal']
    assert len(terminals) == 1
    assert terminals[0]['utterance_id'] == uid
    assert terminals[0]['session_id'] == 'old-session'
    assert terminals[0]['reason'] == reason


@pytest.mark.parametrize('aec', [False, True])
def test_wake_readiness_follows_actual_gate_and_keeps_first_fresh_onset(run, aec):
    p = run.pipeline
    p.input_has_aec = aec

    def sound():
        run.now = .18
        p.feed(VOICE * 4)  # Speaker output during the cue must never be accepted.

    p.on_wake = sound
    p._accept_result('wake', p._generation, None, '제이크야', None)
    p.poll()
    assert bool([e for e in run.events if e['event'] == 'input_ready']) is aec
    assert p.session.utterance_id is None
    if not aec:
        run.now = .479
        p.feed(VOICE * 4)
        assert p.session.utterance_id is None
        run.now = .48
    p.feed(VOICE * 4)
    assert p.session.utterance_id is not None
    assert len([e for e in run.events if e['event'] == 'input_ready']) == 1


def test_combined_wake_command_reuses_one_result_and_emits_one_lifecycle(run):
    p = run.pipeline
    cues = []
    p.on_endpoint = lambda: cues.append('endpoint')
    generation = p._generation
    p._accept_result('wake', generation, None, '제이크야 거실로 가 줘.', None)
    p._accept_result('wake', generation, None, '제이크야 거실로 가 줘.', None)
    assert len(run.transcripts) == 1 and run.transcripts[0][1] == '거실로 가 줘.'
    uid = run.transcripts[0][0]
    assert run.statuses == [('', uid, 'started')]
    assert p.jobs.empty() and cues == ['endpoint']
    assert [e['reason'] for e in run.events if e['event'] == 'utterance_terminal'] == ['published']
    assert not any(e['event'] == 'input_ready' for e in run.events)


def test_inference_watchdog_uses_real_device_clock_even_after_turn_cancel(run, monkeypatch):
    p = run.pipeline
    real_now = [100.0]
    monkeypatch.setattr('malbut_stt.dialogue_pipeline.monotonic', lambda: real_now[0])
    p._inference_started_at = 100.0
    p.inference_timeout_s = 2.0
    p._terminate('session_replaced:proactive')
    run.now = 10000.0
    real_now[0] = 101.999
    p.poll()
    real_now[0] = 102.0
    with pytest.raises(RuntimeError, match='inference deadline exceeded'):
        p.poll()


def test_overlong_combined_wake_never_submits_a_truncated_command(run):
    p = run.pipeline
    p.feed(VOICE * 301 + QUIET * 20)
    assert p.jobs.empty() and run.transcripts == []
    assert 'wake_too_long' in run.reports


def test_raw_reply_timeout_keeps_echo_gate_until_actual_old_terminal(run):
    p = run.pipeline
    p.input_has_aec = False
    uid = publish_turn(run)
    p.on_playback_status('old-answer', 'playing', request_id=uid)
    run.now = 20.0
    p.poll()
    assert p._reply_request_id is None and p._input_blocked(run.now)
    assert run.cancelled == [uid] and run.stopped == ['old-answer']
    p.on_playback_status('unrelated', 'stopped', request_id=uid)
    assert p._input_blocked(run.now)
    p.on_playback_status('old-answer', 'stopped', request_id=uid)
    assert p._input_blocked(run.now)
    run.now += .3
    assert not p._input_blocked(run.now)


def test_duplicate_retired_terminal_does_not_drop_new_raw_capture(run):
    p = run.pipeline
    p.input_has_aec = False
    uid = publish_turn(run)
    p.on_playback_status('old-answer', 'playing', request_id=uid)
    run.now = 20.0
    p.poll()
    p.on_playback_status('old-answer', 'stopped', request_id=uid)
    run.now += .3
    p.session.activate()
    p.feed(VOICE * 4)
    new_uid = p.session.utterance_id
    audio_generation = p._audio_generation
    p.on_playback_status('old-answer', 'stopped', request_id=uid)
    assert p.session.utterance_id == new_uid
    assert p.command_stream.collector.audio == VOICE * 4
    assert p._audio_generation == audio_generation
    assert not p._input_blocked(run.now)


def test_late_old_playback_gates_raw_input_without_replacing_new_request(run):
    p = run.pipeline
    p.input_has_aec = False
    old_uid = publish_turn(run)
    run.now = 5.0
    p.poll()
    new_uid = publish_turn(run)
    p.on_playback_status('late-old', 'playing', request_id=old_uid)
    assert p._reply_request_id == new_uid
    assert p.session.playback_id != 'late-old'
    p.on_request_status(new_uid, 'rejected')
    assert p._reply_request_id is None
    assert p._input_blocked(run.now)
    p.session.activate()
    p.feed(VOICE * 4)
    assert p.session.utterance_id is None
    p.on_playback_status('late-old', 'stopped', request_id='wrong-request')
    assert p._input_blocked(run.now)
    run.now = 305.01  # Sound cannot be declared stopped merely because an ID TTL expires.
    assert p._input_blocked(run.now)
    p.on_playback_status('late-old', 'stopped', request_id=old_uid)
    assert p._input_blocked(run.now)
    run.now += .3
    p.feed(VOICE * 4)
    assert p.session.utterance_id is not None
    assert p.command_stream.collector.audio == VOICE * 4


def test_late_old_raw_playback_discards_current_candidate_without_publishing(run):
    p = run.pipeline
    p.input_has_aec = False
    old_uid = publish_turn(run)
    run.now = 5.0
    p.poll()
    p.session.activate()
    p.feed(VOICE * 4)
    candidate = p.session.utterance_id
    p.on_playback_status('late-old', 'playing', request_id=old_uid)
    assert p.session.utterance_id is None
    assert p._input_blocked(run.now)
    terminals = [e for e in run.events if e['event'] == 'utterance_terminal'
                 and e['utterance_id'] == candidate]
    assert len(terminals) == 1
    assert terminals[0]['reason'] == 'utterance_discarded:obsolete_playback_without_aec'
    assert len(run.transcripts) == 1


def test_request_quiescence_recovers_raw_gate_when_terminal_topic_is_lost(run):
    p = run.pipeline
    p.input_has_aec = False
    uid = publish_turn(run)
    p.on_playback_status('old-answer', 'playing', request_id=uid)
    run.now = 20.0
    p.poll()
    assert p._input_blocked(run.now)
    assert not p.on_request_quiescent('unrelated')
    assert p._input_blocked(run.now)
    assert p.on_request_quiescent(uid)
    assert p.session.playback_state == 'stopped'
    assert p._input_blocked(run.now)
    run.now += .3
    assert not p._input_blocked(run.now)
    assert p.on_request_quiescent(uid)  # A duplicate ACK cannot renew the tail.
    assert not p._input_blocked(run.now)


def test_old_quiescence_preserves_new_request_and_unrelated_raw_playback(run):
    p = run.pipeline
    p.input_has_aec = False
    old_uid = publish_turn(run)
    run.now = 5.0
    p.poll()
    new_uid = publish_turn(run)
    p.on_playback_status('late-old', 'playing', request_id=old_uid)
    p.on_playback_status('new-answer', 'playing', request_id=new_uid)
    assert not p.on_request_quiescent(new_uid)
    assert p.on_request_quiescent(old_uid)
    assert p._reply_request_id == new_uid
    assert p.session.playback_id == 'new-answer'
    assert p.session.playback_state == 'playing'
    assert p._raw_playback_gate and p._input_blocked(run.now)
    assert not p._obsolete_playback_ids


def test_quiescence_for_an_unowned_playback_cannot_clear_its_raw_gate(run):
    p = run.pipeline
    p.input_has_aec = False
    p.on_playback_status('external-answer', 'playing', request_id='external-request')
    assert not p.on_request_quiescent('external-request')
    assert p.session.playback_id == 'external-answer'
    assert p.session.playback_state == 'playing'
    assert p._raw_playback_gate


def test_delayed_old_playing_after_quiescence_cannot_reblock_new_capture(run):
    p = run.pipeline
    p.input_has_aec = False
    uid = publish_turn(run)
    p.on_playback_status('old-answer', 'playing', request_id=uid)
    run.now = 20.0
    p.poll()
    assert p.on_request_quiescent(uid)
    run.now += .3
    p.session.activate()
    p.feed(VOICE * 4)
    new_uid = p.session.utterance_id
    audio_generation = p._audio_generation
    p.on_playback_status('late-old', 'playing', request_id=uid)
    assert p.session.utterance_id == new_uid
    assert p.command_stream.collector.audio == VOICE * 4
    assert p._audio_generation == audio_generation
    assert not p._input_blocked(run.now)
    assert run.stopped[-1] == 'late-old'


def test_request_quiescence_history_is_bounded_and_expires(run):
    p = run.pipeline
    for index in range(300):
        uid = str(index)
        p._reply_request_id = uid
        p._finish_reply('receipt_timeout', cancel=True)
        assert p.on_request_quiescent(uid)
    assert len(p._quiescent_request_ids) == 256
    run.now = 300.0
    assert not p.on_request_quiescent('299')
    assert not p._quiescent_request_ids


def test_retired_request_memory_is_bounded_and_old_receipt_never_reactivates(run):
    p = run.pipeline
    for index in range(300):
        uid = str(index)
        p._reply_request_id = uid
        p._finish_reply('receipt_timeout')
    assert len(p._retired_request_ids) == 256
    p.on_request_status('299', 'accepted')
    assert p._reply_request_id is None
    run.now = 300.0
    assert not p._request_is_retired('299')
    assert not p._retired_request_ids


def test_busy_candidate_is_diagnostic_only_and_gate_reason_has_no_audio(run):
    p = run.pipeline
    p.session.activate()
    p.feed(VOICE * 4 + QUIET * 100, busy_at_capture=True)
    assert run.statuses == run.transcripts == []
    assert any(e['event'] == 'utterance_terminal' and e['reason'] == 'busy'
               for e in run.events)
    assert p.control_web_talk('web', True, 1.0, quiet=False)
    p.feed(VOICE * 4)
    assert any(e['event'] == 'input_gate' and e['reason'] == 'web_talk'
               for e in run.events)
    assert not any('text' in e or 'pcm' in e for e in run.events)


def test_agent_work_finished_cannot_unlock_playback_gate(run):
    uid = publish_turn(run)
    run.pipeline.on_request_status(uid, 'accepted')
    run.pipeline.on_request_status(uid, 'finished')
    assert run.pipeline._reply_request_id == uid
    run.pipeline.on_playback_status('final', 'finished', request_id=uid)
    assert run.pipeline._reply_request_id is None


def test_prepare_shutdown_cancels_before_cleanup_and_is_idempotent(run):
    p = run.pipeline
    uid = publish_turn(run)
    p.prepare_shutdown()
    assert run.cancelled == [uid]
    assert p.stopping.is_set() and not p._closed
    p.prepare_shutdown()
    p.close()
    assert run.cancelled == [uid]


def test_web_lease_status_rechecks_shortened_ttl_and_current_id(run):
    p = run.pipeline
    assert not p.web_talk_is_active('web')
    assert p.control_web_talk('web', True, 10.0, quiet=False)
    run.now = 1.0
    assert p.control_web_talk('web', True, .5, quiet=False)
    assert p.web_talk_is_active('web')
    run.now = 1.5
    assert not p.web_talk_is_active('web')
    assert p.control_web_talk('next', True, 10.0, quiet=False)
    assert not p.web_talk_is_active('web') and p.web_talk_is_active('next')
    assert p.control_web_talk('next', False, 0.0)
    assert not p.web_talk_is_active('next')
    assert p.control_web_talk('last', True, 10.0, quiet=False)
    p.prepare_shutdown()
    assert not p.web_talk_is_active('last')


@pytest.mark.parametrize('name', ['receipt_timeout_s', 'reply_timeout_s', 'inference_timeout_s'])
@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf')])
def test_deadlines_reject_invalid_configuration_before_start(name, value):
    with pytest.raises(ValueError, match=name):
        DialoguePipeline(
            recorder_factory=lambda: None, wake=None, transcriber=None, is_speech=lambda *_: False,
            publish_transcript=lambda *_: None, publish_control=lambda *_: None,
            publish_interruption=lambda *_: None, report=lambda *_: None, **{name: value})
