"""Two explicitly cued quiet human recordings; restore board and service on exit."""
from pathlib import Path
import os,subprocess,time,json,signal,wave,math,struct
root=Path('/tmp/malbut_interaction_20261028/adb-diag');out=root/'cued_human_gains';out.mkdir(exist_ok=True)
if (out/'metadata.json').exists():raise SystemExit('Refusing to overwrite completed experiment')
adb=str(root/'root/usr/bin/adb');env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
def run(*args):return subprocess.run([adb,'-P','5042',*args],env=env,check=True,capture_output=True,timeout=25)
def shell(s):return run('shell',s).stdout.decode(errors='replace').replace('\r','')
def setting(path):
 run('push',str(path),'/data/build/.malbut-cued-stage.ini')
 shell('mv /data/build/.malbut-cued-stage.ini /data/build/user_cfg.ini; chmod 644 /data/build/user_cfg.ini')
def host(*args,check=True):return subprocess.run(args,check=check,capture_output=True,text=True,timeout=25)
def interrupted(signum,frame):raise SystemExit('capture interrupted')
for sig in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):signal.signal(sig,interrupted)
original=shell('cat /data/build/user_cfg.ini');assert 'version=mic6_circle' in original and 'debug=0' in original
(out/'original.ini').write_text(original);(out/'capture.ini').write_text(original.replace('debug=0','debug=1',1))
run('pull','/data/build/origin.pcm',str(out/'original_origin.pcm'))
run('push',str(out/'original.ini'),'/tmp/malbut-cued-original.ini')
assert int(shell('df -k /data | tail -1').split()[3])>40000
with wave.open(str(out/'ding.wav'),'wb') as w:
 w.setparams((1,2,16000,0,'NONE',''))
 w.writeframes(b''.join(struct.pack('<h',int(6000*math.sin(2*math.pi*880*i/16000)*min(1,i/160,(4000-i)/480))) for i in range(4000)))
watchdog=None;capture=None;meta={'human_label':'pending','distance_m':'1–2','trials':[],'scope':'quiet human audio before host AEC; not end-to-end barge-in'}
try:
 host('systemctl','--user','stop','malbut-speech-interaction.service')
 watchdog=int(shell('(sleep 100; cp /tmp/malbut-cued-original.ini /data/build/.malbut-cued-restore.ini; mv /data/build/.malbut-cued-restore.ini /data/build/user_cfg.ini) </dev/null >/tmp/malbut-cued-watch.log 2>&1 & echo $!').strip())
 setting(out/'capture.ini');time.sleep(2)
 state=shell('tail -16 /data/build/demo.log');(out/'capture_state.txt').write_text(state);assert 'libvtn_mic6_circle.so' in state
 for index,gain in enumerate([30,5],1):
  for ch in range(1,7):shell(f'amixer -c 1 cset name="Channel {ch} PGA Gain" {gain}')
  time.sleep(.5)
  raw_start=int(shell('wc -c < /data/build/origin.pcm'))
  row=dict(index=index,gain=gain,raw_start_bytes=raw_start,capture_start_unix=time.time())
  meta['trials'].append(row)
  with (out/f'gain{gain}_usb.pcm').open('wb') as stream:
   capture=subprocess.Popen(['parec','--device=alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback','--raw','--format=s16le','--rate=16000','--channels=1'],stdout=stream,stderr=subprocess.DEVNULL)
   time.sleep(1)
   if capture.poll() is not None:raise RuntimeError('capture failed before cue')
   row['cue_start_unix']=time.time()
   host('paplay','--device=alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo',str(out/'ding.wav'))
   row['cue_end_unix']=time.time();print(f'CUE {index} COMPLETE: microphone gain {gain}, 20-second speaking window OPEN',flush=True)
   time.sleep(20)
   capture.terminate();capture.wait(timeout=5);capture=None;row['capture_end_unix']=time.time()
  run('pull','/data/build/origin.pcm',str(out/f'gain{gain}_snapshot.pcm'))
  (out/f'gain{gain}_raw8.pcm').write_bytes((out/f'gain{gain}_snapshot.pcm').read_bytes()[raw_start:])
  print(f'TRIAL {index} CAPTURE FINISHED',flush=True)
finally:
 if capture is not None and capture.poll() is None:capture.terminate();capture.wait(timeout=5)
 try:
  setting(out/'original.ini');time.sleep(2)
  assert shell('cat /data/build/user_cfg.ini')==original
  state=shell('tail -16 /data/build/demo.log');(out/'restored_state.txt').write_text(state);assert 'libvtn_mic6_circle.so' in state
  (out/'restored_mixer.txt').write_text(shell('amixer -c 1 contents'));meta['original_config_and_circle_runtime_restored']=True
  if watchdog is not None:shell(f'kill {watchdog} 2>/dev/null || true')
 finally:
  run('push',str(out/'original_origin.pcm'),'/data/build/origin.pcm');shell('chmod 644 /data/build/origin.pcm')
  host('systemd-run','--user','--unit=malbut-speech-interaction.service','--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0','/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028/start.sh')
  meta['normal_service_restarted']=True;(out/'metadata.json').write_text(json.dumps(meta,indent=2))
print('CAPTURE FINISHED; original configuration and normal service restored',flush=True)
