"""Compare saved test audio through the existing, authorized API key."""
import json
from pathlib import Path
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'src/malbut/malbut_agent_server'))
from malbut_agent_server.config import Settings, load_env_file
from malbut_agent_server.factory import openai_key_for

settings = Settings.from_env(load_env_file(Path.home() / '.config/malbut/agent.env', target={}))
key = openai_key_for(settings).current()

def run(path):
    start = time.monotonic()
    cpu = time.process_time()
    result = {'file': path.name, 'model': 'gpt-transcribe'}
    try:
        boundary = uuid.uuid4().hex
        chunks = []
        for name, value in [('model', 'gpt-transcribe'), ('languages[]', 'ko')]:
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="utterance.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
        chunks.extend([path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
        req = urllib.request.Request('https://api.openai.com/v1/audio/transcriptions',
            data=b''.join(chunks), headers={'Authorization': 'Bearer ' + key,
                'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with urllib.request.urlopen(req, timeout=20) as response:
            result['text'] = json.load(response)['text']
    except Exception as e:
        result['error_type'] = type(e).__name__
        result['http_status'] = getattr(e, 'code', None)
    result.update(wall_s=time.monotonic()-start, process_cpu_s=time.process_time()-cpu)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result

if __name__ == '__main__':
    folder = ROOT / (sys.argv[1] if len(sys.argv)>1 else 'recordings')
    pattern = sys.argv[2] if len(sys.argv)>2 else '*.wav'
    output = ROOT / (sys.argv[3] if len(sys.argv)>3 else 'api_asr_probe.json')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, sorted(folder.glob(pattern))))
    output.write_text(json.dumps(results,ensure_ascii=False,indent=2))
