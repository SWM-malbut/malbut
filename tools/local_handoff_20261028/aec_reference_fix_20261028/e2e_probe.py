import json,time,subprocess
from pathlib import Path
import rclpy
from rcl_interfaces.msg import Log
from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy
from malbut_interfaces.msg import SpeechTranscript,SpeechPlaybackStatus,SpeechRequest
from malbut_interfaces.srv import ControlSpeechSession
root=Path('/tmp/malbut_aec_fix_20261028')
rclpy.init();node=rclpy.create_node('aec_e2e_probe')
qos=QoSProfile(depth=100,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.VOLATILE)
origin=time.monotonic();events=[];states=[];transcripts=[];requests=[];ready=False;ready_at=None;injected=False;first_play=None;first_id=None;second_end=None
stamp=lambda:round(time.monotonic()-origin,4)
def log(m):
 global ready,ready_at
 if m.name=='malbut_stt' and any(w in m.msg for w in ['waiting_for_wake','aec_','speech_started','barge_in','capture','transcription_failed','transcript_final']):
  events.append([stamp(),m.name,m.msg]);print(events[-1],flush=True)
  if 'waiting_for_wake' in m.msg:ready=True;ready_at=stamp()
 if m.level>=40:events.append([stamp(),m.name,m.msg]);print(events[-1],flush=True)
def state(m):
 global first_play,first_id,second_end
 states.append(dict(at=stamp(),pid=m.playback_id,state=m.state,interim=m.interim,rid=m.request_id));print(states[-1],flush=True)
 if not m.interim and m.state=='playing' and first_play is None:first_play=stamp();first_id=m.playback_id
 if m.state=='finished' and first_id and m.playback_id!=first_id and not m.interim:second_end=stamp()
def transcript(m):
 transcripts.append(dict(at=stamp(),text=m.text,id=m.utterance_id));print(transcripts[-1],flush=True)
def request(m):
 requests.append(dict(at=stamp(),text=m.text,id=m.playback_id,interim=m.interim));print(requests[-1],flush=True)
node.create_subscription(Log,'/rosout',log,100)
node.create_subscription(SpeechPlaybackStatus,'/malbut/speech/playback_status',state,qos)
node.create_subscription(SpeechTranscript,'/malbut/speech/transcript',transcript,qos)
node.create_subscription(SpeechRequest,'/malbut/speech/response',request,qos)
client=node.create_client(ControlSpeechSession,'/malbut/speech/session_control')
future=None;wake_started=False
try:
 while stamp()<125:
  rclpy.spin_once(node,timeout_sec=.02)
  if not wake_started and (ready or stamp()>3):
   if client.service_is_ready() and future is None:
    req=ControlSpeechSession.Request();req.session_id='aec-observe-only';req.check_only=True;future=client.call_async(req)
   if future and future.done() and (ready_at is None or stamp()>ready_at+1.0):
    result=future.result();events.append([stamp(),'barge_available',result.barge_in_available]);print(events[-1],flush=True)
    if not result.barge_in_available:raise RuntimeError('AEC not ready')
    subprocess.Popen(['paplay','--device=malbut_aec_injection_20261028',str(root/'wake.wav')]);wake_started=True
  if first_play is not None and not injected and stamp()>first_play+2:
   events.append([stamp(),'near_injected','interrupt.wav']);subprocess.Popen(['paplay','--device=malbut_aec_injection_20261028',str(root/'interrupt.wav')]);injected=True
  if second_end and stamp()>second_end+2:break
finally:
 result=dict(events=events,states=states,transcripts=transcripts,requests=requests,wall_s=stamp(),injected=injected)
 (root/'e2e.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));node.destroy_node();rclpy.shutdown()
