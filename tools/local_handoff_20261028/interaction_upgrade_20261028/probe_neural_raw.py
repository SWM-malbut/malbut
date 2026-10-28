from pathlib import Path
import sys,numpy as np
from scipy.io import wavfile
from scipy.signal import correlate,correlation_lags
r=Path(__file__).resolve().parent;old=r.parent/'aec_reference_fix_20261028'
sys.path.insert(0,str(r.parent/'src/malbut/malbut_stt'))
from malbut_stt.echo.neural import DTLN
pair=np.fromfile(r/'linear_pair.f32','<f4').reshape(-1,2)
_,h=wavfile.read(r/'human_near_normal.wav');h=h.astype(float)/32768
c=correlate(pair[:,0],h,method='fft');lags=correlation_lags(len(pair),len(h));start=int(lags[np.argmax(c)])
begin=(start-80000)//128*128;end=(start+len(h)+16000)//128*128
for shift in [0,320,640,960]:
 for gain in [1,2]:
  f=DTLN(old/'dtln');out=[]
  for i in range(begin,end,128):out.append(f.process(pair[i:i+128,0]/gain,pair[i-shift:i-shift+128,1])*gain)
  y=np.concatenate(out);a=start-begin-16000+384;b=start-begin+len(h)+8000+384
  wavfile.write(r/'recordings'/f'neural_raw_s{shift}_g{gain}.wav',16000,(y[a:b]*32768).clip(-32768,32767).astype('<i2'))
