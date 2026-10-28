import json,time,sys,uuid
from pathlib import Path
import rclpy
from rcl_interfaces.msg import Log
from malbut_interfaces.msg import SpeechRequest,SpeechPlaybackStatus,SpeechTranscript
phase=sys.argv[1];label='기존 설정' if phase=='before' else '개선 후보 설정'
rclpy.init();node=rclpy.create_node('human_interruption_trial');pub=node.create_publisher(SpeechRequest,'/malbut/speech/response',10)
start=time.monotonic();rows=[];states=[];texts=[];events=[];requests=[];idle=False;idle_at=None;trial=None;stage=0;published_at=None
stamp=lambda:round(time.monotonic()-start,4)
def log(m):
 global idle,idle_at
 if m.name!='malbut_stt':return
 if any(x in m.msg for x in ['aec_','barge_in','speech_started','transcript_final','interruption_asr','waiting_for','session_ended','failed','capture']):
  events.append([stamp(),m.msg]);print(events[-1],flush=True)
 if m.msg=='waiting_for_wake':idle=True;idle_at=stamp()
def state(m):
 row=dict(at=stamp(),pid=m.playback_id,rid=m.request_id,state=m.state);states.append(row);print(row,flush=True)
def transcript(m):
 row=dict(at=stamp(),id=m.utterance_id,text=m.text);texts.append(row);print(row,flush=True)
def request(m):
 requests.append(dict(at=stamp(),pid=m.playback_id,rid=m.request_id,text=m.text))
node.create_subscription(Log,'/rosout',log,100)
node.create_subscription(SpeechPlaybackStatus,'/malbut/speech/playback_status',state,100)
node.create_subscription(SpeechTranscript,'/malbut/speech/transcript',transcript,100)
node.create_subscription(SpeechRequest,'/malbut/speech/response',request,100)
expected=['잠깐, 겨울 이야기만 한 문장으로 해줘.','그만하고 다른 이야기를 해줘.','잠깐, 내 말부터 들어줘.']
file=Path('/tmp/malbut_interaction_20261028/live_'+phase+'.json')
def save():file.write_text(json.dumps(dict(phase=phase,trials=rows,states=states,transcripts=texts,events=events,requests=requests),ensure_ascii=False,indent=2))
try:
 while stamp()<260:
  rclpy.spin_once(node,timeout_sec=.03)
  if trial is not None and ((idle and stamp()>published_at+5) or stamp()>published_at+70):
   rows[-1]['end_at']=stamp();rows[-1]['transcripts']=[t for t in texts if published_at<t['at']<=stamp()];trial=None;save()
   if stage==3:break
  if trial is None and stage<3 and idle and stamp()>idle_at+1:
   stage+=1;trial='conversation-opener:live-'+phase+'-'+str(stage)+'-'+uuid.uuid4().hex
   m=SpeechRequest();m.playback_id=uuid.uuid4().hex;m.request_id=trial;m.request_type=SpeechRequest.DIALOGUE
   m.text=f'{label}, {stage}번째 시험입니다. 설명이 시작되면 준비한 {stage}번째 문장으로 끊어 주세요. 봄은 겨울의 추위가 누그러지고 새 생명이 시작되는 계절입니다. 낮이 길어지고 기온이 오르면서 꽃이 피고 나무에 새잎이 돋습니다. 여름에는 기온이 높아지고 비가 자주 내립니다.'
   idle=False;published_at=stamp();rows.append(dict(index=stage,request_id=trial,playback_id=m.playback_id,start_at=published_at,expected=expected[stage-1]));pub.publish(m);print('TRIAL',stage,flush=True);save()
finally:save();node.destroy_node();rclpy.shutdown()
