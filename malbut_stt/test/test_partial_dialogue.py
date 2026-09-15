"""Partial recognition keeps one final delivery and never loses resumed speech."""

from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline

VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


def pump(pipeline, predicate):
    deadline = monotonic() + 3
    while not predicate():
        pipeline.poll()
        assert monotonic() < deadline
        sleep(0.001)


@pytest.fixture
def state():
    state = SimpleNamespace(final=[], partial=[], reports=[], calls=[], streams=[],
                            release=Event(), entered=Event())
    state.release.set()
    state.reply = lambda pcm, final: '문을 열어 줘.'

    class Stream:
        def transcribe(self, pcm, rate, *, final=False, speech_end_s=None):
            state.speech_end_s = speech_end_s
            state.calls.append((self, pcm, final))
            state.entered.set()
            assert state.release.wait(3)
            return state.reply(pcm, final)

    def create_stream():
        stream = Stream()
        state.streams.append(stream)
        return stream

    pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=None,
        transcriber=SimpleNamespace(create_stream=create_stream),
        is_speech=lambda pcm, _: any(pcm), input_has_aec=True,
        publish_transcript=lambda *args: state.final.append(args),
        publish_control=lambda *_: None, publish_interruption=lambda *_: None,
        report=state.reports.append, on_partial=lambda *args: state.partial.append(args),
        endpoint_predecode_s=0.8,
    )
    pipeline.session.activate()
    pipeline.asr_thread = Thread(target=pipeline._infer, daemon=True)
    pipeline.asr_thread.start()
    state.pipeline = pipeline
    yield state
    state.release.set()
    pipeline.close()


def test_partial_while_speaking_refreshes_the_tail_before_final_delivery(state):
    pipeline = state.pipeline
    pipeline.feed(VOICE * 100)
    pump(pipeline, lambda: len(state.partial) == 1)
    assert state.final == []
    assert state.calls[0][2] is False
    pipeline.feed(QUIET * 74)
    pipeline.poll()
    assert state.final == []
    pipeline.feed(QUIET)
    pump(pipeline, lambda: bool(state.final))
    assert state.final == [state.partial[-1]]
    assert len(state.calls) == 2
    assert state.calls[0][0] is state.calls[1][0]
    assert state.speech_end_s == pytest.approx(2.0)
    pipeline.poll()
    assert len(state.final) == 1


def test_resumed_voice_is_in_final_result_even_when_previous_partial_is_complete(state):
    pipeline = state.pipeline
    pipeline.feed(VOICE * 100)
    pump(pipeline, lambda: bool(state.partial))
    state.reply = lambda pcm, final: '문을 열지 말고 닫아 줘.'
    pipeline.feed(QUIET * 40 + VOICE * 30 + QUIET * 75)
    pump(pipeline, lambda: bool(state.final))
    assert state.final == [(state.partial[0][0], '문을 열지 말고 닫아 줘.')]
    assert len(state.calls) == 2


def test_slow_partial_keeps_latest_pending_audio_without_a_job_backlog(state):
    pipeline = state.pipeline
    state.release.clear()
    pipeline.feed(VOICE * 100)
    assert state.entered.wait(2)
    pipeline.feed(VOICE * 100)
    pipeline.feed(VOICE * 100)
    pipeline.feed(VOICE * 50)
    assert pipeline.jobs.empty()
    assert len(state.calls) == 1
    state.release.set()
    pump(pipeline, lambda: len(state.calls) == 2)
    # The latest 7 seconds of real captured speech; no synthetic age for word stability.
    assert len(state.calls[1][1]) == 7 * 32000
    assert state.final == []


@pytest.mark.parametrize('failure', ['', RuntimeError('temporary decode failure')])
def test_failed_intermediate_result_retries_latest_full_capture_at_final(state, failure):
    def reply(pcm, final):
        if not final:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return '거실에서 기다려 줘.'
    state.reply = reply
    pipeline = state.pipeline
    pipeline.feed(VOICE * 100)
    pump(pipeline, lambda: 'partial_failed' in state.reports)
    assert pipeline.session.active and state.final == []
    pipeline.feed(QUIET * 150)
    pump(pipeline, lambda: bool(state.final))
    assert state.final[0][1] == '거실에서 기다려 줘.'
    assert state.calls[-1][2] is True


def test_failed_inflight_partial_after_endpoint_retries_before_final_failure(state):
    pipeline = state.pipeline
    state.reply = lambda pcm, final: '최종 결과예요.' if final else ''
    state.release.clear()
    pipeline.feed(VOICE * 100)
    assert state.entered.wait(2)
    pipeline.feed(QUIET * 150)
    assert pipeline._busy and state.final == []
    state.release.set()
    pump(pipeline, lambda: bool(state.final))
    assert state.final[0][1] == '최종 결과예요.'
    assert len(state.calls) == 2


def test_weak_trailing_audio_is_decoded_even_when_vad_revision_does_not_change(state):
    pipeline = state.pipeline
    weak = b'\x02\x00' * 320
    pipeline.command_stream.is_speech = lambda pcm, _: pcm[:2] == b'\x01\x00'
    pipeline.command_stream.reset()
    state.reply = lambda pcm, final: ('내일 말고 모레 해줘.' if weak in pcm
                                      else '내일 해줘.')
    pipeline.feed(VOICE * 100)
    pump(pipeline, lambda: bool(state.partial))
    pipeline.feed(weak + QUIET * 74)
    pump(pipeline, lambda: bool(state.final))
    assert state.final[0][1] == '내일 말고 모레 해줘.'
    assert len(state.calls) == 2 and weak in state.calls[-1][1]


def test_new_session_does_not_receive_late_preview_or_reuse_old_stream(state):
    pipeline = state.pipeline
    state.release.clear()
    pipeline.feed(VOICE * 100)
    assert state.entered.wait(2)
    old_uid = pipeline._capture_id
    pipeline._terminate('reset_for_test')
    pipeline.session.activate()
    pipeline.feed(VOICE * 100)
    new_uid = pipeline._capture_id
    state.release.set()
    pump(pipeline, lambda: bool(state.partial))
    assert all(uid == new_uid for uid, _ in state.partial)
    assert old_uid != new_uid
    assert len(state.streams) == 2 and state.streams[0] is not state.streams[1]


def test_latest_partial_failure_never_publishes_an_older_prefix_as_final(state):
    pipeline = state.pipeline
    pipeline.feed(VOICE * 100)
    pump(pipeline, lambda: bool(state.partial))
    state.reply = lambda pcm, final: ''
    pipeline.feed(VOICE * 100 + QUIET * 150)
    pump(pipeline, lambda: 'empty_transcript' in state.reports)
    assert state.final == [] and pipeline.session.active


@pytest.mark.parametrize('decision', ['addressed', 'not_addressed'])
def test_interruption_partials_wait_for_final_text_and_addressee_decision(state, decision):
    pipeline = state.pipeline
    candidates, controls = [], []
    pipeline.publish_interruption = lambda *args: candidates.append(args)
    pipeline.publish_control = lambda *args: controls.append(args)
    pipeline.on_playback_status('playing-answer', 'playing')
    pipeline.feed(VOICE * 100)
    pump(pipeline, lambda: bool(state.partial))
    assert controls == [('playing-answer', 'pause')]
    assert candidates == [] and state.final == []
    pipeline.on_playback_status('playing-answer', 'paused')
    pipeline.feed(QUIET * 75)
    pump(pipeline, lambda: bool(candidates))
    uid, playback_id, text = candidates[0]
    assert state.final == []
    pipeline.on_addressee(uid, playback_id, decision)
    pipeline.on_addressee(uid, playback_id, decision)
    if decision == 'addressed':
        assert state.final == [(uid, text)]
        assert controls[-1] == ('playing-answer', 'stop')
    else:
        assert state.final == []
        assert controls[-1] == ('playing-answer', 'resume')
