"""Skipped jobs must not claim decode metrics from a prior stream snapshot."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline, _StreamInput


@pytest.mark.parametrize('skip', ['preempted', 'stale_generation'])
def test_skipped_job_has_no_previous_stream_metrics(skip):
    previous_metrics = {'decode_calls': [{'model_elapsed_s': 999.0}]}
    events = []

    def forbidden_decode(*args, **kwargs):
        raise AssertionError('Skipped job entered the stream transcriber')

    def observe(event, **fields):
        events.append((event, fields))
        if event == 'result_enqueued':
            pipeline.stopping.set()  # Process exactly one queued job, no background thread.

    pipeline = DialoguePipeline(
        recorder_factory=lambda: None, wake=None,
        transcriber=SimpleNamespace(transcribe=forbidden_decode),
        is_speech=lambda *_: True, publish_transcript=lambda *_: None,
        publish_control=lambda *_: None, publish_interruption=lambda *_: None,
        report=lambda *_: None,
        diagnostics=SimpleNamespace(event=observe, context=lambda **_: nullcontext(), error=None),
    )
    stream = SimpleNamespace(transcribe=forbidden_decode, last_metrics=previous_metrics)
    token = ('skipped-utterance', 1)
    generation = pipeline._generation - (skip == 'stale_generation')
    job_key = ('endpoint', generation, token)
    if skip == 'preempted':
        pipeline._preempted_preview = job_key
    pipeline.jobs.put((*job_key, _StreamInput(b'\x01\x00' * 320, stream, False, .02)))
    try:
        pipeline._infer()
        measured = [fields for event, fields in events if event == 'inference_measured']
        result = [fields for event, fields in events if event == 'inference_result']
        assert len(measured) == len(result) == 1
        assert measured[0]['stream_metrics'] is None
        assert result[0]['stream_metrics'] is None
        assert measured[0]['error'] == ('PreviewSuperseded' if skip == 'preempted' else 'RuntimeError')
        assert stream.last_metrics == previous_metrics
        assert pipeline.results.qsize() == 1
    finally:
        pipeline.close()
