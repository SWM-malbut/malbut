"""Bounded microphone raw-dump diagnostic, restoring config and existing PCM files."""
import subprocess,os,time,json,pathlib
root=pathlib.Path('/tmp/malbut_interaction_20261028/adb-diag')
adb=str(root/'root/usr/bin/adb'); env=dict(os.environ,LD_LIBRARY_PATH=str(root/'root/usr/lib/aarch64-linux-gnu/android'))
folder=root/'debug_probe';folder.mkdir(exist_ok=True)
def run(*args,timeout=12):
 return subprocess.run([adb,'-P','5042',*args],env=env,check=True,capture_output=True,timeout=timeout)
def shell(s):return run('shell',s).stdout.decode(errors='replace').replace('\r','')
config='/data/build/user_cfg.ini'
before=shell('cat '+config)
assert before.count('debug=0')==1
(folder/'user_cfg.before.ini').write_text(before)
(folder/'user_cfg.debug.ini').write_text(before.replace('debug=0','debug=1',1))
existing={}
for name in ['origin.pcm','iat.pcm','ivw1.pcm','ivw2.pcm','ivw3.pcm']:
 try:
  run('pull','/data/build/'+name,str(folder/(name+'.before')));existing[name]=True
 except subprocess.CalledProcessError:existing[name]=False
print('Backup complete; enabling debug for five seconds.',flush=True)
try:
 run('push',str(folder/'user_cfg.debug.ini'),config)
 time.sleep(5)
 # No application restart; inspect whether watched config enables bounded dumps.
 (folder/'observed.txt').write_text(shell('ls -l /data/build/*.pcm; tail -12 /data/build/demo.log'))
finally:
 run('push',str(folder/'user_cfg.before.ini'),config)
 time.sleep(1)
 assert shell('cat '+config)==before
 print('Original config restored.',flush=True)
for name in existing:
 try:run('pull','/data/build/'+name,str(folder/(name+'.after')))
 except subprocess.CalledProcessError:pass
 if existing[name]:run('push',str(folder/(name+'.before')),'/data/build/'+name)
 # Retain newly created diagnostic files, but recording is disabled after restore.
(folder/'result.json').write_text(json.dumps({'restored_config':True,'saved_existing_pcm':existing,'duration_s':5},indent=2))
print((folder/'observed.txt').read_text(),flush=True)
