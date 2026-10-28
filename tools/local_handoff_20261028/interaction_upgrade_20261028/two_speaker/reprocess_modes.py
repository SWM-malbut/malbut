from pathlib import Path
import os,subprocess,json,time,re
root=Path('/tmp/malbut_interaction_20261028/adb-diag');out=root.parent/'two_speaker/pc_probe_2/modes';out.mkdir(exist_ok=True)
env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'));adb=str(root/'root/usr/bin/adb')
def run(*a):return subprocess.run([adb,'-P','5042',*a],env=env,check=True,capture_output=True,timeout=50)
text=run('shell','cat /data/build/vtn/vtn.ini').stdout.decode().replace('\r','');assert re.search(r'cae_enable\s*=\s*1',text)
(out/'original_vtn.ini').write_text(text);(out/'bypass.ini').write_text(re.sub(r'cae_enable\s*=\s*1','cae_enable=0',text))
run('push',str(out/'bypass.ini'),'/tmp/malbut-bypass.ini')
rows=[]
for name,beam,cfg in [('beam_minus2',-2,'/data/build/vtn/vtn.ini'),('cae_off',2,'/tmp/malbut-bypass.ini')]:
 t=time.monotonic();p=run('shell',f'cd /data/build; ./vtn-runner -d /data/build/vtn/libs/libvtn_mic6_circle.so -i /tmp/malbut-pc-source8.pcm -c 8 -f {cfg} -o /tmp/malbut-{name}.pcm --beam {beam} --bit 2 --loop 1')
 (out/f'{name}.log').write_bytes(p.stdout);run('pull',f'/tmp/malbut-{name}.pcm',str(out/f'{name}.pcm'));row=dict(name=name,bytes=(out/f'{name}.pcm').stat().st_size,wall_s=time.monotonic()-t);print(json.dumps(row),flush=True);rows.append(row)
(out/'results.json').write_text(json.dumps(rows,indent=2))
