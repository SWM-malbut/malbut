"""Preserve one uncertain VAD frame without admitting isolated noise bursts."""

import pytest

from malbut_stt.audio import CaptureSettings, UtteranceCollector
from malbut_stt.streaming import StreamingUtteranceCollector


QUIET = bytes(640)
VOICE = b'\x01\x00' * 320


@pytest.mark.parametrize('position', [1, 2, 3])
def test_one_vad_dropout_preserves_short_onset_across_recorder_chunks(position):
    stream = StreamingUtteranceCollector(
        lambda frame, _: frame == VOICE,
        settings=CaptureSettings(silence_timeout_s=0.4),
    )
    onset = VOICE * position + QUIET + VOICE * (4 - position)
    pcm = QUIET * 20 + onset + QUIET * 20
    events = []
    for offset in range(0, len(pcm), 1024):
        events.extend(stream.feed(pcm[offset:offset + 1024]))
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[-1].pcm == QUIET * 14 + onset + QUIET * 20


def test_second_vad_dropout_requires_fresh_evidence():
    stream = StreamingUtteranceCollector(lambda frame, _: frame == VOICE)
    assert stream.feed(VOICE * 2 + QUIET + VOICE + QUIET) == []
    assert stream.feed(VOICE * 3) == []
    assert [event.status for event in stream.feed(VOICE)] == ['speech_started']


@pytest.mark.parametrize('burst', [VOICE, VOICE * 2, VOICE * 3, VOICE + QUIET])
def test_short_bursts_and_repeated_clicks_do_not_accumulate_into_speech(burst):
    stream = StreamingUtteranceCollector(lambda frame, _: frame == VOICE)
    for _ in range(100):
        assert stream.feed(burst + QUIET * 2) == []
        assert not stream.collector.started
        assert len(stream.collector.pre_roll) <= 15
    assert stream.feed((VOICE + QUIET) * 100) == []
    assert not stream.collector.started


@pytest.mark.parametrize('blocked_gap', [False, True])
def test_busy_evidence_is_reset_before_a_fresh_unblocked_onset(blocked_gap):
    stream = StreamingUtteranceCollector(lambda frame, _: frame == VOICE)
    assert stream.feed(VOICE * 3, start_blocked=not blocked_gap) == []
    assert stream.feed(QUIET, start_blocked=blocked_gap) == []
    assert stream.feed(VOICE * 3) == []
    started, = stream.feed(VOICE)
    assert started.status == 'speech_started' and not started.start_blocked


def test_busy_voiced_frame_after_a_dropout_still_blocks_the_whole_onset():
    stream = StreamingUtteranceCollector(lambda frame, _: frame == VOICE)
    assert stream.feed(VOICE * 2 + QUIET) == []
    assert stream.feed(VOICE, start_blocked=True) == []
    started, = stream.feed(VOICE)
    assert started.status == 'speech_started' and started.start_blocked


def test_accepted_dropout_counts_toward_the_utterance_duration_limit():
    collector = UtteranceCollector(
        16000, lambda frame, _: frame == VOICE,
        CaptureSettings(silence_timeout_s=0.02, max_utterance_s=0.08),
    )
    result = collector.feed(VOICE * 2 + QUIET + VOICE * 2)
    assert result is not None and result.status == 'too_long'
    assert result.pcm == b''
