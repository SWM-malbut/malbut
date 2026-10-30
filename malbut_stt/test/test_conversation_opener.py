"""Robot greetings use real playback and wake-free follow-up, without fake STT."""
import pytest
from malbut_stt.audio import CaptureSettings
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


@pytest.mark.parametrize('status_first',[False,True])
def test_same_request_interim_does_not_decline_final_opener(harness,status_first):
    stopped=[]
    p=harness.create(barge_in_mode='reachy',stop_speech=stopped.append)
    p.on_speech_request('progress',request_id=OPENER,interim=True)
    p.on_playback_status('progress','playing',request_id=OPENER,interim=True)
    if status_first:p.on_playback_status('greet','playing',request_id=OPENER)
    p.on_speech_request('greet',request_id=OPENER)
    assert stopped==[] and p._reply_request_id==OPENER
    p.on_playback_status('progress','stopped',request_id=OPENER,interim=True)
    if not status_first:p.on_playback_status('greet','playing',request_id=OPENER)
    assert p.session.active
    p.on_playback_status('greet','finished',request_id=OPENER)
    assert p.session.active and p._followup_deadline is not None
    p.feed(VOICE*20+QUIET*100)
    pump(p,lambda:len(harness.transcripts)==1)
    assert not harness.wake_calls


@pytest.mark.parametrize('request_id,interim',[
    ('conversation-opener:other',True),('',True),(OPENER,False),
])
def test_opener_cannot_replace_unrelated_or_final_playback(harness,request_id,interim):
    stopped=[]
    p=harness.create(barge_in_mode='reachy',stop_speech=stopped.append)
    p.on_playback_status('other','playing',request_id=request_id,interim=interim)
    p.on_speech_request('greet',request_id=OPENER)
    assert stopped==['greet'] and p._reply_request_id is None


@pytest.mark.parametrize('guard',['session','candidate','started','busy','pending','web_talk'])
def test_same_request_interim_never_bypasses_other_opener_guards(harness,guard):
    stopped=[]
    p=harness.create(barge_in_mode='reachy',stop_speech=stopped.append,
        settings=CaptureSettings(min_speech_s=.16))
    p.on_playback_status('progress','playing',request_id=OPENER,interim=True)
    if guard=='session':p.session.activate_proactive('guardian-session')
    elif guard=='candidate':p.feed(VOICE)
    elif guard=='started':p.feed(VOICE*8)
    elif guard=='busy':p._busy=True
    elif guard=='pending':p._pending=('uid','progress','text',45.)
    elif guard=='web_talk':
        assert p.control_web_talk('guardian',True,ttl_s=10,quiet=True)
        stopped.clear()
    p.on_speech_request('greet',request_id=OPENER)
    assert stopped==['greet'] and p._reply_request_id is None


@pytest.mark.parametrize('stale_state',['playing','stopped','failed'])
def test_stale_interim_cannot_exempt_an_unrelated_current_playback(harness,stale_state):
    stopped=[]
    p=harness.create(barge_in_mode='reachy',stop_speech=stopped.append)
    p.on_playback_status('progress','playing',request_id=OPENER,interim=True)
    p.on_playback_status('foreign','playing',request_id='other-request')
    p.on_playback_status('progress',stale_state,request_id=OPENER,interim=True)
    p.on_speech_request('greet',request_id=OPENER)
    assert stopped==['greet'] and p.session.playback_id=='foreign'
