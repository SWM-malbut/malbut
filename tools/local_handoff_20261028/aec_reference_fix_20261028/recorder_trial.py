import os,sys,json,time,wave,argparse
from pathlib import Path
from queue import Queue,Empty
from types import SimpleNamespace
root=Path(__file__).resolve().parent
sys.path[:0]=[str(root/'deps'),str(root/'production_code')]
import numpy as np
from malbut_stt.echo.recorder import EchoCancelledRecorder, IsolatedMicrophoneRecorder
from malbut_stt.audio import SoundDeviceRecorder,CaptureSettings
from malbut_stt.silero_stream import SileroStreamVad
from malbut_stt.echo.vad import SuppressedSilenceVad
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_tts.audio import StreamingPlayer
from malbut_tts.runtime import SpeechRuntime

def main():
 import subprocess
 p=argparse.ArgumentParser();p.add_argument('--name',default='production');p.add_argument('--audio',default='heldout.wav');p.add_argument('--legacy',action='store_true');p.add_argument('--neural-only',action='store_true');p.add_argument('--fault',action='store_true');p.add_argument('--idle',type=float,default=3);args=p.parse_args()
 listing=lambda k:json.loads(subprocess.check_output(['pactl','--format=json','list',k],text=True))
 source=next(s['name'] for s in listing('sources') if 'iflytek_XFM' in s['name']);sink=next(s['name'] for s in listing('sinks') if 'GeneralPlus' in s['name'])
 os.environ.update(PULSE_SOURCE=source,MALBUT_SHARED_MICROPHONE=source,PULSE_SINK=sink)
 config=dict(source=source,sink=sink,models=str(root),calibration_audio=str(root/'calibration.wav'))
 if args.neural_only:config.update(linear=False,reference_lead_s=.016)
 raw=lambda:SoundDeviceRecorder(device_index=0)
 recorder=raw() if args.legacy else EchoCancelledRecorder(config,lambda:IsolatedMicrophoneRecorder(source))
 with wave.open(str(root/args.audio)) as w:reference=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(np.float32)/32768
 class Synth:
  def generate(self,text,cancel):
   for i in range(0,len(reference),320):
    if cancel.is_set():return
    yield reference[i:i+320],16000
 origin=time.monotonic();stamp=lambda:time.monotonic()-origin;events=Queue();controls=[];statuses=[];reports=[];samples=[];cpu=[]
 runtime=SpeechRuntime(Synth(),lambda **kw:StreamingPlayer(device='pulse',**kw),lambda *a:events.put(a))
 def control(pid,cmd):controls.append([stamp(),cmd,runtime.control(pid,cmd)])
 vad=SileroStreamVad() if args.legacy else SuppressedSilenceVad(SileroStreamVad())
 pipeline=DialoguePipeline(recorder_factory=lambda:recorder,wake=None,transcriber=SimpleNamespace(transcribe=lambda *_:'unused'),is_speech=vad.is_speech,publish_transcript=lambda *_:None,publish_control=control,publish_interruption=lambda *_:None,report=lambda e:reports.append([stamp(),e]),input_has_aec=not args.legacy,barge_in_mode='legacy' if args.legacy else 'reachy',settings=CaptureSettings(min_speech_s=.16,silence_timeout_s=2))
 pipeline._complete_capture=lambda *_:None
 submitted=False;terminal=None;faulted=False;ready=None;error=None
 def usage(pid):
  try:
   a=Path(f'/proc/{pid}/stat').read_text().split();return sum(float(a[i]) for i in [13,14])/os.sysconf('SC_CLK_TCK')
  except (OSError,IndexError):return 0
 try:
  pipeline.start();ready=stamp();print('ready',recorder.__dict__.get('aec_ready'),recorder.__dict__.get('failure'),flush=True)
  while stamp()<ready+args.idle+len(reference)/16000+5:
   if not submitted and stamp()>ready+args.idle:
    pipeline._publish_transcript('request','답변');runtime.submit('audio',playback_id='answer',request_id='request');submitted=True
   if args.fault and submitted and not faulted and stamp()>ready+args.idle+2:
    recorder._process.kill();faulted=True
   pipeline.poll()
   if not events.empty():
    pid,state,interim,rid=events.get();statuses.append([stamp(),state]);pipeline.on_playback_status(pid,state,interim=interim,request_id=rid)
    if state in ['finished','failed','stopped']:terminal=stamp()
   if terminal and stamp()>terminal+1:break
   if not cpu or stamp()-cpu[-1][0]>.5:
    child=getattr(recorder,'_process',None);cpu.append([stamp(),usage(os.getpid()),usage(child.pid) if child else 0])
   time.sleep(.005)
 except Exception as e:error=type(e).__name__+': '+str(e)
 finally:
  runtime.close();pipeline.close()
 result=dict(legacy=args.legacy,ready_s=ready,aec_ready=getattr(recorder,'aec_ready',None),failure=getattr(recorder,'failure',None),calibration=getattr(recorder,'calibration',None),controls=controls,statuses=statuses,reports=reports,cpu=cpu,error=error,fault=args.fault,wall_s=stamp(),child_alive=bool(getattr(recorder,'_process',None) and recorder._process.is_alive()))
 (root/(args.name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:result[k] for k in ['controls','statuses','error','failure','child_alive']}),flush=True)
if __name__=='__main__':main()
