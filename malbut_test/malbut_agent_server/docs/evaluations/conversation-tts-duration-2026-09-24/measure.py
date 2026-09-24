import hashlib,json,os,platform,sys,time,wave
from datetime import datetime,timezone
from pathlib import Path
from threading import Event
import httpx,numpy as np,openai
from malbut_tts.api_synthesis import OpenAISynthesizer

assert '--live' in sys.argv, 'pass --live for the bounded paid evaluation'
root=Path('malbut_agent_server/docs/evaluations')
source=root/'conversation-spec-final-followup-2026-09-24.json'
directory=root/'conversation-tts-duration-2026-09-24'
assert not directory.exists(), 'existing run must not be repeated or overwritten'
directory.mkdir()
case=next(c for c in json.loads(source.read_text())['cases'] if c['id']=='long_explanation')
assert len(case['turns'])==2
report={'created_at':datetime.now(timezone.utc).isoformat(),'source':source.name,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'synthetic_only':True,'physical_devices_used':False,'scope':'Production OpenAISynthesizer streamed to WAV, without ROS or speaker. Existing text replay, not fresh conversation generation.','max_api_requests':2,'requests':[],'cases':[],'environment':{'python':platform.python_version(),'openai':openai.__version__,'numpy':np.__version__,'tts_source_sha256':hashlib.sha256(Path('malbut_tts/malbut_tts/api_synthesis.py').read_bytes()).hexdigest()}}
output=directory/'results.json'
def save(): output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
async def before_request(request):
 assert str(request.url)=='https://api.openai.com/v1/audio/speech'
 assert len(report['requests'])<2
 report['requests'].append({'method':request.method,'url':str(request.url),'body':json.loads(request.content)})
 save()
def factory(**kwargs):
 assert kwargs['max_retries']==0 and kwargs['base_url']=='https://api.openai.com/v1'
 return openai.AsyncOpenAI(http_client=httpx.AsyncClient(event_hooks={'request':[before_request]}),**kwargs)
for number,turn in enumerate(case['turns'],1):
 text=turn['decision']['message']
 assert 0<len(text)<=250
 row={'id':f'explanation-{number}','text':text,'characters':len(text),'model':'gpt-4o-mini-tts','voice':'marin','sample_rate':24000,'channels':1,'sample_width_bytes':2,'completed':False,'pcm_frames':0,'chunks':0}
 report['cases'].append(row);save()
 cancel=Event();started=time.monotonic();path=directory/f'explanation-{number}.wav'
 try:
  with wave.open(str(path),'wb') as wav:
   wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
   synth=OpenAISynthesizer(api_key=os.environ['OPENAI_API_KEY'],client_factory=factory)
   row['timeout_seconds']=synth.timeout_seconds
   for chunk,rate in synth.generate(text,cancel):
    assert rate==24000 and chunk.ndim==1 and len(chunk)>0 and np.isfinite(chunk).all()
    if not row['chunks']: row['first_buffer_seconds']=round(time.monotonic()-started,3)
    wav.writeframes(np.rint(chunk*32768).clip(-32768,32767).astype('<i2').tobytes())
    row['pcm_frames']+=len(chunk);row['chunks']+=1
   assert row['pcm_frames']>0 and not cancel.is_set()
   row['completed']=True
 except Exception as error: row['error_type']=type(error).__name__
 finally:
  row['wall_seconds']=round(time.monotonic()-started,3)
  row['pcm_duration_seconds']=row['pcm_frames']/24000
  row['wav']=path.name;row['wav_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
  save();print(row['id'],row['completed'],row['pcm_duration_seconds'],row['wall_seconds'],row.get('error_type'),flush=True)
report['completed_at']=datetime.now(timezone.utc).isoformat();save()
