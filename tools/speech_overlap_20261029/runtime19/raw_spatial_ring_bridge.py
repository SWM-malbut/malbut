"""Experimental live microphone bridge. No file audio enters recognition."""
import os,sys,subprocess,signal,time,json
from pathlib import Path
from collections import deque
import numpy as np
from spatial_filter import SpatialFilter
sys.path.insert(0,str(Path(__file__).resolve().parent/'code'))
from malbut_stt.echo.startup_phase import PhaseBridge
out=Path(sys.argv[1]);weights=np.load(sys.argv[2]);pipe=Path(sys.argv[3]);assert weights.shape==(257,6)
root=Path(__file__).resolve().parent / 'adb-diag/root';env=dict(os.environ,LD_LIBRARY_PATH=str(root/'usr/lib/aarch64-linux-gnu/android'))
reader=subprocess.Popen([str(root/'usr/bin/adb'),'-P','5042','shell',f'echo $$ > /tmp/malbut-fifo-reader-{out.name}; exec cat /data/build/origin.pcm'],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=(out/'adb-reader.log').open('wb'))
recording=bool(os.environ.get('MALBUT_RETEST_RECORD_PREFIX'))
raw_history=deque(maxlen=1875) if recording else None;raw_total=0
processor=SpatialFilter(weights);timing_delay=np.zeros(384,dtype='f4');tail=b'';pending=b'';frames=0;cpu=time.process_time();started=time.monotonic()
phase=None
def stop(sig,frame):reader.terminate()
for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
try:
 phase=PhaseBridge(os.environ['MALBUT_SPATIAL_PHASE_DIR'],weights,SpatialFilter,reader.terminate)
 with pipe.open('wb',buffering=0) as target:
  while True:
   chunk=reader.stdout.read1(32768)
   if not chunk:break
   chunk=tail+chunk;tail=b'\r' if chunk.endswith(b'\r') else b''
   if tail:chunk=chunk[:-1]
   pending+=chunk.replace(b'\r\n',b'\n')
   while len(pending)>=8192:
    if recording:
     raw_history.append((time.monotonic(),pending[:8192]));raw_total+=512
    raw=np.frombuffer(pending[:8192],'<i2').reshape(512,8)[:,:6];pending=pending[8192:];blocks=[]
    processor,raw=phase.prepare(processor,raw,frames)
    for offset in range(0,512,128):
     voice=(processor.process(raw[offset:offset+128])*32).clip(-.98,.98)
     blocks.append(np.column_stack((voice,timing_delay[:128].copy())).astype('<f4'))
     timing_delay[:-128]=timing_delay[128:];timing_delay[-128:]=raw[offset:offset+128,1]
    target.write(np.concatenate(blocks).tobytes());frames+=512
    phase.written(frames)
    if frames==512:(out/'bridge-ready').write_text(str(time.monotonic()))
finally:
 if phase is not None:phase.close()
 if recording:
  (out/'bridge_raw8.pcm').write_bytes(b''.join(x[1] for x in raw_history))
  (out/'bridge_raw8.stamps').write_bytes(np.asarray([x[0] for x in raw_history],dtype='<f8').tobytes())
  (out/'bridge_raw8.window.json').write_text(json.dumps(dict(start_frame=raw_total-len(raw_history)*512,frames=len(raw_history)*512,maximum_seconds=60,full_frames=raw_total),indent=2))
 if reader.poll() is None:reader.terminate()
 reader.wait(timeout=5)
 (out/'bridge-stats.json').write_text(json.dumps(dict(frames=frames,duration_s=frames/16000,wall_s=time.monotonic()-started,cpu_s=time.process_time()-cpu,tail_bytes=len(pending)+len(tail),reader_exit=reader.returncode),indent=2))
