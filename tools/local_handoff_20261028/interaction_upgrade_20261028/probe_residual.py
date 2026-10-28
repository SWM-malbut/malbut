from pathlib import Path
import sys,json
import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate,correlation_lags
r=Path(__file__).resolve().parent;old=r.parent/'aec_reference_fix_20261028'
sys.path[:0]=[str(old/'deps/x86_64'),str(r.parent/'src/malbut/malbut_stt')]
from malbut_stt.echo import processor
source=(r.parent/'src/malbut/malbut_stt/malbut_stt/echo/processor.py').read_text().replace('from .neural import DTLN','from malbut_stt.echo.neural import DTLN')
source=source.replace('self.last_voice = voice.copy()','self.last_voice = out.copy()')
ns={};exec(compile(source,'residual_probe','exec'),ns)
pair=np.fromfile(r/'linear_pair.f32','<f4').reshape(-1,2);j=json.loads((r/'after_linear_human_normal_1.json').read_text())
cal=json.loads(next(e[2][10:] for e in j['events'] if len(e)>2 and isinstance(e[2],str) and e[2].startswith('aec_ready:')))
f=ns['EchoProcessor'](cal['microphone_gain'],old/'dtln');y=f.process_array(pair[:,0],pair[:,1])
_,h=wavfile.read(r/'human_near_normal.wav');h=h.astype(float)/32768
c=correlate(pair[:,0],h,method='fft');lags=correlation_lags(len(pair),len(h));start=int(lags[np.argmax(c)])+512
for pre in [0,.5,1,2]:
 x=f.asr_output[start-int(pre*16000):start+len(h)+8000]
 wavfile.write(r/'recordings'/f'live_residual_{pre}.wav',16000,(x*32768).clip(-32768,32767).astype('<i2'))
