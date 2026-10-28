import json,math,time
from pathlib import Path
import numpy as np
from scipy.signal import resample_poly
from scipy.io import wavfile
from analyze import ROOT,read,alignment,SileroStreamVad
from malbut_stt.echo.processor import EchoProcessor
from malbut_stt.echo.vad import SuppressedSilenceVad
folder=ROOT/'live_aec3_neural';x=read(folder/'mic.wav');r=read(folder/'ref.wav')
c=json.loads((folder/'result.json').read_text())['calibration'];lag=round(c['lag_ms']*16)-640
ref=np.pad(r,(lag,len(x)))[:len(x)]
manifest=json.loads((ROOT.parent/'reachy_start_vs_silero_20261027/evaluation_manifest.json').read_text())
cases=[c for c in manifest['cases'] if c['label_speech'] and c['transform']=='original']
def onset(y):
 v=SuppressedSilenceVad(SileroStreamVad());run=0;hits=[]
 for i in range(0,len(y)-320,320):
  pcm=(y[i:i+320]*32768).clip(-32768,32767).astype('<i2').tobytes();yes=v.is_speech(pcm,16000) and any(pcm)
  run=run+1 if yes else 0
  if run==8:hits.append(round((i+320)/16000,3))
 return hits
rows=[];begin=time.time()
for case in cases:
 rate,near=wavfile.read(case['path']);near=near.astype(np.float32)/32768
 if near.ndim==2:near=near.mean(1)
 if rate!=16000:near=resample_poly(near,16000//math.gcd(rate,16000),rate//math.gcd(rate,16000))
 for ser in [0,-6,-12]:
  start=round(14*16000);n=min(len(near),len(x)-start);near_clip=near[:n]
  scale=np.sqrt(np.mean(x[start:start+n]**2)/(np.mean(near_clip**2)+1e-12))*10**(ser/20)
  mix=x.copy();mix[start:start+n]+=near_clip*scale
  processor=EchoProcessor(gain=c['microphone_normalization'],neural_models=ROOT/'dtln')
  y=processor.process_array(mix,ref);hits=onset(y)
  row=dict(id=case['id'],ser_db=ser,onsets_s=hits,detected=any(14<=h<14+n/16000+1 for h in hits),false_before=any(11.9<h<14 for h in hits))
  rows.append(row);print(row,flush=True)
(ROOT/'final_dual_doubletalk.json').write_text(json.dumps(dict(cases=rows,seconds=time.time()-begin),indent=2))
