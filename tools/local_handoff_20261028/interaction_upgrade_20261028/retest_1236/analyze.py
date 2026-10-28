from pathlib import Path
import sys,json,numpy as np
from scipy.io import wavfile
r=Path(__file__).resolve().parent; ws=r.parents[1]
sys.path[:0]=[str(ws/'aec_reference_fix_20261028/deps/x86_64'),str(ws/'src/malbut/malbut_stt')]
from malbut_stt.echo.vad import SuppressedSilenceVad
from malbut_stt.silero_stream import SileroStreamVad
from malbut_stt.audio import CaptureSettings
from malbut_stt.streaming import StreamingUtteranceCollector
s=(ws/'src/malbut/malbut_stt/malbut_stt/echo/processor.py').read_text().replace('from .neural import DTLN','from malbut_stt.echo.neural import DTLN')
s=s.replace('if float(out @ out) / HOP < self.envelope * 0.0630957344:', 'self.last_ratio = float(out @ out) / HOP / max(self.envelope, 1e-12)\n        if False:').replace('self.last_recovery = dry','self.last_recovery = dry\n        self.last_ratio = 1.0')
s=s.replace('self.asr_output = np.empty_like(near)','self.asr_output = np.empty_like(near)\n        self.ratios = np.empty_like(near)').replace('self.recovery_output[start:start + HOP] = self.last_recovery','self.recovery_output[start:start + HOP] = self.last_recovery\n            self.ratios[start:start + HOP] = self.last_ratio')
ns={};exec(compile(s,'unmasked_probe','exec'),ns);rows=[];folder=r/'audio';folder.mkdir(exist_ok=True)
for phase in ['control_80','control_50','human_80','human_50']:
 x=np.fromfile(r/(phase+'_pair.f32'),'<f4').reshape(-1,2);x=x[:len(x)//512*512];train=np.load(r/(phase+'_training.npz'))
 f=ns['EchoProcessor'](float(train['gain']),ws/'aec_reference_fix_20261028/dtln');f.process_array(train['mic'],train['far']);y=f.process_array(x[:,0],x[:,1]);row={'phase':phase,'length_s':len(x)/16000,'floors':[]}
 for floor in [-12,-16,-20,-25,-99]:
  vad=SuppressedSilenceVad(SileroStreamVad());c=StreamingUtteranceCollector(vad.is_speech,settings=CaptureSettings(pre_roll_s=2,min_speech_s=.16,silence_timeout_s=.8,max_utterance_s=40))
  z=np.where(f.ratios>10**(floor/10),y,0);events=[]
  for i in range(0,len(z),512):
   for e in c.feed((z[i:i+512]*32768).clip(-32768,32767).astype('<i2').tobytes()):
    if e.status=='speech_started':events.append(round(i/16000,3))
  row['floors'].append({'floor_db':floor,'onsets_s':events})
 if phase.startswith('human'):
  for j,begin in enumerate([0,30]):
   for name,z in [('raw',x[:,0]),('aec',y)]:
    wavfile.write(folder/(phase+'_'+str(j)+'_'+name+'.wav'),16000,(z[begin*16000:(begin+20)*16000]*32768).clip(-32768,32767).astype('<i2'))
 rows.append(row);print(json.dumps(row),flush=True)
(r/'gate_analysis.json').write_text(json.dumps(rows,indent=2))
