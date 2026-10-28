"""Compare settled idle speech services on identical copied conversation history."""
from pathlib import Path
import subprocess,sqlite3,shutil,time,json,signal,os
root=Path('/tmp/malbut_interaction_20261028');out=root/'cpu_same_db';out.mkdir(exist_ok=True)
base=Path('/home/ubuntu/.local/share/malbut/speech-releases')
normal='malbut-speech-interaction.service';units=[];rows=[]
def run(*args,check=True,timeout=25):return subprocess.run(args,check=check,capture_output=True,text=True,timeout=timeout)
def interrupted(*_):raise SystemExit('CPU measurement interrupted')
signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGHUP,interrupted)
def logs(unit,since):return run('sudo','-n','journalctl','_SYSTEMD_USER_UNIT='+unit,'--since','@'+str(int(since)),'--no-pager','-o','cat').stdout
run('systemctl','--user','stop',normal)
try:
 source=sqlite3.connect('file:/home/ubuntu/.local/state/malbut/speech-dialogue.sqlite3?mode=ro',uri=True)
 with sqlite3.connect(out/'snapshot.sqlite3') as dest:source.backup(dest)
 count=source.execute('select count(*) from conversation_turns').fetchone()[0];source.close()
 for phase,release in [('before','aec-20261028'),('after','interaction-20261028')]:
  db=out/(phase+'.sqlite3');shutil.copyfile(out/'snapshot.sqlite3',db)
  unit='malbut-cpu-'+phase+'.service';units.append(unit);start=time.time()
  run('systemd-run','--user','--unit='+unit,'--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0','--setenv=ROS_DOMAIN_ID=87','--setenv=ROS_LOCALHOST_ONLY=1',str(base/release/'start.sh'),'agent_conversation_db:='+str(db))
  ready=False
  for _ in range(40):
   text=logs(unit,start)
   if 'aec_ready:' in text:ready=True;break
   if 'aec_failed' in text or 'aec_unavailable' in text:break
   time.sleep(1)
  if not ready:
   (out/(phase+'.log')).write_text(logs(unit,start));raise RuntimeError('AEC not ready for '+phase)
  print(phase+': AEC ready; settling 10 seconds.',flush=True);time.sleep(10)
  measured=time.time()
  result=run('python3',str(root/'retest_1236/cpu_detail.py'),unit,'cpu_same_db_'+phase,'30',timeout=40)
  data=json.loads(result.stdout);windowlog=logs(unit,measured)
  data.update(phase=phase,initial_turn_count=count,valid_idle=not any(t in windowlog for t in ['speech_started','transcript_final','playback:playing']),database=str(db))
  (out/(phase+'.json')).write_text(json.dumps(data,indent=2));(out/(phase+'.log')).write_text(logs(unit,start))
  rows.append(data);print(json.dumps({k:v for k,v in data.items() if k!='processes'}),flush=True)
  run('systemctl','--user','stop',unit)
finally:
 for unit in units:run('systemctl','--user','stop',unit,check=False)
 run('systemd-run','--user','--unit='+normal,'--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0',str(base/'interaction-20261028/start.sh'))
 (out/'comparison.json').write_text(json.dumps({'scope':'Same 434-turn snapshot copied independently per release; both microphone paths calibrated, 10 second settle then30 seconds idle, no greeting, local Whisper. Service cgroup CPU / 6 CPUs.','observations':rows,'normal_service_restored':True},indent=2))
 print('Normal service restored.',flush=True)
