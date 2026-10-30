from pathlib import Path
import hashlib,json,os,subprocess,sys,time
ROOT=Path(__file__).resolve().parent
NORMAL='malbut-speech-interaction.service'
BASE=Path('/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028')
GUARD=BASE.parent/'quiet-duck-guard-20261030'
UNIT=Path('/home/ubuntu/.config/systemd/user')/NORMAL
TEXT='[Unit]\nDescription=Malbut speech interaction with short candidate guard\n\n[Service]\nExecStart='+str(GUARD/'start.sh')+'\nEnvironment=MALBUT_SPEECH_GREETING=0\nRestart=no\nTimeoutStopSec=10\n'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def command(*args,check=True,timeout=20):return subprocess.run(args,capture_output=True,text=True,check=check,timeout=timeout)
def state():
 p=command('systemctl','--user','show',NORMAL,'-p','ActiveState','-p','MainPID','-p','ExecStart','-p','FragmentPath')
 return dict(s.split('=',1) for s in p.stdout.splitlines() if '=' in s)
def save(name,value):
 p=ROOT/name;t=p.with_suffix(p.suffix+'.new');t.write_text(json.dumps(value,indent=2)+'\n');t.replace(p)
def rollback():
 if not (ROOT/'created-unit.json').exists():return
 assert UNIT.is_file() and UNIT.read_text()==TEXT,'Refuse to remove a changed user unit'
 command('systemctl','--user','stop',NORMAL,timeout=30);UNIT.unlink();command('systemctl','--user','daemon-reload')
 command('systemd-run','--user','--unit='+NORMAL,'--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0',str(BASE/'start.sh'))
 save('rolled-back.json',dict(at=time.time(),state=state()))
def main():
 assert not UNIT.exists() and not UNIT.is_symlink() and not UNIT.with_name(NORMAL+'.d').exists()
 assert sha(GUARD/'guard-sha256.json')=='053ec398d901c124b95783f735db5bae5122934395b19eb55c9d73c7439a5240'
 for n,h in json.loads((GUARD/'guard-sha256.json').read_text()).items():assert sha(GUARD/n)==h,n
 s=state();assert s['ActiveState']=='active' and str(BASE/'start.sh') in s['ExecStart'] and '/systemd/transient/' in s['FragmentPath'],s
 for u in ('malbut-two-speaker.service','malbut-quiet-duck-guard.service'):
  assert command('systemctl','--user','is-active','--quiet',u,check=False).returncode!=0
 save('before.json',dict(at=time.time(),normal=s,user_unit_absent=True,source_sha256=sha(BASE/'code/malbut_stt/dialogue_pipeline.py')))
 (ROOT/'transient-before.txt').write_text(command('systemctl','--user','cat',NORMAL).stdout)
 UNIT.parent.mkdir(parents=True,exist_ok=True);temporary=UNIT.with_suffix('.service.quiet-new');assert not temporary.exists();temporary.write_text(TEXT)
 os.link(temporary,UNIT);temporary.unlink();save('created-unit.json',dict(path=str(UNIT),sha256=sha(UNIT),at=time.time()))
 command('systemctl','--user','stop',NORMAL,timeout=30);command('systemctl','--user','daemon-reload');command('systemctl','--user','reset-failed',NORMAL,check=False)
 command('systemctl','--user','start',NORMAL);deadline=time.monotonic()+45
 while time.monotonic()<deadline:
  s=state();assert s['ActiveState'] in ('active','activating'),s
  assert s.get('FragmentPath')==str(UNIT) and str(GUARD/'start.sh') in s['ExecStart'],s
  log=command('sudo','-n','journalctl','_PID='+s['MainPID'],'-n','30','--no-pager','-o','cat').stdout
  assert 'aec_fallback:' not in log
  if 'aec_ready:' in log and 'malbut_speech_capture_ready' in log:
   save('applied.json',dict(at=time.time(),normal=s,unit_sha256=sha(UNIT),guard_manifest_sha256=sha(GUARD/'guard-sha256.json'),pipeline_sha256=sha(GUARD/'code/malbut_stt/dialogue_pipeline.py'),aec_ready=True,capture_ready=True,microphone_changed=False,scope='User explicitly approved only first100ms duck guard; full overlap goal remains open'))
   return
  time.sleep(.5)
 raise TimeoutError('Guard normal service readiness')
if __name__=='__main__':
 if len(sys.argv)>1:
  if sys.argv[1]=='rollback':rollback()
  elif sys.argv[1]=='recover' and not (ROOT/'applied.json').exists():rollback()
 else:main()
