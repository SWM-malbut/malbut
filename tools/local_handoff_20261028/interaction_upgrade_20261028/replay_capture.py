"""Replay identical physical echo + injected near speech without changing VAD."""
from pathlib import Path
import sys, json, wave, inspect, math
import numpy as np
from scipy.signal import correlate, correlation_lags
ROOT=Path(__file__).resolve().parent
OLD=ROOT.parent/'aec_reference_fix_20261028'
sys.path[:0]=[str(OLD/'deps/x86_64'), str(ROOT.parent/'src/malbut/malbut_stt')]
from malbut_stt.echo import processor
from malbut_stt.echo.neural import DTLN
from pywebrtc_audio import AudioProcessor
def read(p):
    with wave.open(str(p)) as w:return np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(np.float32)/32768
def write(name,x):
    p=ROOT/'variants'/f'{name}.wav';p.parent.mkdir(exist_ok=True)
    with wave.open(str(p),'wb') as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes((x*32768).clip(-32768,32767).astype('<i2').tobytes())
pair=np.fromfile(ROOT/'paired.f32','<f4').reshape(-1,2)
training=np.load(ROOT/'training.npz')
h=read(ROOT/'recordings/clean_interrupt.wav')
c=correlate(pair[:,0],h,method='fft');lags=correlation_lags(len(pair),len(h));start=int(lags[np.argmax(c)])
print('injected near starts',start/16000, 'duration',len(h)/16000, flush=True)
rows=[]
for name in ['before','tail160','blend25','blend50','raw']:
    source=inspect.getsource(processor.EchoProcessor)
    if name=='tail160':source=source.replace('(125 if', '(20 if')
    if name in ('blend25','blend50'):
        amount=.25 if name=='blend25' else .5
        source=source.replace('self.last_voice = voice.copy()',f'self.last_voice = voice * {1-amount} + dry * {amount}')
    ns=dict(math=math,Path=Path,np=np,AudioProcessor=AudioProcessor,DTLN=DTLN,RATE=16000,HOP=128)
    exec(source,ns);f=ns['EchoProcessor'](float(training['gain']),OLD/'dtln')
    f.process_array(training['mic'],training['far'])
    vad=f.process_array(pair[:,0],pair[:,1])
    y=f.asr_output if name!='raw' else np.pad(pair[:,0],(512,0))[:len(pair)]
    begin=max(0,start-3200);end=start+len(h)+12800
    write(name,y[begin:end]);write(name+'_vad',vad[begin:end])
    rows.append(dict(mode=name,start_s=start/16000,begin_s=begin/16000,end_s=end/16000))
(ROOT/'variants/manifest.json').write_text(json.dumps(rows,indent=2))
