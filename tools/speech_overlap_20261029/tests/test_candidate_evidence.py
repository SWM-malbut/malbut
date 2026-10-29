"""Behavioral regression for single-window false ducks; no acoustic claims."""
import pytest
from malbut_stt.audio import CapturePCM,CaptureSettings
from test_dialogue_pipeline import harness,VOICE,QUIET

def reply(h):
 p=h.create(aec=True,barge_in_mode='reachy',settings=CaptureSettings(min_speech_s=.16,silence_timeout_s=2))
 p._publish_transcript('old','question');p.on_playback_status('pid','playing',request_id='old');return p

def audio(h,pcm):return CapturePCM(pcm,pcm,source_timestamp=h.now)

@pytest.mark.parametrize('positive_frames',[1,2,3,4])
def test_two_windows_then_quiet_never_changes_output(harness,positive_frames):
 p=reply(harness)
 for _ in range(3):
  p.feed(audio(harness,VOICE*positive_frames));harness.now+=.04
  p.feed(audio(harness,QUIET*3));harness.now+=.06
 assert harness.controls==[] and not p._capture_id
 assert len(harness.transcripts)==1

def test_sustained_speech_ducks_then_stops_once_preserving_asr_pcm(harness):
 p=reply(harness);p.feed(audio(harness,QUIET*50))
 p.feed(audio(harness,VOICE*4));assert not harness.controls
 p.feed(audio(harness,VOICE));assert harness.controls==[('pid','duck')]
 token=p.duck_control_token('pid');p.on_duck_control('pid',token,True,issued_at=0)
 p.feed(audio(harness,VOICE*3));assert not p._capture_id
 prefix=bytes(p.command_stream.collector.candidate_audio)
 harness.now=.281;p.feed(audio(harness,VOICE))
 assert p._capture_id and harness.controls==[('pid','duck'),('pid','stop')]
 assert bytes(p.command_stream.collector.audio)==prefix+VOICE
 uid=p._capture_id;p.on_playback_status('pid','stopped',request_id='old')
 assert p._capture_id==uid

def test_cancelled_candidate_restores_and_next_reply_cannot_inherit_ack(harness):
 p=reply(harness);p.feed(audio(harness,VOICE*5));old=p.duck_control_token('pid')
 p.on_duck_control('pid',old,True,issued_at=0)
 harness.now=.251;p.feed(audio(harness,QUIET))
 assert harness.controls[-1]==('pid','unduck')
 p.on_playback_status('pid','finished',request_id='old')
 p._publish_transcript('new','next');p.on_playback_status('next','playing',request_id='new')
 p.on_duck_control('pid',old,True,issued_at=harness.now)
 p.feed(audio(harness,VOICE*4))
 assert p.duck_control_token('next') is None and p._duck_clear_after is None

def test_expired_cycle_requires_fresh_sustained_candidate(harness):
 p=reply(harness);p.feed(audio(harness,VOICE*5));old=p.duck_control_token('pid')
 p.on_duck_control('pid',old,True,issued_at=0)
 harness.now=.4;p.feed(audio(harness,VOICE*4));assert p.duck_control_token('pid') is None
 p.feed(audio(harness,VOICE));new=p.duck_control_token('pid');assert new!=old
 p.on_duck_control('pid',old,True,issued_at=.4);assert p._duck_clear_after is None
 p.on_duck_control('pid',new,True,issued_at=.4);assert p._duck_clear_after==pytest.approx(.528)
