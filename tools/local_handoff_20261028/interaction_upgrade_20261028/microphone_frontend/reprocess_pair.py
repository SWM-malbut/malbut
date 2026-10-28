"""Use the board's existing offline runner on saved audio, without live config edits."""
from pathlib import Path
import os,subprocess,json,time
from concurrent.futures import ThreadPoolExecutor
root=Path('/tmp/malbut_interaction_20261028/adb-diag');folder=root/'paired_human_2/reprocessed';folder.mkdir(exist_ok=True)
adb=str(root/'root/usr/bin/adb');env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
def run(*args,timeout=180):return subprocess.run([adb,'-P','5042',*args],env=env,check=True,capture_output=True,timeout=timeout)
run('push',str(root/'paired_human_2/array_raw_8ch.pcm'),'/tmp/malbut-human-array.pcm')
rows=[]
def process(beam):
 start=time.monotonic()
 cmd=f'cd /data/build; ./vtn-runner -d /data/build/vtn/libs/libvtn_mic6_circle.so -i /tmp/malbut-human-array.pcm -c 8 -f /data/build/vtn/vtn.ini -o /tmp/malbut-full-beam{beam}.pcm --beam {beam} --bit 2 --loop 1'
 result=run('shell',cmd)
 (folder/f'beam{beam}.log').write_bytes(result.stdout)
 run('pull',f'/tmp/malbut-full-beam{beam}.pcm',str(folder/f'beam{beam}.pcm'))
 row={'beam':beam,'bytes':(folder/f'beam{beam}.pcm').stat().st_size,'wall_s':time.monotonic()-start};print(json.dumps(row),flush=True);return row
with ThreadPoolExecutor(max_workers=2) as executor:rows=list(executor.map(process,range(6)))
(folder/'results.json').write_text(json.dumps(rows,indent=2))
