"""Readiness and user activity must win over an optional opening greeting."""
from concurrent.futures import Future
from types import SimpleNamespace
import pytest
pytest.importorskip('malbut_interfaces.srv')
from malbut_agent_server.speech_greeting import StartupGreeting

def make():
    state=SimpleNamespace(now=0,idle=True,ready=False,said=[],requests=[],cancelled=False)
    def request(message):
        f=Future();state.requests.append((message,f));return f
    node=SimpleNamespace(create_client=lambda *_:SimpleNamespace(
        service_is_ready=lambda:True,call_async=request),create_timer=lambda *_:SimpleNamespace(
        cancel=lambda:setattr(state,'cancelled',True)))
    state.greeting=StartupGreeting(node,lambda *a,**kw:state.said.append((a,kw)),
        lambda:state.idle,ready=lambda:state.ready,clock=lambda:state.now)
    return state

def test_waits_for_microphone_then_speaks_once_without_opening_confirmation():
    s=make();g=s.greeting;g.tick();assert not s.requests
    s.ready=True;g.tick();request,future=s.requests[-1]
    assert request.check_only and not request.active
    future.set_result(SimpleNamespace(barge_in_available=False));g.tick()
    assert not s.said
    s.requests[-1][1].set_result(SimpleNamespace(barge_in_available=True));g.tick();g.tick()
    assert len(s.said)==1 and g.done
    assert s.said[0][1]['request_id'].startswith('conversation-opener:')

def test_speech_or_session_activity_cancels_pending_greeting():
    s=make();s.ready=True;s.greeting.tick()
    s.idle=False;s.greeting.tick()
    assert s.greeting.done and not s.said and s.requests[-1][1].cancelled()

def test_missing_stt_times_out_without_speaking():
    s=make();s.now=121;s.greeting.tick()
    assert s.greeting.done and not s.said
