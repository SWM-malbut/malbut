"""Human speech mixed with matched real robot echo; no microphone-room claim."""
from pathlib import Path
import sys,json,wave,math,time
import numpy as np
from scipy.signal import correlate,correlation_lags,resample_poly
from scipy.io import wavfile
ROOT=Path(__file__).resolve().parent;OLD=ROOT.parent/'aec_reference_fix_20261028'
sys.path[:0]=[str(OLD/'deps/x86_64'),str(ROOT.parent/'src/malbut/malbut_stt')]
from malbut_stt.echo.processor import EchoProcessor
from malbut_stt.echo.vad import SuppressedSilenceVad
from malbut_stt.silero_stream import SileroStreamVad
from malbut_stt.audio import CaptureSettings,CapturePCM
from malbut_stt.streaming import StreamingUtteranceCollector
def read(p):
 rate,y=wavfile.read(p);y=y.astype(np.float32)/32768
 if y.ndim==2:y=y.mean(1)
 if rate!=16000:y=resample_poly(y,16000//math.gcd(rate,16000),rate//math.gcd(rate,16000))
 return y
def write(p,y):
 p.parent.mkdir(exist_ok=True)
 wavfile.write(p,16000,(y*32768).clip(-32768,32767).astype('<i2'))
pair=np.fromfile(ROOT/'paired.f32','<f4').reshape(-1,2);h=read(ROOT/'recordings/clean_interrupt.wav')
c=correlate(pair[:,0],h,method='fft');lags=correlation_lags(len(pair),len(h));start=int(lags[np.argmax(c)])
gain=float(pair[start:start+len(h),0]@h/(h@h))
echo=pair[:,0].copy();echo[start:start+len(h)]-=h*gain
# This replay preserves the original robot STOP time. Later robot replies are
# excluded; onset/detection is therefore a fixed-input comparison, not a live loop.
echo[start+48000:]=0
far=pair[:,1].copy();far[start+48000:]=0
train=np.load(ROOT/'training.npz')
manifest=json.loads((ROOT.parent/'reachy_start_vs_silero_20261027/evaluation_manifest.json').read_text())
cases=[c for c in manifest['cases'] if c['label_speech'] and c['transform']=='original']
rows=[]
for case in cases:
 near=read(case['path'])
 for level in [.18,.06]:
  normalized=near*(level/np.sqrt(np.mean(near*near)+1e-12))
  n=math.ceil((start+len(near)+32000)/512)*512
  x=np.pad(echo,(0,max(0,n-len(echo))))[:n].copy()
  ref=np.pad(far,(0,max(0,n-len(far))))[:n]
  x[start:start+len(near)]+=normalized
  f=EchoProcessor(float(train['gain']),OLD/'dtln')
  f.process_array(train['mic'],train['far'])
  cpu=time.process_time();v=f.process_array(x,ref);cost=time.process_time()-cpu
  stream=StreamingUtteranceCollector(SuppressedSilenceVad(SileroStreamVad()).is_speech,
      settings=CaptureSettings(pre_roll_s=1.,min_speech_s=.16,silence_timeout_s=.8,max_utterance_s=40))
  begin=start-16000;end=start+len(near)+16000
  ident=case['id']+('_normal' if level==.18 else '_quiet')
  # Full recognition input isolates ASR from detection/cropping.
  write(ROOT/'human'/f'{ident}_aec.wav',f.asr_output[begin:end])
  write(ROOT/'human'/f'{ident}_raw.wav',np.pad(x,(512,0))[begin:end])
  write(ROOT/'human'/f'{ident}_clean.wav',np.pad(normalized,(16000,16000)))
  detected=[]
  for i in range(begin,end,512):
   pcm=(f.asr_output[i:i+512]*32768).clip(-32768,32767).astype('<i2').tobytes()
   vad=(v[i:i+512]*32768).clip(-32768,32767).astype('<i2').tobytes()
   for event in stream.feed(CapturePCM(pcm,vad)):
    if event.status=='speech_started':detected.append((i+512-start)/16000)
  row=dict(id=ident,text=case['text'],onsets_s=detected,dsp_cpu_s=cost,audio_s=n/16000)
  if detected:
   for pre in [1,2]:
    offset=max(0,start+round((detected[0]-.16-pre)*16000))
    write(ROOT/'human'/f'{ident}_capture{pre}.wav',f.asr_output[offset:end])
  rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
(ROOT/'human_manifest.json').write_text(json.dumps(dict(cases=rows,injection_gain=gain,scope='Fixed recorded echo and stop time; public human audio mixed after microphone processing'),ensure_ascii=False,indent=2))
