from pathlib import Path
import subprocess,math
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly
root=Path('/tmp/malbut_aec_fix_20261028')
for name,text in [('wake_only','제이크야.'),('question','봄과 여름, 가을과 겨울에 대해 자세히 이야기해 줘.'),('interrupt_simple','그만하고 다른 이야기를 해 줘.')]:
 out=root/(name+'.wav');r=subprocess.run(['/home/ubuntu/.cache/malbut_speech/runtime/bin/piper','--model','/home/ubuntu/.cache/malbut_speech/models/piper-moss-round02-epoch40/ko_KR-moss_calm-medium.onnx','--output_file',str(out)],input=text,text=True,capture_output=True,timeout=60)
 if r.returncode:raise RuntimeError('Fixture synthesis failed')
 rate,x=wavfile.read(out);x=resample_poly(x.astype(np.float32)/32768,16000//math.gcd(rate,16000),rate//math.gcd(rate,16000));x=np.clip(x*(.18/np.sqrt(np.mean(x*x)+1e-12)),-.95,.95);wavfile.write(out,16000,(x*32768).astype(np.int16));print(name,len(x)/16000)
