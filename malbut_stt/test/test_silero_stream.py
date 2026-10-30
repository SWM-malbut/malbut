"""State, causality and endpoint timing for the single capture VAD."""
from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.silero_stream import SileroStreamVad
from malbut_stt.streaming import StreamingUtteranceCollector

VOICE = b'\x01\0' * 320
QUIET = bytes(640)


class Model:
    def __init__(self):
        self.frames = []
        self.resets = 0

    def reset(self):
        self.resets += 1

    def probability(self, pcm):
        assert len(pcm) == 1024
        self.frames.append(pcm)
        return .9 if any(pcm) else .1


def test_20ms_input_only_runs_complete_32ms_windows():
    model = Model()
    vad = SileroStreamVad(model=model)
    assert not vad.is_speech(VOICE, 16000)
    assert model.frames == []
    assert vad.is_speech(VOICE, 16000)
    assert model.frames == [VOICE + VOICE[:384]]
    assert len(vad.pending) == 256
    assert vad.is_speech(VOICE, 16000)
    assert len(model.frames) == 1
    assert len(vad.pending) < 1024


def test_silence_has_no_400ms_hold():
    vad = SileroStreamVad(model=Model())
    vad.is_speech(VOICE*8, 16000)
    assert not vad.is_speech(QUIET*2, 16000)


def test_onset_confirmation_is_applied_once_and_keeps_pcm():
    vad = SileroStreamVad(model=Model())
    stream = StreamingUtteranceCollector(vad.is_speech,
        settings=CaptureSettings(min_speech_s=.16, silence_timeout_s=2.))
    assert stream.feed(VOICE*8) == []  # First 20ms has no complete model window.
    event, = stream.feed(VOICE)
    assert event.status == 'speech_started'
    assert bytes(stream.collector.audio) == VOICE*9
    events = stream.feed(QUIET*103)
    complete, = [e for e in events if e.status == 'complete']
    assert complete.silence_s == 2.


def test_capture_reset_discards_recurrent_state_and_pending_samples():
    model = Model()
    vad = SileroStreamVad(model=model)
    stream = StreamingUtteranceCollector(vad.is_speech)
    stream.feed(VOICE*9)
    resets = model.resets
    stream.reset()
    assert model.resets > resets and vad.pending == b''
    assert not vad.is_speech(QUIET, 16000)


def test_wake_and_command_use_independent_state():
    command = SileroStreamVad(model=Model())
    wake = SileroStreamVad(model=Model())
    p = DialoguePipeline(recorder_factory=lambda: None, wake=None,
        transcriber=SimpleNamespace(transcribe=lambda *_: ''),
        is_speech=command.is_speech, wake_is_speech=wake.is_speech,
        publish_transcript=lambda *_: None, publish_control=lambda *_: None,
        publish_interruption=lambda *_: None, report=lambda *_: None)
    p.wake_stream.feed(VOICE*9)
    assert command.pending == b'' and command.probability == 0.
    assert wake.probability == .9
    p._reset_audio()
    assert command.probability == wake.probability == 0.


def test_idle_resets_model_without_unbounded_buffer():
    model = Model()
    vad = SileroStreamVad(model=model)
    resets = model.resets
    for _ in range(100):
        assert not vad.is_speech(QUIET, 16000)
        assert len(vad.pending) < 1024
    assert model.resets > resets


@pytest.mark.parametrize('pcm,rate', [(b'',16000),(b'\0',16000),(VOICE,8000)])
def test_invalid_input_fails_before_inference(pcm, rate):
    model = Model()
    vad = SileroStreamVad(model=model)
    with pytest.raises(ValueError):
        vad.is_speech(pcm,rate)
    assert model.frames == []


def test_capture_vad_survives_incident_wrapper_reset(tmp_path):
    from malbut_stt.incident import IncidentRecorder
    recorder = IncidentRecorder(tmp_path, save_audio=False)
    try:
        vad = SileroStreamVad(model=Model())
        wrapped = recorder.wrap_vad(vad.is_speech, kind='command')
        stream = StreamingUtteranceCollector(wrapped)
        stream.feed(VOICE*9)
        stream.reset()
        assert vad.probability == 0. and not vad.pending
    finally:
        recorder.close()
