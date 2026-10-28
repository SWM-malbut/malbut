from pathlib import Path
import os,subprocess,json,time,concurrent.futures
base=Path('/tmp/malbut_interaction_20261028');root=base/'adb-diag';out=base/'two_speaker/pc_probe_2/beams';out.mkdir(exist_ok=True)
adb=str(root/'root/usr/bin/adb');env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
def run(*a):return subprocess.run([adb,'-P','5042',*a],env=env,check=True,capture_output=True,timeout=60)
run('push',str(base/'two_speaker/pc_probe_2/gain30_raw8.pcm'),'/tmp/malbut-pc-source8.pcm')
def one(beam):
 t=time.monotonic();outname=f'beam{beam}'
 p=run('shell',f'cd /data/build; ./vtn-runner -d /data/build/vtn/libs/libvtn_mic6_circle.so -i /tmp/malbut-pc-source8.pcm -c 8 -f /data/build/vtn/vtn.ini -o /tmp/malbut-pc-{outname}.pcm --beam {beam} --bit 2 --loop 1')
 (out/(outname+'.log')).write_bytes(p.stdout);run('pull',f'/tmp/malbut-pc-{outname}.pcm',str(out/(outname+'.pcm')))
 row=dict(beam=beam,bytes=(out/(outname+'.pcm')).stat().st_size,wall_s=time.monotonic()-t);print(json.dumps(row),flush=True);return row
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(one,[0,1,2,3,4,5,-1,-3]))
(out/'results.json').write_text(json.dumps(rows,indent=2))
