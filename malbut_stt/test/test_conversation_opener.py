"""Robot greetings use real playback and wake-free follow-up, without fake STT."""
import pytest
from test_dialogue_pipeline import harness, pump, VOICE, QUIET

OPENER='conversation-opener:test'

@pytest.mark.parametrize('status_first',[False,True])
def test_greeting_opens_followup_even_when_ros_topics_arrive_reversed(harness,status_first):
    p=harness.create(barge_in_mode='reachy')
    if status_first:p.on_playback_status('greet','playing',request_id=OPENER)
    p.on_speech_request('greet',request_id=OPENER)
    if not status_first:p.on_playback_status('greet','playing',request_id=OPENER)
    assert p.session.active
    assert harness.transcripts==[]
    p.on_playback_status('greet','finished',request_id=OPENER)
    assert p.session.active and p._followup_deadline is not None
    p.feed(VOICE*20+QUIET*100)
    pump(p,lambda:len(harness.transcripts)==1)
    assert not harness.wake_calls

def test_greeting_can_be_interrupted_without_waiting_for_wake(harness):
    p=harness.create(barge_in_mode='reachy')
    p.on_speech_request('greet',request_id=OPENER)
    p.on_playback_status('greet','playing',request_id=OPENER)
    p.feed(VOICE*20)
    assert ('greet','stop') in harness.controls
    uid=p._capture_id
    p.on_playback_status('greet','stopped',request_id=OPENER)
    p.feed(QUIET*100)
    pump(p,lambda:len(harness.transcripts)==1)
    assert harness.transcripts[0][0]==uid

def test_greeting_never_preempts_existing_user_speech(harness):
    stopped=[]
    p=harness.create(barge_in_mode='reachy',stop_speech=stopped.append)
    p.session.activate();p.feed(VOICE*20)
    uid=p._capture_id
    p.on_speech_request('greet',request_id=OPENER)
    assert stopped==['greet'] and p._capture_id==uid

def test_no_reply_to_greeting_expires_without_repeated_invitation(harness):
    p=harness.create(barge_in_mode='reachy')
    p.on_speech_request('greet',request_id=OPENER)
    p.on_playback_status('greet','playing',request_id=OPENER)
    p.on_playback_status('greet','finished',request_id=OPENER)
    harness.now+=11;p.poll()
    assert not p.session.active
    p.on_speech_request('greet',request_id=OPENER)
    assert p._reply_request_id is None and not p.session.active
