"""Exercise speech boundaries with sample counts instead of wall-clock waits."""

from array import array

import pytest

from malbut_stt.audio import CaptureSettings, UtteranceCollector


FRAME = bytes(640)


def test_recorder_chunks_are_reframed_without_losing_samples():
    """512-sample recorder chunks must reach VAD as 320-sample frames."""
    seen = []
    collector = UtteranceCollector(
        16000, lambda frame, rate: seen.append((frame, rate)) or False,
        CaptureSettings(),
    )
    source = array('h', range(1536)).tobytes()
    for offset in range(0, len(source), 1024):
        assert collector.feed(source[offset:offset + 1024]) is None
    assert len(seen) == 4
    assert all(len(frame) == 640 and rate == 16000 for frame, rate in seen)
    assert b''.join(frame for frame, _ in seen) + collector.pending == source


def test_no_speech_waits_five_seconds_then_discards():
    """Silence after waking must never be transcribed."""
    collector = UtteranceCollector(16000, lambda *_: False, CaptureSettings())
    assert collector.feed(FRAME * 249) is None
    result = collector.feed(FRAME)
    assert result.status == 'no_speech'
    assert result.pcm == b''


def test_preroll_preserves_onset_and_one_second_silence_finalizes():
    """Include post-wake pre-roll exactly once, without trimming the onset."""
    voice = b'\x01\x00' * 320
    collector = UtteranceCollector(
        16000, lambda frame, _: frame == voice, CaptureSettings(),
    )
    assert collector.feed(FRAME * 30 + voice * 4 + FRAME * 49) is None
    result = collector.feed(FRAME)
    assert result.status == 'complete'
    assert result.pcm == FRAME * 14 + voice * 4 + FRAME * 50
    with pytest.raises(RuntimeError):
        collector.feed(FRAME)


def test_renewed_speech_resets_silence_timeout():
    """Pausing for less than the timeout must preserve the same utterance."""
    voice = b'\x01\x00' * 320
    collector = UtteranceCollector(
        16000, lambda frame, _: frame == voice, CaptureSettings(),
    )
    assert collector.feed(voice * 4 + FRAME * 49 + voice + FRAME * 49) is None
    assert collector.feed(FRAME).status == 'complete'


def test_overlong_utterance_is_discarded_instead_of_truncated():
    """An unfinished recording crossing 20 seconds must not become a command."""
    collector = UtteranceCollector(16000, lambda *_: True, CaptureSettings())
    assert collector.feed(FRAME * 1000) is None
    result = collector.feed(FRAME)
    assert result.status == 'too_long'
    assert result.pcm == b''
    assert not collector.audio


@pytest.mark.parametrize('limit', [0, -1, True, float('inf'), float('nan')])
def test_invalid_explicit_utterance_limit_is_rejected(limit):
    with pytest.raises(ValueError, match='finite and positive'):
        CaptureSettings(max_utterance_s=limit)


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf'), True])
def test_bad_timing_settings_are_rejected(value):
    """Invalid settings must fail before any device starts."""
    with pytest.raises(ValueError):
        CaptureSettings(start_timeout_s=value)


def test_incompatible_durations_and_sample_format_are_rejected():
    """Keep the actual VAD and capture duration requirements enforceable."""
    with pytest.raises(ValueError):
        CaptureSettings(silence_timeout_s=20.0)
    with pytest.raises(ValueError):
        CaptureSettings(pre_roll_s=6.0)
    with pytest.raises(ValueError):
        UtteranceCollector(44100, lambda *_: True, CaptureSettings())
    collector = UtteranceCollector(16000, lambda *_: True, CaptureSettings())
    with pytest.raises(ValueError):
        collector.feed(b'\x00')


@pytest.mark.parametrize('value', [0, -1, True, float('nan'), float('inf'), None, '0.08'])
def test_invalid_minimum_speech_duration_is_rejected(value):
    with pytest.raises(ValueError, match='finite and positive'):
        CaptureSettings(min_speech_s=value)


def test_qualification_must_fit_in_preroll():
    with pytest.raises(ValueError, match='minimum speech.*pre-roll'):
        CaptureSettings(min_speech_s=0.32)


@pytest.mark.parametrize('sample_rate', [8000, 16000, 32000, 48000])
def test_default_qualification_is_four_frames_at_every_supported_rate(sample_rate):
    voice = b'\x01\x00' * (sample_rate // 50)
    collector = UtteranceCollector(sample_rate, lambda *_: True, CaptureSettings())
    assert collector.feed(voice * 3) is None
    assert not collector.started and not collector.audio
    assert collector.feed(voice) is None
    assert collector.started and collector.speech_frames == 4
    assert collector.audio == voice * 4


def test_fractional_frame_minimum_rounds_up_without_losing_onset():
    collector = UtteranceCollector(
        16000, lambda *_: True, CaptureSettings(min_speech_s=0.05),
    )
    collector.feed(FRAME * 2)
    assert not collector.started
    collector.feed(FRAME)
    assert collector.started and collector.audio == FRAME * 3


def test_onset_crossing_idle_window_can_still_qualify():
    voice = b'\x01\x00' * 320
    collector = UtteranceCollector(
        16000, lambda frame, _: any(frame),
        CaptureSettings(start_timeout_s=0.08, pre_roll_s=0.08),
    )
    assert collector.feed(FRAME * 3 + voice * 3) is None
    assert not collector.started
    assert collector.feed(voice) is None
    assert collector.started and collector.audio == FRAME * 3 + voice * 4


@pytest.mark.parametrize('max_duration, expected_frames', [(0.1, 6), (0.06, 4)])
def test_qualification_counts_from_first_voiced_frame_for_duration_limit(
    max_duration, expected_frames,
):
    collector = UtteranceCollector(
        16000, lambda *_: True,
        CaptureSettings(silence_timeout_s=0.02, max_utterance_s=max_duration),
    )
    assert collector.feed(FRAME * (expected_frames - 1)) is None
    assert collector.feed(FRAME).status == 'too_long'


def test_qualification_enforces_pcm_budget_with_preserved_preroll():
    voice = b'\x01\x00' * 320
    collector = UtteranceCollector(
        16000, lambda frame, _: any(frame),
        CaptureSettings(silence_timeout_s=0.02, pre_roll_s=0.08, max_buffer_s=0.1),
    )
    assert collector.feed(FRAME * 4 + voice * 3) is None
    assert collector.feed(voice).status == 'buffer_overflow'
    assert not collector.audio and not collector.candidate_audio


def test_collector_preserves_blocked_eligibility_across_partial_input_frame():
    voice = b'\x01\x00' * 320
    collector = UtteranceCollector(16000, lambda frame, _: any(frame), CaptureSettings())
    assert collector.feed(voice[:2], start_blocked=True) is None
    assert collector.feed(voice[2:] + voice * 3) is None
    assert collector.started and collector.start_blocked is True
    assert collector.feed(FRAME * 50).start_blocked is True


def test_collector_quiet_frame_clears_blocked_candidate_before_next_onset():
    voice = b'\x01\x00' * 320
    collector = UtteranceCollector(16000, lambda frame, _: any(frame), CaptureSettings())
    assert collector.feed(voice + FRAME[:320], start_blocked=True) is None
    assert collector.feed(FRAME[320:] + voice * 4) is None
    assert collector.started and collector.start_blocked is False


def test_preroll_cannot_exceed_pcm_budget_before_the_first_speech_frame():
    with pytest.raises(ValueError, match='pre-roll.*audio buffer'):
        CaptureSettings(start_timeout_s=5.0, pre_roll_s=4.0, max_buffer_s=3.0)


def test_real_webrtcvad_silence_when_runtime_library_is_installed():
    """Exercise the real VAD binding without a microphone or credentials."""
    webrtcvad = pytest.importorskip('webrtcvad')
    collector = UtteranceCollector(
        16000, webrtcvad.Vad(2).is_speech, CaptureSettings(),
    )
    assert collector.feed(FRAME * 250).status == 'no_speech'


def test_rolling_pcm_release_keeps_absolute_offsets_and_natural_endpoint():
    voice = b'\x01\x00' * 320
    settings = CaptureSettings(max_utterance_s=None, max_buffer_s=5.0)
    collector = UtteranceCollector(16000, lambda frame, _: any(frame), settings)
    for block in range(30):
        assert collector.feed(voice * 100) is None  # Two seconds of new speech.
        snapshot = collector.snapshot('partial_check')
        assert snapshot.audio_start_s == max(0.0, block * 2 - 1)
        assert len(snapshot.pcm) <= 3 * 32000
        collector.discard_before(block * 2 + 1)  # Keep one second of overlap.
    result = collector.feed(FRAME * 50)
    assert result.status == 'complete'
    assert result.audio_start_s == 59.0
    assert result.pcm == voice * 50 + FRAME * 50


def test_unprocessed_audio_overflow_is_distinct_from_total_utterance_duration():
    collector = UtteranceCollector(
        16000, lambda *_: True,
        CaptureSettings(max_utterance_s=None, max_buffer_s=3.0),
    )
    assert collector.feed(FRAME * 150) is None
    result = collector.feed(FRAME)
    assert result.status == 'buffer_overflow' and result.pcm == b''
    assert not collector.audio


@pytest.mark.parametrize('boundary', [-1, float('nan'), float('inf'), 2.0])
def test_bad_audio_release_boundary_does_not_destroy_retained_pcm(boundary):
    collector = UtteranceCollector(16000, lambda *_: True, CaptureSettings())
    collector.feed(FRAME * 50)
    with pytest.raises(ValueError):
        collector.discard_before(boundary)
    assert len(collector.audio) == 32000
