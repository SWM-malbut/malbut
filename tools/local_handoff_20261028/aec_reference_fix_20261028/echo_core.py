"""Calibrated AEC3 and optional small residual echo model; pure audio boundary."""
import math
from pathlib import Path
import numpy as np
from pywebrtc_audio import AudioProcessor
from dtln_filter import DTLN

RATE=16000
HOP=128

def calibrate(microphone,reference,anchor_difference=0.0):
    x=np.asarray(microphone,dtype=np.float32);r=np.asarray(reference,dtype=np.float32)
    if x.ndim!=1 or r.ndim!=1 or min(len(x),len(r))<RATE*2:
        raise ValueError('Calibration requires at least two seconds of mono audio')
    if not np.isfinite(x).all() or not np.isfinite(r).all():raise ValueError('Invalid calibration PCM')
    n=1<<int(np.ceil(np.log2(len(x)+len(r))))
    corr=np.fft.irfft(np.fft.rfft(x,n)*np.conj(np.fft.rfft(r,n)),n)
    lo,hi=1600,16000
    lag=lo+int(np.argmax(abs(corr[lo:hi])))
    confidence=float(abs(corr[lag])/np.sqrt(np.sum(x*x)*np.sum(r*r)+1e-12))
    delay=lag/RATE+anchor_difference-.04
    if confidence<=.1 or not .05<delay<.9:raise ValueError('Echo reference calibration is unreliable')
    gain=float(np.clip(np.sqrt(np.mean(x*x)/(np.mean(r*r)+1e-12)),1,8))
    return dict(lag_samples=lag,reference_delay_s=delay,confidence=confidence,microphone_gain=gain)

class EchoProcessor:
    def __init__(self,gain=1.0,neural_models=None,suppression_db=20):
        if not math.isfinite(gain) or not 1<=gain<=8:raise ValueError('Invalid microphone normalization')
        if not math.isfinite(suppression_db) or not 10<=suppression_db<=30:raise ValueError('Invalid echo suppression')
        self.gain=gain;self.ratio=10**(-suppression_db/10)
        self.aec=AudioProcessor(sample_rate=RATE,echo_cancellation=True,noise_suppression=True,
                               high_pass_filter=True,auto_gain_control=False,ns_level=2)
        self.neural=DTLN(Path(neural_models)) if neural_models else None
        self.delay_samples=512 if self.neural else 128
        self.mic=np.empty(0,np.float32);self.ref=np.empty(0,np.float32);self.output=np.zeros(128,np.float32)
        self.dry=np.zeros(self.delay_samples,np.float32);self.far_delay=np.zeros(128,np.float32)
        self.envelope=0.;self.reference_hold=0

    def process(self,near,far):
        near=np.asarray(near,dtype=np.float32);far=np.asarray(far,dtype=np.float32)
        if near.shape!=(HOP,) or far.shape!=(HOP,) or not np.isfinite(near).all() or not np.isfinite(far).all():
            raise ValueError('AEC requires finite 128-sample microphone/reference frames')
        self.mic=np.concatenate([self.mic,near]);self.ref=np.concatenate([self.ref,far])
        while len(self.mic)>=160:
            clean=self.aec.process((self.mic[:160]/self.gain).astype(np.float32),self.ref[:160].copy())*self.gain
            self.output=np.concatenate([self.output,clean]);self.mic=self.mic[160:];self.ref=self.ref[160:]
        out=self.output[:HOP].copy();self.output=self.output[HOP:]
        if len(out)!=HOP:raise RuntimeError('AEC output clock discontinuity')
        if self.neural:
            out=self.neural.process(out,self.far_delay);self.far_delay=far.copy()
        dry=self.dry[:HOP].copy();self.dry[:-HOP]=self.dry[HOP:];self.dry[-HOP:]=near
        self.envelope=max(float(np.mean(dry*dry)),self.envelope*.92)
        self.reference_hold=125 if np.mean(far*far)>1e-8 else max(0,self.reference_hold-1)
        if not self.reference_hold:return dry
        if np.mean(out*out)<self.envelope*self.ratio:out.fill(0)
        if not np.isfinite(out).all():raise RuntimeError('AEC produced invalid PCM')
        return out

    def process_array(self,near,far):
        n=min(len(near),len(far))//HOP*HOP;out=np.empty(n,np.float32)
        for i in range(0,n,HOP):out[i:i+HOP]=self.process(near[i:i+HOP],far[i:i+HOP])
        return out
