from pathlib import Path
import subprocess,time,json
r=Path('/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028');base=Path('/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028');out=Path('/tmp/malbut_interaction_20261028/tts_quality/deployment');out.mkdir(exist_ok=True)
s=r/'start.sh';before=s.read_text();old='tts_config:="$base/tts.yaml"';new='tts_config:="$release/tts.yaml"';assert before.count(old)==1
cfg=(base/'tts.yaml').read_text();assert cfg.count('piper_threads: 6')==1
(out/'start.before.sh').write_text(before);(out/'tts.before.yaml').write_text(cfg)
def atom(p,text,mode):
 tmp=p.with_name(p.name+'.tmp');tmp.write_text(text);tmp.chmod(mode);tmp.replace(p)
def start():
 return subprocess.run(['systemd-run','--user','--unit=malbut-speech-interaction.service','--collect','--property=Restart=no','--property=TimeoutStopSec=10','--setenv=MALBUT_SPEECH_GREETING=0',str(s)],check=True,capture_output=True,text=True)
try:
 subprocess.run(['systemctl','--user','stop','malbut-speech-interaction.service'],check=True)
 atom(r/'tts.yaml',cfg.replace('piper_threads: 6','piper_threads: 3'),0o644);atom(s,before.replace(old,new),0o755)
 start();time.sleep(3)
 assert subprocess.check_output(['systemctl','--user','is-active','malbut-speech-interaction.service'],text=True).strip()=='active'
 (out/'deployment.json').write_text(json.dumps(dict(piper_threads_before=6,piper_threads_after=3,scope='current interaction release only; old baseline config preserved',aec_ready_verification='pending'),indent=2));print('Current interaction release uses3threads; normal service active')
except BaseException:
 atom(s,before,0o755)
 subprocess.run(['systemctl','--user','stop','malbut-speech-interaction.service'],check=False);start();raise
