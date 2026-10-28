"""Bounded raw+USB captures, triggered by a separate physical computer speaker."""
from pathlib import Path
import os,subprocess,time,json,signal,select,sys
base=Path('/tmp/malbut_interaction_20261028');root=base/'adb-diag';name=sys.argv[1];beam=int(sys.argv[2]) if len(sys.argv)>2 else -3;assert beam in [-3,0,1,2,3,4,5];assert name.replace('_','').isalnum();out=base/'two_speaker'/name;out.mkdir(parents=True,exist_ok=True)
if (out/'metadata.json').exists():raise SystemExit('Refusing overwrite')
adb=str(root/'root/usr/bin/adb');env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
def run(*args):return subprocess.run([adb,'-P','5042',*args],env=env,check=True,capture_output=True,timeout=25)
def shell(s):return run('shell',s).stdout.decode(errors='replace').replace('\r','')
def setting(path):
 run('push',str(path),'/data/build/.malbut-external-stage.ini');shell('mv /data/build/.malbut-external-stage.ini /data/build/user_cfg.ini; chmod 644 /data/build/user_cfg.ini')
def host(*args,check=True):return subprocess.run(args,check=check,capture_output=True,text=True,timeout=25)
def interrupted(sig,frame):raise SystemExit('capture interrupted')
for sig in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):signal.signal(sig,interrupted)
original=shell('cat /data/build/user_cfg.ini');assert 'version=mic6_circle' in original and 'debug=0' in original
(out/'original.ini').write_text(original);(out/'capture.ini').write_text(original.replace('debug=0','debug=1',1).replace('beam=-3','beam='+str(beam),1))
run('pull','/data/build/origin.pcm',str(out/'original_origin.pcm'));run('push',str(out/'original.ini'),'/tmp/malbut-external-original.ini')
assert int(shell('df -k /data | tail -1').split()[3])>40000
capture=None;watchdog=None;meta={'beam':beam,'scope':'external physical PC speaker into robot raw array and USB; no host AEC','trials':[]}
try:
 host('systemctl','--user','stop','malbut-speech-interaction.service')
 watchdog=int(shell('(sleep 150; cp /tmp/malbut-external-original.ini /data/build/.malbut-external-restore.ini; mv /data/build/.malbut-external-restore.ini /data/build/user_cfg.ini) </dev/null >/tmp/malbut-external-watch.log 2>&1 & echo $!').strip())
 setting(out/'capture.ini');time.sleep(2)
 state=shell('tail -16 /data/build/demo.log');(out/'capture_state.txt').write_text(state);assert 'libvtn_mic6_circle.so' in state
 for idx,gain in enumerate([30,5],1):
  for ch in range(1,7):shell(f'amixer -c 1 cset name="Channel {ch} PGA Gain" {gain}')
  time.sleep(.5);raw_start=int(shell('wc -c < /data/build/origin.pcm'))
  row=dict(index=idx,gain=gain,raw_start_bytes=raw_start,capture_start_unix=time.time());meta['trials'].append(row)
  with (out/f'gain{gain}_usb.pcm').open('wb') as stream:
   capture=subprocess.Popen(['parec','--device=alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback','--raw','--format=s16le','--rate=16000','--channels=1','--latency-msec=32'],stdout=stream,stderr=subprocess.DEVNULL)
   time.sleep(1)
   if capture.poll() is not None:raise RuntimeError('capture exited')
   row['ready_unix']=time.time();print('READY '+json.dumps(dict(gain=gain,index=idx)),flush=True)
   if not select.select([sys.stdin],[],[],25)[0]:raise TimeoutError('external playback acknowledgement missing')
   reply=sys.stdin.readline().strip();assert reply=='PLAYBACK_DONE',repr(reply)
   row['playback_ack_unix']=time.time();time.sleep(2)
   capture.terminate();capture.wait(timeout=5);capture=None
  run('pull','/data/build/origin.pcm',str(out/f'gain{gain}_snapshot.pcm'))
  (out/f'gain{gain}_raw8.pcm').write_bytes((out/f'gain{gain}_snapshot.pcm').read_bytes()[raw_start:]);row['capture_end_unix']=time.time();print('CAPTURED '+str(gain),flush=True)
finally:
 if capture is not None and capture.poll() is None:capture.terminate();capture.wait(timeout=5)
 try:
  setting(out/'original.ini');time.sleep(2);assert shell('cat /data/build/user_cfg.ini')==original
  state=shell('tail -16 /data/build/demo.log');(out/'restored_state.txt').write_text(state);assert 'libvtn_mic6_circle.so' in state
  (out/'restored_mixer.txt').write_text(shell('amixer -c 1 contents'));meta['original_restored']=True
  if watchdog is not None:shell(f'kill {watchdog} 2>/dev/null || true')
 finally:
  run('push',str(out/'original_origin.pcm'),'/data/build/origin.pcm');shell('chmod 644 /data/build/origin.pcm')
  host('systemd-run','--user','--unit=malbut-speech-interaction.service','--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0','/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028/start.sh')
  meta['normal_service_restarted']=True;(out/'metadata.json').write_text(json.dumps(meta,indent=2))
print('FINISHED',flush=True)
