from pathlib import Path
import subprocess,os,time,json,sys
root=Path(__file__).resolve().parent;name=sys.argv[1];beam=int(sys.argv[2]);gain=int(sys.argv[3]);assert name.replace('_','').isalnum() and beam in [-3,2] and gain in [30,15,5]
out=root/name;out.mkdir(exist_ok=True);env=dict(os.environ,PULSE_SERVER='unix:/run/user/1000/pulse/native')
cmd=['ssh','-S','/tmp/malbut-dialogue-20261028-ssh','ubuntu@192.168.0.86',f'python3 /tmp/malbut_interaction_20261028/two_speaker/e2e_remote.py {name} {beam} {gain}']
p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1);rows=[]
with (out/'console.log').open('w') as log:
 try:
  for line in p.stdout:
   log.write(line);log.flush();print(line.strip(),flush=True)
   if line.startswith('PC_PLAY '):
    row=json.loads(line[8:]);row['local_start_unix']=time.time()
    subprocess.run(['paplay','--device=alsa_output.pci-0000_00_1f.3.analog-stereo','--volume=45000',str(root/'human_normalized.wav')],env=env,check=True,timeout=20)
    row['local_end_unix']=time.time();rows.append(row)
  if p.wait(timeout=10):raise RuntimeError('remote test failed')
 finally:
  (out/'playback.json').write_text(json.dumps(rows,indent=2))
  if p.poll() is None:p.terminate();p.wait(timeout=10)
