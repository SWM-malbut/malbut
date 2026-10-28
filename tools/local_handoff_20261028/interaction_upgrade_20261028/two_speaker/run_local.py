"""Drive separate computer loudspeaker only after remote capture confirms readiness."""
from pathlib import Path
import subprocess,os,time,json,sys
root=Path(__file__).resolve().parent;name=sys.argv[1];out=root/name;out.mkdir(exist_ok=True)
audio=root/'human_normalized.wav'
volume=int(sys.argv[2]) if len(sys.argv)>2 else 45000
beam=int(sys.argv[3]) if len(sys.argv)>3 else -3
assert beam in [-3,0,1,2,3,4,5]
assert 0<volume<=65536
env=dict(os.environ,PULSE_SERVER='unix:/run/user/1000/pulse/native')
ssh=['ssh','-S','/tmp/malbut-dialogue-20261028-ssh','ubuntu@192.168.0.86',f'python3 /tmp/malbut_interaction_20261028/two_speaker/capture_remote.py {name} {beam}']
p=subprocess.Popen(ssh,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1);rows=[]
try:
 for line in p.stdout:
  print(line.strip(),flush=True)
  if line.startswith('READY '):
   row=json.loads(line[6:]);row.update(source=str(audio),stream_volume=volume,local_start_unix=time.time(),expected='특히 중소형 빌딩은 개인투자자의 전폭적인 지지를 받고 있다')
   subprocess.run(['paplay','--device=alsa_output.pci-0000_00_1f.3.analog-stereo','--volume='+str(volume),str(audio)],env=env,check=True,timeout=20)
   row['local_end_unix']=time.time();rows.append(row);p.stdin.write('PLAYBACK_DONE\n');p.stdin.flush()
 if p.wait(timeout=10):raise RuntimeError('remote capture failed')
finally:
 (out/'playback.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
 if p.poll() is None:p.terminate();p.wait(timeout=10)
