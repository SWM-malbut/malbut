import json
import os
from pathlib import Path
import subprocess
from threading import Event,Thread
import time
import numpy as np
import sounddevice as sd
from pulse_capture import Capture

os.umask(0o077);root=Path(__file__).resolve().parent
def listing(kind):return json.loads(subprocess.check_output(['pactl','--format=json','list',kind],text=True))
assert not listing('sink-inputs') and not listing('source-outputs')
source=next(x['name'] for x in listing('sources') if 'iflytek_XFM' in x['name'])
sink=next(x['name'] for x in listing('sinks') if 'GeneralPlus' in x['name'])
stop=Event();data={};errors=[]
os.environ['PULSE_SINK']=sink
warm=sd.OutputStream(device='pulse',samplerate=16000,channels=1,blocksize=160,
    latency='low',callback=lambda out,*_:out.fill(0))
warm.start()
def record(name,device):
    stream=None;frames=[];times=[];latencies=[]
    try:
        stream=Capture(device)
        while not stop.is_set():
            x,t,l=stream.read();frames.append(x);times.append(t);latencies.append(l)
    except Exception as e:errors.append(str(e))
    finally:
        if stream:stream.close()
        data[name]=(np.concatenate(frames),np.array(times),np.array(latencies))
threads=[Thread(target=record,args=(n,s)) for n,s in [('mic',source),('ref',sink+'.monitor')]]
for t in threads:t.start()
player=None
try:
    time.sleep(3)
    player=subprocess.Popen(['paplay','--device='+sink,'--latency-msec=20','--process-time-msec=10',str(root/'reference.wav')])
    player.wait(timeout=40);time.sleep(1)
finally:
    if player and player.poll() is None:player.terminate();player.wait(timeout=3)
    stop.set()
    for t in threads:t.join(timeout=3)
    warm.stop();warm.close()
    assert not any(t.is_alive() for t in threads)
    np.savez(root/'timestamp_warm.npz',**{n+'_'+key:arr for n,vals in data.items() for key,arr in zip(('audio','time','latency'),vals)})
    print(json.dumps(dict(errors=errors,frames={n:len(v[1]) for n,v in data.items()})))
