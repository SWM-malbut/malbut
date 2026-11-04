"""A resumed utterance must not wait behind decoding its earlier pause."""
from threading import Event

import pytest

from test_final_preemption import (
    make_pipeline, queue_preview, start_worker, wait_for, VOICE, QUIET, FINAL_TEXT,
)


def reasons(state):
    return [fields['reason'] for event, fields in state.traces if event == 'job_preempted']


def test_queued_old_endpoint_skipped_and_all_resumed_pcm_reaches_new_endpoint(make_pipeline):
    cancels = []
    state = make_pipeline(start=False, cancel=lambda: cancels.append(True), endpoint_preempt_s=.8)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'endpoint')
    prefix = VOICE + QUIET * 20
    resumed = VOICE * 20
    pipeline.feed(resumed)
    pipeline.poll()
    assert reasons(state) == ['speech_resumed']
    assert not cancels and not state.transcripts and not pipeline._busy
    assert pipeline._capture_id == key[1] and pipeline._generation == key[0]
    assert bytes(pipeline.command_stream.collector.audio) == prefix + resumed
    start_worker(pipeline)
    wait_for(lambda: pipeline._endpoint_job is None, pipeline)
    pipeline.feed(QUIET * 30)
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)]
    assert len(state.calls) == 1 and state.calls[0][0] == prefix + resumed + QUIET * 20
    assert 'partial_failed' not in state.reports


def test_running_old_endpoint_cancelled_before_final_boundary_and_successor_untouched(make_pipeline):
    entered, cancelled = Event(), Event()
    successor_entered, successor_release = Event(), Event()
    cancel_calls = []
    calls = []

    def cancel():
        cancel_calls.append(True)
        cancelled.set()

    def decode(pcm, **options):
        calls.append((pcm, options))
        if len(calls) == 1:
            entered.set()
            assert cancelled.wait(2)
            raise RuntimeError('cancelled stale endpoint')
        cancelled.clear()
        successor_entered.set()
        assert successor_release.wait(2)
        assert not cancelled.is_set(), 'stale endpoint cancellation reached successor'
        return FINAL_TEXT

    state = make_pipeline(decode=decode, cancel=cancel, endpoint_preempt_s=.8)
    state.releases.extend([cancelled, successor_release])
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'endpoint')
    assert entered.wait(1)
    pipeline.audio.put((pipeline._audio_generation, 0., VOICE * 20, False))
    pipeline.poll()
    assert cancelled.is_set() and pipeline.command_stream.collector.silent_frames == 0
    assert reasons(state) == ['speech_resumed']
    wait_for(lambda: pipeline._endpoint_job is None, pipeline)
    assert not state.transcripts and not pipeline._busy
    count = len(cancel_calls)
    pipeline.feed(QUIET * 30)
    wait_for(successor_entered.is_set, pipeline)
    for _ in range(5):
        pipeline.poll()
    assert len(cancel_calls) == count and not cancelled.is_set()
    successor_release.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)] and len(calls) == 2
    assert calls[1][0] == VOICE + QUIET * 20 + VOICE * 20 + QUIET * 20
    assert all(not options['final'] for _, options in calls)
    assert 'partial_failed' not in state.reports


def test_backend_without_cancellation_discards_old_text_but_keeps_resumed_audio(make_pipeline):
    entered, release = Event(), Event()
    calls = []

    def decode(pcm, **_):
        calls.append(pcm)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            return '이전 멈춤 구간까지만 들었어요.'
        return FINAL_TEXT

    state = make_pipeline(decode=decode, endpoint_preempt_s=.8)
    state.releases.append(release)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'endpoint')
    assert entered.wait(1)
    pipeline.feed(VOICE * 20)
    pipeline.poll()
    assert reasons(state) == ['speech_resumed'] and not state.transcripts
    release.set()
    wait_for(lambda: pipeline._endpoint_job is None, pipeline)
    assert not state.transcripts
    pipeline.feed(QUIET * 30)
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)] and len(calls) == 2
    assert calls[1] == VOICE + QUIET * 20 + VOICE * 20 + QUIET * 20
    assert 'partial_failed' not in state.reports


def test_optout_retains_final_only_cancellation_after_resumed_speech(make_pipeline):
    cancelled, entered = Event(), Event()

    def decode(_pcm, **options):
        if not options['final']:
            entered.set()
            assert cancelled.wait(2)
            raise RuntimeError('cancelled at final')
        cancelled.clear()
        return FINAL_TEXT

    state = make_pipeline(decode=decode, cancel=cancelled.set, endpoint_preempt_s=None)
    state.releases.append(cancelled)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'endpoint')
    assert entered.wait(1)
    pipeline.feed(VOICE * 20)
    pipeline.poll()
    assert not cancelled.is_set() and not reasons(state)
    pipeline.feed(QUIET * 100)
    wait_for(lambda: state.transcripts, pipeline)
    assert reasons(state) == ['final_capture'] and state.transcripts == [(key[1], FINAL_TEXT)]


def test_current_endpoint_survives_continued_silence_and_final_capture(make_pipeline):
    entered, release = Event(), Event()
    cancel_calls = []

    def decode(_pcm, **_):
        entered.set()
        assert release.wait(2)
        return FINAL_TEXT

    state = make_pipeline(decode=decode, cancel=lambda: cancel_calls.append(True), endpoint_preempt_s=.8)
    state.releases.append(release)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'endpoint')
    assert entered.wait(1)
    pipeline.feed(QUIET * 40)
    pipeline.poll()
    assert not cancel_calls and not state.transcripts
    pipeline.feed(QUIET * 40)
    pipeline.poll()
    assert pipeline._endpoint_final == key and not cancel_calls
    release.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)] and len(state.calls) == 1
    assert not reasons(state)


def test_partial_during_resumed_speech_keeps_existing_priority_policy(make_pipeline):
    state = make_pipeline(start=False, endpoint_preempt_s=.8)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'partial')
    pipeline.feed(VOICE * 20)
    pipeline.poll()
    assert not reasons(state)
    assert pipeline._endpoint_job == key and pipeline._endpoint_is_partial
    pipeline.feed(QUIET * 40)
    pipeline.poll()
    assert reasons(state) == ['endpoint_priority']


def test_incomplete_resumed_sentence_still_waits_full_two_second_silence(make_pipeline):
    incomplete = '조금 더 설명한 다음에'
    state = make_pipeline(start=False, decode=lambda *_a, **_kw: incomplete, endpoint_preempt_s=.8)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'endpoint')
    pipeline.feed(VOICE * 20)
    pipeline.poll()
    start_worker(pipeline)
    wait_for(lambda: pipeline._endpoint_job is None, pipeline)
    pipeline.feed(QUIET * 30)
    wait_for(lambda: pipeline._endpoint_result is not None, pipeline)
    assert not state.transcripts
    pipeline.feed(QUIET * 69)
    pipeline.poll()
    assert not state.transcripts
    pipeline.feed(QUIET)
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], incomplete)] and len(state.calls) == 1
