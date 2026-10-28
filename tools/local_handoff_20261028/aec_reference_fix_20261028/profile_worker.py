from recorder_trial import *
import cProfile,pstats,threading
from malbut_stt.echo.worker import run
if __name__=='__main__':
 import subprocess
 listing=lambda k:json.loads(subprocess.check_output(['pactl','--format=json','list',k],text=True))
 source=next(s['name'] for s in listing('sources') if 'iflytek_XFM' in s['name']);sink=next(s['name'] for s in listing('sinks') if 'GeneralPlus' in s['name'])
 stop=threading.Event();threading.Timer(35,stop.set).start()
 class Output:
  def put(self,item,**kw):
   if item[0]=='ready':threading.Thread(target=lambda:subprocess.run(['paplay','--device='+sink,str(root/'heldout.wav')]),daemon=True).start()
   elif item[0]=='error':print(item,flush=True)
 profile=cProfile.Profile();t0=time.process_time();profile.runcall(run,dict(source=source,sink=sink,models=str(root),calibration_audio=str(root/'calibration.wav')),Output(),stop)
 print('CPU',time.process_time()-t0)
 pstats.Stats(profile).sort_stats('tottime').print_stats(30)
