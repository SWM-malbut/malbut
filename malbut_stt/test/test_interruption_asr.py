"""Recognition fallback and request ownership under interrupted playback."""
from types import SimpleNamespace
import io
import wave
import pytest

from malbut_stt.interruption_asr import InterruptionTranscriber
from malbut_stt.audio import CapturePCM, CaptureSettings
from test_dialogue_pipeline import harness, pump, VOICE, QUIET


def test_bounded_api_receives_intact_audio_and_korean_hint():
    calls=[]
    def transport(headers,body,timeout):
        calls.append((headers,body,timeout))
        return {'text':'  그만하고 다른 이야기를 해 줘.  '}
    t=InterruptionTranscriber(lambda:'test-key',None,transport=transport)
    pcm=VOICE*60
    assert t.transcribe(pcm,16000)=='그만하고 다른 이야기를 해 줘.'
    headers,body,timeout=calls[0]
    assert b'languages[]' in body and b'\r\nko\r\n' in body
    with wave.open(io.BytesIO(body[body.index(b'RIFF'):])) as wav:
        assert wav.getframerate()==16000 and wav.readframes(wav.getnframes())==pcm
    assert timeout==5


@pytest.mark.parametrize('failure',[TimeoutError(),ValueError(),ConnectionError()])
def test_api_failure_uses_same_complete_pcm_locally(failure):
    seen=[];events=[]
    def transport(*_):raise failure
    local=SimpleNamespace(transcribe=lambda pcm,rate:seen.append((pcm,rate)) or '로컬 결과')
    t=InterruptionTranscriber(lambda:'test-key',local,transport=transport,report=events.append)
    assert t.transcribe(VOICE*60,16000)=='로컬 결과'
    assert seen==[(VOICE*60,16000)] and 'local_fallback' in events[0]


def test_only_interruption_uses_remote_and_retains_soft_onset(harness):
    calls=[]
    remote=SimpleNamespace(transcribe=lambda pcm,rate:calls.append(bytes(pcm)) or '다른 이야기 해줘')
    p=harness.create(barge_in_mode='reachy',interruption_transcriber=remote,
        settings=CaptureSettings(min_speech_s=.16,silence_timeout_s=2.0))
    p._publish_transcript('old','질문')
    p.on_playback_status('answer','playing',request_id='old')
    soft=b'\x02\x00'*320
    p.feed(CapturePCM(QUIET*50,QUIET*50,soft*50))
    p.feed(VOICE*20+QUIET*100)
    pump(p,lambda:len(harness.transcripts)==2)
    assert calls and calls[0].startswith(soft*50)
    assert not harness.command_calls
    p.on_playback_status('answer','stopped',request_id='old')
    uid=harness.transcripts[-1][0]
    p.on_playback_status('next','playing',request_id=uid)
    p.on_playback_status('next','finished',request_id=uid)
    p.feed(VOICE*20+QUIET*100)
    pump(p,lambda:len(harness.transcripts)==3)
    assert len(calls)==1 and len(harness.command_calls)==1


def test_late_remote_result_after_session_reset_is_discarded(harness):
    from threading import Event
    entered,release=Event(),Event()
    def remote(pcm,rate):
        entered.set();assert release.wait(2);return '늦은 응답'
    p=harness.create(barge_in_mode='reachy',interruption_transcriber=SimpleNamespace(transcribe=remote))
    p._publish_transcript('old','질문');p.on_playback_status('answer','playing',request_id='old')
    p.feed(VOICE*20+QUIET*100)
    assert entered.wait(1)
    p.start_session('new-session');release.set()
    pump(p,lambda:not p.results.empty() or not p._busy)
    assert harness.transcripts==[('old','질문')]
