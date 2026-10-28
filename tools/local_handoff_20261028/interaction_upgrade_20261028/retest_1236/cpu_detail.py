import json,subprocess,time,os,sys
from pathlib import Path
unit=sys.argv[1]; label=sys.argv[2];seconds=float(sys.argv[3]) if len(sys.argv)>3 else 30
props=dict(x.split('=',1) for x in subprocess.check_output(['systemctl','--user','show',unit,'-p','ControlGroup'],text=True).splitlines())
cgroup=Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/')
def sample():
 props=dict(x.split('=',1) for x in subprocess.check_output(['systemctl','--user','show',unit,'-p','CPUUsageNSec','-p','MemoryCurrent','-p','ActiveState'],text=True).splitlines())
 result={}
 for pid in (cgroup/'cgroup.procs').read_text().split():
  try:
   p=Path('/proc')/pid; stat=p.joinpath('stat').read_text().rsplit(')',1)[1].split();cmd=p.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode()
   result[pid]={'ticks':int(stat[11])+int(stat[12]),'cmd':cmd}
  except (FileNotFoundError,ProcessLookupError): pass
 return {'mono':time.monotonic(),'time':time.time(),'processes':result,**props}
a=sample();time.sleep(seconds);b=sample();dt=b['mono']-a['mono'];ticks=os.sysconf('SC_CLK_TCK')
rows=[]
for pid,v in b['processes'].items():
 if pid in a['processes']:rows.append({'pid':pid,'cpu_percent_one_core':(v['ticks']-a['processes'][pid]['ticks'])/ticks/dt*100,'cmd':v['cmd']})
result={'unit':unit,'elapsed_s':dt,'cpu_count':os.cpu_count(),'cpu_percent_total':(int(b['CPUUsageNSec'])-int(a['CPUUsageNSec']))/1e9/dt/os.cpu_count()*100,'memory_mb':int(b['MemoryCurrent'])/1e6,'active_state':b['ActiveState'],'start':a['time'],'end':b['time'],'processes':rows}
Path('/tmp/malbut_interaction_20261028/retest_1236/'+label+'.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
