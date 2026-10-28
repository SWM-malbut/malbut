import subprocess,wave,math,json
from pathlib import Path
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly
root=Path('/tmp/malbut_aec_fix_20261028');release=Path('/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028')
for name,text in [('wake','제이크야, 대한민국의 사계절을 차례대로 자세히 설명해 줘.'),('interrupt','잠깐만. 한 문장으로 짧게 알려 줘.')]:
 output=root/(name+'_generated.wav')
 p=subprocess.run(['/home/ubuntu/.cache/malbut_speech/runtime/bin/piper','--model','/home/ubuntu/.cache/malbut_speech/models/piper-moss-round02-epoch40/ko_KR-moss_calm-medium.onnx','--output_file',str(output)],input=text,text=True,capture_output=True,timeout=60)
 if p.returncode:raise RuntimeError('Piper fixture synthesis failed')
 rate,x=wavfile.read(output);y=resample_poly(x.astype(np.float32)/32768,16000//math.gcd(rate,16000),rate//math.gcd(rate,16000));gain=min(.16/np.sqrt(np.mean(y*y)+1e-12),.65/(np.max(abs(y))+1e-12));wavfile.write(root/(name+'.wav'),16000,(y*gain*32768).astype(np.int16))
 print(name,round(len(y)/16000,3))
modules=[]
try:
 modules.append(subprocess.check_output(['pactl','load-module','module-null-sink','sink_name=malbut_aec_injection_20261028','rate=16000','channels=1','channel_map=mono'],text=True).strip())
 modules.append(subprocess.check_output(['pactl','load-module','module-loopback','source=alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback','sink=malbut_aec_injection_20261028','latency_msec=50','adjust_time=0','source_dont_move=true','sink_dont_move=true'],text=True).strip())
 import yaml
 cfg=yaml.safe_load((release/'stt.yaml').read_text());cfg['/**']['ros__parameters']['aec_source']='malbut_aec_injection_20261028.monitor';(release/'stt-injection.yaml').write_text(yaml.safe_dump(cfg))
 (root/'injection_modules.json').write_text(json.dumps(modules))
 print('Owned injection modules created',modules)
except BaseException:
 for m in reversed(modules):subprocess.run(['pactl','unload-module',m])
 raise
