from pathlib import Path
import wave,json,time
from malbut_stt.cpp_transcription import CppWhisperTranscriber
root=Path('/tmp/malbut_aec_fix_20261028')
t=CppWhisperTranscriber('/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin','/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so',use_gpu=True,n_threads=6,decode_timeout_s=30)
rows=[]
try:
 for name in ['wake','interrupt']:
  with wave.open(str(root/(name+'.wav'))) as w:pcm=w.readframes(w.getnframes())
  start=time.monotonic();text=t.transcribe(pcm,16000);rows.append(dict(name=name,text=text,seconds=time.monotonic()-start));print(rows[-1],flush=True)
finally:t.close()
(root/'fixture_asr.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
