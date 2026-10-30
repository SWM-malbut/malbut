"""Suppression follows the completed model window, including its last 20 ms tick."""
from malbut_stt.echo.vad import SuppressedSilenceVad
from malbut_stt.silero_stream import SileroStreamVad


class LingeringModel:
    def reset(self):
        pass

    def probability(self, pcm):
        assert len(pcm) == 1024
        return .9


def test_signal_window_survives_tick_without_new_inference_but_zero_window_does_not():
    stream = SileroStreamVad(model=LingeringModel())
    vad = SuppressedSilenceVad(stream)
    voice, quiet = b'\x01\0' * 320, bytes(640)
    assert not vad.is_speech(voice, 16000)
    assert vad.is_speech(voice, 16000)
    # 128 pending samples + this 320-sample tick cannot complete a new window.
    assert vad.is_speech(quiet, 16000)
    assert vad.is_speech(quiet, 16000)  # Mixed signal/silence window.
    assert not vad.is_speech(quiet, 16000)  # Entirely zero model window.
    for _ in range(30):
        assert not vad.is_speech(quiet, 16000)
    assert stream.probability == 0


def test_reset_discards_signal_window_support():
    stream = SileroStreamVad(model=LingeringModel())
    vad = SuppressedSilenceVad(stream)
    assert vad.is_speech(b'\x01\0' * 640, 16000)
    vad.reset()
    assert not vad.is_speech(bytes(640), 16000)
