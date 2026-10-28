"""Analyze whole cued recordings without expected-text conditioning or VAD cropping."""
from pathlib import Path
import wave,json,sys
import numpy as np
from malbut_stt.silero_stream import SileroStreamVad
r=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent/'cued_human_gains';out=r/'wav';out.mkdir(exist_ok=True)
def analyze(x):
 a=x.astype(np.float32)/32768;v=SileroStreamVad();probs=[]
 for i in range(0,len(x)-511,512):
  v.is_speech(np.ascontiguousarray(x[i:i+512]).tobytes(),16000);probs.append(v.probability)
 return dict(duration_s=len(x)/16000,rms_dbfs=float(20*np.log10(np.sqrt(np.mean(a*a))+1e-12)),peak_dbfs=float(20*np.log10(np.max(np.abs(a))+1e-12)),clipped_fraction=float(np.mean(np.abs(a)>=32767/32768)),speech_s=float(sum(p>=.5 for p in probs)*.032),speech_max=float(max(probs,default=0)),speech_windows_s=[round(i*.032,3) for i,p in enumerate(probs) if p>=.5])
def wav(p,x):
 with wave.open(str(p),'wb') as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(np.ascontiguousarray(x).tobytes())
rows=[]
for gain in [30,5]:
 raw=np.fromfile(r/f'gain{gain}_raw8.pcm',dtype='<i2').reshape(-1,8);usb=np.fromfile(r/f'gain{gain}_usb.pcm',dtype='<i2')
 row=dict(gain=gain,raw_channels=[dict(channel=ch,**analyze(raw[:,ch])) for ch in range(6)],usb=analyze(usb));rows.append(row)
 wav(out/f'gain{gain}_raw0.wav',raw[:,0]);wav(out/f'gain{gain}_usb.wav',usb)
 print(json.dumps({k:v for k,v in row.items() if k!='raw_channels'}),flush=True)
(r/'analysis.json').write_text(json.dumps(rows,indent=2))
