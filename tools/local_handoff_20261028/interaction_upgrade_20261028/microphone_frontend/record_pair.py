"""90-second paired raw-array and processed-USB capture with restoration."""
import subprocess,os,time,json,pathlib,hashlib,signal
root=pathlib.Path('/tmp/malbut_interaction_20261028/adb-diag')
folder=root/'paired_human_3';folder.mkdir(exist_ok=True)
adb=str(root/'root/usr/bin/adb');env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
def run(*args,timeout=20):return subprocess.run([adb,'-P','5042',*args],env=env,check=True,capture_output=True,timeout=timeout)
def shell(s):return run('shell',s).stdout.decode(errors='replace').replace('\r','')
def set_config(path):
 # ADB truncates before writing; firmware watcher may read defaults mid-transfer.
 # Stage on the SAME filesystem, then atomically replace the watched path.
 run('push',str(path),'/data/build/.malbut-user-config.tmp')
 shell('mv /data/build/.malbut-user-config.tmp /data/build/user_cfg.ini; chmod 644 /data/build/user_cfg.ini')

def host(*args,check=True):return subprocess.run(args,check=check,capture_output=True,text=True,timeout=25)
config='/data/build/user_cfg.ini';before=shell('cat '+config);assert before.count('debug=0')==1
(folder/'user_cfg.before.ini').write_text(before);(folder/'user_cfg.debug.ini').write_text(before.replace('debug=0','debug=1',1))
run('pull','/data/build/origin.pcm',str(folder/'origin.before.pcm'))
assert int(shell("df -k /data | tail -1").split()[3]) > 50000
meta={'duration_requested_s':90,'microphone':'XFM-DP-V0.0.18','distance_m':'1–2','human_label':'pending','before_config_sha256':hashlib.sha256(before.encode()).hexdigest()}
process=None;original=(folder/'origin.before.pcm').read_bytes();watchdog=None
def interrupted(signum,frame):raise SystemExit('paired recording interrupted')
signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGHUP,interrupted)
# Device-local fail-safe restores recording flag even if the host connection dies.
run('push',str(folder/'user_cfg.before.ini'),'/tmp/malbut-pair-config-before.ini')
try:
 host('systemctl','--user','stop','malbut-speech-interaction.service')
 watchdog=int(shell('(sleep 120; cp /tmp/malbut-pair-config-before.ini /data/build/.malbut-watchdog-restore.ini; mv /data/build/.malbut-watchdog-restore.ini /data/build/user_cfg.ini) </dev/null >/tmp/malbut-pair-restore.log 2>&1 & echo $!').strip())
 set_config(folder/'user_cfg.debug.ini')
 time.sleep(2)
 meta['raw_bytes_at_usb_start']=int(shell('wc -c < /data/build/origin.pcm'))
 meta['usb_capture_started_unix']=time.time()
 with (folder/'usb_processed.pcm').open('wb') as stream:
  process=subprocess.Popen(['parec','--device=alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback','--raw','--format=s16le','--rate=16000','--channels=1'],stdout=stream,stderr=subprocess.DEVNULL)
  time.sleep(.5)
  if process.poll() is not None:raise RuntimeError('capture exited before cue')
  print('PAIR_RECORDING_READY: 90 second window open',flush=True)
  try:process.wait(timeout=89.5)
  except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
 meta['usb_capture_finished_unix']=time.time()
 print('PAIR_RECORDING_FINISHED',flush=True)
finally:
 if process is not None and process.poll() is None:process.terminate();process.wait(timeout=5)
 set_config(folder/'user_cfg.before.ini');time.sleep(1)
 assert shell('cat '+config)==before
 if watchdog is not None:shell('kill '+str(watchdog)+' 2>/dev/null || true')
 meta['config_restored']=True
 try:
  run('pull','/data/build/origin.pcm',str(folder/'origin.after.pcm'))
  captured=(folder/'origin.after.pcm').read_bytes()
  offset=len(original) if captured.startswith(original) else 0
  (folder/'array_raw_8ch.pcm').write_bytes(captured[offset:])
  meta['raw_prefix_removed_bytes']=offset
 finally:
  run('push',str(folder/'origin.before.pcm'),'/data/build/origin.pcm')
  shell('chmod 644 /data/build/origin.pcm')
  host('systemd-run','--user','--unit=malbut-speech-interaction.service','--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0','/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028/start.sh')
  meta['normal_service_restarted']=True
  (folder/'metadata.json').write_text(json.dumps(meta,indent=2))
print(json.dumps(meta),flush=True)
