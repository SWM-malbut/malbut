from analyze import *
from malbut_stt.echo import processor
from malbut_stt.echo.vad import SuppressedSilenceVad
import inspect,types
# Evaluate the same linear filter and clocks, replacing neural suppression with
# an equal delay, so only suppression and speech quality differ.
class Delay:
 def __init__(self,*_):self.reset()
 def reset(self):self.buffer=np.zeros(384,np.float32)
 def process(self,near,far):
  y=self.buffer[:128].copy();self.buffer[:-128]=self.buffer[128:];self.buffer[-128:]=near;return y
source=inspect.getsource(processor.EchoProcessor).replace('self.envelope * 0.01','self.envelope * self.ratio')
ns=dict(np=np,math=__import__('math'),Path=Path,AudioProcessor=AudioProcessor,DTLN=Delay,RATE=16000,HOP=128);exec(source,ns);Linear=ns['EchoProcessor']
def hits(y):
 v=SuppressedSilenceVad(SileroStreamVad());run=0;out=[]
 for i in range(0,len(y)-320,320):
  pcm=(y[i:i+320]*32768).clip(-32768,32767).astype('<i2').tobytes();run=run+1 if v.is_speech(pcm,16000) else 0
  if run==8:out.append((i+320)/16000)
 return out
rows=[]
for folder in ['live_aec3_residual','live_aec3_neural','live_aec3_calibrated','live_calibrated']:
 p=ROOT/folder;c=json.loads((p/'result.json').read_text());x=read(p/'mic.wav');r=read(p/'ref.wav');cal=c['calibration'];lag=round(cal['lag_ms']*16)-640;rr=np.pad(r,(lag,len(x)))[:len(x)]
 for db in [20,18,16,14]:
  f=Linear(cal['microphone_normalization'],'unused');f.ratio=10**(-db/10);y=f.process_array(x,rr);h=hits(y);start=c['statuses'][0][0];row=dict(folder=folder,db=db,hits=[v for v in h if v>start]);rows.append(row);print(row,flush=True)
(ROOT/'linear_quality.json').write_text(json.dumps(rows,indent=2))
