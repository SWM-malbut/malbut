"""Offline clip-level WebRTC/Silero comparison, optionally with the same Whisper input.

No ROS, microphone, speaker, network, or production settings are used. This is
an input-admission experiment, not a replay of streaming endpoint decisions.
"""

import argparse
import csv
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
from time import perf_counter
import wave


RATE = 16000
MAX_SAMPLES = 60 * RATE


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_wav(path):
    with wave.open(str(path), 'rb') as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(),
                source.getcomptype()) != (1, 2, RATE, 'NONE'):
            raise ValueError('WAV must be uncompressed mono 16kHz PCM16')
        count = source.getnframes()
        if not 0 < count <= MAX_SAMPLES:
            raise ValueError('WAV duration must be greater than zero and at most 60 seconds')
        pcm = source.readframes(count)
    if len(pcm) != count * 2:
        raise ValueError('WAV contains truncated PCM')
    return pcm


def load_cases(manifest):
    manifest = Path(manifest).resolve()
    cases = json.loads(manifest.read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError('manifest must be a nonempty list')
    seen, loaded = set(), []
    for item in cases:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
            raise ValueError('each case needs a nonempty text id')
        if item['id'] in seen:
            raise ValueError('duplicate case id')
        seen.add(item['id'])
        if item.get('label') not in ('speech', 'non_speech', 'unknown'):
            raise ValueError('case label must be speech, non_speech or unknown')
        if item.get('initial_prompt') is not None and not isinstance(item['initial_prompt'], str):
            raise ValueError('initial_prompt must be text or null')
        path = (manifest.parent / item['wav']).resolve()
        pcm = read_wav(path)
        digest = hashlib.sha256(pcm).hexdigest()
        if item.get('pcm_sha256', digest) != digest:
            raise ValueError(f"PCM checksum mismatch: {item['id']}")
        loaded.append((dict(item, wav=str(path), pcm_sha256=digest), pcm))
    return loaded


def summarize_frames(frames, min_speech_ms):
    """Use valid input samples only; zero padding cannot satisfy the gate."""
    longest = run = voiced = 0
    intervals = []
    for frame in frames:
        count = frame['end_sample'] - frame['start_sample']
        if frame['speech']:
            voiced += count
            run += count
            longest = max(longest, run)
            if intervals and intervals[-1][1] == frame['start_sample']:
                intervals[-1][1] = frame['end_sample']
            else:
                intervals.append([frame['start_sample'], frame['end_sample']])
        else:
            run = 0
    return dict(voiced_s=voiced / RATE, longest_run_s=longest / RATE,
                gate_pass=longest >= math.ceil(min_speech_ms * RATE / 1000),
                intervals_s=[[a / RATE, b / RATE] for a, b in intervals], frames=frames)


class WebRtcDetector:
    def __init__(self, mode=2):
        if type(mode) is not int or mode not in range(4):
            raise ValueError('WebRTC mode must be 0..3')
        self.mode = mode

    def frames(self, pcm):
        import webrtcvad

        # WebRTC has state too; never carry the preceding case into this case.
        model = webrtcvad.Vad(self.mode)
        frames = []
        for offset in range(0, len(pcm), 640):
            block = pcm[offset:offset + 640]
            frames.append(dict(start_sample=offset // 2,
                               end_sample=(offset + len(block)) // 2,
                               speech=bool(model.is_speech(block.ljust(640, b'\0'), RATE))))
        return frames


class SileroDetector:
    """NumPy ONNX runner for the official Silero v6 recurrent model.

    State/context protocol follows OnnxWrapper in snakers4/silero-vad at
    5cd7945676eb32225748052e2e6a0580e4686a08 (v6.2.3). At 16kHz each 512-sample
    window receives the previous 64 samples and a (2, 1, 128) recurrent state.
    We expose raw frame scores, not Silero's get_speech_timestamps heuristics.
    """
    def __init__(self, path, threshold=.5):
        if not math.isfinite(threshold) or not 0 < threshold < 1:
            raise ValueError('Silero threshold must be between zero and one')
        from malbut_stt.silero_vad import SileroVad

        self.model = SileroVad(path)
        self.threshold = threshold

    def frames(self, pcm):
        self.model.reset()
        frames = []
        for offset in range(0, len(pcm), 1024):
            block = pcm[offset:offset + 1024]
            probability = self.model.probability(block.ljust(1024, b'\0'))
            frames.append(dict(start_sample=offset // 2, end_sample=(offset + len(block)) // 2,
                               probability=probability, speech=probability >= self.threshold))
        return frames


def decode(pcm, transcriber, prompt=None):
    import numpy as np

    started = perf_counter()
    try:
        segments, info = transcriber.model.transcribe(
            np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0,
            language='ko', beam_size=1, condition_on_previous_text=False,
            initial_prompt=prompt)
        segments = [dict(text=s.text, start=s.start, end=s.end) for s in segments]
        return dict(status='ok', text=''.join(s['text'] for s in segments).strip(),
                    segments=segments, native_decode=info.native_decode,
                    elapsed_s=perf_counter() - started)
    except Exception as error:
        return dict(status='error', error=f'{type(error).__name__}: {error}',
                    native_decode=getattr(error, 'native_decode', None),
                    elapsed_s=perf_counter() - started)


def compare_case(case, pcm, detectors, min_speech_ms=80, transcriber=None):
    if not math.isfinite(min_speech_ms) or not 0 < min_speech_ms <= 60000:
        raise ValueError('min_speech_ms must be in (0, 60000]')
    result = dict(case, duration_s=len(pcm) / (RATE * 2), vad={})
    for name, detector in detectors.items():
        started = perf_counter()
        result['vad'][name] = summarize_frames(detector.frames(pcm), min_speech_ms)
        result['vad'][name]['elapsed_s'] = perf_counter() - started
    # Same complete PCM is decoded exactly once. Gates neither crop/reorder the
    # waveform nor alter the decoder. This isolates admission from recognition.
    if transcriber is not None:
        result['raw_whisper'] = decode(pcm, transcriber, case.get('initial_prompt'))
        for value in result['vad'].values():
            value['would_emit_nonempty_text'] = (
                value['gate_pass'] and bool(result['raw_whisper']['text'])
                if result['raw_whisper']['status'] == 'ok' else None)
    return result


def summarize_results(results):
    summary = {}
    for name in ('webrtc', 'silero'):
        known = [r for r in results if r['label'] != 'unknown']
        speech = [r for r in known if r['label'] == 'speech']
        negative = [r for r in known if r['label'] == 'non_speech']
        summary[name] = dict(
            speech_cases=len(speech), non_speech_cases=len(negative),
            unknown_cases=len(results) - len(known),
            speech_rejected=sum(not r['vad'][name]['gate_pass'] for r in speech),
            non_speech_admitted=sum(r['vad'][name]['gate_pass'] for r in negative),
            non_speech_with_text=sum(r['vad'][name].get('would_emit_nonempty_text') is True
                                     for r in negative),
            whisper_evaluated_cases=sum(
                'raw_whisper' in r and r['raw_whisper']['status'] == 'ok' for r in results))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--silero-model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    parser.add_argument('--webrtc-mode', type=int, choices=range(4), default=2)
    parser.add_argument('--silero-threshold', type=float, default=.5)
    parser.add_argument('--min-speech-ms', type=float, default=80)
    parser.add_argument('--whisper-model', type=Path)
    parser.add_argument('--whisper-library', type=Path)
    parser.add_argument('--threads', type=int, default=6)
    args = parser.parse_args(argv)
    if bool(args.whisper_model) != bool(args.whisper_library):
        parser.error('supply both --whisper-model and --whisper-library')
    if not math.isfinite(args.min_speech_ms) or not 0 < args.min_speech_ms <= 60000:
        parser.error('--min-speech-ms must be in (0, 60000]')
    cases = load_cases(args.manifest)
    detectors = dict(webrtc=WebRtcDetector(args.webrtc_mode),
                     silero=SileroDetector(args.silero_model, args.silero_threshold))
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = dict(
        scope='offline_clip_admission_only', manifest_sha256=file_hash(args.manifest),
        runner_sha256=file_hash(__file__), platform=platform.platform(),
        packages={name: version(name) for name in ('numpy', 'onnxruntime', 'webrtcvad-wheels')},
        webrtc_mode=args.webrtc_mode, webrtc_frame_ms=20, silero_frame_ms=32,
        silero_model=str(args.silero_model.resolve()),
        silero_sha256=file_hash(args.silero_model), silero_threshold=args.silero_threshold,
        min_speech_ms=args.min_speech_ms,
        limitations=['No live pipeline, acoustic echo, capture-device or endpoint replay.',
                     'Raw frame threshold plus consecutive-speech gate; no segment cropping.',
                     'Labels/provenance come from the manifest; unknown is excluded from errors.',
                     'CPU Whisper results need not match the robot CUDA backend.'])
    transcriber, results = None, []
    try:
        if args.whisper_model:
            from malbut_stt.cpp_transcription import CppWhisperTranscriber

            transcriber = CppWhisperTranscriber(
                args.whisper_model, args.whisper_library, use_gpu=False, n_threads=args.threads)
            metadata['whisper'] = dict(transcriber.metadata,
                                       model_sha256=file_hash(args.whisper_model),
                                       library_sha256=file_hash(args.whisper_library))
        (args.output / 'experiment.json').write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        with (args.output / 'results.jsonl').open('w') as stream:
            for case, pcm in cases:
                result = compare_case(case, pcm, detectors, args.min_speech_ms, transcriber)
                results.append(result)
                stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                progress = dict(id=case['id'], gates={
                    name: v['gate_pass'] for name, v in result['vad'].items()},
                    text=result.get('raw_whisper', {}).get('text'))
                print(json.dumps(progress, ensure_ascii=False), flush=True)
    finally:
        if transcriber:
            transcriber.close()
    summary = summarize_results(results)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    with (args.output / 'comparison.csv').open('w') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'label', 'duration_s', 'webrtc_gate', 'silero_gate',
                         'webrtc_voiced_s', 'silero_voiced_s', 'raw_whisper_text'])
        for r in results:
            writer.writerow([r['id'], r['label'], r['duration_s'],
                             r['vad']['webrtc']['gate_pass'], r['vad']['silero']['gate_pass'],
                             r['vad']['webrtc']['voiced_s'], r['vad']['silero']['voiced_s'],
                             r.get('raw_whisper', {}).get('text')])
    return 1 if any(r.get('raw_whisper', {}).get('status') == 'error' for r in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
