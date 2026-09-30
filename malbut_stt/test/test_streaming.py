"""Verify continuous utterance boundaries without opening a microphone."""

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.streaming import StreamingUtteranceCollector


QUIET = bytes(640)
VOICE = b'\x01\x00' * 320
SECOND = b'\x02\x00' * 320


def collector(**kwargs):
    return StreamingUtteranceCollector(lambda frame, _: any(frame), **kwargs)


def test_utterance_finishes_after_two_seconds_of_silence():
    stream = collector()
    assert [event.status for event in stream.feed(VOICE * 4)] == ['speech_started']
    assert stream.feed(QUIET * 99) == []
    completed, = stream.feed(QUIET)
    assert completed.status == 'complete'
    assert completed.pcm == VOICE * 4 + QUIET * 100


def test_back_to_back_utterances_in_one_read_preserve_both_first_words():
    stream = collector()
    events = stream.feed(VOICE * 4 + QUIET * 100 + SECOND * 4 + QUIET * 100)
    assert [event.status for event in events] == [
        'speech_started', 'complete', 'speech_started', 'complete',
    ]
    assert events[1].pcm == VOICE * 4 + QUIET * 100
    assert events[3].pcm == SECOND * 4 + QUIET * 100


def test_device_chunk_boundaries_do_not_change_recorded_audio():
    stream = collector()
    pcm = QUIET * 7 + VOICE * 4 + QUIET * 100
    events = []
    for offset in range(0, len(pcm), 1024):
        events.extend(stream.feed(pcm[offset:offset + 1024]))
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[1].pcm == pcm
    assert not stream.pending


def test_a_pause_shorter_than_two_seconds_keeps_one_utterance():
    stream = collector()
    events = stream.feed(VOICE * 4 + QUIET * 99 + SECOND + QUIET * 100)
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[1].pcm == VOICE * 4 + QUIET * 99 + SECOND + QUIET * 100


def test_idle_silence_does_not_create_a_command_or_grow_the_pending_buffer():
    stream = collector()
    for _ in range(10):
        assert stream.feed(QUIET * 250) == []
        assert not stream.pending
        assert not stream.collector.started


def test_overlong_tail_is_not_transcribed_as_a_separate_command():
    settings = CaptureSettings(
        start_timeout_s=1, silence_timeout_s=0.1, max_utterance_s=0.2,
        # One-frame qualification isolates this test's overlong-tail boundary.
        pre_roll_s=0.02, min_speech_s=0.02,
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
    stream.feed(VOICE * 3 + SECOND[:20])
    stream.reset()
    events = stream.feed(SECOND * 4 + QUIET * 100)
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[1].pcm == SECOND * 4 + QUIET * 100


def test_invalid_pcm_does_not_mutate_the_stream():
    stream = collector()
    with pytest.raises(ValueError, match='whole samples'):
        stream.feed(b'\x01')
    assert not stream.pending


@pytest.mark.parametrize('positive_frames', [1, 2, 3])
def test_short_noise_bursts_never_emit_onset_preview_or_completion(positive_frames):
    stream = collector(early_endpoint_s=0.8, partial_interval_s=0.04)
    for _ in range(20):
        assert stream.feed(VOICE * positive_frames + QUIET * 100) == []
    assert not stream.collector.started
    assert not stream.collector.candidate_audio
    assert not stream.pending


def test_repeated_clicks_cannot_accumulate_voice_evidence_or_unbounded_idle_audio():
    stream = collector()
    for _ in range(1000):
        assert stream.feed(VOICE + QUIET) == []
        assert not stream.collector.started and not stream.collector.candidate_audio
        assert len(stream.collector.pre_roll) <= 15
        assert not stream.collector.audio and not stream.pending


@pytest.mark.parametrize('silence_s', [0.4, 2.0])
def test_short_qualified_answer_works_with_wake_and_confirmation_endpoints(silence_s):
    stream = collector(settings=CaptureSettings(silence_timeout_s=silence_s))
    assert stream.feed(VOICE * 3) == []
    assert [event.status for event in stream.feed(VOICE)] == ['speech_started']
    quiet_frames = round(silence_s / 0.02)
    completed, = stream.feed(QUIET * quiet_frames)
    assert completed.status == 'complete'
    assert completed.pcm == VOICE * 4 + QUIET * quiet_frames


def test_false_spike_then_real_onset_preserves_audio_across_irregular_chunks():
    stream = collector()
    pcm = VOICE + QUIET * 2 + SECOND * 4 + QUIET * 100
    events = []
    sizes = [2, 638, 1024, 78, 1280, 6]
    offset = 0
    while offset < len(pcm):
        size = sizes[(offset // 2) % len(sizes)]
        events.extend(stream.feed(pcm[offset:offset + size]))
        offset += size
    assert [event.status for event in events] == ['speech_started', 'complete']
    assert events[1].pcm == pcm and not stream.pending


@pytest.mark.parametrize('blocked_frame', range(4))
def test_any_blocked_frame_during_qualification_marks_the_whole_onset(blocked_frame):
    stream = collector()
    events = []
    for frame in range(4):
        events.extend(stream.feed(VOICE, start_blocked=frame == blocked_frame))
    assert len(events) == 1 and events[0].status == 'speech_started'
    assert events[0].start_blocked is True
    completed, = stream.feed(QUIET * 100)
    assert completed.start_blocked is True


@pytest.mark.parametrize('prefix_bytes', [2, 64, 638])
def test_blocked_partial_frame_cannot_become_a_clean_onset(prefix_bytes):
    stream = collector()
    assert stream.feed(VOICE[:prefix_bytes], start_blocked=True) == []
    started, = stream.feed(VOICE[prefix_bytes:] + VOICE * 3)
    assert started.status == 'speech_started' and started.start_blocked is True


def test_blocked_quiet_prefix_does_not_contaminate_clean_onset_in_same_chunk():
    stream = collector()
    assert stream.feed(QUIET[:320], start_blocked=True) == []
    started, = stream.feed(QUIET[320:] + VOICE * 4)
    assert started.status == 'speech_started' and started.start_blocked is False


def test_quiet_resets_blocked_candidate_before_fresh_onset_in_same_chunk():
    stream = collector()
    assert stream.feed(VOICE * 3, start_blocked=True) == []
    started, = stream.feed(QUIET + SECOND * 4)
    assert started.status == 'speech_started' and started.start_blocked is False


def test_blocked_first_utterance_does_not_contaminate_next_utterance():
    stream = collector()
    assert stream.feed(VOICE * 3, start_blocked=True) == []
    events = stream.feed(VOICE + QUIET * 100 + SECOND * 4)
    assert [(event.status, event.start_blocked) for event in events] == [
        ('speech_started', True), ('complete', True), ('speech_started', False),
    ]


def test_reset_clears_blocked_candidate_and_partial_frame():
    stream = collector()
    assert stream.feed(VOICE + VOICE[:320], start_blocked=True) == []
    stream.reset()
    started, = stream.feed(SECOND * 4)
    assert started.start_blocked is False


def test_empty_chunk_does_not_mark_pending_samples_as_blocked():
    stream = collector()
    assert stream.feed(VOICE[:320]) == []
    assert stream.feed(b'', start_blocked=True) == []
    started, = stream.feed(VOICE[320:] + VOICE * 3)
    assert started.start_blocked is False


def test_blocked_audio_after_qualification_does_not_change_onset_eligibility():
    stream = collector()
    started, = stream.feed(VOICE * 4)
    completed, = stream.feed(VOICE + QUIET * 100, start_blocked=True)
    assert started.start_blocked is False and completed.start_blocked is False
