"""Replay unchanged Silero with/without candidate gain controls. No ASR/API calls."""
import hashlib, inspect, json, math, sys
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parent
WS=ROOT.parent
mode=sys.argv[1]
source=ROOT/'before' if mode=='before' else WS/'src/malbut'
for package in ('malbut_stt','malbut_tts','malbut_agent_server'):
    sys.path.insert(0,str(source/package))
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.silero_stream import SileroStreamVad


def materialize(case):
    path=Path(case['path'])
    assert hashlib.sha256(path.read_bytes()).hexdigest()==case['source_sha256']
    audio,rate=sf.read(path,dtype='float32',always_2d=True)
    audio=audio.mean(axis=1)
    if rate!=16000:
        divisor=math.gcd(rate,16000)
        audio=resample_poly(audio,16000//divisor,rate//divisor)
    pcm=np.rint(np.clip(audio*32768,-32768,32767)).astype('<i2')
    if case['transform'].startswith('pause'):
        pause=12800 if case['transform'].startswith('pause800') else 25600
        cut=int(len(pcm)*.6)
        pcm=np.concatenate((pcm[:cut],np.zeros(pause,dtype='<i2'),pcm[cut:]))
    pcm=np.pad(pcm,(8192,96000))
    return np.pad(pcm,(0,(-len(pcm))%320))


def replay(pcm):
    now=[0.]
    controls,onsets,turns=[],[],[]
    vad=SileroStreamVad(WS.parent/'.cache/malbut_speech/models/silero-v6.2.3.onnx')
    extra={'barge_in_ducking':True} if mode=='after' else {}
    p=DialoguePipeline(recorder_factory=lambda:None,wake=None,
        transcriber=SimpleNamespace(transcribe=lambda *_:'unused'),
        is_speech=vad.is_speech,publish_transcript=lambda *_:None,
        publish_control=lambda pid,command: controls.append([round(now[0],4),command]),
        publish_interruption=lambda *_:None,
        report=lambda event: onsets.append(round(now[0],4)) if event=='speech_started' else None,
        input_has_aec=True,clock=lambda:now[0],
        settings=CaptureSettings(silence_timeout_s=2.,min_speech_s=.16,pre_roll_s=.3,
                                 max_utterance_s=None,max_buffer_s=60.),**extra)
    p.session.activate()
    p.on_playback_status('answer','playing')
    def completed(kind,event):
        turns.append({'end_s':round(now[0],4),'pcm_sha256':hashlib.sha256(event.pcm).hexdigest(),
                      'silence_s':event.silence_s})
        p.session.discard_utterance(p._capture_id)
        p._capture_id=None
        p._endpoint_candidate=p._endpoint_result=p._endpoint_job=None
        p._generation+=1
        p._drain(p.jobs)
    p._complete_capture=completed
    for i,frame in enumerate(pcm.reshape(-1,320)):
        now[0]=(i+1)*.02
        p.feed(frame.tobytes())
    p._update_ducking() if mode=='after' else None
    return {'onsets':onsets,'turns':turns,'controls':controls}


manifest=json.loads((WS/'reachy_start_vs_silero_20261027/evaluation_manifest.json').read_text())
rows=[]
if '--queries-only' in sys.argv:
    rows = json.loads((ROOT/(mode+'.json')).read_text())['cases']
else:
    for case in manifest['cases']:
        rows.append({'id':case['id'],'speech':case['label_speech'],'result':replay(materialize(case))})

# Real orchestration with a scripted provider: measures routing and lifecycle,
# not a language model's ability to select a tool or recognize a scene.
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.schemas import AgentDecision,AgentRequest,ProviderResult,RobotState

class Provider:
    def __init__(self,name,args):self.name,self.args=name,args
    def complete(self,request,memories,turns,tools,conversation_summary=None,**kwargs):
        decision=(AgentDecision('tool_call','조회',tool_name=self.name,arguments=self.args)
                  if self.name in [t.name for t in tools] else
                  AgentDecision('message','현재 지원하지 않아요.',reason='unsupported_capability'))
        return ProviderResult(decision,'offline-fixture','fixed',1.)

queries=[]
for name,args in [('search_robot_docs',{'query':'순찰 방법'}),
                  ('describe_camera_view',{'question':'지금 앞에 뭐가 있어?'})]:
    runtime=build_orchestrator(Settings(database_path=':memory:'),http_server=False)
    observed=[]
    if mode=='after':
        from malbut_agent_server.information_queries import RobotGuide,configure_information_queries
        configure_information_queries(runtime,guide=RobotGuide(),camera=SimpleNamespace(
            answer=lambda question:observed.append(question) or '컵이 보여요.'))
    runtime.provider=Provider(name,args)
    runtime.conversation_store.create('fixture-user','fixture-chat')
    request=AgentRequest(request_id='q',user_id='fixture-user',conversation_id='fixture-chat',
        turn_id='t',utterance='질문',robot_state=RobotState(),
        available_tools=getattr(runtime,'information_query_tools',()))
    try:
        result=runtime.handle(request)
        queries.append({'tool':name,'advertised':name in request.available_tools,
            'decision':result.decision.type,'reason':result.decision.reason,
            'message':result.decision.message,'camera_fixture_calls':len(observed)})
    finally:runtime.close()

(ROOT/(mode+'.json')).write_text(json.dumps({'mode':mode,'cases':rows,'queries':queries,
    'scope':{'real_silero':True,'asr_inference':False,'semantic_endpoint':False,
        'api_calls':0,'camera_input':'scripted answer','real_microphone':False,
        'real_speaker':False,'aec':'assumed for replay, not measured'}},ensure_ascii=False,indent=2)+'\n')
print(mode, len(rows),'audio conditions and',len(queries),'orchestrator comparisons completed')
