"""Validate adaptive endpoints with deterministic audio and delayed ASR replies."""

import math
from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.endpoint import is_complete_korean_utterance
from malbut_stt.streaming import StreamingUtteranceCollector


VOICE = b'\x01\x00' * 320
SECOND = b'\x02\x00' * 320
QUIET = bytes(640)


@pytest.mark.parametrize('text', [
    '거실로 이동해 주세요.', '계속 기다려 줄래요?', '지금 멈춰 줘',
    '반갑습니다', '문이 열려 있습니까?', '어디에 있나요', '괜찮아요!',
    '그림이 정말 멋지네요.', '오늘은 외출하지 마', '이것은 의자예요',
    '오늘은 집에 있을 거야.', '오늘 날씨가 어때?', '지금 어디에 있어?',
    '지금 몇 시야?', '지금 누구야?', '관심이 없다.', '그렇게 한다.',
    '여기에 있다', '아무도 없어', '눈이 많이 왔다.',
    '이제 준비가 끝났어.', '저녁은 언제 먹을까?', '우산이 필요하겠네.',
    '아니, 열지 마.', '다녀왔어.', '함께 갈까?',
    '뭐지? 너 너무 느리다', '너무 느려.', '너무 느려요.',
    '오늘 기분이 좋다', '나는 이게 좋아', '정말 좋아요', '지금 뭐해?',
    '알겠지?', '한번 해봐라',
    '제 이름은 제이크입니다.', '정말입니까?',
])
def test_explicit_korean_final_endings_can_shorten_the_wait(text):
    assert is_complete_korean_utterance(text)


@pytest.mark.parametrize('text', [
    '', '...', '제이크야', '저기요', '거실에서.', '지금 문을 닫고!',
    '조금 기다렸다가', '비가 오면', '이게 필요한데요.', '나는 사과하고요',
    '멈춰 줘. 그리고', '말해 주세요라는', '“문을 닫아 주세요”',
    'This looks complete.', '오늘 날씨', '계속할게', '그런데요?',
    '캐나다.', '바다?', '사이다!', '오늘은 집에 있을 거야라고',
    '이게 어디야라는',
    '친구네', '군대', '내가 준비를 끝냈어도', '먹을까 하고',
    '너무 느려서', '기분이 좋아도', '너무 느리다고', '좋다며',
    '한번 해봐라라고', '좋아요라는',
])
def test_fragments_connectives_quotes_and_punctuation_keep_fallback(text):
    assert not is_complete_korean_utterance(text)


def test_early_snapshot_is_once_per_pause_and_revision_survives_reset():
    stream = StreamingUtteranceCollector(lambda frame, _: any(frame), early_endpoint_s=1.5)
    stream.feed(VOICE)
    assert stream.feed(QUIET * 74) == []
    candidate, = stream.feed(QUIET)
    assert candidate.status == 'endpoint_check' and candidate.silence_s == 1.5
    assert stream.feed(QUIET * 10) == []
    stream.feed(SECOND)
    assert stream.finish_endpoint(candidate.revision) is None
    newer, = stream.feed(QUIET * 75)
    assert newer.revision != candidate.revision
    assert stream.finish_endpoint(candidate.revision) is None
    stream.reset()
    stream.feed(VOICE)
    newest, = stream.feed(QUIET * 75)
    assert newest.revision not in (candidate.revision, newer.revision)
    assert stream.finish_endpoint(newer.revision) is None
    assert stream.finish_endpoint(newest.revision).silence_s == 1.5


@pytest.fixture
def run():
    state = SimpleNamespace(now=0.0, transcripts=[], reports=[], controls=[])
    pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=None, transcriber=None,
        is_speech=lambda frame, _: any(frame),
        publish_transcript=lambda uid, text: state.transcripts.append((uid, text)),
        publish_control=lambda pid, cmd: state.controls.append((pid, cmd)),
        publish_interruption=lambda *_: None, report=state.reports.append,
        clock=lambda: state.now, input_has_aec=True,
    )
    pipeline.session.activate()
    state.pipeline = pipeline
    yield state
    pipeline.close()


def candidate(run):
    pipeline = run.pipeline
    pipeline.feed(VOICE)
    uid = pipeline.session.utterance_id
    pipeline.feed(QUIET * 74)
    assert pipeline.jobs.empty()
    run.now = 1.5
    pipeline.feed(QUIET)
    job = pipeline.jobs.get_nowait()
    assert job[0] == 'endpoint' and job[2][0] == uid
    assert job[3] == VOICE + QUIET * 150
    assert not pipeline._busy
    return job


def reply(run, job, text, error=None):
    kind, generation, token, _pcm = job
    run.pipeline.results.put_nowait((kind, generation, token, text, error))
    run.pipeline.poll()


def test_complete_candidate_finalizes_at_15_seconds_and_reuses_the_text(run):
    job = candidate(run)
    reply(run, job, '문을 닫아 주세요.')
    assert run.transcripts == [(job[2][0], '문을 닫아 주세요.')]
    assert run.pipeline.jobs.empty()
    assert not run.pipeline._busy
    assert 'endpoint_finalized:silence_s=1.50' in run.reports


@pytest.mark.parametrize('fallback_s', [3.0, 3.01, 1.51])
def test_candidate_zero_padding_matches_actual_fallback_pcm_without_waiting(run, fallback_s):
    settings = CaptureSettings(silence_timeout_s=fallback_s)
    stream = StreamingUtteranceCollector(
        lambda frame, _: any(frame), settings=settings, early_endpoint_s=1.5,
    )
    run.pipeline.command_stream = stream
    run.pipeline.feed(VOICE + QUIET * 75)
    job = run.pipeline.jobs.get_nowait()
    observed = stream.collector.snapshot('endpoint_check')
    assert job[0] == 'endpoint' and job[2][1] == observed.revision
    assert observed.pcm == VOICE + QUIET * 75 and observed.silence_s == 1.5
    assert stream.collector.silent_frames == 75
    baseline = StreamingUtteranceCollector(lambda frame, _: any(frame), settings=settings)
    events = baseline.feed(VOICE + QUIET * math.ceil(fallback_s / 0.02))
    final, = [event for event in events if event.status == 'complete']
    assert job[3] == final.pcm
    assert observed.pcm == VOICE + QUIET * 75  # The immutable capture was not padded.
    reply(run, job, '문을 닫아 주세요.')
    assert run.transcripts == [(job[2][0], '문을 닫아 주세요.')]
    assert 'endpoint_finalized:silence_s=1.50' in run.reports


def test_padding_preserves_observed_nonzero_vad_negative_audio(run):
    stream = StreamingUtteranceCollector(
        lambda frame, _: frame[:2] == VOICE[:2], early_endpoint_s=1.5,
    )
    run.pipeline.command_stream = stream
    observed_pcm = VOICE + SECOND * 75
    run.pipeline.feed(observed_pcm)
    job = run.pipeline.jobs.get_nowait()
    observed = stream.collector.snapshot('endpoint_check')
    assert observed.pcm == observed_pcm and observed.silence_s == 1.5
    assert job[3] == observed_pcm + QUIET * 75
    assert job[3] != VOICE + SECOND * 150  # Real future noise is not assumed identical.
    assert stream.collector.silent_frames == 75
    reply(run, job, '문을 닫아 주세요.')
    assert 'endpoint_finalized:silence_s=1.50' in run.reports


def test_synthetic_padding_does_not_advance_the_real_maximum_utterance_limit(run):
    pipeline = run.pipeline
    pipeline.feed(VOICE * 890 + QUIET * 75)  # 19.3 seconds actually captured.
    job = pipeline.jobs.get_nowait()
    assert len(job[3]) / 32000 == 20.8  # The extra input is inference-only.
    assert pipeline.command_stream.collector.speech_frames == 965
    assert pipeline.session.active and 'utterance_discarded:too_long' not in run.reports
    pipeline.feed(QUIET * 36)  # Actual capture now exceeds the unchanged 20-second limit.
    assert not pipeline.session.active
    assert 'utterance_discarded:too_long' in run.reports
    reply(run, job, '문을 닫아 주세요.')
    assert run.transcripts == []


def test_uncertain_candidate_keeps_three_seconds_without_a_second_decode(run):
    job = candidate(run)
    reply(run, job, '문을 닫고.')
    assert run.transcripts == []
    run.pipeline.feed(QUIET * 74)
    assert run.transcripts == [] and run.pipeline.jobs.empty()
    run.pipeline.feed(QUIET)
    assert run.transcripts == [(job[2][0], '문을 닫고.')]
    assert run.pipeline.jobs.empty()
    assert 'endpoint_finalized:silence_s=3.00' in run.reports


def test_result_arriving_after_three_seconds_reuses_the_inflight_decode(run):
    job = candidate(run)
    run.pipeline.feed(QUIET * 75)
    assert run.pipeline.jobs.empty() and run.pipeline._busy
    reply(run, job, '문을 닫아 주세요.')
    assert run.transcripts == [(job[2][0], '문을 닫아 주세요.')]
    assert not run.pipeline._busy
    assert run.pipeline.jobs.empty()


def test_reset_releases_busy_owned_by_a_finalized_endpoint_wait(run):
    old = candidate(run)
    pipeline = run.pipeline
    pipeline.feed(QUIET * 75)
    assert pipeline._busy and pipeline._endpoint_final is not None
    pipeline.overflow.set()
    pipeline.poll()
    assert not pipeline._busy and pipeline._endpoint_final is None
    reply(run, old, '오래된 문장입니다.')
    assert not pipeline._busy and pipeline._endpoint_job is None
    assert run.transcripts == []
    pipeline.feed(VOICE + QUIET * 20)
    wake_job = pipeline.jobs.get_nowait()
    assert wake_job[0] == 'wake'
    reply(run, wake_job, '제이크')
    assert pipeline.session.active


def test_stale_endpoint_does_not_release_busy_owned_by_a_new_wake_job(run):
    old = candidate(run)
    pipeline = run.pipeline
    pipeline.feed(QUIET * 75)
    pipeline.overflow.set()
    pipeline.poll()
    pipeline.feed(VOICE + QUIET * 20)
    wake_job = pipeline.jobs.get_nowait()
    assert wake_job[0] == 'wake' and pipeline._busy
    reply(run, old, '오래된 문장입니다.')
    assert pipeline._busy and not pipeline.session.active
    reply(run, wake_job, '제이크')
    assert not pipeline._busy and pipeline.session.active


def test_renewed_speech_invalidates_old_complete_text_even_after_new_15_second_pause(run):
    old = candidate(run)
    run.pipeline.feed(SECOND + QUIET * 75)
    reply(run, old, '문을 닫아 주세요.')
    assert run.transcripts == []
    new = run.pipeline.jobs.get_nowait()
    assert new[2] != old[2]
    assert new[3] == VOICE + QUIET * 75 + SECOND + QUIET * 150
    reply(run, new, '문을 닫아 주시고 불도 꺼 주세요.')
    assert run.transcripts == [(old[2][0], '문을 닫아 주시고 불도 꺼 주세요.')]


def test_renewed_speech_drops_a_cached_uncertain_transcript(run):
    old = candidate(run)
    reply(run, old, '문을 닫고')
    run.pipeline.feed(SECOND + QUIET * 150)
    final = run.pipeline.jobs.get_nowait()
    assert final[0] == 'command'
    assert final[3] == VOICE + QUIET * 75 + SECOND + QUIET * 150
    reply(run, final, '문을 닫고 불을 꺼 주세요.')
    assert run.transcripts == [(old[2][0], '문을 닫고 불을 꺼 주세요.')]


def test_obsolete_inflight_result_does_not_clear_a_new_final_job_busy_flag(run):
    old = candidate(run)
    run.pipeline.feed(SECOND + QUIET * 150)
    assert run.pipeline._busy
    reply(run, old, '이전 후보예요.')
    assert run.pipeline._busy and run.transcripts == []
    final = run.pipeline.jobs.get_nowait()
    assert final[0] == 'command'
    reply(run, final, '이어진 문장이에요.')
    assert not run.pipeline._busy
    assert run.transcripts == [(old[2][0], '이어진 문장이에요.')]


def test_final_audio_replaces_an_obsolete_candidate_not_yet_taken_by_worker(run):
    pipeline = run.pipeline
    pipeline.feed(VOICE + QUIET * 75)
    assert not pipeline.jobs.empty()
    pipeline.feed(SECOND + QUIET * 150)
    final = pipeline.jobs.get_nowait()
    assert final[0] == 'command'
    assert final[3] == VOICE + QUIET * 75 + SECOND + QUIET * 150
    reply(run, final, '이어진 문장이에요.')
    assert len(run.transcripts) == 1


@pytest.mark.parametrize('reason', ['overflow', 'too_long', 'close'])
def test_invalidated_audio_never_uses_a_late_candidate(run, reason):
    old = candidate(run)
    pipeline = run.pipeline
    if reason == 'overflow':
        pipeline.overflow.set()
        pipeline.poll()
        assert 'audio_queue_overflow' in run.reports
    elif reason == 'too_long':
        pipeline.feed(SECOND * 1001)
        assert 'utterance_discarded:too_long' in run.reports
    else:
        pipeline.close()
        assert pipeline._endpoint_job is None and pipeline._endpoint_candidate is None
    reply(run, old, '완결된 문장입니다.')
    assert run.transcripts == [] and not pipeline.session.active


def test_candidate_failure_does_not_end_the_utterance_early(run):
    job = candidate(run)
    reply(run, job, None, 'RuntimeError')
    assert run.pipeline.session.active and run.transcripts == []
    run.pipeline.feed(QUIET * 75)
    assert run.pipeline.session.active and run.pipeline.session.utterance_id is None
    assert 'transcription_failed:RuntimeError' in run.reports
    assert run.pipeline.jobs.empty()
    next_job = candidate(run)
    assert next_job[2][0] != job[2][0]
    reply(run, next_job, '문을 닫아 주세요.')
    assert run.transcripts == [(next_job[2][0], '문을 닫아 주세요.')]


def test_report_includes_actual_audio_silence_and_candidate_wait(run):
    job = candidate(run)
    run.now = 2.1
    run.pipeline.feed(QUIET * 30)
    reply(run, job, '완료했습니다.')
    assert 'endpoint_checked:wait_s=0.600' in run.reports
    assert 'endpoint_finalized:silence_s=2.10' in run.reports


def test_wake_detection_keeps_its_short_audio_boundary(run):
    pipeline = run.pipeline
    pipeline.session.terminate()
    pipeline.feed(VOICE + QUIET * 20)
    job = pipeline.jobs.get_nowait()
    assert job[0] == 'wake'
    assert 'checking_endpoint:silence_s=1.50' not in run.reports
