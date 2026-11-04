"""Acoustic completion changes the hold without losing resumed or stale speech."""

from types import SimpleNamespace
from threading import Event, Thread

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from test_final_preemption import wait_for

VOICE = b'\x01\x00' * 320
TAIL = b'\x02\x00' * 320
QUIET = bytes(640)


@pytest.fixture
def make_pipeline():
    pipelines = []

    def make(**overrides):
        state = SimpleNamespace(transcripts=[], reports=[], chimes=[])
        options = dict(
            recorder_factory=lambda: None, wake=None, transcriber=None,
            is_speech=lambda pcm, _: any(pcm), input_has_aec=True,
            publish_transcript=lambda uid, text: state.transcripts.append((uid, text)),
            publish_control=lambda *_: None, publish_interruption=lambda *_: None,
            report=state.reports.append, clock=lambda: 0.,
            on_endpoint=lambda: state.chimes.append(True), chime_tail_s=0.,
            endpoint_predecode_s=.4, endpoint_min_silence_s=.8,
            adaptive_endpoint=True, smart_turn=object(),
            settings=CaptureSettings(silence_timeout_s=2., min_speech_s=.02),
        )
        options.update(overrides)
        state.pipeline = DialoguePipeline(**options)
        state.pipeline.session.activate()
        pipelines.append(state.pipeline)
        return state

    yield make
    for pipeline in pipelines:
        pipeline.close()


def answer(pipeline, job, text, complete, error=None):
    pipeline.results.put((*job[:3], text, error, 0., complete))
    pipeline.poll()


@pytest.mark.parametrize('complete,text,frames', [
    (True, '삼각형', 40),  # A complete short answer need not have a lexical ending.
    (False, '좋아요.', 100),  # Punctuation/ending cannot override the acoustic model.
    (None, '좋아요.', 100),  # Missing/failed semantic inference is conservative.
])
def test_complete_grace_and_incomplete_fallback(make_pipeline, complete, text, frames):
    state = make_pipeline()
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    job = p.jobs.get_nowait()
    assert job[0] == 'endpoint'
    assert job[3].endpoint_pcm == VOICE + QUIET * 20
    answer(p, job, text, complete)
    p.feed(QUIET * (frames - 21))
    p.poll()
    assert not state.transcripts and not state.chimes
    p.feed(QUIET)
    p.poll()
    assert state.transcripts == [(job[2][0], text)]
    assert len(state.chimes) == 1 and p.jobs.empty()
    answer(p, job, '늦은 중복 결과', True)
    assert state.transcripts == [(job[2][0], text)]


@pytest.mark.parametrize('complete,pause_frames', [(True, 39), (False, 80), (None, 99)])
def test_resumption_retains_pcm_and_rejects_previous_revision(
        make_pipeline, complete, pause_frames):
    state = make_pipeline()
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    old = p.jobs.get_nowait()
    answer(p, old, '앞부분이에요.', complete)
    p.feed(QUIET * (pause_frames - 20))
    p.poll()
    assert not state.transcripts
    p.feed(TAIL * 10 + QUIET * 20)
    p.poll()
    new = p.jobs.get_nowait()
    assert new[2][0] == old[2][0] and new[2][1] != old[2][1]
    assert new[3].endpoint_pcm == VOICE + QUIET * pause_frames + TAIL * 10 + QUIET * 20
    answer(p, old, '오래된 완결 결과예요.', True)
    p.feed(QUIET * 20)
    p.poll()
    assert not state.transcripts
    answer(p, new, '이어 말한 부분까지 들었어요.', True)
    assert state.transcripts == [(old[2][0], '이어 말한 부분까지 들었어요.')]


def test_queued_resumption_wins_over_ready_result_at_commit_boundary(make_pipeline):
    state = make_pipeline()
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    old = p.jobs.get_nowait()
    p.feed(QUIET * 20)
    # poll drains the microphone before accepting the completed inference.
    p.audio.put((p._audio_generation, 0., TAIL, False))
    answer(p, old, '끝났어요.', True)
    assert not state.transcripts and not state.chimes
    assert p.command_stream.collector.silent_frames == 0
    assert bytes(p.command_stream.collector.audio) == VOICE + QUIET * 40 + TAIL


@pytest.mark.parametrize('frames', [40, 100])
def test_inflight_asr_is_reused_without_duplicate_inference(make_pipeline, frames):
    state = make_pipeline()
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    job = p.jobs.get_nowait()
    p.feed(QUIET * (frames - 20))
    p.poll()
    assert not state.transcripts
    answer(p, job, '정상 답변입니다.', True)
    assert state.transcripts == [(job[2][0], '정상 답변입니다.')]
    assert p.jobs.empty() and len(state.chimes) == 1


@pytest.mark.parametrize('text,error', [('', None), ('감사합니다.', None), (None, 'RuntimeError')])
def test_semantic_completion_does_not_publish_unusable_asr(make_pipeline, text, error):
    state = make_pipeline()
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    job = p.jobs.get_nowait()
    answer(p, job, text, True, error)
    p.feed(QUIET * 20)
    p.poll()
    assert not state.transcripts and not state.chimes
    p.feed(QUIET * 60)
    assert p.jobs.get_nowait()[0] == 'command'
    assert not state.transcripts


def test_reset_discards_old_semantic_and_asr_results(make_pipeline):
    state = make_pipeline()
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    old = p.jobs.get_nowait()
    p._terminate('test_reset')
    p.session.activate()
    p.feed(TAIL + QUIET * 20)
    new = p.jobs.get_nowait()
    answer(p, old, '이전 대화예요.', True)
    p.feed(QUIET * 20)
    p.poll()
    assert not state.transcripts
    answer(p, new, '새 대화예요.', True)
    assert state.transcripts == [(new[2][0], '새 대화예요.')]


def test_explicit_short_fallback_is_not_extended(make_pipeline):
    state = make_pipeline(endpoint_predecode_s=None,
        settings=CaptureSettings(silence_timeout_s=.6, min_speech_s=.02))
    p = state.pipeline
    p.feed(VOICE + QUIET * 29)
    assert p.jobs.empty()
    p.feed(QUIET)
    job = p.jobs.get_nowait()
    assert job[0] == 'command' and len(state.chimes) == 1


@pytest.mark.parametrize('value', [None, 0, 1, 'true'])
def test_mode_requires_boolean(make_pipeline, value):
    with pytest.raises(ValueError, match='adaptive_endpoint'):
        make_pipeline(adaptive_endpoint=value)


def test_mode_requires_acoustic_model(make_pipeline):
    with pytest.raises(ValueError, match='requires Smart Turn'):
        make_pipeline(smart_turn=None)


@pytest.mark.parametrize('fail_guard', [False, True])
def test_classifier_and_asr_run_concurrently_and_guard_failure_keeps_asr(make_pipeline, fail_guard):
    guard_entered, asr_entered = Event(), Event()

    def check(pcm):
        assert pcm == VOICE + QUIET * 20
        guard_entered.set()
        assert asr_entered.wait(1), 'classifier serialized ahead of Whisper'
        if fail_guard:
            raise RuntimeError('classifier failed')
        return SimpleNamespace(complete=True, probability=.9, inference_ms=10.)

    def transcribe(pcm, _):
        asr_entered.set()
        assert guard_entered.wait(1), 'Whisper serialized ahead of classifier'
        return '정상 인식입니다.'

    state = make_pipeline(smart_turn=SimpleNamespace(check=check),
                          transcriber=SimpleNamespace(transcribe=transcribe))
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    p.asr_thread = Thread(target=p._infer)
    p.asr_thread.start()
    result = p.results.get(timeout=2)
    assert result[3:5] == ('정상 인식입니다.', None)
    assert result[-1] is (None if fail_guard else True)
    p.results.put_nowait(result)
    p.poll()
    p.feed(QUIET * (80 if fail_guard else 20))
    p.poll()
    assert state.transcripts == [(result[2][0], '정상 인식입니다.')]


def test_speech_resumed_during_classifier_rejects_old_result_and_keeps_audio(make_pipeline):
    entered, release, asr_done = Event(), Event(), Event()
    observed, calls = [], []

    def check(pcm):
        observed.append(pcm)
        if len(observed) == 1:
            entered.set()
            assert release.wait(2)
        return SimpleNamespace(complete=True, probability=.9, inference_ms=10.)

    def transcribe(pcm, _):
        calls.append(bytes(pcm))
        asr_done.set()
        return '이어진 문장입니다.' if len(calls) > 1 else '이전 문장입니다.'

    state = make_pipeline(smart_turn=SimpleNamespace(check=check),
                          transcriber=SimpleNamespace(transcribe=transcribe))
    p = state.pipeline
    p.feed(VOICE + QUIET * 20)
    p.asr_thread = Thread(target=p._infer)
    p.asr_thread.start()
    try:
        assert entered.wait(1) and asr_done.wait(1)
        uid = p._capture_id
        p.feed(TAIL * 10 + QUIET * 20)
        p.poll()
        release.set()
        wait_for(lambda: len(observed) == 2, p)
        p.feed(QUIET * 20)
        wait_for(lambda: state.transcripts, p)
        assert state.transcripts == [(uid, '이어진 문장입니다.')]
        assert observed[1] == VOICE + QUIET * 20 + TAIL * 10 + QUIET * 20
        assert calls[1] == VOICE + QUIET * 20 + TAIL * 10 + QUIET * 100
    finally:
        release.set()
