"""Explicit PulseAudio capture with record-latency timestamps, no defaults changed."""
import ctypes as c
import time
import numpy as np

class SampleSpec(c.Structure):
    _fields_=[('format',c.c_int),('rate',c.c_uint32),('channels',c.c_uint8)]
class BufferAttr(c.Structure):
    _fields_=[(name,c.c_uint32) for name in ('maxlength','tlength','prebuf','minreq','fragsize')]

class Capture:
    def __init__(self,source,hop=128,channels=1):
        self.hop=hop;self.lib=c.CDLL('libpulse-simple.so.0');self.error=c.c_int()
        self.lib.pa_simple_new.restype=c.c_void_p
        self.lib.pa_simple_new.argtypes=[c.c_char_p,c.c_char_p,c.c_int,c.c_char_p,c.c_char_p,c.POINTER(SampleSpec),c.c_void_p,c.POINTER(BufferAttr),c.POINTER(c.c_int)]
        self.lib.pa_simple_read.argtypes=[c.c_void_p,c.c_void_p,c.c_size_t,c.POINTER(c.c_int)]
        self.lib.pa_simple_get_latency.argtypes=[c.c_void_p,c.POINTER(c.c_int)]
        self.lib.pa_simple_get_latency.restype=c.c_uint64
        self.lib.pa_simple_free.argtypes=[c.c_void_p]
        spec=SampleSpec(3,16000,channels);attr=BufferAttr(32000*channels,0xffffffff,0xffffffff,0xffffffff,hop*2*channels)
        self.handle=self.lib.pa_simple_new(None,b'malbut_aec_reference_fix',2,source.encode(),b'capture',c.byref(spec),None,c.byref(attr),c.byref(self.error))
        if not self.handle:raise RuntimeError(f'Pulse capture failed: {self.error.value}')
        self.buf=np.empty(hop if channels==1 else (hop,channels),'<i2')
    def read(self):
        result=self.lib.pa_simple_read(self.handle,self.buf.ctypes.data,self.buf.nbytes,c.byref(self.error))
        if result<0:raise RuntimeError(f'Pulse read failed: {self.error.value}')
        latency=self.lib.pa_simple_get_latency(self.handle,c.byref(self.error))
        if latency==2**64-1:raise RuntimeError('Pulse latency unavailable')
        return self.buf.astype('float32')/32768,time.monotonic()-latency/1e6,latency/1e6
    def close(self):
        if self.handle:self.lib.pa_simple_free(self.handle);self.handle=None
