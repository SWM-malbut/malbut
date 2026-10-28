from pathlib import Path
import os,subprocess,json,time,sys,signal
base=Path('/tmp/malbut_interaction_20261028');root=base/'adb-diag';out=base/'two_speaker'/sys.argv[1];out.mkdir(exist_ok=True);beam=int(sys.argv[2]);gain=int(sys.argv[3]);assert beam in [-3,2] and gain in [30,15,5]
if (out/'result.json').exists():raise SystemExit('Refusing overwrite')
adb=str(root/'root/usr/bin/adb');env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
def run(*args):return subprocess.run([adb,'-P','5042',*args],env=env,check=True,capture_output=True,timeout=25)
def shell(s):return run('shell',s).stdout.decode().replace('\r','')
def setting(p):run('push',str(p),'/data/build/.malbut-e2e-stage.ini');shell('mv /data/build/.malbut-e2e-stage.ini /data/build/user_cfg.ini; chmod 644 /data/build/user_cfg.ini')
def host(*args,check=True):return subprocess.run(args,check=check,capture_output=True,text=True,timeout=25)
def interrupted(sig,frame):raise SystemExit('e2e interrupted')
for sig in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT):signal.signal(sig,interrupted)
original=shell('cat /data/build/user_cfg.ini');assert 'beam=-3' in original and 'debug=0' in original
(out/'original.ini').write_text(original);(out/'candidate.ini').write_text(original.replace('beam=-3','beam='+str(beam)))
run('push',str(out/'original.ini'),'/tmp/malbut-e2e-original.ini');watchdog=None;probe=None
try:
 host('systemctl','--user','stop','malbut-speech-interaction.service')
 watchdog=int(shell('(sleep 200; cp /tmp/malbut-e2e-original.ini /data/build/.malbut-e2e-restore.ini; mv /data/build/.malbut-e2e-restore.ini /data/build/user_cfg.ini) </dev/null >/tmp/malbut-e2e-watch.log 2>&1 & echo $!').strip())
 setting(out/'candidate.ini');time.sleep(2)
 for ch in range(1,7):shell(f'amixer -c 1 cset name="Channel {ch} PGA Gain" {gain}')
 (out/'candidate_mixer.txt').write_text(shell('amixer -c 1 contents'));(out/'candidate_runtime.txt').write_text(shell('tail -16 /data/build/demo.log'))
 host('systemd-run','--user','--unit=malbut-two-speaker.service','--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=ROS_DOMAIN_ID=88','--setenv=ROS_LOCALHOST_ONLY=1','--setenv=MALBUT_RETEST_RECORD_PREFIX='+str(out/'capture'),'--setenv=MALBUT_TWO_SPEAKER_DB='+str(out/'conversation.sqlite3'),str(base/'two_speaker/e2e_start.sh'))
 script='source /home/ubuntu/.local/share/malbut/speech-releases/aec-20261028/environment.sh\nexport ROS_DOMAIN_ID=88 ROS_LOCALHOST_ONLY=1\npython3 '+str(base/'two_speaker/e2e_probe.py')+' '+str(out/'result.json')
 probe=subprocess.Popen(['bash','-c',script]);probe.wait(timeout=175)
 if probe.returncode:raise RuntimeError('probe failed')
finally:
 if probe is not None and probe.poll() is None:probe.terminate();probe.wait(timeout=5)
 host('systemctl','--user','stop','malbut-two-speaker.service',check=False)
 setting(out/'original.ini');time.sleep(2);assert shell('cat /data/build/user_cfg.ini')==original
 if watchdog:shell(f'kill {watchdog} 2>/dev/null || true')
 state=shell('tail -16 /data/build/demo.log');(out/'restored_runtime.txt').write_text(state);assert 'libvtn_mic6_circle.so' in state
 host('systemd-run','--user','--unit=malbut-speech-interaction.service','--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0','/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028/start.sh')
 (out/'settings.json').write_text(json.dumps(dict(beam=beam,gain=gain,normal_restored=True,stt='current normal profile; local Whisper; no provisional ducking',tts_threads=3),indent=2))
