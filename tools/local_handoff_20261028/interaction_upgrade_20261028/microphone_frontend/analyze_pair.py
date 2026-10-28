"""Prepare paired microphone diagnostics; no recognition prompt or API call."""
import json,sys,wave
from pathlib import Path
import numpy as np
from malbut_stt.silero_stream import SileroStreamVad
folder=Path(sys.argv[1]);meta=json.loads((folder/'metadata.json').read_text())
raw=np.fromfile(folder/'array_raw_8ch.pcm',dtype='<i2').reshape(-1,8)
usb=np.fromfile(folder/'usb_processed.pcm',dtype='<i2')
offset=(meta['raw_bytes_at_usb_start']-meta['raw_prefix_removed_bytes'])/(16000*8*2)
def activity(x):
 v=SileroStreamVad();out=[]
 for i in range(0,len(x)-511,512):
  v.is_speech(np.ascontiguousarray(x[i:i+512]).tobytes(),16000);out.append(v.probability)
 return np.array(out)
def summary(x):
 a=x.astype(float)/32768
 return dict(rms_dbfs=float(20*np.log10(max(np.sqrt(np.mean(a*a)),1e-10))),peak_dbfs=float(20*np.log10(max(abs(a).max(),1e-10))),clipped_fraction=float(np.mean(abs(a)>.999)))
probs=[activity(raw[:,i]) for i in range(6)];up=activity(usb)
active=np.maximum.reduce(probs)>=.5
points=np.flatnonzero(active);groups=[]
for i in points:
 t=i*.032
 if not groups or t-groups[-1][1]>.7:groups.append([t,t+.032,1])
 else:groups[-1][1]=t+.032;groups[-1][2]+=1
# Preserve independently detected speech clusters; no use of expected text.
windows=[(max(0,a-1),min(len(raw)/16000,b+1),n) for a,b,n in groups if n*.032>=.16]
clips=folder/'clips';clips.mkdir(exist_ok=True)
def wav(path,x,channels=1):
 with wave.open(str(path),'wb') as w:
  w.setparams((channels,2,16000,0,'NONE','not compressed'));w.writeframes(np.ascontiguousarray(x).tobytes())
rows=[]
for idx,(a,b,n) in enumerate(windows):
 if b-a>20:continue
 start,end=int(a*16000),int(b*16000)
 prefix=f'clip{idx:02d}'
 (clips/(prefix+'_array8.pcm')).write_bytes(np.ascontiguousarray(raw[start:end]).tobytes())
 for ch in range(6):wav(clips/(prefix+f'_raw{ch}.wav'),raw[start:end,ch])
 ua=max(0,a-offset-1);ub=min(len(usb)/16000,b-offset+1)
 if ub>ua:wav(clips/(prefix+'_usb.wav'),usb[int(ua*16000):int(ub*16000)])
 rows.append(dict(id=prefix,raw_start_s=a,raw_end_s=b,speech_frames=n,usb_start_s=ua,usb_end_s=ub))
result=dict(raw_duration_s=len(raw)/16000,usb_duration_s=len(usb)/16000,raw_offset_at_usb_start_s=offset,alignment='Capture-start estimate with extra USB padding; no latency inference',raw_channels=[dict(channel=i,**summary(raw[:,i]),speech_s=float(np.sum(probs[i]>=.5)*.032) if i<6 else None) for i in range(8)],usb=dict(**summary(usb),speech_s=float(np.sum(up>=.5)*.032)),windows=rows,all_candidate_windows=windows)
(folder/'analysis.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
