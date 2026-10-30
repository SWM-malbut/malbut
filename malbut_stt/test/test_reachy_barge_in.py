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


@pytest.mark.parametrize('frames', [1, 2, 3, 4])
def test_short_candidate_cannot_duck_or_accumulate_across_quiet(harness, frames):
    p = reply(harness)
    for _ in range(3):
        p.feed(VOICE * frames)
        assert not harness.controls
        p.feed(QUIET)
        assert not harness.controls
    assert not p.command_stream.collector.started
    assert p._reply_request_id == 'old-request'


def test_five_frame_candidate_ducks_then_recovers_without_stopping(harness):
    p = reply(harness)
    p.feed(VOICE * 4)
    assert not harness.controls
    p.feed(VOICE)
    assert harness.controls == [('old-playback', 'duck')]
    assert not p.command_stream.collector.started
    p.feed(QUIET)
    assert harness.controls == [('old-playback', 'duck'), ('old-playback', 'unduck')]
    assert p._reply_request_id == 'old-request'


def test_slow_candidate_processing_renews_duck_until_confirmation(harness):
    p = reply(harness)
    # Seven captured frames remain a candidate even if owner scheduling is slow.
    for now in [0, .08, .16, .24, .32, .40, .48]:
        harness.now = now
        p.feed(VOICE)
    assert not p.command_stream.collector.started
    assert harness.controls == [('old-playback', 'duck')] * 2
    harness.now = .56
    p.feed(VOICE)
    assert p._capture_id
    assert harness.controls[-1] == ('old-playback', 'stop')


@pytest.mark.parametrize('end', ['quiet', 'reset', 'finished', 'failed'])
def test_expired_candidate_cannot_renew_duck(harness, end):
    p = reply(harness)
    p.feed(VOICE * 5)
    harness.now = .16
    p.feed(VOICE)
    assert harness.controls == [('old-playback', 'duck')] * 2
    if end == 'quiet':
        p.feed(QUIET)
    elif end == 'reset':
        p._reset_audio()
    else:
        p.on_playback_status('old-playback', end, request_id='old-request')
    assert harness.controls[-1] == ('old-playback', 'unduck')
    before = list(harness.controls)
    harness.now = .5
    p.feed(QUIET)
    assert harness.controls == before


@pytest.mark.parametrize('terminal', ['stopped', 'finished', 'failed'])
def test_confirmed_speech_stops_once_and_old_terminal_cannot_drop_new_turn(harness, terminal):
    p = reply(harness)
    p.feed(QUIET * 3 + VOICE * 5)
    assert harness.controls == [('old-playback', 'duck')]
    p.feed(VOICE * 3)
    assert harness.controls == [('old-playback', 'duck'), ('old-playback', 'stop')]
    assert bytes(p.command_stream.collector.audio) == QUIET * 3 + VOICE * 8
    uid = p._capture_id
    assert uid and p._reply_request_id is None
    p.on_playback_status('old-playback', terminal, request_id='old-request')
    assert p._capture_id == uid and p.session.active
    p.feed(VOICE * 10 + QUIET * 100)
    pump(p, lambda: len(harness.transcripts) == 2)
    assert harness.transcripts[-1][0] == uid
    assert harness.command_calls[-1][0].startswith(QUIET * 3 + VOICE * 18)
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
