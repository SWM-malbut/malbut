"""Real provider comparison using synthetic conversation, no personal history."""
from pathlib import Path
from dataclasses import replace
import sys,json,time
ROOT=Path(__file__).resolve().parent
mode='after'
source=ROOT/'before' if mode=='before' else ROOT.parent/'src/malbut'
sys.path.insert(0,str(source/'malbut_agent_server'))
from malbut_agent_server.config import Settings,load_env_file
from malbut_agent_server.factory import build_provider
from malbut_agent_server.schemas import SpeechAgentRequest,RobotState
from malbut_agent_server.conversation import ConversationTurn

settings=Settings.from_env(load_env_file(Path.home()/'.config/malbut/agent.env',target={}))
provider=build_provider(replace(settings,provider='openai',database_path=':memory:'))
cases=[
 ('start_topic','심심해. 네가 먼저 재미있는 이야기 좀 해줘.',False),
 ('start_activity','뭘 해야 할지 모르겠네. 우리 간단하게 뭐라도 해볼까?',False),
 ('daily','오늘 산책하다가 길고양이 두 마리를 봤어.',False),
 ('listen_only','오늘 회의가 길어서 지쳤어. 해결책 말고 그냥 들어줘.',False),
 ('change_topic','그만하고 다른 이야기를 해줘.',True),
 ('short_correction','아니, 봄 말고 겨울 이야기만 한 문장으로 해줘.',True),
 ('stop_talking','잠깐, 그만 말해.',True),
 ('uncertain_word','금문하고 이야기를 해줘.',True),
 ('unseen_scene','퇴근길에 강아지 세 마리를 봤어.',False),
 ('quiet_close','응, 이제 좀 쉴게. 더 묻지는 마.',False),
]
cases=[c for c in cases if c[0] in {'start_topic','change_topic','short_correction','stop_talking','unseen_scene','quiet_close'}]
rows=[]
for ident,text,interrupted in cases:
    fields=dict(request_id='eval-'+ident,user_id='synthetic-eval',conversation_id='eval-session',
                turn_id='eval-turn-'+ident,utterance=text,robot_state=RobotState(),available_tools=())
    if mode=='after':fields['previous_reply_interrupted']=interrupted
    request=SpeechAgentRequest(**fields)
    start=time.monotonic();cpu=time.process_time()
    try:
        result=provider.complete(request,[],[],[])
        row=dict(id=ident,input=text,response=result.decision.message,decision=result.decision.type,
                 model=result.model,wall_s=time.monotonic()-start,cpu_s=time.process_time()-cpu)
    except Exception as e:row=dict(id=ident,error_type=type(e).__name__)
    rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    (ROOT/'dialogue_refined_v2.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
