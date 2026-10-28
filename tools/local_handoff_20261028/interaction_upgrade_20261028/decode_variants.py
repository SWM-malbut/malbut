from pathlib import Path
import wave,json,time,sys
from malbut_stt.cpp_transcription import CppWhisperTranscriber
root=Path('/tmp/malbut_interaction_20261028')
t=CppWhisperTranscriber('/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin', '/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so', use_gpu=True,n_threads=6,decode_timeout_s=30)
rows=[]
try:
 for p in sorted((root/(sys.argv[1] if len(sys.argv)>1 else 'variants')).glob('*.wav')):
  if p.stem.endswith(('_vad','_raw')) or p.stem.endswith('_quiet_clean'):continue
  with wave.open(str(p)) as w:pcm=w.readframes(w.getnframes())
  start=time.monotonic();cpu=time.process_time();text=t.transcribe(pcm,16000)
  row=dict(file=p.name,text=text,wall_s=time.monotonic()-start,cpu_s=time.process_time()-cpu)
  rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
finally:t.close()
(root/(sys.argv[2] if len(sys.argv)>2 else 'variant_asr.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2))
