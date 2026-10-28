import json,time,sys,uuid
from pathlib import Path
import rclpy
from rcl_interfaces.msg import Log
from malbut_interfaces.msg import SpeechRequest,SpeechPlaybackStatus,SpeechTranscript
phase=sys.argv[1];mode=sys.argv[2];count=int(sys.argv[3]);label='대조 시험입니다. 이번에는 말씀하지 말고 들어 주세요.' if mode=='control' else '말 끊기를 시험할게요. 지금 제가 말하는 동안 언제든 말씀해 주세요.'
rclpy.init();node=rclpy.create_node('human_interruption_trial');pub=node.create_publisher(SpeechRequest,'/malbut/speech/response',10)
start=time.monotonic();rows=[];states=[];texts=[];events=[];requests=[];idle=False;idle_at=None;trial=None;stage=0;published_at=None; aec_ready=False; aec_error=None
stamp=lambda:round(time.monotonic()-start,4)
def log(m):
 global idle,idle_at,aec_ready,aec_error
 if m.name!='malbut_stt':return
 if any(x in m.msg for x in ['aec_','barge_in','speech_started','transcript_final','interruption_asr','waiting_for','session_ended','failed','capture']):
  events.append([stamp(),m.msg]);print(events[-1],flush=True)
 if m.msg.startswith('aec_ready:'): aec_ready=True
 if m.msg.startswith('aec_fallback:'): aec_error=m.msg
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
expected=['잠깐, 겨울 이야기만 한 문장으로 해줘.']*count
file=Path('/tmp/malbut_interaction_20261028/retest_1236/'+phase+'.json')
def save():file.write_text(json.dumps(dict(phase=phase,trials=rows,states=states,transcripts=texts,events=events,requests=requests),ensure_ascii=False,indent=2))
try:
 while stamp()<260:
  rclpy.spin_once(node,timeout_sec=.03)
  if aec_error: raise RuntimeError(aec_error)
  if trial is not None and ((idle and stamp()>published_at+5) or stamp()>published_at+70):
   rows[-1]['end_at']=stamp();rows[-1]['transcripts']=[t for t in texts if published_at<t['at']<=stamp()];trial=None;save()
   if stage==count:break
  if trial is None and stage<count and aec_ready and idle and stamp()>idle_at+1:
   stage+=1;trial='conversation-opener:live-'+phase+'-'+str(stage)+'-'+uuid.uuid4().hex
   m=SpeechRequest();m.playback_id=uuid.uuid4().hex;m.request_id=trial;m.request_type=SpeechRequest.DIALOGUE
   m.text=f'{stage}번째 {label} 봄은 겨울의 추위가 누그러지고 새 생명이 시작되는 계절입니다. 낮이 길어지고 기온이 오르면서 꽃이 피고 나무에 새잎이 돋습니다. 여름에는 기온이 높아지고 비가 자주 내립니다. 가을이 오면 나뭇잎이 노랗고 붉게 물듭니다. 지금도 편하게 말씀하셔도 됩니다. 겨울에는 기온이 내려가고 눈이 내리기도 합니다. 사계절마다 주변의 풍경이 달라지는 것을 볼 수 있습니다.'
   idle=False;published_at=stamp();rows.append(dict(index=stage,request_id=trial,playback_id=m.playback_id,start_at=published_at,expected=expected[stage-1]));pub.publish(m);print('TRIAL',stage,flush=True);save()
finally:save();node.destroy_node();rclpy.shutdown()
