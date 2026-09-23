"""Endpoint feedback is once per capture and never becomes a new utterance."""

from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline


VOICE = b'\x01\x00' * 320
SECOND = b'\x02\x00' * 320
QUIET = bytes(640)


@pytest.fixture
def run():
    state = SimpleNamespace(now=0.0, chimes=[], transcripts=[], reports=[])
    pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=None, transcriber=None,
        is_speech=lambda frame, _: any(frame),
        publish_transcript=lambda uid, text: state.transcripts.append((uid, text)),
        publish_control=lambda *_: None, publish_interruption=lambda *_: None,
        report=state.reports.append, clock=lambda: state.now, input_has_aec=True,
        on_endpoint=lambda: state.chimes.append(pipeline.session.utterance_id),
    )
    pipeline.session.activate()
    state.pipeline = pipeline
    yield state
    pipeline.close()


def candidate(state):
    p = state.pipeline
    p.feed(VOICE + QUIET * 50)
    job = p.jobs.get_nowait()
    assert job[0] == 'endpoint'
    return job


def reply(state, job, text='문을 닫아 주세요.', error=None):
    state.pipeline.results.put_nowait((*job[:3], text, error))
    state.pipeline.poll()


def test_provisional_pause_and_resumed_speech_only_chime_at_final_endpoint(run):
    p = run.pipeline
    old = candidate(run)
    assert run.chimes == []
    p.feed(SECOND)
    reply(run, old)
    assert run.chimes == [] and run.transcripts == []
    p.feed(QUIET * 50)
    current = p.jobs.get_nowait()
    reply(run, current)
    assert run.chimes == [current[2][0]]
    assert run.transcripts == [(current[2][0], '문을 닫아 주세요.')]
    reply(run, current)  # A repeated/late result cannot repeat feedback.
    p.poll()
    assert len(run.chimes) == len(run.transcripts) == 1


def test_chime_precedes_slow_final_inference_without_losing_its_audio(run):
    p = run.pipeline
    job = candidate(run)
    p.feed(QUIET * 50)
    uid = job[2][0]
    assert run.chimes == [uid] and run.transcripts == []
    assert p._endpoint_final == (job[1], *job[2])
    assert p._final_input == VOICE + QUIET * 100
    reply(run, job, error='RuntimeError')
    final_job = p.jobs.get_nowait()
    assert final_job[0] == 'command' and final_job[3] == VOICE + QUIET * 100
    reply(run, final_job)
    assert run.transcripts == [(uid, '문을 닫아 주세요.')]
    assert run.chimes == [uid]


@pytest.mark.parametrize('aec', [False, True])
def test_chime_echo_queue_and_tail_are_excluded_then_new_speech_is_accepted(run, aec):
    p = run.pipeline
    p.input_has_aec = aec
    job = candidate(run)
    saved_generation = p._generation

    def sound():
        run.chimes.append(p.session.utterance_id)
        assert p._chime_playing
        p.feed(SECOND)
        assert not p.command_stream.collector.started
        # Simulate a capture that arrived around the gate transition.
        p.audio.put_nowait((p._audio_generation, run.now, SECOND, False))
        run.now += .15

    p.on_endpoint = sound
    p.feed(QUIET * 50)
    assert p._generation == saved_generation and p.audio.empty()
    assert not p._chime_playing and p._chime_gate_until == pytest.approx(.45)
    reply(run, job)
    assert run.transcripts == [(job[2][0], '문을 닫아 주세요.')]
    run.now = .44
    p.feed(SECOND)
    assert p.session.utterance_id is None
    run.now = .46
    p.feed(SECOND)
    assert p.session.utterance_id is not None
    assert p.command_stream.collector.audio == SECOND


def test_chime_retains_incremental_stream_and_final_snapshot(run):
    p = run.pipeline
    stream = object()
    p._stream_factory = lambda: stream
    p.command_stream.partial_interval_s = 2.0
    job = candidate(run)
    assert p._stream is stream
    p.feed(QUIET * 50)
    assert p._stream is stream and p._final_input.stream is stream
    assert p._final_input.pcm == VOICE + QUIET * 100
    reply(run, job)
    assert len(run.chimes) == len(run.transcripts) == 1


def test_failed_output_preserves_transcript_and_releases_echo_gate(run):
    def failed_sound():
        run.chimes.append('attempt')
        raise RuntimeError('device unavailable')

    run.pipeline.on_endpoint = failed_sound
    job = candidate(run)
    reply(run, job)
    assert run.chimes == ['attempt']
    assert run.transcripts == [(job[2][0], '문을 닫아 주세요.')]
    assert 'endpoint_chime_failed:RuntimeError' in run.reports
    assert not run.pipeline._chime_playing and run.pipeline.session.active
    run.now = .31
    run.pipeline.feed(SECOND)
    assert run.pipeline.command_stream.collector.started


def test_feedback_drops_remaining_events_precomputed_from_the_same_pcm_chunk(run):
    p = run.pipeline
    job = candidate(run)
    reply(run, job, '문을 닫고')  # Incomplete sentence waits for two seconds.
    assert run.chimes == []
    p.feed(QUIET * 50 + SECOND)
    assert run.chimes == [job[2][0]]
    assert run.transcripts == [(job[2][0], '문을 닫고')]
    assert p.session.utterance_id is None and not p.command_stream.collector.started
    run.now = .31
    p.feed(SECOND)
    assert p.session.utterance_id is not None and p.command_stream.collector.audio == SECOND


@pytest.mark.parametrize('status', ['playing', 'paused'])
def test_existing_tts_playback_never_gets_a_second_output_stream(run, status):
    p = run.pipeline
    p.on_playback_status('answer', 'playing')
    p.feed(VOICE)  # A real interruption requests pause before its acknowledgement.
    if status == 'paused':
        p.on_playback_status('answer', 'paused')
    job = candidate(run)
    p.feed(QUIET * 50)
    assert run.chimes == [] and p._chime_gate_until == 0.0
    reply(run, job)
    # Existing addressee handling still decides whether to interrupt the answer.
    assert p.pending_addressee is not None


def test_wake_and_busy_discarded_captures_do_not_chime(run):
    p = run.pipeline
    p.session.terminate()
    p.feed(VOICE + QUIET * 20)
    wake = p.jobs.get_nowait()
    assert wake[0] == 'wake' and run.chimes == []
    reply(run, wake, '제이크야')
    p.feed(VOICE, busy_at_capture=True)
    p.feed(QUIET * 100)
    assert run.chimes == [] and p.jobs.empty()


@pytest.mark.parametrize('limit', ['duration', 'buffer'])
def test_rejected_over_limit_audio_is_not_acknowledged(run, limit):
    from malbut_stt.streaming import StreamingUtteranceCollector

    p = run.pipeline
    p.command_stream = StreamingUtteranceCollector(
        lambda frame, _: any(frame),
        settings=CaptureSettings(silence_timeout_s=2.0,
                                 max_utterance_s=3.0 if limit == 'duration' else None,
                                 max_buffer_s=3.0),
    )
    p.feed(VOICE * 151)
    assert run.chimes == [] and run.transcripts == []
    assert any(event.startswith('utterance_discarded:') for event in run.reports)
