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
    assert collector.feed(FRAME * 30 + voice * 2 + FRAME * 49) is None
    result = collector.feed(FRAME)
    assert result.status == 'complete'
    assert result.pcm == FRAME * 14 + voice * 2 + FRAME * 50
    with pytest.raises(RuntimeError):
        collector.feed(FRAME)


def test_renewed_speech_resets_silence_timeout():
    """Pausing for less than the timeout must preserve the same utterance."""
    voice = b'\x01\x00' * 320
    collector = UtteranceCollector(
        16000, lambda frame, _: frame == voice, CaptureSettings(),
    )
    assert collector.feed(voice + FRAME * 49 + voice + FRAME * 49) is None
    assert collector.feed(FRAME).status == 'complete'


def test_overlong_utterance_is_discarded_instead_of_truncated():
    """An unfinished recording crossing 20 seconds must not become a command."""
    collector = UtteranceCollector(16000, lambda *_: True, CaptureSettings())
    assert collector.feed(FRAME * 1000) is None
    result = collector.feed(FRAME)
    assert result.status == 'too_long'
    assert result.pcm == b''
    assert not collector.audio


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


def test_real_webrtcvad_silence_when_runtime_library_is_installed():
    """Exercise the real VAD binding without a microphone or credentials."""
    webrtcvad = pytest.importorskip('webrtcvad')
    collector = UtteranceCollector(
        16000, webrtcvad.Vad(2).is_speech, CaptureSettings(),
    )
    assert collector.feed(FRAME * 250).status == 'no_speech'
