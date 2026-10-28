"""Measure the entire isolated speech service, normalized to six robot CPUs."""
import json,subprocess,time,sys,os
from pathlib import Path
unit=sys.argv[1];label=sys.argv[2];seconds=float(sys.argv[3]) if len(sys.argv)>3 else 60
def sample():
    data=subprocess.check_output(['systemctl','--user','show',unit,'-p','CPUUsageNSec','-p','MemoryCurrent','-p','ActiveState'],text=True)
    return dict(line.split('=',1) for line in data.splitlines())
first=sample();start=time.monotonic();time.sleep(seconds);last=sample();elapsed=time.monotonic()-start
result=dict(unit=unit,elapsed_s=elapsed,cpu_count=os.cpu_count(),cpu_percent_total=(int(last['CPUUsageNSec'])-int(first['CPUUsageNSec']))/1e9/elapsed/os.cpu_count()*100,memory_mb=int(last['MemoryCurrent'])/1e6,active_state=last['ActiveState'])
Path('/tmp/malbut_interaction_20261028/'+label+'.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result),flush=True)
