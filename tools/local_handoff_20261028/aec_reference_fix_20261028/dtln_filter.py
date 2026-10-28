"""Streaming DTLN-AEC evaluation adapter (16 kHz, 128-sample hops).

Algorithm adapted from breizhn/DTLN-aec/run_aec.py, MIT licensed.
Pinned upstream revision and license are stored with the evaluation models.
"""
from pathlib import Path
import numpy as np
from tflite_runtime.interpreter import Interpreter

class DTLN:
    def __init__(self, models, size=128):
        self.models=[]
        for part in (1,2):
            i=Interpreter(model_path=str(Path(models)/f'dtln_aec_{size}_{part}.tflite'),num_threads=1)
            i.allocate_tensors();inputs=i.get_input_details();outputs=i.get_output_details()
            self.models.append((i,inputs,outputs,np.zeros(inputs[1]['shape'],np.float32)))
        self.mic=np.zeros(512,np.float32);self.ref=np.zeros(512,np.float32);self.out=np.zeros(512,np.float32)
        silence=np.zeros(128,np.float32)
        for _ in range(256):self.process(silence,silence)
        self.warm_states=[state.copy() for _,_,_,state in self.models]
        self.reset()

    def reset(self):
        for (_,_,_,state),warm in zip(self.models,self.warm_states):state[:]=warm
        self.mic.fill(0);self.ref.fill(0);self.out.fill(0)

    def process(self,near,far):
        assert near.shape==far.shape==(128,)
        self.mic[:-128]=self.mic[128:];self.mic[-128:]=near
        self.ref[:-128]=self.ref[128:];self.ref[-128:]=far
        xf=np.fft.rfft(self.mic).astype(np.complex64)
        values=[np.abs(xf).reshape(1,1,-1).astype(np.float32),
                np.abs(np.fft.rfft(self.ref)).reshape(1,1,-1).astype(np.float32)]
        i,ins,outs,state=self.models[0]
        for detail,val in zip(ins,[values[0],state,values[1]]):i.set_tensor(detail['index'],val)
        i.invoke();mask=i.get_tensor(outs[0]['index']);state[:]=i.get_tensor(outs[1]['index'])
        estimate=np.fft.irfft(xf*mask).reshape(1,1,-1).astype(np.float32)
        i,ins,outs,state=self.models[1]
        for detail,val in zip(ins,[estimate,state,self.ref.reshape(1,1,-1)]):i.set_tensor(detail['index'],val)
        i.invoke();block=i.get_tensor(outs[0]['index']);state[:]=i.get_tensor(outs[1]['index'])
        self.out[:-128]=self.out[128:];self.out[-128:]=0;self.out+=block.reshape(-1)
        return self.out[:128].copy()

class ResidualAEC:
    """AEC residual attenuation, preserving input when no render reference exists."""
    def __init__(self,models,size=128,rejection_db=20):
        self.filter=DTLN(models,size);self.delay=np.zeros(384,np.float32)
        self.hold=0;self.ratio=10**(-rejection_db/10)
    def process(self,near,far):
        dry=self.delay[:128].copy();self.delay[:-128]=self.delay[128:];self.delay[-128:]=near
        if np.mean(far*far)>1e-8:self.hold=125
        else:self.hold=max(0,self.hold-1)
        if not self.hold:
            self.filter.reset()
            return dry
        clean=self.filter.process(near,far)
        if np.mean(clean*clean)<np.mean(dry*dry)*self.ratio:
            return np.zeros(128,np.float32)
        return clean

def apply(near,far,models,size=128):
    f=DTLN(models,size);n=min(len(near),len(far))//128*128;y=np.empty(n,np.float32)
    for i in range(0,n,128):y[i:i+128]=f.process(near[i:i+128],far[i:i+128])
    return y

def apply_residual(near,far,models):
    f=ResidualAEC(models);n=min(len(near),len(far))//128*128;y=np.empty(n,np.float32)
    for i in range(0,n,128):y[i:i+128]=f.process(near[i:i+128],far[i:i+128])
    return y
