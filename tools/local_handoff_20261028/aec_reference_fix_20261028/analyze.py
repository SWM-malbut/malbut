import json
from pathlib import Path
import sys
import wave
import numpy as np
from scipy import signal
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'deps/x86_64'),str(ROOT.parent/'src/malbut/malbut_stt')]
from pywebrtc_audio import AudioProcessor
from malbut_stt.silero_stream import SileroStreamVad

def read(p):
    if p.suffix=='.sw':return np.fromfile(p,'<f4')
    with wave.open(str(p)) as w:
        assert w.getframerate()==16000 and w.getnchannels()==1
        return np.frombuffer(w.readframes(w.getnframes()),'<i2').astype('float32')/32768

def metric(x):
    vad=SileroStreamVad()
    count=run=longest=0;first=None
    for i in range(0,len(x)-320,320):
        yes=vad.is_speech((x[i:i+320]*32768).clip(-32768,32767).astype('<i2').tobytes(),16000)
        count+=yes;run=run+1 if yes else 0;longest=max(longest,run)
        if run>=8 and first is None:first=(i+320)/16000
    return dict(rms_db=float(20*np.log10(np.sqrt(np.mean(x*x))+1e-12)),
        speech_s=count*.02,longest_s=longest*.02,first_false_interrupt_s=first)

def alignment(x,r):
    cc=signal.correlate(x,r,method='fft');lags=signal.correlation_lags(len(x),len(r))
    keep=(lags>=-16000)&(lags<=32000);idx=np.argmax(abs(cc[keep]));lag=int(lags[keep][idx])
    return dict(lag_samples=lag,lag_ms=lag/16,correlation=float(cc[keep][idx]/(np.linalg.norm(x)*np.linalg.norm(r))))

def process(x,r,delay=0):
    n=min(len(x),len(r));n=n//160*160
    ap=AudioProcessor(sample_rate=16000,echo_cancellation=True,noise_suppression=True,
        high_pass_filter=True,auto_gain_control=False,stream_delay_ms=delay,ns_level=2)
    out=np.empty(n,'float32')
    for i in range(0,n,160):out[i:i+160]=ap.process(x[i:i+160].copy(),r[i:i+160].copy())
    return out

if __name__=='__main__':
    folder=ROOT/'reference'
    if folder.exists():
        x=read(folder/'aec_rec.sw');r=read(folder/'aec_play.sw')
    else:
        folder=ROOT.parent/'reachy_barge_in_20261028/robot/routed'
        x=read(folder/'raw.wav');r=np.pad(read(ROOT/'reference.wav'),(16000,len(x)))[:len(x)]
    align=alignment(x,r);print('alignment',align, 'lengths',len(x),len(r),flush=True)
    results=[dict(mode='raw',**metric(x))]
    for shift in [0, int(align['lag_samples']),max(0,int(align['lag_samples'])-800)]:
        if shift>=0:ref=np.pad(r,(shift,len(x)))[:len(x)]
        else:ref=np.pad(r[-shift:],(0,len(x)))[:len(x)]
        y=process(x,ref)
        path=ROOT/f'aec3_shift_{shift}.wav'
        with wave.open(str(path),'wb') as w:
            w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes((y*32768).clip(-32768,32767).astype('<i2').tobytes())
        row=dict(mode='aec3',shift=shift,**metric(y));results.append(row);print(row,flush=True)
    (ROOT/'analysis.json').write_text(json.dumps(dict(alignment=align,results=results),indent=2))
