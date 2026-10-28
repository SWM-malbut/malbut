from analyze import *
from malbut_stt.echo.vad import SuppressedSilenceVad
from malbut_stt.echo.neural import DTLN
import math
source=(ROOT/'intelligible_processor_candidate.py').read_text()
rows=[]
def evaluate(y):
 v=SuppressedSilenceVad(SileroStreamVad());run=0;hits=[]
 for i in range(0,len(y)-320,320):
  p=(y[i:i+320]*32768).clip(-32768,32767).astype('<i2').tobytes();run=run+1 if v.is_speech(p,16000) else 0
  if run==8:hits.append((i+320)/16000)
 return hits
for db in [20,23,26,29]:
 ns={};exec(source.replace('self.envelope * 0.01',f'self.envelope * {10**(-db/10)}'),ns);Filter=ns['EchoProcessor']
 for folder in ['live_aec3_residual','live_aec3_neural','live_aec3_calibrated']:
  p=ROOT/folder;c=json.loads((p/'result.json').read_text());x=read(p/'mic.wav');r=read(p/'ref.wav');cal=c['calibration'];lag=round(cal['lag_ms']*16)-640;rr=np.pad(r,(lag,len(x)))[:len(x)]
  for mix in [False,True] if folder=='live_aec3_neural' else [False]:
   xx=x.copy()
   if mix:
    h=read(ROOT/'human_interrupt.wav');start=14*16000;n=min(len(h),len(x)-start);scale=np.sqrt(np.mean(x[start:start+n]**2)/(np.mean(h[:n]**2)+1e-12))*.5;xx[start:start+n]+=h[:n]*scale
   f=Filter(cal['microphone_normalization'],ROOT/'dtln');y=f.process_array(xx,rr);hits=evaluate(y);row=dict(db=db,folder=folder,mix=mix,hits=[h for h in hits if h>c['statuses'][0][0]]);rows.append(row);print(row,flush=True)
(ROOT/'confidence_grid.json').write_text(json.dumps(rows,indent=2))
