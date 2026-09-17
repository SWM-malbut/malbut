"""Continuous dialogue tests use deterministic PCM, local-model fakes, and no ROS."""

from queue import Queue
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline

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
            self.frames.put([0] * 512)
            self.active = False
            self.reads = 0

        def start(self):
            self.active = True

        def read(self):
            self.reads += 1
            return self.frames.get(timeout=3.0)

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

    def create(*, aec=True, is_speech=None):
        pipeline = DialoguePipeline(
            recorder_factory=lambda: state.recorder,
            wake=SimpleNamespace(transcribe=wake),
            transcriber=SimpleNamespace(transcribe=transcribe),
            is_speech=is_speech or (lambda frame, rate: frame[:2] != b'\x00\x00'),
            publish_transcript=lambda uid, text: state.transcripts.append((uid, text)),
            publish_control=lambda pid, cmd: state.controls.append((pid, cmd)),
            publish_interruption=lambda uid, pid, text: state.candidates.append((uid, pid, text)),
            report=state.reports.append, clock=lambda: state.now, input_has_aec=aec,
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
    pipeline.feed(VOICE + QUIET * 20)
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


def test_one_open_microphone_runs_wake_then_consecutive_commands(harness):
    pipeline = harness.create()
    wake_up(harness, pipeline)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    first_id = harness.transcripts[0][0]
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 2)
    assert harness.transcripts == [(first_id, '문장 1'),
                                   (harness.transcripts[1][0], '문장 2')]
    assert harness.transcripts[1][0] != first_id
    assert len(harness.wake_calls) == 1 and len(harness.command_calls) == 2
    assert harness.recorder.active and harness.closed == []
    assert 'wake_chime_unavailable' in harness.reports
    pipeline.close()
    pipeline.close()
    assert harness.closed == ['stop', 'delete']
    assert not pipeline.capture_thread.is_alive() and not pipeline.asr_thread.is_alive()
    assert pipeline.audio.empty() and pipeline.jobs.empty() and pipeline.results.empty()


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
    assert pipeline.session.active


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
    pipeline.feed(VOICE + QUIET * 20)
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
    wait_for(lambda: harness.recorder.reads >= 3)
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
    assert 'session_ended:tts_timeout' in harness.reports


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


def test_busy_utterance_tail_stays_discarded_after_asr_failure_without_ending_dialogue(harness):
    pipeline = harness.create()
    pipeline.session.activate()
    uid = pipeline.session.user_speech_started()
    pipeline._busy = True
    pipeline.feed(VOICE)
    pipeline.results.put_nowait(('command', pipeline._generation, uid, None, 'RuntimeError'))
    pipeline.poll()
    assert pipeline.session.active and pipeline.session.utterance_id is None
    pipeline.feed(VOICE + QUIET * 20)
    assert pipeline.jobs.empty() and harness.wake_calls == []
    pipeline.feed(QUIET * 130)
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts[0][0] != uid
    assert harness.wake_calls == []


@pytest.mark.parametrize('failure', ['', ' \n', None, RuntimeError('private model details')])
def test_failed_command_waits_for_new_speech_without_another_wake(harness, failure):
    pipeline = harness.create()
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
    assert pipeline.session.active and pipeline.session.utterance_id is None
    assert pipeline.session.deadline is None and not pipeline._busy
    assert pipeline.pending_addressee is None
    assert harness.transcripts == [] and harness.candidates == []
    assert harness.controls == []
    pipeline.transcriber.transcribe = original
    finish_command(pipeline)
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts[0][0] != first
    assert harness.transcripts[0][1] == '문장 1'
    assert len(harness.wake_calls) == 1


@pytest.mark.parametrize('failure', ['', RuntimeError('private model details')])
def test_failed_interruption_keeps_actual_pause_and_waits_for_another_utterance(harness, failure):
    pipeline = harness.create()
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
    wait_for(lambda: harness.recorder.reads == 2)
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


def test_capture_worker_failure_reports_read_phase_and_releases_device(harness):
    first_read = True

    def fail_read():
        nonlocal first_read
        if first_read:
            first_read = False
            return [0] * 512
        raise RuntimeError('private microphone details')

    harness.recorder.read = fail_read
    pipeline = harness.create()
    wait_for(lambda: pipeline.capture_error is not None)
    with pytest.raises(RuntimeError):
        pipeline.poll()
    assert pipeline.phase == 'reading_microphone'
    pipeline.close()
    assert harness.closed == ['stop', 'delete']
    assert not pipeline.capture_thread.is_alive() and not pipeline.asr_thread.is_alive()
    assert harness.transcripts == []


def test_start_waits_for_first_microphone_frame_before_workers_or_ready(harness):
    """Keep startup pending while the microphone has not delivered usable audio."""
    harness.recorder.frames.get_nowait()
    finished = Event()
    failures = []
    vad_frames = []

    def start():
        try:
            harness.create(is_speech=lambda pcm, rate: vad_frames.append((pcm, rate)))
        except Exception as error:
            failures.append(error)
        finally:
            finished.set()

    starter = Thread(target=start)
    starter.start()
    try:
        wait_for(lambda: harness.recorder.reads == 1)
        pipeline = harness.pipeline
        assert not finished.wait(timeout=0.05)
        assert pipeline.phase == 'reading_microphone'
        assert pipeline.capture_thread is None and pipeline.asr_thread is None
        assert harness.reports == [] and vad_frames == []
        harness.recorder.frames.put([0] * 512)
        assert finished.wait(timeout=2.0)
        assert failures == []
        assert pipeline.phase == 'running'
        assert vad_frames == [(QUIET, 16000)]
        assert harness.reports == ['waiting_for_wake']
        assert pipeline.audio.empty() and harness.wake_calls == []
    finally:
        harness.recorder.frames.put([0] * 512)
        starter.join(timeout=3.0)
        harness.pipeline.close()
    assert not starter.is_alive()


@pytest.mark.parametrize('failure,exception', [
    ('read', RuntimeError), ('short_frame', ValueError),
    ('invalid_pcm', OverflowError), ('vad', RuntimeError),
])
def test_first_microphone_validation_failure_closes_without_workers(harness, failure, exception):
    """Reject failed audio validation before publishing readiness or starting ASR."""
    def read():
        if failure == 'read':
            raise RuntimeError('private microphone details')
        if failure == 'short_frame':
            return [0] * 320
        return [40000 if failure == 'invalid_pcm' else 0] * 512

    def vad(pcm, rate):
        assert pcm == QUIET and rate == 16000
        raise RuntimeError('private VAD details')

    harness.recorder.read = read
    with pytest.raises(exception):
        harness.create(is_speech=vad if failure == 'vad' else None)
    pipeline = harness.pipeline
    assert pipeline.phase == 'reading_microphone'
    assert pipeline.capture_thread is None and pipeline.asr_thread is None
    assert harness.reports == harness.wake_calls == harness.command_calls == []
    pipeline.close()
    pipeline.close()
    assert harness.closed == ['stop', 'delete']
    assert not pipeline._started and pipeline.recorder is None


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
