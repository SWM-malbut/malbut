"""Continuous dialogue tests use deterministic PCM, local-model fakes, and no ROS."""

from queue import Queue
import json
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline, MAX_RETIRED_SESSION_IDS
from malbut_stt.audio import CaptureSettings, MicrophoneOverflow

VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


def wait_for(predicate):
    deadline = monotonic() + 2.0
    while not predicate():
        assert monotonic() < deadline, 'background worker did not reach expected state'
        sleep(0.001)


@pytest.fixture
def harness():
    state = SimpleNamespace(
        now=0.0, transcripts=[], controls=[], candidates=[], reports=[],
        wake_calls=[], command_calls=[], closed=[], allow_asr=Event(), entered_asr=Event(),
    )
    state.allow_asr.set()

    class Recorder:
        sample_rate = 16000

        def __init__(self):
            self.frames = Queue()
            self.active = False
            self.reads = 0

        def start(self):
            self.active = True

        def read(self):
            self.reads += 1
            frame = self.frames.get(timeout=3.0)
            if isinstance(frame, Exception):
                raise frame
            return frame

        def stop(self):
            self.active = False
            state.closed.append('stop')
            self.frames.put([0] * 320)
            state.allow_asr.set()

        def delete(self):
            state.closed.append('delete')

    state.recorder = Recorder()

    def wake(pcm, rate):
        assert state.recorder.active
        state.wake_calls.append((pcm, rate))
        return '제이크야'

    def transcribe(pcm, rate):
        assert state.recorder.active
        state.command_calls.append((pcm, rate))
        state.entered_asr.set()
        assert state.allow_asr.wait(timeout=3.0)
        return '문장 ' + str(len(state.command_calls))

    def create(*, aec=True, **options):
        # Keep the one-frame onsets used by these session/race boundary fixtures.
        options.setdefault('settings', CaptureSettings(
            silence_timeout_s=2.0, min_speech_s=.02))
        pipeline = DialoguePipeline(
            recorder_factory=lambda: state.recorder,
            wake=SimpleNamespace(transcribe=wake),
            transcriber=SimpleNamespace(transcribe=transcribe),
            is_speech=lambda frame, rate: frame[:2] != b'\x00\x00',
            publish_transcript=lambda uid, text: state.transcripts.append((uid, text)),
            publish_control=lambda pid, cmd: state.controls.append((pid, cmd)),
            publish_interruption=lambda uid, pid, text: state.candidates.append((uid, pid, text)),
            report=state.reports.append, clock=lambda: state.now, input_has_aec=aec,
            **options,
        )
        state.pipeline = pipeline
        pipeline.start()
        return pipeline

    state.create = create
    yield state
    if hasattr(state, 'pipeline'):
        state.pipeline.close()


def pump(pipeline, predicate):
    def advance():
        pipeline.poll()
        return predicate()
    wait_for(advance)


def wake_up(harness, pipeline):
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    assert len(harness.wake_calls) == 1


def finish_command(pipeline):
    pipeline.feed(VOICE + QUIET * 100)


def interrupt(harness, pipeline):
    wake_up(harness, pipeline)
    pipeline.on_playback_status('p1', 'playing')
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    assert harness.controls == [('p1', 'pause')]
    assert harness.candidates == []
    pipeline.feed(QUIET * 99)
    assert harness.candidates == [] and not pipeline._busy
    pipeline.feed(QUIET)
    pump(pipeline, lambda: len(harness.candidates) == 1)
    return uid


@pytest.mark.parametrize('aec', [False, True])
def test_each_command_requires_wake_and_waits_for_its_final_reply(harness, aec):
    pipeline = harness.create(aec=aec)
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    first_id = harness.transcripts[0][0]
    assert not pipeline.session.active
    # TV speech and even wake phrases cannot queue while Agent/TTS prepares.
    finish_command(pipeline)
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pipeline.poll()
    assert len(harness.command_calls) == len(harness.wake_calls) == 1
    for state in ('playing', 'finished'):
        pipeline.on_playback_status('progress', state, interim=True, request_id=first_id)
    pipeline.on_playback_status('other', 'failed', request_id='unrelated')
    harness.now = 1.0
    finish_command(pipeline)
    assert len(harness.transcripts) == 1 and pipeline.jobs.empty()
    pipeline.on_playback_status('answer', 'playing', request_id=first_id)
    finish_command(pipeline)
    assert len(harness.transcripts) == 1
    pipeline.on_playback_status('answer', 'finished', request_id=first_id)
    assert not pipeline.session.active
    harness.now = 1.31
    pipeline.wake.transcribe = lambda pcm, rate: '구독과 좋아요 부탁드려요'
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: 'not_wake' in harness.reports)
    assert len(harness.transcripts) == 1
    pipeline.wake.transcribe = lambda pcm, rate: '제이크야'
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 2)
    assert harness.transcripts == [(first_id, '문장 1'),
                                   (harness.transcripts[1][0], '문장 2')]
    assert harness.transcripts[1][0] != first_id
    # A late terminal from the old request cannot unlock the new pending turn.
    pipeline.on_playback_status('answer', 'finished', request_id=first_id)
    finish_command(pipeline)
    assert len(harness.command_calls) == 2
    assert harness.recorder.active and harness.closed == []
    assert 'wake_chime_unavailable' in harness.reports
    pipeline.close()
    pipeline.close()
    assert harness.closed == ['stop', 'delete']
    assert not pipeline.capture_thread.is_alive() and not pipeline.asr_thread.is_alive()
    assert pipeline.audio.empty() and pipeline.jobs.empty() and pipeline.results.empty()


@pytest.mark.parametrize('state', ['failed', 'stopped'])
def test_reply_terminal_before_playing_reopens_wake_and_discards_buffered_audio(harness, state):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    uid = harness.transcripts[0][0]
    # A queued progress notice may fail or be cancelled without finishing the turn.
    pipeline.on_playback_status('progress', state, interim=True, request_id=uid)
    assert pipeline._input_blocked(harness.now)
    pipeline.audio.put_nowait((pipeline._audio_generation, harness.now, VOICE * 4, False))
    pipeline.on_playback_status('answer', state, request_id=uid)
    assert not pipeline._input_blocked(harness.now)
    assert not pipeline.session.active and pipeline.audio.empty()
    assert not pipeline.wake_stream.collector.started
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    assert len(harness.wake_calls) == 2


def test_proactive_session_replaces_pending_reply_without_waiting_for_it(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    ordinary_id = harness.transcripts[0][0]
    assert pipeline.start_session('incident-1')
    pipeline.on_playback_status('old-answer', 'failed', request_id=ordinary_id)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 2)
    assert pipeline.session.session_id == 'incident-1'
    assert pipeline.session.active and not pipeline._input_blocked(harness.now)


def test_audio_overflow_during_reply_wait_cannot_reopen_input(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    uid = harness.transcripts[0][0]
    pipeline.overflow.set()
    pipeline.poll()
    pipeline.feed(VOICE * 4 + QUIET * 20)
    finish_command(pipeline)
    assert len(harness.wake_calls) == len(harness.command_calls) == 1
    assert 'audio_queue_overflow' in harness.reports
    assert pipeline._input_blocked(harness.now)
    pipeline.on_playback_status('answer', 'failed', request_id=uid)
    assert not pipeline._input_blocked(harness.now)


def test_proactive_session_stops_question_at_speech_start_without_addressee(harness):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(args))
    assert pipeline.start_session('incident-1')
    pipeline.on_playback_status('question-1', 'playing')
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    assert harness.controls == [('question-1', 'stop')]
    assert statuses == [('incident-1', uid, 'started')]
    assert harness.transcripts == []
    pipeline.on_playback_status('question-1', 'stopped')
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts == [(uid, '문장 1')]
    assert harness.wake_calls == harness.candidates == []


def test_proactive_deadline_belongs_to_agent_and_stale_close_is_rejected(harness):
    pipeline = harness.create()
    assert pipeline.start_session('incident-1')
    pipeline.on_playback_status('question-1', 'playing')
    pipeline.on_playback_status('question-1', 'finished')
    harness.now = 11.0
    pipeline.poll()
    assert pipeline.session.active and pipeline.session.deadline is None
    assert pipeline.stop_session('not-yet-opened-incident')
    assert pipeline.session.session_id == 'incident-1'
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert pipeline.stop_session('incident-1')
    assert not pipeline.session.active and pipeline.session.session_id == ''


def test_close_before_open_prevents_late_session_activation(harness):
    pipeline = harness.create()
    assert pipeline.stop_session('canceled-before-open')
    assert not pipeline.start_session('canceled-before-open')
    assert not pipeline.session.active and not pipeline.session.session_id
    assert not pipeline.stop_session('canceled-before-open')


def test_completed_or_replaced_session_cannot_supersede_current_session(harness):
    pipeline = harness.create()
    assert pipeline.start_session('first')
    assert pipeline.start_session('second')
    assert not pipeline.start_session('first')
    assert not pipeline.stop_session('first')
    assert pipeline.session.session_id == 'second'
    assert pipeline.stop_session('second')
    assert not pipeline.start_session('second')
    assert pipeline.start_session('third')
    assert pipeline.stop_session('not-yet-received')
    assert not pipeline.start_session('not-yet-received')
    assert pipeline.session.session_id == 'third'


def test_retired_session_reservations_are_bounded_and_validate_ids(harness):
    pipeline = harness.create()
    for invalid in ('', ' ', None, 'x' * 201):
        assert not pipeline.stop_session(invalid)
    for index in range(MAX_RETIRED_SESSION_IDS + 1):
        assert pipeline.stop_session(f'future-{index}')
    assert len(pipeline._retired_session_ids) == MAX_RETIRED_SESSION_IDS
    assert 'future-0' not in pipeline._retired_session_ids
    assert not pipeline.start_session(f'future-{MAX_RETIRED_SESSION_IDS}')


def test_session_query_never_creates_closes_or_retires_sessions(harness):
    pipeline = harness.create()
    assert not pipeline.session_is_active('not-opened')
    assert pipeline._retired_session_ids == {}
    assert pipeline.start_session('first')
    for _ in range(2):
        assert pipeline.session_is_active('first')
        assert not pipeline.session_is_active('not-opened')
    assert pipeline.session.session_id == 'first'
    assert pipeline._retired_session_ids == {}
    assert pipeline.stop_session('first')
    retired = dict(pipeline._retired_session_ids)
    assert not pipeline.session_is_active('first')
    assert not pipeline.session_is_active('not-opened')
    assert dict(pipeline._retired_session_ids) == retired
    assert not pipeline.session.active


def test_proactive_speech_before_playing_stops_delayed_question(harness):
    pipeline = harness.create()
    assert pipeline.start_session('incident-1')
    pipeline.feed(VOICE)
    pipeline.on_playback_status('question-1', 'playing')
    assert harness.controls == [('question-1', 'stop')]
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.candidates == []


def test_proactive_replacement_discards_ordinary_asr_result(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1.0)
    assert pipeline.start_session('incident-1')
    harness.allow_asr.set()
    pump(pipeline, lambda: not pipeline._busy)
    assert harness.transcripts == []
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts[0][1] == '문장 2'


def test_next_question_session_discards_previous_question_asr_result(harness):
    pipeline = harness.create()
    assert pipeline.start_session('question-1')
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1.0)
    assert pipeline.start_session('question-2')
    assert not pipeline.stop_session('question-1')
    harness.allow_asr.set()
    pump(pipeline, lambda: not pipeline._busy)
    assert harness.transcripts == []
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert pipeline.session.session_id == 'question-2'
    assert harness.transcripts[0][1] == '문장 2'


def test_proactive_answer_is_captured_while_previous_asr_is_still_running(harness):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(args))
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1.0)
    assert pipeline.start_session('confirmation-1')
    pipeline.on_playback_status('question-1', 'playing')
    pipeline.on_playback_status('question-1', 'finished')
    finish_command(pipeline)
    assert statuses[0][0::2] == ('', 'started')
    assert statuses[1][0::2] == ('confirmation-1', 'started')
    assert pipeline._busy and pipeline.jobs.qsize() == 1
    assert 'speech_discarded:busy' not in harness.reports
    harness.allow_asr.set()
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts == [(statuses[1][1], '문장 2')]


def test_previous_asr_result_cannot_release_new_generation_busy_owner(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1.0)
    next_entered, next_allowed = Event(), Event()

    def next_transcribe(pcm, rate):
        next_entered.set()
        assert next_allowed.wait(timeout=3.0)
        return '새 확인 답변'

    pipeline.transcriber.transcribe = next_transcribe
    try:
        assert pipeline.start_session('confirmation-1')
        finish_command(pipeline)
        assert pipeline._busy
        harness.allow_asr.set()
        assert next_entered.wait(timeout=1.0)
        # The sole worker has returned the old ASR and is now decoding our answer.
        pipeline.poll()
        assert pipeline._busy and harness.transcripts == []
        next_allowed.set()
        pump(pipeline, lambda: len(harness.transcripts) == 1)
        assert harness.transcripts[0][1] == '새 확인 답변'
    finally:
        next_allowed.set()


def test_proactive_replacement_removes_queued_obsolete_inference(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1.0)
    pipeline.jobs.put_nowait(('endpoint', pipeline._generation, ('old', 1), VOICE))
    pipeline._endpoint_job = (pipeline._generation, 'old', 1)
    assert pipeline.start_session('confirmation-1')
    assert pipeline.jobs.empty() and pipeline._endpoint_job is None
    finish_command(pipeline)
    assert pipeline.jobs.qsize() == 1
    harness.allow_asr.set()
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert len(harness.command_calls) == 2


def test_proactive_no_aec_still_blocks_playback_echo(harness):
    statuses = []
    pipeline = harness.create(aec=False, publish_input_status=lambda *a: statuses.append(a))
    assert pipeline.start_session('incident-1')
    pipeline.on_playback_status('question-1', 'playing')
    finish_command(pipeline)
    assert statuses == [] and harness.transcripts == []
    pipeline.on_playback_status('question-1', 'finished')
    harness.now = 0.31
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert statuses[0][0::2] == ('incident-1', 'started')


def test_addressed_interruption_stops_and_publishes_once_after_matching_decision(harness):
    pipeline = harness.create()
    uid = interrupt(harness, pipeline)
    assert harness.candidates == [(uid, 'p1', '문장 1')]
    assert harness.transcripts == []
    pipeline.on_addressee('wrong', 'p1', 'addressed')
    pipeline.on_addressee(uid, 'wrong', 'addressed')
    assert harness.transcripts == [] and len(harness.controls) == 1
    pipeline.on_addressee(uid, 'p1', 'addressed')
    pipeline.on_addressee(uid, 'p1', 'addressed')
    assert harness.controls == [('p1', 'pause'), ('p1', 'stop')]
    assert harness.transcripts == [(uid, '문장 1')]
    assert not pipeline.session.active and pipeline._input_blocked(harness.now)


def test_not_addressed_waits_for_pause_ack_then_resumes_without_transcript(harness):
    pipeline = harness.create()
    uid = interrupt(harness, pipeline)
    pipeline.on_addressee(uid, 'p1', 'not_addressed')
    assert harness.controls == [('p1', 'pause')]
    pipeline.on_playback_status('p1', 'paused')
    pipeline.on_playback_status('p1', 'paused')
    pipeline.on_addressee(uid, 'p1', 'not_addressed')
    assert harness.controls == [('p1', 'pause'), ('p1', 'resume')]
    assert harness.transcripts == [] and pipeline.session.active


@pytest.mark.parametrize('reason', ['agent', 'timeout'])
def test_unknown_discards_without_resume_and_requires_fresh_wake(harness, reason):
    pipeline = harness.create()
    uid = interrupt(harness, pipeline)
    pipeline.on_playback_status('p1', 'paused')
    if reason == 'agent':
        pipeline.on_addressee(uid, 'p1', 'unknown')
    else:
        harness.now = 44.999
        pipeline.poll()
        assert pipeline.session.active
        harness.now = 45.0
        pipeline.poll()
    assert not pipeline.session.active
    assert harness.controls == [('p1', 'pause')] and harness.transcripts == []
    assert 'addressee_unknown:' + reason in harness.reports
    pipeline.on_addressee(uid, 'p1', 'addressed')
    assert harness.transcripts == []
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    assert len(harness.wake_calls) == 2


def test_callbacks_remain_responsive_while_single_asr_worker_is_blocked(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1.0)
    started = monotonic()
    pipeline.on_playback_status('p1', 'playing')
    pipeline.on_playback_status('p1', 'paused')
    assert monotonic() - started < 0.5
    assert harness.controls == [('p1', 'pause')]
    assert harness.transcripts == [] and harness.candidates == []
    assert harness.recorder.active
    harness.allow_asr.set()
    pump(pipeline, lambda: len(harness.candidates) == 1)
    uid = harness.candidates[0][0]
    pipeline.on_addressee(uid, 'p1', 'addressed')
    assert harness.transcripts == [(uid, '문장 1')]


def test_capture_during_candidate_inference_remains_part_of_the_same_utterance(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    # The read already blocked before wake belongs to the previous audio generation.
    harness.recorder.frames.put([0] * 320)
    wait_for(lambda: harness.recorder.reads >= 2)
    harness.allow_asr.clear()
    pipeline.feed(VOICE + QUIET * 50)
    assert harness.entered_asr.wait(timeout=1.0)
    uid = pipeline.session.utterance_id
    assert not pipeline._busy and pipeline._endpoint_job is not None
    harness.recorder.frames.put([1] * 320)
    wait_for(lambda: not pipeline.audio.empty())
    pipeline.poll()
    assert pipeline.session.utterance_id == uid
    assert 'speech_discarded:busy' not in harness.reports
    harness.allow_asr.set()
    pump(pipeline, lambda: pipeline._endpoint_job is None)
    assert harness.transcripts == []
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts == [(uid, '문장 2')]
    assert len(harness.command_calls) == 2
    assert harness.command_calls[1][0] == VOICE + QUIET * 50 + VOICE + QUIET * 100


def test_normal_tts_completion_ends_session_after_five_seconds(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    pipeline.on_playback_status('p1', 'playing')
    pipeline.on_playback_status('p1', 'finished')
    harness.now = 4.999
    pipeline.poll()
    assert pipeline.session.active
    harness.now = 5.0
    pipeline.poll()
    assert not pipeline.session.active
    assert 'session_ended:input_timeout' in harness.reports


def test_predeadline_queued_onset_is_processed_before_late_status_callback(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    pipeline.on_playback_status('p1', 'playing')
    pipeline.on_playback_status('p1', 'finished')
    pipeline.audio.put_nowait((pipeline._audio_generation, 4.9, VOICE, False))
    harness.now = 5.1
    pipeline.on_playback_status('p1', 'playing')  # Stale terminal playback update.
    uid = pipeline.session.utterance_id
    assert pipeline.session.active and uid is not None
    assert pipeline.session.deadline is None
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts == [(uid, '문장 1')]
    assert len(harness.wake_calls) == 1


def test_busy_backlog_stays_discarded_after_result_is_ready(harness):
    pipeline = harness.create()
    pipeline.session.activate()
    uid = pipeline.session.user_speech_started()
    pipeline._busy = True
    pipeline.audio.put_nowait((pipeline._audio_generation, 0.0, VOICE, True))
    pipeline.results.put_nowait(('command', pipeline._generation, uid, '이전 문장', None))
    pipeline.poll()
    assert harness.transcripts == [(uid, '이전 문장')]
    assert pipeline.session.utterance_id is None
    pipeline.feed(VOICE * 20 + QUIET * 100)
    assert pipeline.jobs.empty() and harness.command_calls == []
    pipeline.on_playback_status('reply', 'finished', request_id=uid)
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 2)
    assert len(harness.command_calls) == 1


def test_busy_capture_timestamp_tag_survives_a_result_accepted_before_enqueue(harness):
    pipeline = harness.create()
    pipeline.session.activate()
    pipeline.feed(VOICE, busy_at_capture=True)
    pipeline.feed(VOICE * 10 + QUIET * 100)
    assert pipeline.jobs.empty() and pipeline.session.utterance_id is None
    assert 'speech_discarded:busy' in harness.reports


def test_busy_utterance_tail_is_discarded_after_asr_failure(harness):
    pipeline = harness.create()
    pipeline.session.activate()
    uid = pipeline.session.user_speech_started()
    pipeline._busy = True
    pipeline.feed(VOICE)
    pipeline.results.put_nowait(('command', pipeline._generation, uid, None, 'RuntimeError'))
    pipeline.poll()
    assert not pipeline.session.active and pipeline.session.utterance_id is None
    pipeline.feed(VOICE + QUIET * 20)
    assert pipeline.jobs.empty() and harness.wake_calls == []
    pipeline.on_playback_status('retry', 'failed', request_id=uid)
    pipeline.feed(QUIET * 130)
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts[0][0] != uid
    assert len(harness.wake_calls) == 1


@pytest.mark.parametrize('failure', ['', ' \n', None, RuntimeError('private model details')])
def test_failed_command_closes_turn_and_requires_another_wake(harness, failure):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(args))
    wake_up(harness, pipeline)
    original = pipeline.transcriber.transcribe

    def fail(_pcm, _rate):
        if isinstance(failure, Exception):
            raise failure
        return failure

    pipeline.transcriber.transcribe = fail
    pipeline.feed(VOICE)
    first = pipeline.session.utterance_id
    pipeline.feed(QUIET * 100)
    expected = 'transcription_failed:RuntimeError' if isinstance(failure, Exception) else (
        'empty_transcript'
    )
    pump(pipeline, lambda: expected in harness.reports)
    assert not pipeline.session.active and pipeline.session.utterance_id is None
    assert pipeline.session.deadline is None and not pipeline._busy
    assert pipeline.pending_addressee is None
    assert harness.transcripts == [] and harness.candidates == []
    assert harness.controls == []
    assert statuses == [('', first, 'started')] + (
        [('', first, 'failed')] if isinstance(failure, Exception) else [])
    if isinstance(failure, Exception):
        assert pipeline._reply_request_id == first
        finish_command(pipeline)
        assert pipeline.jobs.empty()
        pipeline.on_playback_status('retry', 'failed', request_id=first)
    assert pipeline._reply_request_id is None
    pipeline.wake.transcribe = lambda pcm, rate: '호출어 없는 말'
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: 'not_wake' in harness.reports)
    assert harness.transcripts == []
    pipeline.wake.transcribe = lambda pcm, rate: '제이크야'
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    pipeline.transcriber.transcribe = original
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts[0][0] != first
    assert harness.transcripts[0][1] == '문장 1'
    assert statuses[-1] == ('', harness.transcripts[0][0], 'started')


@pytest.mark.parametrize('terminal', ['finished', 'failed', 'stopped'])
def test_failed_command_gates_retry_before_notification_until_matching_terminal(harness, terminal):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(
        (*args, pipeline._input_blocked(harness.now))))
    wake_up(harness, pipeline)
    pipeline.feed(VOICE)
    uid, generation = pipeline.session.utterance_id, pipeline._generation
    pipeline._accept_result('command', generation, uid, None, 'ValueError')
    assert statuses[-1] == ('', uid, 'failed', True)
    pipeline.on_playback_status('retry', terminal, interim=True, request_id=uid)
    pipeline.on_playback_status('other', terminal, request_id='other')
    assert pipeline._input_blocked(harness.now)
    pipeline.audio.put_nowait((pipeline._audio_generation, harness.now, VOICE, False))
    pipeline.on_playback_status('retry-final', terminal, request_id=uid)
    assert not pipeline._input_blocked(harness.now) and pipeline.audio.empty()
    wake_up_count = len(harness.wake_calls)
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    pipeline._accept_result('command', generation, uid, '이전 결과', None)
    assert harness.transcripts == [] and len(harness.wake_calls) == wake_up_count + 1


@pytest.mark.parametrize('text,error', [('', None), (None, 'ValueError')])
def test_confirmation_failure_remains_owned_by_agent(harness, text, error):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(args))
    assert pipeline.start_session('confirmation')
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    pipeline._accept_result('command', pipeline._generation, uid, text, error)
    assert statuses == [('confirmation', uid, 'started'), ('confirmation', uid, 'failed')]
    assert pipeline.session_is_active('confirmation')
    assert pipeline._reply_request_id is None
    assert pipeline.stop_session('confirmation')
    assert not pipeline.session.active


def test_wake_without_command_times_out_after_chime_guard(harness):
    pipeline = harness.create(on_wake=lambda: setattr(harness, 'now', 2.0))
    wake_up(harness, pipeline)
    harness.now = 7.299
    pipeline.poll()
    assert pipeline.session.active
    harness.now = 7.3
    pipeline.poll()
    assert not pipeline.session.active and harness.transcripts == []
    assert harness.command_calls == []


def test_missing_retry_notice_cannot_leave_input_blocked_forever(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    pipeline._accept_result('command', pipeline._generation, uid, None, 'ValueError')
    harness.now = 44.999
    pipeline.poll()
    assert pipeline._input_blocked(harness.now)
    harness.now = 45.0
    pipeline.poll()
    assert not pipeline._input_blocked(harness.now) and not pipeline.session.active
    assert 'retry_notice_timeout' in harness.reports


def test_command_start_before_wake_timeout_survives_late_poll(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    pipeline.audio.put_nowait((pipeline._audio_generation, 4.9, VOICE, False))
    harness.now = 5.1
    pipeline.poll()
    assert pipeline.session.active and pipeline.session.utterance_id is not None
    assert pipeline.session.deadline is None
    assert pipeline._command_start_deadline is None


def test_unrelated_playback_cannot_erase_wake_listening_deadline(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    pipeline.on_playback_status('old-progress', 'playing', interim=True)
    pipeline.on_playback_status('old-progress', 'finished', interim=True)
    harness.now = 5.0
    pipeline.poll()
    assert not pipeline.session.active and pipeline._command_start_deadline is None


def test_diagnostics_correlate_worker_error_and_wake_boundary(harness, tmp_path):
    from malbut_stt.diagnostics import TranscriptionDiagnostics

    diagnostics = TranscriptionDiagnostics(tmp_path)
    pipeline = harness.create(diagnostics=diagnostics)
    wake_up(harness, pipeline)

    def fail(pcm, rate):
        raise ValueError('incremental transcription omitted recent speech')

    pipeline.transcriber.transcribe = fail
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    generation = pipeline._generation
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: 'transcription_failed:ValueError' in harness.reports)
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    result = next(e for e in events if e['event'] == 'inference_result'
                  and e['context']['kind'] == 'command')
    assert result['context']['utterance_id'] == uid
    assert result['context']['generation'] == generation
    assert result['error_detail'] == 'incremental transcription omitted recent speech'
    assert any(e['event'] == 'wake_input_ready' for e in events)
    assert all('omitted recent speech' not in message for message in harness.reports)


def test_ordinary_input_status_ignores_empty_ids_and_busy_discard(harness):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(args))
    wake_up(harness, pipeline)
    pipeline._input_status('failed')
    pipeline._busy = True
    pipeline.feed(VOICE + QUIET * 100)
    assert 'speech_discarded:busy' in harness.reports
    assert statuses == []


@pytest.mark.parametrize('failure', ['', RuntimeError('private model details')])
def test_failed_interruption_keeps_actual_pause_and_waits_for_another_utterance(harness, failure):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(args))
    wake_up(harness, pipeline)
    original = pipeline.transcriber.transcribe

    def fail(_pcm, _rate):
        if isinstance(failure, Exception):
            raise failure
        return failure

    pipeline.transcriber.transcribe = fail
    pipeline.on_playback_status('p1', 'playing')
    pipeline.feed(VOICE)
    first = pipeline.session.utterance_id
    pipeline.on_playback_status('p1', 'paused')
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: pipeline.session.utterance_id is None)
    assert pipeline.session.active and pipeline.session.playback_state == 'paused'
    assert pipeline.session.playback_id == 'p1' and pipeline.session._control == 'pause'
    assert pipeline.session.deadline is None and not pipeline._busy
    assert pipeline.pending_addressee is None
    assert harness.controls == [('p1', 'pause')]
    assert harness.candidates == [] and harness.transcripts == []
    # An ordinary retry notice would queue behind the still-paused answer.
    assert statuses == [('', first, 'started')]
    pipeline.transcriber.transcribe = original
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.candidates) == 1)
    second, pid, text = harness.candidates[0]
    assert second != first and (pid, text) == ('p1', '문장 1')
    assert harness.controls == [('p1', 'pause')]
    pipeline.on_addressee(second, pid, 'addressed')
    assert harness.controls == [('p1', 'pause'), ('p1', 'stop')]
    assert harness.transcripts == [(second, '문장 1')]
    assert len(harness.wake_calls) == 1


@pytest.mark.parametrize('stale', ['generation', 'utterance_id'])
@pytest.mark.parametrize('failure', [(None, 'RuntimeError'), ('', None)])
def test_stale_failure_cannot_discard_current_speech(harness, stale, failure):
    pipeline = harness.create()
    pipeline.session.activate()
    pipeline.on_playback_status('p1', 'playing')
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    generation = pipeline._generation
    result_uid = uid
    if stale == 'generation':
        generation -= 1
    else:
        result_uid = 'old-utterance'
    reports = list(harness.reports)
    pipeline._accept_result('command', generation, result_uid, *failure)
    assert pipeline.session.active and pipeline.session.utterance_id == uid
    assert pipeline._capture_id == uid and pipeline._utterance_playback_id == 'p1'
    assert pipeline.session.interrupted_playback_id == 'p1'
    assert harness.reports == reports
    assert harness.controls == [('p1', 'pause')]
    assert harness.transcripts == [] and harness.candidates == []


def test_raw_playback_audio_and_queued_echo_are_suppressed_until_tail_guard(harness):
    pipeline = harness.create(aec=False)
    wake_up(harness, pipeline)
    pipeline.audio.put_nowait((pipeline._audio_generation, 0.0, QUIET, False))
    pipeline.on_playback_status('p1', 'playing')
    pipeline.feed(VOICE + QUIET * 100)
    assert pipeline.jobs.empty() and harness.transcripts == [] and harness.controls == []
    assert 'barge_in_requires_aec' in harness.reports
    old_generation = pipeline._audio_generation
    pipeline.on_playback_status('p1', 'finished')
    pipeline.audio.put_nowait((old_generation, 0.0, VOICE, False))
    pipeline.poll()
    harness.now = 0.29
    pipeline.feed(VOICE)
    assert pipeline.session.utterance_id is None
    harness.now = 0.31
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    assert uid is not None
    pipeline.on_playback_status('p1', 'playing')
    assert pipeline.session.utterance_id == uid  # Late duplicate cannot discard capture.
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts == [(uid, '문장 1')]


def test_raw_external_pause_allows_new_wake_after_finite_drain(harness):
    pipeline = harness.create(aec=False)
    pipeline.on_playback_status('p1', 'playing')
    pipeline.on_playback_status('p1', 'paused')
    harness.now = 0.31
    wake_up(harness, pipeline)
    assert harness.controls == []


def test_real_capture_thread_keeps_microphone_open_and_drops_playing_frames(harness):
    pipeline = harness.create(aec=False)
    wait_for(lambda: harness.recorder.reads == 1)
    harness.recorder.frames.put([1] * 320)
    wait_for(lambda: not pipeline.audio.empty())
    generation, _, pcm, busy = pipeline.audio.get_nowait()
    assert generation == pipeline._audio_generation and pcm == VOICE and not busy
    pipeline.on_playback_status('p1', 'playing')
    before = harness.recorder.reads
    harness.recorder.frames.put([1] * 320)
    wait_for(lambda: harness.recorder.reads > before)
    assert pipeline.audio.empty()
    assert harness.recorder.active


def test_overflow_discards_incomplete_audio_without_inference(harness):
    pipeline = harness.create()
    pipeline.session.activate()
    pipeline.feed(VOICE)
    pipeline.overflow.set()
    pipeline.poll()
    assert not pipeline.session.active
    assert pipeline.jobs.empty() and harness.command_calls == []
    assert 'audio_queue_overflow' in harness.reports


def test_capture_ready_waits_for_valid_input_after_overflow(harness, monkeypatch):
    capture_time = [100.0]
    monkeypatch.setattr('malbut_stt.dialogue_pipeline.monotonic', lambda: capture_time[0])
    pipeline = harness.create()
    assert not pipeline.capture_ready.is_set()
    harness.recorder.frames.put(MicrophoneOverflow('microphone input overflow'))
    wait_for(pipeline.overflow.is_set)
    assert not pipeline.capture_ready.is_set()
    pipeline.poll()
    assert pipeline.capture_error is None and pipeline.capture_thread.is_alive()
    capture_time[0] = 104.0
    harness.recorder.frames.put([0] * 320)
    wait_for(pipeline.capture_ready.is_set)
    assert pipeline._last_capture_at == 104.0
    capture_time[0] = 105.0
    pipeline.poll()
    assert pipeline.capture_error is None
    assert harness.transcripts == []


def test_microphone_overflow_discards_partial_utterance_and_keeps_capture(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    pipeline.feed(VOICE)
    assert pipeline.command_stream.collector.started
    harness.recorder.frames.put(MicrophoneOverflow('microphone input overflow'))
    wait_for(pipeline.overflow.is_set)
    pipeline.poll()
    assert not pipeline.session.active
    assert not pipeline.command_stream.collector.started
    assert harness.transcripts == [] and harness.command_calls == []
    assert pipeline.capture_error is None and pipeline.capture_thread.is_alive()
    assert harness.closed == []
    harness.recorder.frames.put([0] * 320)
    wait_for(pipeline.capture_ready.is_set)
    pipeline.poll()
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    assert len(harness.wake_calls) == 2
    finish_command(pipeline)
    pump(pipeline, lambda: bool(harness.transcripts))
    assert len(harness.transcripts) == 1


def test_capture_worker_failure_reports_read_phase_and_releases_device(harness):
    def fail_read():
        raise RuntimeError('private microphone details')

    harness.recorder.read = fail_read
    pipeline = harness.create()
    wait_for(lambda: pipeline.capture_error is not None)
    assert not pipeline.capture_ready.is_set()
    with pytest.raises(RuntimeError):
        pipeline.poll()
    assert pipeline.phase == 'reading_microphone'
    pipeline.close()
    assert harness.closed == ['stop', 'delete']
    assert not pipeline.capture_thread.is_alive() and not pipeline.asr_thread.is_alive()
    assert harness.transcripts == []


@pytest.mark.parametrize('first_frame', [False, True])
def test_blocked_microphone_read_cannot_verify_or_open_a_session(
        harness, monkeypatch, first_frame):
    capture_time = [100.0]
    monkeypatch.setattr('malbut_stt.dialogue_pipeline.monotonic', lambda: capture_time[0])
    pipeline = harness.create()
    wait_for(lambda: harness.recorder.reads == 1)
    if first_frame:
        harness.recorder.frames.put([0] * 320)
        wait_for(lambda: harness.recorder.reads == 2)
        pipeline.poll()
    assert pipeline.start_session('incident')

    # Dialogue deadlines may advance independently of real device capture.
    harness.now = 1000.0
    capture_time[0] = 104.999
    pipeline.poll()
    assert pipeline.session_is_active('incident')
    capture_time[0] = 105.0
    assert not pipeline.session_is_active('incident')
    assert pipeline.session.active and pipeline._retired_session_ids == {}
    assert not pipeline.start_session('incident')
    assert not pipeline.start_session('another')
    assert pipeline.session.session_id == 'incident'
    with pytest.raises(RuntimeError, match='microphone input timeout') as failure:
        pipeline.poll()
    assert pipeline.phase == 'reading_microphone'
    assert harness.transcripts == harness.command_calls == []

    # A late read cannot revive a pipeline whose capture failure was observed.
    harness.recorder.frames.put([0] * 320)
    wait_for(lambda: not pipeline.capture_thread.is_alive())
    assert not pipeline.session_is_active('incident')
    assert not pipeline.start_session('another')
    with pytest.raises(RuntimeError) as repeated:
        pipeline.poll()
    assert repeated.value is failure.value


def test_late_microphone_frame_cannot_hide_capture_timeout_before_poll(harness, monkeypatch):
    capture_time = [100.0]
    monkeypatch.setattr('malbut_stt.dialogue_pipeline.monotonic', lambda: capture_time[0])
    pipeline = harness.create()
    wait_for(lambda: harness.recorder.reads == 1)
    harness.recorder.frames.put([0] * 320)
    wait_for(lambda: harness.recorder.reads == 2)
    pipeline.poll()
    assert pipeline.start_session('incident')

    capture_time[0] = 105.0
    harness.recorder.frames.put([0] * 320)
    wait_for(lambda: not pipeline.capture_thread.is_alive())
    assert not pipeline.session_is_active('incident')
    with pytest.raises(RuntimeError, match='microphone input timeout'):
        pipeline.poll()
    assert pipeline.audio.empty() and harness.command_calls == []


def test_late_first_microphone_frame_does_not_report_capture_ready(harness, monkeypatch):
    capture_time = [100.0]
    monkeypatch.setattr('malbut_stt.dialogue_pipeline.monotonic', lambda: capture_time[0])
    pipeline = harness.create()
    wait_for(lambda: harness.recorder.reads == 1)

    capture_time[0] = 105.0
    harness.recorder.frames.put([0] * 320)
    wait_for(lambda: not pipeline.capture_thread.is_alive())

    assert not pipeline.capture_ready.is_set()
    with pytest.raises(RuntimeError, match='microphone input timeout'):
        pipeline.poll()
    assert pipeline.audio.empty() and harness.transcripts == []


@pytest.mark.parametrize('playback_gate', [False, True])
def test_quiet_microphone_frames_remain_healthy_during_playback(
        harness, monkeypatch, playback_gate):
    capture_time = [100.0]
    monkeypatch.setattr('malbut_stt.dialogue_pipeline.monotonic', lambda: capture_time[0])
    pipeline = harness.create(aec=False)
    assert pipeline.start_session('incident')
    if playback_gate:
        pipeline.on_playback_status('question', 'playing')
    wait_for(lambda: harness.recorder.reads == 1)
    for _ in range(4):
        capture_time[0] += 4.0
        reads = harness.recorder.reads
        harness.recorder.frames.put([0] * 320)
        wait_for(lambda: harness.recorder.reads > reads)
        if playback_gate:
            assert pipeline.audio.empty()
        pipeline.poll()
        assert pipeline.session_is_active('incident')
    assert pipeline.capture_error is None
    assert harness.transcripts == harness.command_calls == []


def test_unsupported_microphone_rate_is_deleted_without_starting_workers(harness):
    harness.recorder.sample_rate = 8000
    with pytest.raises(ValueError, match='16kHz'):
        harness.create()
    pipeline = harness.pipeline
    pipeline.close()
    assert harness.closed == ['delete']
    assert pipeline.capture_thread is None and pipeline.asr_thread is None


def test_close_suppresses_inflight_asr_and_releases_workers(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1.0)
    pipeline.close()  # Recorder.stop unblocks the fake model, like graceful native completion.
    pipeline.poll()
    pipeline.on_addressee('late', 'late', 'addressed')
    assert harness.transcripts == []
    assert not pipeline.capture_thread.is_alive() and not pipeline.asr_thread.is_alive()
    assert harness.closed == ['stop', 'delete']


@pytest.mark.parametrize('operation', ['stop', 'delete'])
def test_device_cleanup_failure_still_joins_workers_and_close_is_idempotent(harness, operation):
    pipeline = harness.create()
    original = getattr(harness.recorder, operation)

    def fail():
        original()
        raise RuntimeError('private device detail')

    setattr(harness.recorder, operation, fail)
    with pytest.raises(RuntimeError):
        pipeline.close()
    pipeline.close()
    assert harness.closed == ['stop', 'delete']
    assert not pipeline._started
    assert not pipeline.capture_thread.is_alive() and not pipeline.asr_thread.is_alive()
    assert pipeline.audio.empty() and pipeline.jobs.empty() and pipeline.results.empty()


@pytest.mark.parametrize('aec', [False, True])
def test_wake_chime_and_queued_echo_are_excluded_before_command_capture(harness, aec):
    chimes = []

    def chime():
        chimes.append('played')
        pipeline.feed(VOICE)
        assert not pipeline.command_stream.collector.started
        pipeline.audio.put_nowait((pipeline._audio_generation, harness.now, VOICE, False))
        harness.now += 0.18

    pipeline = harness.create(aec=aec, on_wake=chime)
    wake_up(harness, pipeline)
    assert chimes == ['played'] and pipeline.audio.empty()
    pipeline.feed(VOICE)
    assert not pipeline.command_stream.collector.started
    harness.now += 0.3
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert len(harness.command_calls) == 1
    assert 'wake_chime_unavailable' not in harness.reports


def test_failed_wake_chime_reports_failure_and_returns_to_wake(harness):
    def chime():
        raise RuntimeError('private device detail')

    pipeline = harness.create(on_wake=chime)
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: 'wake_chime_failed:RuntimeError' in harness.reports)
    assert not pipeline.session.active and not pipeline._chime_playing
    assert harness.transcripts == [] and not pipeline.command_stream.collector.audio
    assert all('private' not in report for report in harness.reports)


def test_close_cancels_decode_before_joining_worker(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1)
    cancelled = []

    def cancel():
        cancelled.append(True)
        harness.allow_asr.set()

    pipeline.transcriber.cancel = cancel
    pipeline.close()
    assert cancelled == [True]
    assert not pipeline.asr_thread.is_alive()
    assert harness.transcripts == []
    assert 'asr_shutdown_pending' not in harness.reports


def test_overlong_capture_cancels_inflight_preview_and_drops_late_result(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    pipeline.feed(VOICE + QUIET * 50)
    assert harness.entered_asr.wait(timeout=1)
    cancelled = []

    def cancel():
        cancelled.append(True)
        harness.allow_asr.set()

    pipeline.transcriber.cancel = cancel
    pipeline.feed(VOICE * 1001)
    assert cancelled == [True]
    pump(pipeline, lambda: pipeline._endpoint_job is None)
    assert harness.transcripts == [] and not pipeline.session.active


def test_cancelled_queued_preview_releases_bookkeeping_without_decoding(harness):
    pipeline = harness.create()
    pipeline._endpoint_job = (0, 'old', 1)
    pipeline._endpoint_requested_at = harness.now
    pipeline._terminate('utterance_discarded:too_long')
    # Simulate a job dequeued after termination, before it reaches native decode.
    pipeline.jobs.put_nowait(('partial', 0, ('old', 1), VOICE))
    pump(pipeline, lambda: pipeline._endpoint_job is None)
    assert harness.command_calls == [] and harness.transcripts == []
    assert pipeline._endpoint_requested_at is None
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)


@pytest.mark.parametrize('aec', [True, False])
def test_web_talk_blocks_wake_and_audio_but_keeps_capture_alive(harness, aec):
    pipeline = harness.create(aec=aec)
    assert pipeline.control_web_talk('web-1', True, 10.0)
    pipeline.feed(VOICE * 4 + QUIET * 100)
    harness.recorder.frames.put([0] * 320)
    wait_for(pipeline.capture_ready.is_set)
    pipeline.poll()
    assert pipeline.audio.empty() and pipeline.jobs.empty()
    assert harness.wake_calls == harness.command_calls == harness.transcripts == []
    assert harness.recorder.active
    assert pipeline.control_web_talk('web-1', False, 0.0)
    pipeline.feed(VOICE * 4 + QUIET * 20)
    assert pipeline.jobs.empty()
    harness.now = 0.31
    wake_up(harness, pipeline)


def test_web_talk_renews_then_expires_without_replaying_buffered_speech(harness):
    pipeline = harness.create()
    assert pipeline.control_web_talk('web-1', True, 1.0)
    harness.now = 0.8
    assert pipeline.control_web_talk('web-1', True, 1.0)
    harness.now = 1.01
    pipeline.poll()
    assert pipeline._input_blocked(harness.now)
    pipeline.audio.put_nowait((pipeline._audio_generation, harness.now, VOICE, False))
    harness.now = 1.81
    pipeline.poll()
    assert pipeline.audio.empty() and not pipeline.session.active
    assert pipeline._input_blocked(harness.now)
    assert harness.reports.count('web_talk_started') == 1
    assert 'web_talk_expired' in harness.reports
    harness.now = 2.12
    wake_up(harness, pipeline)


def test_old_web_talk_release_cannot_clear_new_lease(harness):
    pipeline = harness.create()
    assert pipeline.control_web_talk('old', True, 10.0)
    assert pipeline.control_web_talk('new', True, 10.0)
    assert not pipeline.control_web_talk('old', False, 0.0)
    assert pipeline._input_blocked(harness.now)
    assert pipeline.control_web_talk('new', False, 0.0)
    harness.now = 0.31
    assert not pipeline._input_blocked(harness.now)


@pytest.mark.parametrize('lease_id,active,ttl_s', [
    ('', True, 10.0), (' ', True, 10.0), ('x' * 201, True, 10.0),
    ('web', 1, 10.0), ('web', True, 0.0), ('web', True, 15.01),
    ('web', True, float('nan')), ('web', True, float('inf')),
    ('web', True, True), ('web', True, '10'),
])
def test_invalid_web_talk_request_does_not_gate_normal_wake(harness, lease_id, active, ttl_s):
    pipeline = harness.create()
    assert not pipeline.control_web_talk(lease_id, active, ttl_s)
    assert not pipeline._input_blocked(harness.now)
    wake_up(harness, pipeline)


def test_web_talk_invalidates_inflight_asr_and_queued_capture_before_ack(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    harness.allow_asr.clear()
    finish_command(pipeline)
    assert harness.entered_asr.wait(timeout=1)
    cancelled = []

    def cancel():
        cancelled.append(True)

    pipeline.transcriber.cancel = cancel
    pipeline.audio.put_nowait((pipeline._audio_generation, harness.now, VOICE, False))
    assert pipeline.control_web_talk('web-1', True, 10.0)
    assert cancelled == [True] and pipeline.audio.empty() and pipeline.jobs.empty()
    assert not pipeline.session.active and not pipeline._busy
    harness.allow_asr.set()
    wait_for(lambda: not pipeline.results.empty())
    pipeline.poll()
    assert harness.transcripts == []
    assert pipeline.control_web_talk('web-1', False, 0.0)
    harness.now = 0.31
    pipeline.poll()
    assert harness.transcripts == [] and not pipeline.session.active


@pytest.mark.parametrize('speech_started', [True, False])
def test_web_talk_aborts_confirmation_as_session_failure_not_silence(harness, speech_started):
    statuses = []
    pipeline = harness.create(publish_input_status=lambda *args: statuses.append(args))
    assert pipeline.start_session('incident-1')
    if speech_started:
        pipeline.feed(VOICE * 4)
        assert statuses[-1][2] == 'started'
    assert pipeline.control_web_talk('web-1', True, 10.0)
    assert statuses[-1] == ('incident-1', '', 'failed')
    assert not pipeline.session_is_active('incident-1')
    assert not pipeline.start_session('incident-2')
    assert pipeline.stop_session('incident-2')
    assert pipeline.control_web_talk('web-1', True, 10.0)
    assert sum(status[2] == 'failed' for status in statuses) == 1
    assert pipeline.control_web_talk('web-1', False, 0.0)
    assert not pipeline.start_session('incident-3')
    harness.now = 0.31
    assert not pipeline.start_session('incident-1')
    assert not pipeline.start_session('incident-2')
    assert pipeline.start_session('incident-3')


@pytest.mark.parametrize('reply_ends_during_talk', [True, False])
def test_web_talk_does_not_release_unfinished_ordinary_reply(harness, reply_ends_during_talk):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    uid = harness.transcripts[0][0]
    assert pipeline.control_web_talk('web-1', True, 10.0)
    if reply_ends_during_talk:
        pipeline.on_playback_status('answer', 'finished', request_id=uid)
        assert pipeline._input_blocked(harness.now)
    assert pipeline.control_web_talk('web-1', False, 0.0)
    harness.now = 0.31
    assert pipeline._input_blocked(harness.now) is not reply_ends_during_talk
    if not reply_ends_during_talk:
        pipeline.on_playback_status('answer', 'finished', request_id=uid)
    assert not pipeline._input_blocked(harness.now)


@pytest.mark.parametrize('release_before_timeout', [True, False])
def test_web_talk_preserves_retry_notice_timeout(harness, release_before_timeout):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    pipeline._accept_result('command', pipeline._generation, uid, None, 'ValueError')
    deadline = pipeline._retry_notice_deadline
    harness.now = 40.0
    assert pipeline.control_web_talk('web-1', True, 10.0)
    assert pipeline._retry_notice_deadline == deadline
    if release_before_timeout:
        assert pipeline.control_web_talk('web-1', False, 0.0)
    harness.now = deadline
    pipeline.poll()
    assert pipeline._reply_request_id is None
    assert pipeline._input_blocked(harness.now) is not release_before_timeout
    if not release_before_timeout:
        assert pipeline.control_web_talk('web-1', False, 0.0)
        harness.now += 0.31
    assert not pipeline._input_blocked(harness.now)


def test_web_talk_silences_current_and_requested_speech_until_it_ends(harness):
    stopped = []
    pipeline = harness.create(stop_speech=stopped.append)
    pipeline.on_speech_request('before')
    assert stopped == []
    assert pipeline.control_web_talk('web-1', True, 10.0)
    assert stopped == ['']
    assert pipeline.control_web_talk('web-1', True, 10.0)
    assert stopped == ['']
    pipeline.on_speech_request('answer')
    pipeline.on_speech_request('')
    pipeline.on_speech_request('x' * 201)
    assert stopped == ['', 'answer']
    pipeline.on_playback_status('raced', 'playing')
    pipeline.on_playback_status('raced', 'stopped')
    assert stopped == ['', 'answer', 'raced']
    assert harness.reports.count('web_talk_speech_stopped') == 3
    assert pipeline.control_web_talk('web-1', False, 0.0)
    pipeline.on_speech_request('after')
    pipeline.on_playback_status('after', 'playing')
    assert stopped == ['', 'answer', 'raced']


def test_expired_web_talk_no_longer_silences_speech(harness):
    stopped = []
    pipeline = harness.create(stop_speech=stopped.append)
    assert pipeline.control_web_talk('web-1', True, 1.0)
    harness.now = 1.01
    pipeline.on_speech_request('late')
    assert stopped == ['']
    assert 'web_talk_expired' in harness.reports


def test_startup_quarantine_gates_input_without_silencing_speech(harness):
    stopped = []
    pipeline = harness.create(stop_speech=stopped.append)
    assert pipeline.control_web_talk('startup-quarantine', True, 3.0, quiet=False)
    assert pipeline._input_blocked(harness.now)
    pipeline.on_speech_request('greeting')
    pipeline.on_playback_status('greeting', 'playing')
    assert stopped == []
    # A guardian lease that takes over the quarantine still silences the Agent.
    assert pipeline.control_web_talk('web-1', True, 10.0)
    assert stopped == ['']
