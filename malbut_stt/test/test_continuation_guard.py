"""Continuation safety must survive a fast ASR, including boundary races."""
from threading import Event, Thread
from types import SimpleNamespace
import pytest
from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline

VOICE = b'\x01\x00' * 320
TAIL = b'\x02\x00' * 320
QUIET = bytes(640)

@pytest.fixture
def run():
    states=[]
    def create(**kwargs):
        state=SimpleNamespace(text=[], chimes=[], now=0.)
        p=DialoguePipeline(recorder_factory=lambda:None,wake=None,transcriber=None,
            is_speech=lambda pcm,_: any(pcm),publish_transcript=lambda uid,text:state.text.append((uid,text)),
            publish_control=lambda *_:None,publish_interruption=lambda *_:None,report=lambda *_:None,
            clock=lambda:state.now,input_has_aec=True,
            endpoint_predecode_s=.4,endpoint_min_silence_s=1.8,
            settings=CaptureSettings(silence_timeout_s=2.,min_speech_s=.02), **kwargs)
        p.session.activate();state.p=p;states.append(state);return state
    yield create
    for s in states:s.p.close()

def result(p,job,text='삼각형이 뭐예요?'):
    p.results.put((*job[:3],text,None));p.poll()

@pytest.mark.parametrize('pause_frames',[20,40,60,80,89])
def test_fast_complete_text_waits_for_resumption_and_preserves_all_pcm(run,pause_frames):
    s=run();p=s.p
    p.feed(VOICE+QUIET*20);old=p.jobs.get_nowait();uid=old[2][0]
    result(p,old)
    p.feed(QUIET*(pause_frames-20));p.poll()
    assert not s.text
    p.feed(TAIL*10+QUIET*20);new=p.jobs.get_nowait()
    assert new[2][0]==uid and new[2][1]!=old[2][1]
    assert new[3].startswith(VOICE+QUIET*pause_frames+TAIL*10)
    result(p,old)  # Late duplicate must never complete the new revision.
    assert not s.text
    result(p,new,'삼각형이 뭐예요? 두 문장으로 답해주세요.')
    assert not s.text
    p.feed(QUIET*70);p.poll()
    assert s.text==[(uid,'삼각형이 뭐예요? 두 문장으로 답해주세요.')]


def test_queued_resumption_wins_before_accepting_cached_result(run):
    s=run();p=s.p
    p.feed(VOICE+QUIET*20);job=p.jobs.get_nowait()
    p.feed(QUIET*69)
    p.audio.put((p._audio_generation,0.,TAIL,False))
    result(p,job)
    assert not s.text and p.command_stream.collector.silent_frames==0


def test_incomplete_text_retains_two_second_fallback(run):
    s=run();p=s.p;p.feed(VOICE+QUIET*20);job=p.jobs.get_nowait()
    result(p,job,'삼각형에 대해서')
    p.feed(QUIET*70);p.poll();assert not s.text
    p.feed(QUIET*10);p.poll()
    assert s.text==[(job[2][0],'삼각형에 대해서')]


def test_final_decode_can_start_during_chime_but_cannot_publish_before_drain(run):
    entered=Event();done=Event()
    def chime():
        job=p.jobs.get(timeout=.5)
        assert job[0]=='command'
        assert job[3]==VOICE+QUIET*100
        # Worker completes while the caller is inside output drain.
        def worker():
            entered.set();p.results.put((*job[:3],'정상 문장입니다.',None));done.set()
        t=Thread(target=worker);t.start();assert done.wait(.5);t.join()
        assert not s.text
    s=run(on_endpoint=chime);p=s.p
    # Remove the unusable early job; final capture must enqueue its full PCM.
    p.feed(VOICE+QUIET*20);old=p.jobs.get_nowait();result(p,old,text='')
    p.feed(QUIET*80)
    assert entered.is_set() and not s.text
    p.poll();assert s.text==[(old[2][0],'정상 문장입니다.')]

@pytest.mark.parametrize('minimum',[1.8,1.9,2.0])
def test_protected_minimum_is_allowed(minimum):
    p=DialoguePipeline(recorder_factory=None,wake=None,transcriber=None,is_speech=lambda *_:False,
        publish_transcript=None,publish_control=None,publish_interruption=None,report=None,
        endpoint_min_silence_s=minimum)
    p.close()

@pytest.mark.parametrize('minimum',[True,None,'1.8',.19,2.01,float('inf'),float('nan')])
def test_invalid_minimum_rejected(minimum):
    with pytest.raises(ValueError,match='endpoint_min_silence_s'):
        DialoguePipeline(recorder_factory=None,wake=None,transcriber=None,is_speech=lambda *_:False,
            publish_transcript=None,publish_control=None,publish_interruption=None,report=None,
            endpoint_min_silence_s=minimum)
