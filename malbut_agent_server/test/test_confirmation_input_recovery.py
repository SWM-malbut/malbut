"""Actual incremental guard -> STT worker -> fall session, without a microphone."""

from collections import deque
from concurrent.futures import Future
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from malbut_agent_server.situation_dialogue import MockSituationProvider, SituationDialogue
from test_situation_session import Harness


@pytest.fixture
def trial(monkeypatch, request):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / 'malbut_stt'))
    from malbut_stt.dialogue_pipeline import DialoguePipeline
    from malbut_stt.incremental import IncrementalWhisperStream

    h = Harness()
    h.engine = SituationDialogue(MockSituationProvider())
    modes, calls, statuses, transcripts, reports = deque(), [], [], [], []

    class Model:
        def transcribe(self, audio, **options):
            mode = modes.popleft()
            calls.append((mode, len(audio)))
            # No exception is injected. Production IncrementalWhisperStream
            # rejects this real-shaped decode because VAD observed 2 s of speech.
            segments = [] if mode == 'empty' else [SimpleNamespace(
                text=('누락된 앞부분' if mode == 'missing' else
                      '무슨 말인지 모르겠어' if mode == 'ambiguous' else '도와줘'),
                start=0., end=.2 if mode == 'missing' else 1.9)]
            return iter(segments), None

    backend = SimpleNamespace(create_stream=lambda: IncrementalWhisperStream(Model()))

    def status(*event):
        statuses.append(event)
        h.session.input_status(*event)

    def transcript(uid, text):
        transcripts.append(text)
        h.session.transcript(pipeline.session.session_id, uid, text)

    pipeline = DialoguePipeline(recorder_factory=None, wake=None, transcriber=backend,
        is_speech=lambda frame, _: any(frame), publish_transcript=transcript,
        publish_control=lambda *_: None, publish_interruption=lambda *_: None,
        publish_input_status=status, report=reports.append, clock=lambda: h.now,
        partial_interval_s=.8 if getattr(request, 'param', '') == 'previews' else 100.,
        endpoint_predecode_s=None)

    def response(accepted):
        future = Future()
        future.set_result(SimpleNamespace(accepted=accepted))
        return future

    openings = []
    def open_session(sid):
        accepted = pipeline.start_session(sid)
        future = (Future() if getattr(request, 'param', '') == 'delayed_open' and not openings
                  else response(accepted))
        openings.append(future)
        return future
    h.open_session = open_session
    h.close_session = lambda sid: response(pipeline.stop_session(sid))
    h.verify_session = lambda sid: response(pipeline.session_is_active(sid))
    pipeline.asr_thread = Thread(target=pipeline._infer)
    pipeline.asr_thread.start()
    h.begin_question()
    h.session.playback(h.session.playback_id, 'finished')
    h.session.tick()
    assert h.session.phase == ('opening' if getattr(request, 'param', '') == 'delayed_open'
                               else 'listening')

    def capture(mode):
        modes.append(mode)
        h.now += 1
        pipeline.feed(b'\x01\x00' * (320 * 100) + bytes(640 * 100))
        deadline = monotonic() + 2
        while h.session.phase in ('hearing', 'opening'):
            pipeline.poll()
            assert monotonic() < deadline, (h.session.phase, reports)
            sleep(.001)

    state = SimpleNamespace(h=h, pipeline=pipeline, statuses=statuses,
        transcripts=transcripts, reports=reports, calls=calls, capture=capture,
        openings=openings, modes=modes)
    yield state
    h.session.cancel()
    pipeline.close()
    assert not pipeline.asr_thread.is_alive()


def replay(trial):
    h = trial.h
    old_session = h.session.session_id
    first_question = next(event[1] for event in h.events if event[0] == 'speak')
    trial.capture('missing')
    assert trial.calls and trial.calls[-1][0] == 'missing'
    assert h.session.phase == 'retrying' and not h.finishes
    assert trial.transcripts == []
    h.session.tick()
    assert h.session.phase == 'replaying'
    spoken = [event for event in h.events if event[0] == 'speak']
    assert [event[1] for event in spoken] == [first_question, first_question]
    assert spoken[0][2] != spoken[1][2]
    h.session.playback(h.session.playback_id, 'finished')
    h.session.tick()
    assert h.session.phase == 'listening'
    assert h.session.session_id and h.session.session_id != old_session
    assert not h.engine._history  # No incomplete text was interpreted.
    return old_session


def test_actual_missing_tail_replays_once_then_complete_answer_succeeds(trial):
    old = replay(trial)
    assert 'transcription_failed:MissingRecentSpeech' in trial.reports
    failures = [(sid, uid, state) for sid, uid, state in trial.statuses if state != 'started']
    assert failures[0][0] == old and failures[0][1] and failures[0][2] == 'incomplete'
    assert failures[1] == (old, '', 'failed')
    # Ordered old-session fallback, duplicates and late text cannot fail or
    # answer the fresh turn, even when delivered after its opening ACK.
    trial.h.session.input_status(old, '', 'failed')
    trial.h.session.input_status(old, 'old-utterance', 'incomplete')
    assert not trial.h.session.transcript(old, 'old-utterance', '도와줘')
    trial.capture('complete')
    trial.h.complete_work()
    assert trial.h.outcomes[0].help_needed is True
    assert trial.h.session.no_response is False
    trial.h.session.playback(trial.h.session.playback_id, 'finished')
    assert trial.h.finishes[0][0] == 'succeeded'
    assert trial.transcripts == ['도와줘']


@pytest.mark.parametrize('second', ['missing', 'empty', 'silence', 'hardware'])
def test_failed_retry_or_silence_never_becomes_no_response(trial, second):
    replay(trial)
    h = trial.h
    if second in ('missing', 'empty'):
        trial.capture(second)
    elif second == 'hardware':
        h.session.input_status(h.session.session_id, '', 'failed')
    else:
        h.now += 10
        h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert h.outcomes == [] and h.session.no_response is False
    assert len([e for e in h.events if e[0] == 'speak']) == 2


def test_retry_does_not_renew_original_hearing_budget(trial):
    trial.capture('missing')
    h = trial.h
    assert h.session.phase == 'retrying'
    original_deadline = h.session._deadline
    h.now = original_deadline - .1
    h.session.tick()
    assert h.session.phase == 'replaying'
    h.session.playback(h.session.playback_id, 'finished')
    h.session.tick()
    h.now = original_deadline
    h.session.tick()
    assert h.finishes == [('aborted', None)]
    assert not h.outcomes


def test_cancel_while_closing_failed_input_never_replays(trial):
    close = Future()
    trial.h.close_session = lambda _sid: close
    trial.capture('missing')
    h = trial.h
    assert h.session.phase == 'retrying'
    h.session.tick()
    assert len([e for e in h.events if e[0] == 'speak']) == 1
    h.session.cancel()
    assert close.cancelled()
    h.session.tick()
    assert h.finishes == [('canceled', None)]
    assert len([e for e in h.events if e[0] == 'speak']) == 1


def test_other_situation_keeps_operational_failure(trial):
    trial.h.session.request.situation_type = 'other'
    trial.capture('missing')
    assert trial.h.finishes == [('aborted', None)]
    assert len([e for e in trial.h.events if e[0] == 'speak']) == 1


@pytest.mark.parametrize('trial', ['delayed_open'], indirect=True)
def test_started_and_missing_tail_before_open_ack_still_close_before_replay(trial):
    old = replay(trial)
    assert trial.openings[0].cancelled()
    assert not trial.pipeline.start_session(old)  # A delayed START cannot reopen the retired ID.
    trial.h.session.input_status(old, '', 'failed')
    assert trial.h.session.phase == 'listening'


def test_old_owner_ignores_new_state_but_receives_original_operational_failure(trial):
    current = trial.h.session.input_status
    trial.h.session.input_status = lambda sid, uid, state: (
        None if state == 'incomplete' else current(sid, uid, state))
    trial.capture('missing')
    assert trial.h.finishes == [('aborted', None)]
    assert not trial.h.outcomes and not trial.h.session.no_response
    assert trial.statuses[-1][1:] == ('', 'failed')


def test_cancel_during_replay_rejects_late_playback_and_input(trial):
    trial.capture('missing')
    h = trial.h
    old = h.session._last_session_id
    h.session.tick()
    assert h.session.phase == 'replaying'
    playback = h.session.playback_id
    h.session.cancel()
    h.session.playback(playback, 'finished')
    h.session.input_status(old, 'late', 'incomplete')
    assert not h.session.transcript(old, 'late', '도와줘')
    assert h.finishes == [('canceled', None)] and len(trial.openings) == 1


def test_retry_is_one_per_confirmation_not_one_per_microphone_session(trial):
    replay(trial)
    trial.capture('ambiguous')
    h = trial.h
    h.complete_work()
    assert h.session.phase == 'speaking' and not h.outcomes
    h.session.playback(h.session.playback_id, 'finished')
    h.session.tick()
    trial.capture('missing')
    assert h.finishes == [('aborted', None)]
    assert len([e for e in h.events if e[0] == 'speak']) == 3


def test_stale_utterance_in_current_session_cannot_replay(trial):
    h = trial.h
    sid = h.session.session_id
    h.session.input_status(sid, 'current', 'started')
    h.session.input_status(sid, 'different', 'incomplete')
    assert h.session.phase == 'hearing' and not h.session._input_retry_used


def pump_until(trial, predicate):
    deadline = monotonic() + 2
    while not predicate():
        trial.pipeline.poll()
        assert monotonic() < deadline, trial.reports
        sleep(.001)


def test_ordinary_missing_tail_keeps_failure_cue_without_confirmation_retry(trial):
    trial.h.session.cancel()
    trial.pipeline.session.activate()
    trial.reports.clear()
    trial.statuses.clear()
    trial.modes.append('missing')
    trial.pipeline.feed(b'\x01\x00' * (320 * 100) + bytes(640 * 100))
    pump_until(trial, lambda: 'waiting_for_wake' in trial.reports)
    assert 'transcription_failed:MissingRecentSpeech' in trial.reports
    assert 'failure_chime_unavailable' in trial.reports
    assert not trial.pipeline.session.active and trial.transcripts == []
    assert not any(state == 'incomplete' for _, _, state in trial.statuses)


@pytest.mark.parametrize('trial', ['previews'], indirect=True)
def test_missing_preview_still_recovers_final_without_an_audible_retry(trial):
    trial.modes.extend(['missing'] + ['complete'] * 5)
    trial.pipeline.feed(b'\x01\x00' * (320 * 100))
    pump_until(trial, lambda: 'partial_failed' in trial.reports)
    assert trial.h.session.phase == 'hearing' and not trial.h.session._input_retry_used
    trial.pipeline.feed(bytes(640 * 100))
    pump_until(trial, lambda: trial.h.session.phase == 'thinking')
    trial.h.complete_work()
    assert trial.transcripts == ['도와줘']
    assert trial.h.outcomes[0].help_needed is True
    assert len([e for e in trial.h.events if e[0] == 'speak']) == 2  # Question + closing.
    assert not any(state == 'incomplete' for _, _, state in trial.statuses)
