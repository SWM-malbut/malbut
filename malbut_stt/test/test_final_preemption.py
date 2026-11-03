"""Final capture preempts obsolete work without cancelling its successor."""

from contextlib import nullcontext
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.incremental import IncrementalWhisperStream


VOICE = b'\x01\x00' * 320
QUIET = bytes(640)
FINAL_TEXT = '마지막 말까지 정확하게 들었어요.'


def wait_for(predicate, pipeline=None):
    deadline = monotonic() + 2
    while not predicate():
        assert monotonic() < deadline, 'worker did not reach the expected boundary'
        if pipeline is not None:
            pipeline.poll()
        sleep(.001)


@pytest.fixture
def make_pipeline():
    created = []

    def make(*, decode=None, cancel=None, start=True, on_trace=None, stream=None,
             endpoint_preempt_s=None):
        state = SimpleNamespace(calls=[], reports=[], traces=[], transcripts=[], releases=[])

        def transcribe(pcm, rate, **options):
            state.calls.append((pcm, options))
            return FINAL_TEXT if decode is None else decode(pcm, **options)

        stream = stream or SimpleNamespace(transcribe=transcribe, retained_start_s=0.)
        backend = SimpleNamespace(create_stream=lambda: stream, transcribe=transcribe)
        if cancel is not None:
            backend.cancel = cancel

        def trace(event, **fields):
            state.traces.append((event, fields))
            if on_trace is not None:
                on_trace(event, **fields)

        pipeline = DialoguePipeline(
            recorder_factory=lambda: None, wake=None, transcriber=backend,
            is_speech=lambda frame, _: any(frame),
            publish_transcript=lambda uid, text: state.transcripts.append((uid, text)),
            publish_control=lambda *_: None, publish_interruption=lambda *_: None,
            report=state.reports.append, input_has_aec=True,
            diagnostics=SimpleNamespace(event=trace, context=lambda **_: nullcontext(), error=None),
            settings=CaptureSettings(silence_timeout_s=2, min_speech_s=.02,
                                     max_utterance_s=None),
            endpoint_predecode_s=.4, endpoint_min_silence_s=.6,
            endpoint_preempt_s=endpoint_preempt_s,
            clock=lambda: 0.,
        )
        state.pipeline = pipeline
        pipeline.session.activate()
        created.append(state)
        if start:
            start_worker(pipeline)
        return state

    yield make
    for state in created:
        for release in state.releases:
            release.set()
        state.pipeline.close()
        assert state.pipeline.asr_thread is None or not state.pipeline.asr_thread.is_alive()


def start_worker(pipeline):
    pipeline.asr_thread = Thread(target=pipeline._infer)
    pipeline.asr_thread.start()


def queue_preview(pipeline, kind):
    if kind == 'partial':
        pipeline.feed(VOICE * 100)
    else:
        pipeline.feed(VOICE + QUIET * 20)
    assert pipeline._endpoint_job is not None
    return pipeline._endpoint_job


def finish(pipeline, kind, *, current=False):
    if kind == 'endpoint' and not current:
        pipeline.feed(VOICE)  # A new revision makes the earlier pause obsolete.
    pipeline.feed(QUIET * 100)


@pytest.mark.parametrize('kind', ['partial', 'endpoint'])
def test_queued_obsolete_preview_is_replaced_before_worker_starts(make_pipeline, kind):
    cancelled = []
    state = make_pipeline(start=False, cancel=lambda: cancelled.append(True))
    pipeline = state.pipeline
    original_generation = pipeline._generation
    queue_preview(pipeline, kind)
    finish(pipeline, kind)
    assert cancelled == [] and pipeline._generation == original_generation
    assert state.reports.count('preview_preempted:kind=' + kind) == 1
    start_worker(pipeline)
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts[0][1] == FINAL_TEXT
    assert len(state.calls) == 1 and state.calls[0][1]['final'] is True
    assert pipeline._preempted_preview is None


def test_preview_dequeued_but_not_entered_is_skipped(make_pipeline):
    dequeued, proceed = Event(), Event()

    def trace(event, **fields):
        if event == 'job_dequeued' and fields['kind'] == 'partial':
            dequeued.set()
            assert proceed.wait(2)

    cancelled = []
    state = make_pipeline(on_trace=trace, cancel=lambda: cancelled.append(True))
    state.releases.append(proceed)
    pipeline = state.pipeline
    queue_preview(pipeline, 'partial')
    assert dequeued.wait(1)
    finish(pipeline, 'partial')
    assert cancelled == []  # No native call owns the backend yet.
    proceed.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert len(state.calls) == 1 and state.calls[0][1]['final'] is True
    assert 'partial_failed' not in state.reports
    assert state.reports.count('preview_preempted:kind=partial') == 1


@pytest.mark.parametrize('kind', ['partial', 'endpoint'])
def test_running_obsolete_preview_is_cancelled_and_final_keeps_all_pcm(make_pipeline, kind):
    entered, cancelled = Event(), Event()
    previews = []

    def decode(pcm, *, final, **_):
        if not final:
            previews.append(pcm)
            entered.set()
            assert cancelled.wait(2)
            raise RuntimeError('decode cancelled')
        cancelled.clear()  # Native backend resets cancellation for a new decode.
        return FINAL_TEXT

    state = make_pipeline(decode=decode, cancel=cancelled.set)
    state.releases.append(cancelled)
    pipeline = state.pipeline
    key = queue_preview(pipeline, kind)
    assert entered.wait(1)
    # Pauses shorter than the final boundary must not preempt the preview.
    pipeline.feed(QUIET * 20)
    pipeline.poll()
    assert not cancelled.is_set()
    finish(pipeline, kind)
    assert pipeline._generation == key[0]
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)]
    assert len(state.calls) == 2
    final_pcm, final_options = state.calls[1]
    assert final_pcm.startswith(previews[0]) and len(final_pcm) > len(previews[0])
    assert final_options['final'] is True
    assert 'partial_failed' not in state.reports
    assert state.reports.count('preview_preempted:kind=' + kind) == 1
    cancelled_results = [fields for event, fields in state.traces
                         if event == 'inference_result' and fields['error'] == 'PreviewSuperseded']
    assert len(cancelled_results) == 1
    assert pipeline._partial_cost_s is None


@pytest.mark.parametrize('endpoint_preempt_s', [None, .8])
def test_current_endpoint_finishes_without_preemption_or_duplicate_final(
        make_pipeline, endpoint_preempt_s):
    entered, release = Event(), Event()
    cancellations = []

    def decode(_pcm, **_):
        entered.set()
        assert release.wait(2)
        return FINAL_TEXT

    state = make_pipeline(decode=decode, cancel=lambda: cancellations.append(True),
                          endpoint_preempt_s=endpoint_preempt_s)
    state.releases.append(release)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'endpoint')
    assert entered.wait(1)
    pipeline.feed(QUIET * 20)
    pipeline.poll()  # .8 s silence must leave a current endpoint alone.
    finish(pipeline, 'endpoint', current=True)
    pipeline.poll()
    assert cancellations == [] and pipeline._endpoint_final == key
    release.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert len(state.calls) == 1 and state.transcripts == [(key[1], FINAL_TEXT)]
    assert not any(event == 'job_preempted' for event, _ in state.traces)
    assert not any(event.startswith('preview_preempted:') for event in state.reports)


@pytest.mark.parametrize('cancel_raises', [False, True])
def test_backend_without_working_cancel_retains_serial_correctness(make_pipeline, cancel_raises):
    entered, release = Event(), Event()

    def decode(_pcm, *, final, **_):
        if not final:
            entered.set()
            assert release.wait(2)
            return '아직 끝나지 않은 말'
        return FINAL_TEXT

    def broken_cancel():
        if not release.is_set():
            raise RuntimeError('backend does not support cancellation')

    state = make_pipeline(decode=decode, cancel=broken_cancel if cancel_raises else None)
    # close() has its separate backend contract; this test covers preemption.
    state.releases.append(release)
    pipeline = state.pipeline
    queue_preview(pipeline, 'partial')
    assert entered.wait(1)
    finish(pipeline, 'partial')
    pipeline.poll()
    assert not state.transcripts and len(state.calls) == 1
    if cancel_raises:
        del pipeline.transcriber.cancel
    release.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts[0][1] == FINAL_TEXT and len(state.calls) == 2
    assert 'partial_failed' not in state.reports


def test_native_entry_cancel_reset_is_retried_only_until_obsolete_job_retires(make_pipeline):
    before_reset, allow_reset, in_native = Event(), Event(), Event()
    final_entered, finish_final, cancelled = Event(), Event(), Event()
    cancellation_calls = []

    def cancel():
        cancellation_calls.append(True)
        cancelled.set()

    def decode(_pcm, *, final, **_):
        if not final:
            # The first native decode has not reset its cancel flag yet.
            before_reset.set()
            assert allow_reset.wait(2)
            cancelled.clear()
            in_native.set()
            assert cancelled.wait(2)
            raise RuntimeError('decode cancelled')
        cancelled.clear()
        final_entered.set()
        assert finish_final.wait(2)
        assert not cancelled.is_set(), 'obsolete preview cancellation leaked into final'
        return FINAL_TEXT

    state = make_pipeline(decode=decode, cancel=cancel)
    state.releases.extend([allow_reset, cancelled, finish_final])
    pipeline = state.pipeline
    queue_preview(pipeline, 'partial')
    assert before_reset.wait(1)
    finish(pipeline, 'partial')
    assert len(cancellation_calls) == 1
    allow_reset.set()
    assert in_native.wait(1) and not cancelled.is_set()
    pipeline.poll()  # Repeat the signal cleared by this job's native entry.
    assert len(cancellation_calls) == 2
    assert final_entered.wait(1)
    calls_at_final = len(cancellation_calls)
    for _ in range(5):
        pipeline.poll()
    assert len(cancellation_calls) == calls_at_final
    finish_final.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts[0][1] == FINAL_TEXT

    # A subsequent ordinary follow-up also starts with a clean cancellation
    # owner, even though its final uses the same native backend.
    first_uid = state.transcripts[0][0]
    pipeline.on_playback_status('reply', 'playing', request_id=first_uid)
    pipeline.on_playback_status('reply', 'finished', request_id=first_uid)
    assert pipeline.session.active
    pipeline.feed(VOICE + QUIET * 100)
    wait_for(lambda: len(state.transcripts) == 2, pipeline)
    assert state.transcripts[1][0] != first_uid
    assert state.transcripts[1][1] == FINAL_TEXT


def test_incremental_recovery_cancel_preserves_committed_prefix_for_final(make_pipeline):
    before_reset, allow_reset, in_native = Event(), Event(), Event()
    final_entered, finish_final, cancelled = Event(), Event(), Event()
    durations = []
    original = [SimpleNamespace(start=4 * index + .2, end=4 * index + 3.5,
                                text=f' 문장{index}') for index in range(6)]
    suffix = [SimpleNamespace(start=.7, end=4., text=' 문장5'),
              SimpleNamespace(start=4.7, end=8., text=' 마지막 말')]

    def native(audio, **_):
        durations.append(len(audio) / 16000)
        index = len(durations)
        if index <= 2:
            return iter(original), None
        if index == 3:
            # Changed overlap makes the actual stream recover its full PCM.
            return iter([SimpleNamespace(start=.7, end=4., text=' 달라진 말'),
                         suffix[1]]), None
        if index == 4:
            before_reset.set()
            assert allow_reset.wait(2)
            cancelled.clear()  # A second native entry clears the first signal.
            in_native.set()
            assert cancelled.wait(2)
            raise RuntimeError('decode cancelled')
        assert index == 5
        cancelled.clear()
        final_entered.set()
        assert finish_final.wait(2)
        assert not cancelled.is_set()
        return iter(suffix), None

    stream = IncrementalWhisperStream(SimpleNamespace(transcribe=native))
    stream.transcribe(VOICE * 1200, 16000)
    stream.transcribe(VOICE * 1300, 16000)
    committed = list(stream._committed)
    previous = list(stream._previous)
    assert stream.retained_start_s == 19.5
    state = make_pipeline(stream=stream, cancel=cancelled.set)
    state.releases.extend([allow_reset, cancelled, finish_final])
    pipeline = state.pipeline
    pipeline.feed(VOICE * 1400)
    assert before_reset.wait(1)
    pipeline.feed(QUIET * 100)
    assert cancelled.is_set()
    allow_reset.set()
    assert in_native.wait(1) and not cancelled.is_set()
    pipeline.poll()
    assert final_entered.wait(1)
    # Recovery failed before committing anything. The final still owns every
    # uncommitted sample and can decode the ordinary overlapping suffix.
    assert stream._committed == committed and stream._previous == previous
    assert stream.retained_start_s == 19.5
    pipeline.poll()
    assert not cancelled.is_set()
    finish_final.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert durations == [24., 26., 8.5, 28., 10.5]
    assert state.transcripts[0][1] == '문장0 문장1 문장2 문장3 문장4 문장5 마지막 말'
    assert 'partial_failed' not in state.reports


def test_completed_preview_is_not_cancelled_or_rolled_back(make_pipeline):
    result_ready, allow_result = Event(), Event()
    committed, cancel_calls = [], []

    def decode(_pcm, *, final, **_):
        if not final:
            committed.append('safe prefix')
            return '진행 중인 문장'
        assert committed == ['safe prefix']
        return FINAL_TEXT

    def trace(event, **fields):
        if event == 'inference_result' and fields['text'] == '진행 중인 문장':
            result_ready.set()
            assert allow_result.wait(2)

    state = make_pipeline(decode=decode, cancel=lambda: cancel_calls.append(True),
                          on_trace=trace)
    state.releases.append(allow_result)
    pipeline = state.pipeline
    queue_preview(pipeline, 'partial')
    assert result_ready.wait(1)
    finish(pipeline, 'partial')
    pipeline.poll()
    assert cancel_calls == []
    allow_result.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts[0][1] == FINAL_TEXT and committed == ['safe prefix']


def test_stable_silence_replaces_queued_partial_with_fresh_endpoint_but_keeps_fallback(
        make_pipeline):
    incomplete = '마지막 말을 다 한 다음에'
    state = make_pipeline(start=False, decode=lambda *_args, **_kw: incomplete,
                          endpoint_preempt_s=.8)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'partial')
    pipeline.feed(QUIET * 39)
    pipeline.poll()
    assert not any(event == 'job_preempted' for event, _ in state.traces)
    pipeline.feed(QUIET)
    pipeline.poll()
    assert pipeline._capture_id == key[1] and not pipeline._busy
    for _ in range(3):
        pipeline.poll()
    assert state.reports.count('preview_preempted:kind=partial') == 1
    start_worker(pipeline)
    wait_for(lambda: pipeline._endpoint_result is not None, pipeline)
    assert not state.transcripts  # An incomplete sentence still needs 2 s.
    assert len(state.calls) == 1
    assert state.calls[0][0] == VOICE * 100 + QUIET * 40
    assert state.calls[0][1]['speech_end_s'] == pytest.approx(2.)
    assert state.calls[0][1]['final'] is False
    assert pipeline._partial_cost_s is None
    pipeline.feed(QUIET * 59)
    pipeline.poll()
    assert not state.transcripts and pipeline._capture_id == key[1]
    pipeline.feed(QUIET)
    pipeline.poll()
    assert state.transcripts == [(key[1], incomplete)] and len(state.calls) == 1
    assert [fields['reason'] for event, fields in state.traces
            if event == 'job_preempted'] == ['endpoint_priority']


def test_early_preemption_retries_native_reset_then_leaves_current_endpoint_running(
        make_pipeline):
    before_reset, allow_reset, in_native = Event(), Event(), Event()
    endpoint_entered, finish_endpoint, cancelled = Event(), Event(), Event()
    calls, cancellation_calls = [], []

    def cancel():
        cancellation_calls.append(True)
        cancelled.set()

    def decode(_pcm, **options):
        calls.append(options)
        if len(calls) == 1:
            before_reset.set()
            assert allow_reset.wait(2)
            cancelled.clear()
            in_native.set()
            assert cancelled.wait(2)
            raise RuntimeError('cancelled stale partial')
        cancelled.clear()
        endpoint_entered.set()
        assert finish_endpoint.wait(2)
        assert not cancelled.is_set(), 'partial cancellation leaked into the endpoint'
        return FINAL_TEXT

    state = make_pipeline(decode=decode, cancel=cancel, endpoint_preempt_s=.8)
    state.releases.extend([allow_reset, cancelled, finish_endpoint])
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'partial')
    assert before_reset.wait(1)
    pipeline.feed(QUIET * 40)
    pipeline.poll()
    assert len(cancellation_calls) == 1
    allow_reset.set()
    assert in_native.wait(1) and not cancelled.is_set()
    pipeline.poll()
    wait_for(endpoint_entered.is_set, pipeline)
    assert len(cancellation_calls) >= 2
    count_at_endpoint = len(cancellation_calls)
    for _ in range(5):
        pipeline.poll()
    pipeline.feed(QUIET * 60)  # Final capture waits for this current endpoint.
    pipeline.poll()
    assert len(cancellation_calls) == count_at_endpoint
    assert state.reports.count('preview_preempted:kind=partial') == 1
    assert pipeline._endpoint_final == key
    finish_endpoint.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)] and len(calls) == 2
    assert all(options['final'] is False for options in calls)
    assert 'partial_failed' not in state.reports and pipeline._partial_cost_s is None


def test_buffered_resumed_speech_is_consumed_before_silence_preemption(make_pipeline):
    state = make_pipeline(start=False, endpoint_preempt_s=.8)
    pipeline = state.pipeline
    first = queue_preview(pipeline, 'partial')
    pipeline.feed(QUIET * 40)
    pipeline.audio.put((pipeline._audio_generation, 0., VOICE, False))
    pipeline.poll()
    assert not any(event == 'job_preempted' for event, _ in state.traces)
    assert pipeline.command_stream.collector.revision > first[2]
    pipeline.feed(QUIET * 40)
    pipeline.poll()
    assert state.reports.count('preview_preempted:kind=partial') == 1
    start_worker(pipeline)
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(first[1], FINAL_TEXT)]
    assert state.calls[0][0] == VOICE * 100 + QUIET * 40 + VOICE + QUIET * 40


def test_resuming_after_preemption_invalidates_snapshot_and_preserves_entire_utterance(
        make_pipeline):
    entered, release = Event(), Event()
    calls = []

    def decode(pcm, **_):
        calls.append(pcm)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            return '다시 말하기 전의 결과'
        return FINAL_TEXT

    state = make_pipeline(decode=decode, endpoint_preempt_s=.8)
    state.releases.append(release)  # Backend without cancel finishes serially.
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'partial')
    assert entered.wait(1)
    pipeline.feed(QUIET * 40)
    pipeline.poll()
    pipeline.feed(VOICE * 20)
    release.set()
    wait_for(lambda: pipeline._endpoint_job is None, pipeline)
    assert pipeline._endpoint_candidate is None and not state.transcripts
    pipeline.feed(QUIET * 40)
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)] and len(calls) == 2
    assert calls[-1] == VOICE * 100 + QUIET * 40 + VOICE * 20 + QUIET * 20
    assert 'partial_failed' not in state.reports


def test_final_capture_does_not_repeat_early_preemption_request(make_pipeline):
    entered, release = Event(), Event()

    def decode(_pcm, *, final, **_):
        if not final:
            entered.set()
            assert release.wait(2)
        return FINAL_TEXT

    state = make_pipeline(decode=decode, endpoint_preempt_s=.8)
    state.releases.append(release)
    pipeline = state.pipeline
    key = queue_preview(pipeline, 'partial')
    assert entered.wait(1)
    pipeline.feed(QUIET * 40)
    pipeline.poll()
    pipeline.feed(QUIET * 60)
    pipeline.poll()
    assert state.reports.count('preview_preempted:kind=partial') == 1
    release.set()
    wait_for(lambda: state.transcripts, pipeline)
    assert state.transcripts == [(key[1], FINAL_TEXT)]
    assert len(state.calls) == 2 and state.calls[-1][1]['final'] is True


@pytest.mark.parametrize('value', [True, '0.8', -.1, .19, 1.01, float('inf'), float('nan')])
def test_invalid_endpoint_preemption_floor_rejected(make_pipeline, value):
    with pytest.raises(ValueError, match='endpoint_preempt_s'):
        make_pipeline(endpoint_preempt_s=value)
