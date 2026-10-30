"""Preserve late speech and isolate playback ownership at reply boundaries."""
import pytest

from malbut_stt.audio import CapturePCM, CaptureSettings
from test_dialogue_pipeline import harness, VOICE, QUIET

PREFIX = b'\x2a\x00' * 1600


def reply(harness, mode='reachy', aec=True):
    p = harness.create(aec=aec, barge_in_mode=mode,
        settings=CaptureSettings(min_speech_s=.16, silence_timeout_s=2.0))
    p._publish_transcript('request', '질문')
    p.on_playback_status('old', 'playing', request_id='request')
    return p


def test_weak_pre_roll_survives_owner_finish_before_vad_candidate(harness):
    p = reply(harness)
    p.feed(CapturePCM(PREFIX, QUIET * 5))
    assert not p.command_stream.collector.candidate_audio
    p.on_playback_status('old', 'finished', request_id='request')
    p.feed(VOICE * 8)
    assert p._capture_id and PREFIX in p.command_stream.collector.audio
    assert not harness.controls


def test_silent_pre_roll_stays_bounded_and_does_not_create_speech(harness):
    p = reply(harness)
    p.feed(QUIET * 150)
    p.on_playback_status('old', 'finished', request_id='request')
    p.feed(QUIET * 100)
    assert len(p.command_stream.collector.pre_roll) <= 100
    assert not p._capture_id and len(harness.transcripts) == 1
    assert p._followup_deadline == 10


def test_natural_opener_completion_retires_its_request_timeout(harness):
    p = harness.create(barge_in_mode='reachy')
    request_id = 'conversation-opener:slow-greeting'
    p.on_speech_request('greeting', request_id=request_id)
    p.on_playback_status('greeting', 'playing', request_id=request_id)
    harness.now = 40.
    p.on_playback_status('greeting', 'finished', request_id=request_id)
    assert p._reply_request_id is None and p.session.active
    assert p._followup_deadline == 50.
    harness.now = 45.
    p.poll()
    assert p.session.active and p._followup_deadline == 50.


@pytest.mark.parametrize('state', ['failed', 'stopped'])
def test_failed_or_stopped_reply_does_not_transfer_pre_roll(harness, state):
    p = reply(harness)
    p.feed(CapturePCM(PREFIX, QUIET * 5))
    p.on_playback_status('old', state, request_id='request')
    assert not p.command_stream.collector.pre_roll
    assert p._reply_request_id is None and not p.session.active


@pytest.mark.parametrize('blocked', ['busy', 'pending', 'chime', 'chime_tail', 'agent'])
def test_blocked_owner_does_not_transfer_pre_roll(harness, blocked):
    p = reply(harness)
    p.feed(CapturePCM(PREFIX, QUIET * 5))
    if blocked == 'busy':
        p._busy = True
    elif blocked == 'pending':
        p._pending = ('uid', 'old', 'text', 45.)
    elif blocked == 'chime':
        p._chime_playing = True
    elif blocked == 'chime_tail':
        p._chime_gate_until = .5
    else:
        p.session.session_id = 'agent'
    p.on_playback_status('old', 'finished', request_id='request')
    assert not p.command_stream.collector.pre_roll


@pytest.mark.parametrize('mode,aec', [('legacy', True), ('legacy', False)])
def test_legacy_natural_completion_keeps_existing_reset(harness, mode, aec):
    p = reply(harness, mode, aec)
    before = p._audio_generation
    p.on_playback_status('old', 'finished', request_id='request')
    assert p._audio_generation > before and not p.command_stream.collector.pre_roll


@pytest.mark.parametrize('state', ['finished', 'failed', 'stopped'])
@pytest.mark.parametrize('confirmed', [False, True])
def test_old_or_queued_nonowner_terminal_cannot_cancel_new_playback_or_capture(
        harness, state, confirmed):
    p = reply(harness)
    p.on_playback_status('new', 'playing', request_id='request')
    p.feed(VOICE * (8 if confirmed else 3))
    before = (p._capture_id, p._audio_generation, p.command_stream.collector)
    for pid in ('old','queued-with-same-request'):
        p.on_playback_status(pid, state, request_id='request')
        assert (p._capture_id, p._audio_generation, p.command_stream.collector) == before
        assert p.session.active and p.session.playback_id == 'new'
    if not confirmed:
        p.feed(VOICE * 5)
        assert p._capture_id
        assert ('new', 'stop') in harness.controls


@pytest.mark.parametrize('state', ['failed', 'stopped'])
def test_failure_before_first_playing_still_releases_reply(harness, state):
    p = harness.create(aec=True, barge_in_mode='reachy')
    p._publish_transcript('request', '질문')
    p.on_playback_status('never-played', state, request_id='request')
    assert p._reply_request_id is None and not p.session.active


def test_interim_terminal_does_not_complete_correlated_final(harness):
    p = reply(harness)
    p.feed(CapturePCM(PREFIX, QUIET * 5))
    before = p._audio_generation
    p.on_playback_status('progress', 'stopped', interim=True, request_id='request')
    assert p._reply_request_id == 'request' and p._audio_generation == before
    assert PREFIX in b''.join(p.command_stream.collector.pre_roll)
