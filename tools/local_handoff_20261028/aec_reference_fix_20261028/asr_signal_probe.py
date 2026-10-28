from analyze import *
from malbut_stt.echo import processor
import inspect,math
from malbut_stt.echo.neural import DTLN
from scipy.io import wavfile
s=inspect.getsource(processor.EchoProcessor).replace('self.last_voice = voice.copy()','self.last_voice = voice.copy()\n        self.last_neural = out.copy()').replace('self.last_voice = dry.copy()','self.last_voice = dry.copy()\n            self.last_neural = dry.copy()')
ns=dict(math=math,Path=Path,np=np,AudioProcessor=AudioProcessor,DTLN=DTLN,RATE=16000,HOP=128);exec(s,ns);Filter=ns['EchoProcessor']
p=ROOT/'live_aec3_neural';c=json.loads((p/'result.json').read_text())['calibration'];x=read(p/'mic.wav');r=read(p/'ref.wav');lag=round(c['lag_ms']*16)-640;ref=np.pad(r,(lag,len(x)))[:len(x)];h=read(ROOT/'human_interrupt_strong.wav');start=14*16000;near=np.zeros_like(x);near[start:start+len(h)]=h
f=Filter(c['microphone_normalization'],ROOT/'dtln');nn=[];linear=[];gated=[]
for i in range(0,len(x),128):
 y=f.process((x+near)[i:i+128],ref[i:i+128]);gated.append(y);nn.append(f.last_neural.copy());linear.append(f.last_voice.copy())
out=ROOT/'asr_signals';out.mkdir(exist_ok=True)
for name,data in [('dry',near),('linear',np.concatenate(linear)),('neural',np.concatenate(nn)),('gated',np.concatenate(gated))]:
 a=data[start:start+len(h)+8000];wavfile.write(out/(name+'.wav'),16000,(a*32768).clip(-32768,32767).astype(np.int16))
print('Prepared matched ASR inputs')
