"""Streaming DTLN-AEC adapter (16 kHz, 128-sample hops).

Algorithm adapted from breizhn/DTLN-aec/run_aec.py, MIT licensed.
Pinned upstream revision and model hashes are in models.json; the upstream
MIT license is included in DTLN_LICENSE.
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
