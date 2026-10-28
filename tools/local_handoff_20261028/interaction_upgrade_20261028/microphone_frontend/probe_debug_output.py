"""Known loudspeaker signal: validate whether raw-dump mode affects USB output."""
from pathlib import Path
import subprocess,os,time,json
root=Path('/tmp/malbut_interaction_20261028/adb-diag');out=root/'debug_output_control';out.mkdir(exist_ok=True)
adb=str(root/'root/usr/bin/adb');env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
def run(*args):return subprocess.run([adb,'-P','5042',*args],env=env,check=True,capture_output=True,timeout=20)
def shell(s):return run('shell',s).stdout.decode(errors='replace').replace('\r','')
def setting(path):
 run('push',str(path),'/data/build/.malbut-diag-stage.ini');shell('mv /data/build/.malbut-diag-stage.ini /data/build/user_cfg.ini; chmod 644 /data/build/user_cfg.ini')
def host(*args,check=True):return subprocess.run(args,check=check,capture_output=True,text=True,timeout=25)
before=shell('cat /data/build/user_cfg.ini');assert 'version=mic6_circle' in before and 'debug=0' in before
(out/'original.ini').write_text(before)
run('pull','/data/build/origin.pcm',str(out/'original_origin.pcm'))
run('push',str(out/'original.ini'),'/tmp/malbut-control-original.ini')
watchdog=int(shell('(sleep 90; cp /tmp/malbut-control-original.ini /data/build/.malbut-control-restore.ini; mv /data/build/.malbut-control-restore.ini /data/build/user_cfg.ini) </dev/null >/tmp/malbut-control-watch.log 2>&1 & echo $!').strip())
meta=[];capture=None
try:
 host('systemctl','--user','stop','malbut-speech-interaction.service')
 for flag in [0,1]:
  cfg=out/f'debug{flag}.ini';cfg.write_text(before.replace('debug=0',f'debug={flag}',1));setting(cfg);time.sleep(2)
  state=shell('tail -16 /data/build/demo.log');(out/f'debug{flag}_state.txt').write_text(state)
  assert 'libvtn_mic6_circle.so' in state
  with (out/f'debug{flag}_usb.pcm').open('wb') as stream:
   capture=subprocess.Popen(['parec','--device=alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback','--raw','--format=s16le','--rate=16000','--channels=1'],stdout=stream,stderr=subprocess.DEVNULL)
   time.sleep(2);started=time.time();print('Playing known sentence, debug='+str(flag),flush=True)
   host('paplay','--device=alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo',str(root/'diagnostic_sentence.wav'))
   ended=time.time();time.sleep(2);capture.terminate();capture.wait(timeout=5);capture=None
   meta.append(dict(debug=flag,playback_start=started,playback_end=ended))
finally:
 if capture is not None:capture.terminate();capture.wait(timeout=5)
 setting(out/'original.ini');time.sleep(2)
 assert shell('cat /data/build/user_cfg.ini')==before
 state=shell('tail -16 /data/build/demo.log');(out/'restored_state.txt').write_text(state);assert 'libvtn_mic6_circle.so' in state
 shell('kill '+str(watchdog)+' 2>/dev/null || true')
 run('pull','/data/build/origin.pcm',str(out/'control_origin_after.pcm'))
 run('push',str(out/'original_origin.pcm'),'/data/build/origin.pcm');shell('chmod 644 /data/build/origin.pcm')
 host('systemd-run','--user','--unit=malbut-speech-interaction.service','--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0','/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028/start.sh')
 (out/'metadata.json').write_text(json.dumps(dict(trials=meta,config_restored=True,runtime_circle_restored=True),indent=2))
 print('Control done; normal configuration and service restored.',flush=True)
