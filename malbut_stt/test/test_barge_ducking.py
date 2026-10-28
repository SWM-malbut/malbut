"""Compare onset candidates against unchanged capture timing and controls."""
from types import SimpleNamespace
import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline

VOICE, QUIET = b'\x01\x00'*320, bytes(640)


def trial(enabled, frames, *, aec=True):
    controls, started = [], []
    now = [0.]
    p = DialoguePipeline(recorder_factory=lambda: None, wake=None,
        transcriber=SimpleNamespace(transcribe=lambda *_: '알겠어요.'),
        is_speech=lambda pcm, _: any(pcm), publish_transcript=lambda *_: None,
        publish_control=lambda pid, command: controls.append((round(now[0], 3), command)),
        publish_interruption=lambda *_: None, report=lambda *_: None,
        publish_input_status=lambda _, uid, state: started.append((round(now[0], 3), state)),
        settings=CaptureSettings(min_speech_s=.16, silence_timeout_s=2.),
        input_has_aec=aec, barge_in_ducking=enabled, clock=lambda: now[0])
    p.session.activate()
    p.on_playback_status('answer', 'playing')
    for frame in frames:
        now[0] += .02
        p.feed(frame)
    return p, controls, started


def test_false_candidate_only_ducks_and_restores_without_pausing():
    baseline, old, _ = trial(False, [VOICE]*3+[QUIET])
    p, new, started = trial(True, [VOICE]*3+[QUIET])
    assert old == [] and started == []
    assert new == [(.02, 'duck'), (.08, 'unduck')]
    assert not p.command_stream.collector.started


def test_confirmed_start_keeps_existing_160_ms_and_pause():
    _, old, old_started = trial(False, [VOICE]*8)
    _, new, started = trial(True, [VOICE]*8)
    assert old_started == started == [(.16, 'started')]
    assert old == [(.16, 'pause')]
    assert new == [(.02, 'duck'), (.16, 'pause')]


def test_requires_aec_and_resets_on_audio_discontinuity():
    _, controls, _ = trial(True, [VOICE]*4, aec=False)
    assert controls == []
    p, controls, _ = trial(True, [VOICE]*4)
    p._reset_audio()
    assert [command for _, command in controls] == ['duck', 'unduck']


@pytest.mark.parametrize('enabled', [False, True])
def test_confirmation_session_never_ducks(enabled):
    p, controls, _ = trial(enabled, [])
    p.session.session_id = 'fall-confirmation'
    p.feed(VOICE*8)
    assert [command for _, command in controls] == ['stop']
