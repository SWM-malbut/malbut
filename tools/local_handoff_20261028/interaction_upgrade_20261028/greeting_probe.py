import json,time,subprocess
from pathlib import Path
import rclpy
from rcl_interfaces.msg import Log
from malbut_interfaces.msg import SpeechTranscript,SpeechPlaybackStatus,SpeechRequest
rclpy.init();n=rclpy.create_node('greeting_trial');start=time.monotonic();states=[];requests=[];transcripts=[];logs=[];opener=None;ended=None;sent=False;reply_end=None
now=lambda:round(time.monotonic()-start,4)
def req(m):
 global opener
 row=dict(at=now(),pid=m.playback_id,rid=m.request_id,text=m.text);requests.append(row);print(row,flush=True)
 if m.request_id.startswith('conversation-opener:'):opener=m.playback_id
def st(m):
 global ended,reply_end
 row=dict(at=now(),pid=m.playback_id,rid=m.request_id,state=m.state);states.append(row);print(row,flush=True)
 if m.playback_id==opener and m.state=='finished':ended=now()
 if sent and m.playback_id!=opener and m.state=='finished':reply_end=now()
def tr(m):
 row=dict(at=now(),text=m.text);transcripts.append(row);print(row,flush=True)
def log(m):
 if m.name=='malbut_stt' and any(x in m.msg for x in ['waiting_for_wake','aec_ready','barge_in_confirmed','followup','speech_started']):logs.append([now(),m.msg]);print(logs[-1],flush=True)
n.create_subscription(SpeechRequest,'/malbut/speech/response',req,100)
n.create_subscription(SpeechPlaybackStatus,'/malbut/speech/playback_status',st,100)
n.create_subscription(SpeechTranscript,'/malbut/speech/transcript',tr,100)
n.create_subscription(Log,'/rosout',log,100)
try:
 while now()<95:
  rclpy.spin_once(n,timeout_sec=.02)
  if ended and not sent and now()>ended+1:
   subprocess.Popen(['paplay','--device=malbut_aec_injection_20261028','/tmp/malbut_aec_fix_20261028/question.wav']);sent=True
  if reply_end and now()>reply_end+12:break
finally:
 Path('/tmp/malbut_interaction_20261028/greeting_result.json').write_text(json.dumps(dict(requests=requests,states=states,transcripts=transcripts,logs=logs,sent_without_wake=sent),ensure_ascii=False,indent=2));n.destroy_node();rclpy.shutdown()
