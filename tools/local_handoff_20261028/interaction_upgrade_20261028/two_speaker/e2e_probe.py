import json,time,sys,uuid,subprocess,os
from pathlib import Path
import rclpy
from rcl_interfaces.msg import Log
from malbut_interfaces.msg import SpeechRequest,SpeechPlaybackStatus,SpeechTranscript
out=Path(sys.argv[1]);rclpy.init();node=rclpy.create_node('two_speaker_controlled_trial');pub=node.create_publisher(SpeechRequest,'/malbut/speech/response',10)
cg=Path('/sys/fs/cgroup'+subprocess.check_output(['systemctl','--user','show','malbut-two-speaker.service','--property=ControlGroup','--value'],text=True).strip())
def cpu():return int(dict(line.split() for line in (cg/'cpu.stat').read_text().splitlines())['usage_usec'])
ready_at=0;tts_ready=False;agent_ready=False
start=time.monotonic();rows=[];events=[];states=[];texts=[];aec=False;failure=None;current=None;idle=False
stamp=lambda:time.monotonic()-start
def save():out.write_text(json.dumps(dict(trials=rows,events=events,states=states,transcripts=texts,aec_ready=aec,failure=failure),ensure_ascii=False,indent=2))
def log(m):
 global aec,failure,idle,ready_at,tts_ready,agent_ready
 if m.name=='malbut_tts' and 'TTS listening' in m.msg:tts_ready=True
 if m.name=='malbut_agent_communication' and 'speech_dialogue_ready' in m.msg:agent_ready=True
 if m.name!='malbut_stt':return
 if any(k in m.msg for k in ['aec_','barge_in','speech_started','transcript_final','interruption_asr','waiting_for','session_ended','capture_ready']):events.append(dict(at=stamp(),message=m.msg));print('EVENT '+m.msg,flush=True)
 if m.msg.startswith('aec_ready:'):aec=True;ready_at=stamp()
 if m.msg.startswith('aec_fallback:'):failure=m.msg
 if m.msg=='waiting_for_wake':idle=True

def state(m):
 row=dict(at=stamp(),pid=m.playback_id,rid=m.request_id,state=m.state);states.append(row)
 if current and m.playback_id==current['pid']:
  if m.state=='playing':current.setdefault('playing_at',stamp())
  elif m.state in ['finished','stopped','failed','cancelled','completed']:current.setdefault('terminal_at',stamp());current['terminal_state']=m.state
 print('STATE '+json.dumps(row),flush=True)
def transcript(m):
 row=dict(at=stamp(),text=m.text);texts.append(row);print('TEXT '+json.dumps(row,ensure_ascii=False),flush=True)
node.create_subscription(Log,'/rosout',log,100);node.create_subscription(SpeechPlaybackStatus,'/malbut/speech/playback_status',state,100);node.create_subscription(SpeechTranscript,'/malbut/speech/transcript',transcript,100)
text='봄은 겨울의 추위가 누그러지고 새 생명이 시작되는 계절입니다. 낮이 길어지고 기온이 오르면서 꽃이 피고 나무에 새잎이 돋습니다. 여름에는 기온이 높아지고 비가 자주 내립니다. 가을이 오면 나뭇잎이 노랗고 붉게 물듭니다. 겨울에는 기온이 내려가고 눈이 내리기도 합니다.'
stage=0
try:
 while stamp()<160:
  rclpy.spin_once(node,timeout_sec=.02)
  if failure:raise RuntimeError(failure)
  if current and current['mode']=='overlap' and 'playing_at' in current and 'terminal_at' not in current and 'pc_requested_at' not in current and stamp()-current['playing_at']>=5:
   current['pc_requested_at']=stamp();print('PC_PLAY '+json.dumps(dict(index=stage,at=stamp())),flush=True);save()
  if current and ('terminal_at' in current and stamp()>current['terminal_at']+16 or stamp()>current['published_at']+65):
   current['end_at']=stamp();current['cpu_end_usec']=cpu();current['cpu_percent_total']=(current['cpu_end_usec']-current['cpu_start_usec'])/1e6/(current['end_at']-current['published_at'])/os.cpu_count()*100;current['transcripts']=[x for x in texts if x['at']>=current['published_at']];save();current=None
   if stage==2:break
  if current is None and stage<2 and aec and tts_ready and agent_ready and stamp()>ready_at+5 and (idle or stage==1):
   mode=['control','overlap'][stage];stage+=1;idle=False
   current=dict(mode=mode,pid=uuid.uuid4().hex,rid='conversation-opener:two-speaker-'+uuid.uuid4().hex,published_at=stamp(),cpu_start_usec=cpu());rows.append(current)
   m=SpeechRequest();m.playback_id=current['pid'];m.request_id=current['rid'];m.request_type=SpeechRequest.DIALOGUE;m.text=text;pub.publish(m);print('TRIAL '+mode,flush=True);save()
finally:save();node.destroy_node();rclpy.shutdown()
