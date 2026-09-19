"""Bound long speech PCM while delivering its accumulated text exactly once."""

from array import array
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.transcription import LocalWhisperTranscriber


QUIET = bytes(640)
REPEATED = frozenset((31, 32, 255, 256, 599, 600))


def token_text(value):
    return '다시' if value in REPEATED else f'단어{value:04d}'


def token_pcm(value):
    """Encode one identifiable second of actual PCM, rather than scripted ASR."""
    return (array('h', [value]) * 16000).tobytes()


class PcmTokenModel:
    """Decode amplitude runs and relative timestamps from the provided samples."""

    def __init__(self):
        self.calls = []
        self.release = Event()
        self.release.set()
        self.entered = Event()
        self.block_next = False

    def hold_next(self):
        self.entered.clear()
        self.release.clear()
        self.block_next = True

    def transcribe(self, audio, **_options):
        # Do not retain arrays in instrumentation: that would itself grow PCM
        # memory across the ten-minute test and obscure the production bound.
        values = np.rint(audio * 32768).astype(np.int16)
        boundaries = np.r_[0, np.flatnonzero(values[1:] != values[:-1]) + 1,
                           len(values)]
        segments = [
            SimpleNamespace(text=' ' + token_text(int(values[start])),
                            start=start / 16000, end=end / 16000)
            for start, end in zip(boundaries[:-1], boundaries[1:])
            if start < end and values[start] != 0
        ]
        self.calls.append((len(audio), len(segments)))
        if self.block_next:
            self.block_next = False
            self.entered.set()
            assert self.release.wait(5), 'test did not release the fake decoder'
        return iter(segments), None


def pump(pipeline, predicate):
    deadline = monotonic() + 5
    while True:
        pipeline.poll()
        if predicate():
            return
        assert monotonic() < deadline, 'ASR worker did not reach the expected state'
        sleep(0.001)


def settle(pipeline):
    pump(pipeline, lambda: pipeline._endpoint_job is None
         and pipeline.jobs.empty() and pipeline.results.empty())


def retained_pcm_sizes(pipeline):
    """Inspect each retained collector/snapshot, including queued work."""
    sizes = [len(pipeline.command_stream.collector.audio),
             len(pipeline.command_stream.pending)]
    if pipeline._endpoint_candidate is not None:
        sizes.append(len(pipeline._endpoint_candidate[1].pcm))
    if pipeline._final_input is not None:
        payload = pipeline._final_input
        sizes.append(len(payload.pcm if hasattr(payload, 'pcm') else payload))
    with pipeline.jobs.mutex:
        for _, _, _, payload in pipeline.jobs.queue:
            sizes.append(len(payload.pcm if hasattr(payload, 'pcm') else payload))
    return sizes


@pytest.fixture
def rolling():
    created = []

    def create(max_buffer_s=60.0):
        state = SimpleNamespace(now=0.0, final=[], partial=[], reports=[],
                                model=PcmTokenModel())
        transcriber = LocalWhisperTranscriber.__new__(LocalWhisperTranscriber)
        transcriber.model = state.model
        pipeline = DialoguePipeline(
            recorder_factory=lambda: None, wake=None, transcriber=transcriber,
            is_speech=lambda pcm, _: any(pcm), input_has_aec=True,
            publish_transcript=lambda *args: state.final.append(args),
            publish_control=lambda *_: None, publish_interruption=lambda *_: None,
            report=state.reports.append,
            on_partial=lambda *args: state.partial.append(args),
            clock=lambda: state.now,
            settings=CaptureSettings(max_utterance_s=None, max_buffer_s=max_buffer_s,
                                     silence_timeout_s=2.0),
            endpoint_predecode_s=0.8,
        )
        pipeline.session.activate()
        pipeline.asr_thread = Thread(target=pipeline._infer, daemon=True)
        pipeline.asr_thread.start()
        state.pipeline = pipeline
        created.append(state)
        return state

    yield create
    for state in created:
        state.model.release.set()
        state.pipeline.close()


def test_ten_minutes_keep_all_pcm_tokens_and_publish_once_after_natural_silence(rolling):
    state = rolling()
    pipeline = state.pipeline
    peak_pcm = 0
    utterance_id = None
    for value in range(1, 601):
        state.now += 1.0
        pipeline.feed(token_pcm(value))
        peak_pcm = max(peak_pcm, *retained_pcm_sizes(pipeline))
        settle(pipeline)
        if utterance_id is None:
            utterance_id = pipeline.session.utterance_id
        assert pipeline.session.utterance_id == utterance_id
        assert state.final == [] and pipeline.session.active
    assert state.partial and all(uid == utterance_id for uid, _ in state.partial)
    assert pipeline.command_stream.collector.audio_start_s > 500
    assert peak_pcm <= 60 * 32000
    # The last token has no sentence-final ending, so the two-second natural
    # silence boundary applies even if an early endpoint preview completes.
    pipeline.feed(QUIET * 99)
    settle(pipeline)
    assert state.final == []
    pipeline.feed(QUIET)
    pump(pipeline, lambda: bool(state.final))
    expected = ' '.join(token_text(value) for value in range(1, 601))
    assert state.final == [(utterance_id, expected)]
    assert state.final[0][1].split().count('다시') == len(REPEATED)
    assert max(samples for samples, _ in state.model.calls) <= 60 * 16000
    assert not any('too_long' in event or 'buffer_overflow' in event
                   for event in state.reports)
    for _ in range(3):
        pipeline.poll()
    assert state.final == [(utterance_id, expected)]


def test_late_old_stream_watermark_cannot_trim_the_next_utterance(rolling):
    state = rolling()
    pipeline = state.pipeline
    for value in range(1, 25):
        pipeline.feed(token_pcm(value))
        settle(pipeline)
    old_stream = pipeline._stream
    old_uid = pipeline.session.utterance_id
    assert old_stream.retained_start_s > 0
    state.model.hold_next()
    pipeline.feed(token_pcm(25) + token_pcm(26))
    assert state.model.entered.wait(2)
    pipeline._terminate('reset_for_test')
    pipeline.session.activate()
    fresh = token_pcm(9001)
    pipeline.feed(fresh)
    new_uid = pipeline.session.utterance_id
    assert new_uid != old_uid
    assert pipeline._stream is not old_stream
    state.model.release.set()
    settle(pipeline)
    assert pipeline.command_stream.collector.audio_start_s == 0
    assert bytes(pipeline.command_stream.collector.audio) == fresh
    assert state.final == []
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: bool(state.final))
    assert state.final == [(new_uid, token_text(9001))]


def test_slow_preview_coalesces_pending_work_without_losing_rolling_audio(rolling):
    state = rolling()
    pipeline = state.pipeline
    for value in range(1, 25):
        pipeline.feed(token_pcm(value))
        settle(pipeline)
    assert pipeline._stream.retained_start_s > 0
    prior_calls = len(state.model.calls)
    state.model.hold_next()
    pipeline.feed(token_pcm(25) + token_pcm(26))
    assert state.model.entered.wait(2)
    for value in range(27, 36):
        pipeline.feed(token_pcm(value))
        pipeline.poll()
        assert pipeline.jobs.empty()
        assert len(state.model.calls) == prior_calls + 1
        assert state.final == []
    state.model.release.set()
    settle(pipeline)
    assert len(state.model.calls) == prior_calls + 2
    pipeline.feed(QUIET * 100)
    pump(pipeline, lambda: bool(state.final))
    assert [text for _, text in state.final] == [
        ' '.join(token_text(value) for value in range(1, 36)),
    ]
    assert max(samples for samples, _ in state.model.calls) <= 60 * 16000


def test_late_preview_cannot_hide_weak_tail_in_a_rolling_final(rolling):
    state = rolling()
    pipeline = state.pipeline
    for value in range(1, 25):
        pipeline.feed(token_pcm(value))
        settle(pipeline)
    assert pipeline.command_stream.collector.audio_start_s > 0
    weak = token_pcm(9002)[:640]
    is_speech = lambda pcm, _: any(pcm) and pcm[:2] != weak[:2]
    pipeline.command_stream.is_speech = is_speech
    pipeline.command_stream.collector.is_speech = is_speech
    state.model.hold_next()
    pipeline.feed(token_pcm(25) + token_pcm(26))
    assert state.model.entered.wait(2)
    uid = pipeline.session.utterance_id
    revision = pipeline.command_stream.collector.revision
    pipeline.feed(weak)
    assert pipeline.command_stream.collector.revision == revision
    pipeline.feed(QUIET * 100)
    assert state.final == [] and pipeline._busy
    state.model.release.set()
    pump(pipeline, lambda: bool(state.final))
    expected = ' '.join([*(token_text(value) for value in range(1, 27)),
                         token_text(9002)])
    assert state.final == [(uid, expected)]
    settle(pipeline)
    assert state.final == [(uid, expected)]


def test_stalled_decoder_overflow_never_sends_a_partial_command_and_recovers(rolling):
    state = rolling(max_buffer_s=8.0)
    pipeline = state.pipeline
    state.model.hold_next()
    pipeline.feed(token_pcm(1) + token_pcm(2))
    assert state.model.entered.wait(2)
    peak_pcm = 0
    for value in range(3, 21):
        pipeline.feed(token_pcm(value))
        peak_pcm = max(peak_pcm, *retained_pcm_sizes(pipeline))
        pipeline.poll()
        assert pipeline.jobs.qsize() <= 1
        assert state.final == []
    assert peak_pcm <= 8 * 32000
    assert 'utterance_discarded:buffer_overflow' in state.reports
    assert not pipeline.session.active and pipeline._tail_stream.discarding
    state.model.release.set()
    settle(pipeline)
    assert state.final == []
    pipeline.feed(QUIET * 100)
    assert pipeline._tail_stream is None
    pipeline.session.activate()
    pipeline.feed(token_pcm(9001) + QUIET * 100)
    pump(pipeline, lambda: bool(state.final))
    assert [text for _, text in state.final] == [token_text(9001)]
