"""Compare fixed synthetic Korean WAVs with local Whisper and OpenAI STT."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
from time import monotonic
import wave

from malbut_stt.local_wake import is_wake_phrase
from malbut_stt.transcription import OpenAITranscriber


def write_json(path, data):
    """Write human-readable experiment metadata without credentials."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def read_pcm(path):
    """Load the exact mono PCM16 16kHz samples shared by both engines."""
    with wave.open(str(path)) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        return audio.readframes(audio.getnframes())


def prepare(corpus_path, output):
    """Freeze the corpus and generated waveforms before running any recognizer."""
    fingerprint = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    metadata_path = output / 'experiment.json'
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text())
        if metadata['corpus_sha256'] != fingerprint:
            raise ValueError('corpus changed: use a new output directory')
        for case in metadata['cases']:
            pcm = read_pcm(output / case['wav'])
            if hashlib.sha256(pcm).hexdigest() != case['pcm_sha256']:
                raise ValueError('waveform changed: use a new output directory')
        return metadata

    output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(corpus_path.read_text())
    for case in cases:
        path = output / (case['id'] + '.wav')
        if case['text']:
            subprocess.run([
                'say', '-v', 'Yuna', '-r', str(case['rate']), '--file-format=WAVE',
                '--data-format=LEI16@16000', '--channels=1', '-o', str(path), case['text'],
            ], check=True, capture_output=True)
            pcm = b'\0' * 9600 + read_pcm(path) + b'\0' * 12800
        else:
            pcm = b'\0' * 64000
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(pcm)
        case.update(wav=path.name, audio_s=len(pcm) / 32000,
                    pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                    wav_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    metadata = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'corpus_sha256': fingerprint,
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'platform': platform.platform(), 'python': platform.python_version(),
        'packages': {name: importlib.metadata.version(name)
                     for name in ('openai', 'faster-whisper', 'ctranslate2')},
        'tts': 'macOS say Yuna; 0.3s leading + 0.4s trailing silence; silence case 2s',
        'local': {'model': 'small', 'device': 'cpu', 'compute_type': 'int8',
                  'cpu_threads': 6, 'beam_size': 1, 'language': 'ko',
                  'condition_on_previous_text': False},
        'local_hint': '로봇 이름은 제이크입니다.',
        'openai': {'model': 'gpt-transcribe', 'languages': ['ko'],
                   'timeout_s': 30, 'max_retries': 0},
        'timing': 'one timed run per case/engine; local warmed; API includes network',
        'cases': cases,
    }
    write_json(metadata_path, metadata)
    return metadata


def run(output, metadata, engine, transcribe):
    """Checkpoint each attempt; never automatically repeat an API request."""
    result_path = output / 'results.jsonl'
    attempt_path = output / 'attempts.jsonl'
    attempted = set()
    if attempt_path.exists():
        attempted = {(r['id'], r['engine']) for r in
                     (json.loads(line) for line in attempt_path.read_text().splitlines())}
    for case in metadata['cases']:
        if (case['id'], engine) in attempted:
            continue
        row = {'id': case['id'], 'engine': engine, 'pcm_sha256': case['pcm_sha256']}
        with attempt_path.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        pcm = read_pcm(output / case['wav'])
        started = monotonic()
        try:
            text = transcribe(pcm)
            row.update(status='ok', text=text, wake_detected=is_wake_phrase(text))
        except Exception as error:
            row.update(status='error', error=type(error).__name__)
        row['elapsed_s'] = round(monotonic() - started, 6)
        with result_path.open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row.get('error') in ('AuthenticationError', 'PermissionDeniedError', 'RateLimitError'):
            break


def main():
    """Generate once, run local comparisons, or explicitly call the paid API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['prepare', 'local', 'openai'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, default=(
        Path(__file__).resolve().parents[1] / 'test/fixtures/stt_comparison_ko.json'
    ))
    parser.add_argument('--model-path', type=Path)
    options = parser.parse_args()
    metadata = prepare(options.corpus, options.output)
    if options.stage == 'prepare':
        print(json.dumps({'cases': len(metadata['cases']),
                          'total_audio_s': sum(c['audio_s'] for c in metadata['cases'])}))
    elif options.stage == 'local':
        if not options.model_path or not options.model_path.is_dir():
            parser.error('--model-path must be a downloaded model directory')
        import numpy as np
        from faster_whisper import WhisperModel

        started = monotonic()
        model = WhisperModel(str(options.model_path), device='cpu', compute_type='int8',
                             cpu_threads=6, local_files_only=True)
        metadata['local_model_load_s'] = round(monotonic() - started, 6)
        # Silence warmup does not use an evaluation case or call an external service.
        segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language='ko',
                                       beam_size=1, condition_on_previous_text=False)
        list(segments)
        metadata['local_warmup'] = 'one second of silence; excluded from timings'
        write_json(options.output / 'experiment.json', metadata)
        for engine, hint in [('local_base', None), ('local_hint', metadata['local_hint'])]:
            def transcribe(pcm):
                samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
                segments, _ = model.transcribe(
                    samples, language='ko', beam_size=1, condition_on_previous_text=False,
                    initial_prompt=hint,
                )
                return ''.join(segment.text for segment in segments).strip()
            run(options.output, metadata, engine, transcribe)
    else:
        from openai import OpenAI

        with OpenAI(api_key=os.environ['OPENAI_API_KEY'],
                    base_url='https://api.openai.com/v1', timeout=30, max_retries=0) as client:
            transcriber = OpenAITranscriber(client)
            run(options.output, metadata, 'openai', lambda pcm: transcriber.transcribe(pcm, 16000))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'error': type(error).__name__}), flush=True)
        raise SystemExit(1) from None
