"""Live reference aligned AEC with actual Silero and production playback."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
from queue import Queue,Empty
import subprocess
import sys
from threading import Thread,Event
import time
from types import SimpleNamespace
import wave

os.umask(0o077);root=Path(__file__).resolve().parent
sys.path[:0]=[str(root/'deps'),str(root/'probe_code')]
import numpy as np
import sounddevice as sd
from pulse_capture import Capture
from pywebrtc_audio import AudioProcessor
from dtln_filter import DTLN
from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.silero_stream import SileroStreamVad
from malbut_tts.audio import StreamingPlayer
from malbut_tts.runtime import SpeechRuntime

parser=argparse.ArgumentParser();parser.add_argument('--mode',default='improved',choices=['legacy','improved','raw_reachy']);parser.add_argument('--name',default='live');parser.add_argument('--delay-ms',type=float,default=380);parser.add_argument('--observe',action='store_true');parser.add_argument('--calibrate',action='store_true');parser.add_argument('--neural',action='store_true');parser.add_argument('--audio',default='reference.wav');args=parser.parse_args()
def listing(kind):return json.loads(subprocess.check_output(['pactl','--format=json','list',kind],text=True))
assert not listing('sink-inputs') and not listing('source-outputs'),'Audio in use'
source=next(x['name'] for x in listing('sources') if 'iflytek_XFM' in x['name'])
sink=next(x['name'] for x in listing('sinks') if 'GeneralPlus' in x['name'])
os.environ['PULSE_SINK']=sink
with wave.open(str(root/args.audio)) as w:
    reference=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype('float32')/32768
with wave.open(str(root/'reference.wav')) as w:
    calibration_audio=np.frombuffer(w.readframes(6*16000),'<i2').astype('float32')/32768
class Prerecorded:
    def generate(self,text,cancel):
        audio=calibration_audio if text=='calibrate' else reference
        for i in range(0,len(audio),320):
            if cancel.is_set():return
            yield audio[i:i+320],16000

events=Queue(maxsize=2048);stopping=Event();origin=time.monotonic();cpu0=time.process_time()
errors=[];recorded={n:[] for n in ['mic','ref','processed']};offsets={'mic':deque(maxlen=125),'ref':deque(maxlen=125)}
controls=[];statuses=[];reports=[];anchors=None;aligned_reference=[];nframes={'mic':0,'ref':0};route=None
def stamp():return round(time.monotonic()-origin,4)
def capture(name,device):
    cap=None
    try:
        cap=Capture(device);i=0
        while not stopping.is_set():
            x,t,lat=cap.read();events.put((name,i,x,t),timeout=.2);i+=1
    except Exception as e:errors.append(type(e).__name__+': '+str(e));stopping.set()
    finally:
        if cap:cap.close()
def on_status(pid,state,interim,rid):events.put(('status',pid,state,interim,rid,stamp()))
runtime=SpeechRuntime(Prerecorded(),lambda **kw:StreamingPlayer(device='pulse',**kw),on_status)
def control(pid,cmd):
    at=stamp();ok=False if args.observe else runtime.control(pid,cmd);controls.append(dict(at_s=at,command=cmd,accepted=ok,returned_at_s=stamp()))
vad=SileroStreamVad()
p=DialoguePipeline(recorder_factory=lambda:None,wake=None,transcriber=SimpleNamespace(transcribe=lambda *_:'unused'),
    is_speech=lambda pcm,rate:vad.is_speech(pcm,rate) and (args.mode!='improved' or any(pcm)),publish_transcript=lambda *_:None,publish_control=control,publish_interruption=lambda *_:None,
    report=lambda e:reports.append([stamp(),e]),input_has_aec=args.mode!='legacy',
    barge_in_mode='legacy' if args.mode=='legacy' else 'reachy',settings=CaptureSettings(min_speech_s=.16,silence_timeout_s=2.0))
p.command_stream.early_endpoint_s=None;p._complete_capture=lambda *_:None
aec=AudioProcessor(sample_rate=16000,echo_cancellation=True,noise_suppression=True,high_pass_filter=True,auto_gain_control=False,ns_level=2)
aec_gain=1.0;pending_mic=np.empty(0,np.float32);pending_ref=np.empty(0,np.float32);aec_output=np.zeros(128,np.float32)
neural=DTLN(root) if args.neural else None
dry_delay=np.zeros(512 if neural else 128,np.float32);far_delay=np.zeros(128,np.float32);input_envelope=0.0;reference_hold=0
def filter_audio(near,far):
    global pending_mic,pending_ref,aec_output,dry_delay,far_delay,input_envelope,reference_hold
    pending_mic=np.concatenate([pending_mic,near]);pending_ref=np.concatenate([pending_ref,far])
    while len(pending_mic)>=160:
        out=aec.process((pending_mic[:160]/aec_gain).astype('float32'),pending_ref[:160].copy())*aec_gain
        aec_output=np.concatenate([aec_output,out]);pending_mic=pending_mic[160:];pending_ref=pending_ref[160:]
    out=aec_output[:128].copy();aec_output=aec_output[128:]
    assert len(out)==128
    if neural:
        out=neural.process(out,far_delay)
        far_delay=far.copy()
    dry=dry_delay[:128].copy();dry_delay[:-128]=dry_delay[128:];dry_delay[-128:]=near
    input_envelope=max(float(np.mean(dry*dry)),input_envelope*.92)
    reference_hold=125 if np.mean(far*far)>1e-8 else max(0,reference_hold-1)
    if reference_hold and np.mean(out*out)<input_envelope*.01:out.fill(0)
    if not reference_hold:out=dry
    return out
warm=sd.OutputStream(device='pulse',samplerate=16000,channels=1,blocksize=160,latency='low',callback=lambda out,*_:out.fill(0))
warm.start();threads=[Thread(target=capture,args=(n,d),daemon=True) for n,d in [('mic',source),('ref',sink+'.monitor')]]
for t in threads:t.start()
ref_blocks={};submitted=False;terminal=None;processing_times=[];calibration_started=False;calibration_done=None;calibration=None
try:
    print('Warmup and live trial',args.mode,flush=True)
    while stamp()<len(reference)/16000+22 and not stopping.is_set():
        if not submitted and stamp()>=3 and min(nframes.values())>=250:
            anchors={n:float(np.median(v)) for n,v in offsets.items()}
            if args.calibrate and not calibration_started:
                runtime.submit('calibrate',playback_id='calibration',request_id='calibration');calibration_started=True
            if args.calibrate and calibration_done is not None and stamp()-calibration_done>1:
                x=np.concatenate(recorded['mic']);r=np.concatenate(recorded['ref']);n=1<<int(np.ceil(np.log2(len(x)+len(r))))
                corr=np.fft.irfft(np.fft.rfft(x,n)*np.conj(np.fft.rfft(r,n)),n)
                lo,hi=1600,16000;lag=lo+int(np.argmax(abs(corr[lo:hi])))
                confidence=float(abs(corr[lag])/np.sqrt(np.sum(x*x)*np.sum(r*r)+1e-12))
                delay=lag/16000+anchors['mic']-anchors['ref']-.04
                assert confidence>.1 and .05<delay<.9,'Unreliable echo calibration'
                args.delay_ms=delay*1000;calibration=dict(lag_ms=lag/16,confidence=confidence,delay_ms=args.delay_ms)
                aec_gain=float(np.clip(np.sqrt(np.mean(x*x)/(np.mean(r*r)+1e-12)),1,8))
                rr=np.pad(r,(max(0,lag-640),len(x)))[:len(x)]
                for j in range(0,min(len(x),len(rr))-128,128):filter_audio(x[j:j+128],rr[j:j+128])
                calibration['microphone_normalization']=aec_gain
                print('Measured calibration',calibration,flush=True)
            if not args.calibrate or calibration is not None:
                print('Clock anchor difference ms',(anchors['mic']-anchors['ref'])*1000,flush=True)
                p._publish_transcript('probe-request','기존 답변')
                runtime.submit('prerecorded',playback_id='probe-answer',request_id='probe-request');submitted=True
        if terminal is not None and stamp()-terminal>1:break
        try:event=events.get(timeout=.1)
        except Empty:continue
        if event[0]=='status':
            _,pid,state,interim,rid,at=event
            if pid=='calibration':
                if state=='finished':calibration_done=stamp()
                elif state in ('failed','stopped'):raise RuntimeError('Calibration playback failed')
                continue
            statuses.append([at,state]);p.on_playback_status(pid,state,interim=interim,request_id=rid)
            if state=='playing':route={k:listing(k) for k in ('sources','sinks','sink-inputs','source-outputs')}
            if state in ('finished','stopped','failed'):terminal=stamp()
            continue
        name,index,x,t=event;nframes[name]+=1;recorded[name].append(x);offsets[name].append(t-index*.008)
        if name=='ref':
            ref_blocks[index]=x
            for old in list(ref_blocks):
                if old<index-500:del ref_blocks[old]
            continue
        if not submitted:
            recorded['processed'].append(x);continue
        delta=round((anchors['mic']-anchors['ref']-args.delay_ms/1000)*16000)
        sample=index*128+delta;block,offset=divmod(sample,128)
        if block<0:ref=np.zeros(128,np.float32)
        elif block in ref_blocks and block+1 in ref_blocks:
            ref=np.concatenate([ref_blocks[block],ref_blocks[block+1]])[offset:offset+128]
        else:raise RuntimeError('Reference gap: refusing unreferenced interruption')
        begin=time.monotonic();clean=filter_audio(x,ref) if args.mode=='improved' else x
        processing_times.append(time.monotonic()-begin);recorded['processed'].append(clean);aligned_reference.append(ref)
        p.feed((clean*32768).clip(-32768,32767).astype('<i2').tobytes())
finally:
    stopping.set();runtime.close();p.close()
    for t in threads:t.join(timeout=2)
    warm.stop();warm.close()
    out=root/args.name;out.mkdir(exist_ok=True)
    for n,frames in recorded.items():
        with wave.open(str(out/(n+'.wav')),'wb') as w:
            w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes((np.concatenate(frames)*32768).clip(-32768,32767).astype('<i2').tobytes())
    result=dict(mode=args.mode,delay_ms=args.delay_ms,calibration=calibration,anchors=anchors,controls=controls,statuses=statuses,reports=reports,
                errors=errors,threads_stopped=not any(t.is_alive() for t in threads),routes=route,
                cpu_seconds=time.process_time()-cpu0,wall_seconds=stamp(),
                observation_only=args.observe,neural=args.neural,audio=args.audio,
                processing_ms=(1000*np.quantile(processing_times,[.5,.95,.99,1])).tolist() if processing_times else [])
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['mode','controls','statuses','errors','cpu_seconds','wall_seconds']}),flush=True)
