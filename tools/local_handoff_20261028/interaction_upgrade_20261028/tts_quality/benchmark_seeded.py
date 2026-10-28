"""Synthesis-only comparison on the same Jetson; no speaker or live-service changes."""
import os,sys,json,time,wave,threading,resource
from pathlib import Path
import numpy as np
root=Path(__file__).resolve().parent
sys.path.insert(0,str(root/'code'))
from malbut_tts.piper_synthesis import PiperSynthesizer
from malbut_tts.api_synthesis import OpenAISynthesizer
cases=[
 ('command','잠깐, 겨울 이야기만 한 문장으로 해줘.'),
 ('winter','겨울밤, 창밖에 내리던 눈이 작은 별빛처럼 마을을 포근히 감쌌어요.'),
 ('stop','네, 멈출게요.'),
 ('story','비 오는 날 창가에 앉아 있던 고양이가 빗방울 하나를 앞발로 톡 건드렸는데, 빗방울이 별처럼 반짝였대요.'),
 ('followup','퇴근길에 강아지 세 마리를 보셨군요. 어떤 모습이었나요?'),
]
backend=sys.argv[1];folder=root/backend;folder.mkdir(exist_ok=True)
if (folder/'result.json').exists():raise SystemExit('Refusing to overwrite result')
if backend.startswith('piper'):s=PiperSynthesizer('/home/ubuntu/.cache/malbut_speech/models/piper-moss-round02-epoch40/ko_KR-moss_calm-medium.onnx',threads=int(backend.split('_')[0].removeprefix('piper') or '6'))
else:s=OpenAISynthesizer(api_key=os.environ.get('OPENAI_API_KEY'),notice_path=None)
import onnxruntime as ort
ort.set_seed(20261028);np.random.seed(20261028)
t=time.monotonic();c=time.process_time();s.load();load=dict(wall_s=time.monotonic()-t,cpu_s=time.process_time()-c)
cases=[(f'{name}_r{rep}',text) for rep in range(3) for name,text in cases]
rows=[]
for index,(name,text) in enumerate(cases):
 t=time.monotonic();c=time.process_time();first=None;pcm=[];row=dict(id=name,text=text)
 try:
  for chunk,rate in s.generate(text,threading.Event()):
   if first is None:first=time.monotonic()-t
   pcm.append(chunk)
  row.update(wall_s=time.monotonic()-t,cpu_s=time.process_time()-c,first_chunk_s=first,sample_rate=rate)
  audio=np.concatenate(pcm);row['audio_duration_s']=len(audio)/rate
  row['rms_dbfs']=float(20*np.log10(np.sqrt(np.mean(audio*audio))+1e-12))
  with wave.open(str(folder/(name+'.wav')),'wb') as w:
   w.setparams((1,2,rate,0,'NONE',''));w.writeframes((np.clip(audio,-1,1)*32767).astype('<i2').tobytes())
  if backend.startswith('piper'):row['normalized_text']=s._normalizer(text)
 except Exception as e:row['error_type']=type(e).__name__;row['error_code']=getattr(e,'code',None)
 rows.append(row);print(json.dumps(dict(backend=backend,**row),ensure_ascii=False),flush=True)
 (folder/'result.json').write_text(json.dumps(dict(backend=backend,scope='Same Jetson synthesis only; first case includes lazy warm-up; no playback, no microphone, no whole-service CPU measurement',load=load,max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,cases=rows),ensure_ascii=False,indent=2))
