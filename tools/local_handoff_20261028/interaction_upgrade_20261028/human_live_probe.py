from pathlib import Path
import sys,json,numpy as np
from scipy.io import wavfile
r=Path(__file__).resolve().parent;old=r.parent/'aec_reference_fix_20261028'
sys.path[:0]=[str(old/'deps/x86_64'),str(r.parent/'src/malbut/malbut_stt')]
from malbut_stt.echo.neural import DTLN
from malbut_stt.echo.vad import SuppressedSilenceVad
from malbut_stt.silero_stream import SileroStreamVad
from malbut_stt.audio import CaptureSettings
from malbut_stt.streaming import StreamingUtteranceCollector
s=(r.parent/'src/malbut/malbut_stt/malbut_stt/echo/processor.py').read_text().replace('from .neural import DTLN','from malbut_stt.echo.neural import DTLN')
s=s.replace('if float(out @ out) / HOP < self.envelope * 0.0630957344:', 'self.last_ratio = float(out @ out) / HOP / max(self.envelope, 1e-12)\n        if False:')
s=s.replace('self.last_recovery = dry','self.last_recovery = dry\n        self.last_ratio = 1.0')
s=s.replace('self.asr_output = np.empty_like(near)','self.asr_output = np.empty_like(near)\n        self.ratios = np.empty_like(near)')
s=s.replace('self.recovery_output[start:start + HOP] = self.last_recovery','self.recovery_output[start:start + HOP] = self.last_recovery\n            self.ratios[start:start + HOP] = self.last_ratio')
ns={};exec(compile(s,'unmasked_probe','exec'),ns)
x=np.fromfile(r/'human_live_pair.f32','<f4').reshape(-1,2);x=x[:len(x)//512*512]
train=np.load(r/'human_live_training.npz');f=ns['EchoProcessor'](float(train['gain']),old/'dtln');f.process_array(train['mic'],train['far']);y=f.process_array(x[:,0],x[:,1])
rows=[]
for floor in [-12,-16,-20,-25,-99]:
 vad=SuppressedSilenceVad(SileroStreamVad());c=StreamingUtteranceCollector(vad.is_speech,settings=CaptureSettings(pre_roll_s=2,min_speech_s=.16,silence_timeout_s=.8,max_utterance_s=40))
 z=np.where(f.ratios>10**(floor/10),y,0)
 events=[]
 for i in range(0,len(z),512):
  for e in c.feed((z[i:i+512]*32768).clip(-32768,32767).astype('<i2').tobytes()):
   if e.status=='speech_started':events.append(round(i/16000,3))
 rows.append(dict(floor_db=floor,onsets_s=events));print(rows[-1],flush=True)
folder=r/'human_live';folder.mkdir(exist_ok=True)
for name,z in [('raw',x[:,0]),('aec',y)]:wavfile.write(folder/(name+'.wav'),16000,(z[:25*16000]*32768).clip(-32768,32767).astype('<i2'))
n=DTLN(old/'dtln');z=np.concatenate([n.process(x[i:i+128,0],x[i:i+128,1]) for i in range(0,len(x),128)])
wavfile.write(folder/'neural_raw.wav',16000,(z[:25*16000]*32768).clip(-32768,32767).astype('<i2'))
(r/'human_live_gate.json').write_text(json.dumps(rows,indent=2))
