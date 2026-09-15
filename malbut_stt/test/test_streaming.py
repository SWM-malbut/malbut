"""Verify continuous utterance boundaries without opening a microphone."""

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.streaming import StreamingUtteranceCollector


QUIET = bytes(640)
VOICE = b'\x01\x00' * 320
SECOND = b'\x02\x00' * 320


def collector(**kwargs):
    return StreamingUtteranceCollector(lambda frame, _: any(frame), **kwargs)


def test_utterance_finishes_after_three_seconds_of_silence():
    stream = collector()
    assert [event.status for event in stream.feed(VOICE)] == ['speech_started']
    assert stream.feed(QUIET * 149) == []
    completed, = stream.feed(QUIET)
    assert completed.status == 'complete'
    assert completed.pcm == VOICE + QUIET * 150


def test_back_to_back_utterances_in_one_read_preserve_both_first_words():
    stream = collector()
    events = stream.feed(VOICE + QUIET * 150 + SECOND + QUIET * 150)
    assert [event.status for event in events] == [
        'speech_started', 'complete', 'speech_started', 'complete',
    ]
    assert events[1].pcm == VOICE + QUIET * 150
    assert events[3].pcm == SECOND + QUIET * 150


def test_device_chunk_boundaries_do_not_change_recorded_audio():
    stream = collector()
    pcm = QUIET * 7 + VOICE * 2 + QUIET * 150
    events = []
    for offset in range(0, len(pcm), 1024):
        events.extend(stream.feed(pcm[offset:offset + 1024]))
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[1].pcm == pcm
    assert not stream.pending


def test_a_pause_shorter_than_three_seconds_keeps_one_utterance():
    stream = collector()
    events = stream.feed(VOICE + QUIET * 100 + SECOND + QUIET * 150)
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[1].pcm == VOICE + QUIET * 100 + SECOND + QUIET * 150


def test_idle_silence_does_not_create_a_command_or_grow_the_pending_buffer():
    stream = collector()
    for _ in range(10):
        assert stream.feed(QUIET * 250) == []
        assert not stream.pending
        assert not stream.collector.started


def test_overlong_tail_is_not_transcribed_as_a_separate_command():
    settings = CaptureSettings(
        start_timeout_s=1, silence_timeout_s=0.1, max_utterance_s=0.2,
        pre_roll_s=0.02,
    )
    stream = collector(settings=settings)
    events = stream.feed(VOICE * 30 + QUIET * 5 + SECOND + QUIET * 5)
    assert [event.status for event in events] == [
        'speech_started', 'too_long', 'speech_started', 'complete',
    ]
    assert events[1].pcm == b''
    assert events[3].pcm == SECOND + QUIET * 5


def test_reset_removes_pre_disconnect_audio_and_partial_frame():
    stream = collector()
    stream.feed(VOICE + SECOND[:20])
    stream.reset()
    events = stream.feed(SECOND + QUIET * 150)
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[1].pcm == SECOND + QUIET * 150


def test_invalid_pcm_does_not_mutate_the_stream():
    stream = collector()
    with pytest.raises(ValueError, match='whole samples'):
        stream.feed(b'\x01')
    assert not stream.pending
