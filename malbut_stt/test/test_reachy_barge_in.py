"""Ordinary reply interruption through the real capture/session state machine."""
import pytest

from malbut_stt.audio import CaptureSettings
from test_dialogue_pipeline import harness, pump, VOICE, QUIET


def reply(harness, *, aec=True, mode='reachy'):
    p = harness.create(aec=aec, barge_in_mode=mode,
        settings=CaptureSettings(min_speech_s=.16, silence_timeout_s=2.0))
    p._publish_transcript('old-request', '처음 질문')
    p.on_playback_status('old-playback', 'playing', request_id='old-request')
    return p


def test_reachy_requires_aec(harness):
    with pytest.raises(ValueError, match='echo-cancelled'):
        harness.create(aec=False, barge_in_mode='reachy')


@pytest.mark.parametrize('mode,aec', [('legacy', False), ('legacy', True)])
def test_existing_reply_gate_stays_closed(harness, mode, aec):
    p = reply(harness, mode=mode, aec=aec)
    p.feed(VOICE * 20 + QUIET * 100)
    assert not harness.controls
    assert len(harness.transcripts) == 1
    assert not p.command_stream.collector.started


@pytest.mark.parametrize('pid,request_id,interim', [
    ('progress', 'old-request', True), ('foreign', 'other-request', False),
])
def test_only_matching_final_playback_opens_input(harness, pid, request_id, interim):
    p = harness.create(barge_in_mode='reachy')
    p._publish_transcript('old-request', '처음 질문')
    p.on_playback_status(pid, 'playing', request_id=request_id, interim=interim)
    p.feed(VOICE * 20)
    assert not harness.controls
    assert p._input_blocked(harness.now)


def test_short_candidate_recovers_without_stopping_answer(harness):
    p = reply(harness)
    p.feed(VOICE * 3)
    assert harness.controls == [('old-playback', 'duck')]
    p.feed(QUIET)
    assert harness.controls == [('old-playback', 'duck'), ('old-playback', 'unduck')]
    assert p._reply_request_id == 'old-request'


@pytest.mark.parametrize('terminal', ['stopped', 'finished', 'failed'])
def test_confirmed_speech_stops_once_and_old_terminal_cannot_drop_new_turn(harness, terminal):
    p = reply(harness)
    p.feed(VOICE * 3)
    p.feed(VOICE * 5)
    assert harness.controls == [('old-playback', 'duck'), ('old-playback', 'stop')]
    uid = p._capture_id
    assert uid and p._reply_request_id is None
    p.on_playback_status('old-playback', terminal, request_id='old-request')
    assert p._capture_id == uid and p.session.active
    p.feed(VOICE * 10 + QUIET * 100)
    pump(p, lambda: len(harness.transcripts) == 2)
    assert harness.transcripts[-1][0] == uid
    assert not harness.candidates  # confirmed onset already chose to interrupt
    assert p._reply_request_id == uid
    p.on_playback_status('old-playback', 'playing', request_id='old-request')
    assert p._input_blocked(harness.now)  # stale reply cannot open the next wait


def test_natural_completion_opens_existing_followup_window(harness):
    p = reply(harness)
    p.on_playback_status('old-playback', 'finished', request_id='old-request')
    assert p.session.active and p._reply_request_id is None
    assert p._followup_deadline == harness.now + 10


def test_natural_completion_preserves_speech_started_in_last_160_ms(harness):
    p = reply(harness)
    p.feed(VOICE * 3)
    p.on_playback_status('old-playback', 'finished', request_id='old-request')
    p.feed(VOICE * 5)
    assert p._capture_id
    assert 'stop' not in [command for _, command in harness.controls]
    p.feed(VOICE * 10 + QUIET * 100)
    pump(p, lambda: len(harness.transcripts) == 2)


def test_web_call_still_blocks_reachy_capture(harness):
    p = reply(harness)
    assert p.control_web_talk('guardian-call', True, ttl_s=10, quiet=False)
    p.feed(VOICE * 10)
    assert not harness.controls
    assert p._input_blocked(harness.now)
