from analyze import *
from dtln_filter import DTLN
import json
rows=[]
for folder in ['live_aec3_residual','live_aec3_neural','live_calibrated']:
 p=ROOT/folder;c=json.loads((p/'result.json').read_text());x=read(p/'mic.wav');r=read(p/'ref.wav');cal=c['calibration'];lag=round(cal['lag_ms']*16);gain=cal.get('microphone_normalization',1)
 for offset in [0,128,256]:
  rr=np.pad(r,(lag-offset,len(x)))[:len(x)];f=DTLN(ROOT/'dtln');out=[];hold=0;env=0;dry=np.zeros(384,np.float32)
  for i in range(0,min(len(x),len(rr))-128,128):
   a=x[i:i+128];b=rr[i:i+128];d=dry[:128].copy();dry[:-128]=dry[128:];dry[-128:]=a;env=max(np.mean(d*d),env*.92)
   hold=125 if np.mean(b*b)>1e-8 else max(0,hold-1)
   y=f.process(a/gain,b)*gain
   if hold and np.mean(y*y)<env*.01:y.fill(0)
   if not hold:y=d
   out.append(y)
  y=np.concatenate(out);v=SileroStreamVad();run=0;hits=[]
  for i in range(0,len(y)-320,320):
   pcm=(y[i:i+320]*32768).clip(-32768,32767).astype('<i2').tobytes();yes=v.is_speech(pcm,16000) and any(pcm);run=run+1 if yes else 0
   if run==8:hits.append((i+320)/16000)
  start=c['statuses'][0][0] if c['statuses'] else 11
  row=dict(folder=folder,offset=offset,hits=hits,post_start=[h for h in hits if h>start]);rows.append(row);print(row,flush=True)
(ROOT/'single_stage.json').write_text(json.dumps(rows,indent=2))
