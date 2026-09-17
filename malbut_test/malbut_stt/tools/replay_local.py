"""Replay local mono 16 kHz PCM16 WAV audio through the real dialogue pipeline.

The input is paced like a 512-sample recorder; no microphone, speaker, ROS, or
remote service is opened. The replay deadline starts after local model loading.
By default dialogue starts active; --wake first requires a recognized wake word.
"""

import argparse
from copy import deepcopy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import struct
from threading import Event
from time import monotonic
import wave

from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.transcription import LocalWhisperTranscriber
from malbut_stt.wake import LocalWakeRecognizer


class _WavRecorder:
    """Supply real-time 32 ms chunks, then block after the added silence ends."""

    sample_rate = 16000
    frame_length = 512

    def __init__(self, pcm, tail_s=4.0):
        self.pcm = pcm + bytes(round(tail_s * self.sample_rate) * 2)
        self.offset = 0
        self.started_at = None
        self.finished_at = None
        self.stopped = Event()
        self.deleted = False
        self.chunks = 0

    def start(self):
        self.started_at = monotonic()

    def read(self):
        if self.offset >= len(self.pcm):
            self.stopped.wait()
            return [0] * self.frame_length
        target = self.started_at + (self.chunks + 1) * self.frame_length / self.sample_rate
        self.stopped.wait(max(0.0, target - monotonic()))
        if self.stopped.is_set():
            return [0] * self.frame_length
        frame = self.pcm[self.offset:self.offset + self.frame_length * 2]
        self.offset += len(frame)
        self.chunks += 1
        if self.offset >= len(self.pcm):
            self.finished_at = monotonic()
        frame = frame.ljust(self.frame_length * 2, b'\x00')
        return list(struct.unpack('<512h', frame))

    def stop(self):
        self.stopped.set()

    def delete(self):
        self.deleted = True
        self.pcm = b''


class _MeasuredModel:
    """Measure each real model call through complete consumption of lazy segments."""

    def __init__(self, model, calls, origin):
        self.model = model
        self.calls = calls
        self.origin = origin

    def transcribe(self, audio, **options):
        started = monotonic()
        call = {
            'index': len(self.calls) + 1, 'started_s': started - self.origin,
            'input_samples': len(audio), 'language': options.get('language'),
            'initial_prompt': options.get('initial_prompt'),
            'beam_size': (options.get('beam_size')
                          if getattr(self.model, 'beam_search', True) else None),
            'decoding': getattr(self.model, 'decoding', None),
            'text': None, 'error': None,
            'finished_s': None, 'elapsed_s': None,
        }
        self.calls.append(call)

        def finish():
            ended = monotonic()
            call.update(finished_s=ended - self.origin, elapsed_s=ended - started)

        try:
            segments, info = self.model.transcribe(audio, **options)
        except Exception as error:
            call['error'] = type(error).__name__
            finish()
            raise

        def measured_segments():
            text = []
            try:
                for segment in segments:
                    text.append(segment.text)
                    yield segment
                call['text'] = ''.join(text).strip()
            except Exception as error:
                call['error'] = type(error).__name__
                raise
            finally:
                finish()

        return measured_segments(), info


def _read_wav(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError('WAV must be an existing local file')
    with wave.open(str(path), 'rb') as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(),
                source.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ValueError('WAV must be uncompressed mono 16kHz PCM16')
        frames = source.getnframes()
        pcm = source.readframes(frames)
    if len(pcm) != frames * 2:
        raise ValueError('WAV contains truncated PCM data')
    return pcm, {
        'path': str(path), 'sample_rate': 16000, 'channels': 1, 'sample_width_bytes': 2,
        'samples': frames, 'duration_s': frames / 16000,
        'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
    }


def _idle(pipeline):
    # Replay completion must include a speculative endpoint job, even when the
    # first transcript was already emitted. These fields belong to this pipeline.
    return (pipeline.session.utterance_id is None and pipeline.pending_addressee is None
            and not pipeline._busy and pipeline._endpoint_job is None
            and pipeline.audio.empty() and pipeline.jobs.empty() and pipeline.results.empty())


def replay_wav(wav_path, transcriber=None, *, model_path=None, compute_type=None, backend=None,
               max_runtime_s=120.0, wake_required=False):
    """Return one replay report; a supplied model may be reused sequentially.

    ``max_runtime_s`` bounds observation after model loading. Shutdown may add
    the pipeline's bounded native-inference join grace. Reuse is safe only when
    ``clean_shutdown`` is true; cancellation does not stop native inference.
    ``wake_required`` starts in wake detection instead of active dialogue.
    """
    if (type(max_runtime_s) not in (int, float) or not math.isfinite(max_runtime_s)
            or max_runtime_s <= 0):
        raise ValueError('max_runtime_s must be finite and positive')
    if type(wake_required) is not bool:
        raise ValueError('wake_required must be a boolean')
    if compute_type is not None and compute_type not in ('int8', 'float32'):
        raise ValueError('compute_type must be int8 or float32')
    if backend not in (None, 'faster-whisper', 'mlx'):
        raise ValueError('backend must be faster-whisper or mlx')
    if backend == 'mlx' and compute_type is not None:
        raise ValueError('MLX uses fixed fp16; compute_type cannot be overridden')
    if transcriber is not None and backend is not None:
        raise ValueError('backend must be selected when constructing the reused model')
    if transcriber is not None and compute_type is not None:
        raise ValueError('compute_type must be selected when constructing the reused model')
    if transcriber is not None and isinstance(transcriber.model, _MeasuredModel):
        raise ValueError('the model is still owned by an unfinished replay')
    pcm, source = _read_wav(wav_path)
    import webrtcvad

    load_started = monotonic()
    reused = transcriber is not None
    if transcriber is None:
        if model_path is None:
            raise ValueError('a local model path or loaded transcriber is required')
        if backend == 'mlx':
            from malbut_stt.mlx_transcription import MlxWhisperTranscriber

            transcriber = MlxWhisperTranscriber(model_path)
        else:
            transcriber = LocalWhisperTranscriber(model_path, compute_type=compute_type or 'int8')
    load_s = None if reused else monotonic() - load_started
    vad = webrtcvad.Vad(2)
    calls, events, transcripts = [], [], []
    origin = monotonic()
    speech_spans, pending_spans, utterance_spans = {}, [], {}
    original_model = transcriber.model
    actual_backend = getattr(transcriber, 'backend',
                             'faster-whisper' if type(transcriber) is LocalWhisperTranscriber
                             else None)
    backend_compute_type = getattr(original_model, 'compute_type',
                                   getattr(getattr(original_model, 'model', None),
                                           'compute_type', None))
    if not isinstance(backend_compute_type, str):
        backend_compute_type = None
    transcriber.model = _MeasuredModel(original_model, calls, origin)
    recorder = _WavRecorder(pcm)

    def report(event):
        if event in ('speech_started', 'speech_discarded:busy') and pending_spans:
            span = pending_spans.pop(0)
            if event == 'speech_started':
                utterance_spans[pipeline.session.utterance_id] = span
        events.append({'event': event, 'at_s': monotonic() - origin})

    def is_speech(frame, rate):
        speech = vad.is_speech(frame, rate)
        if (speech and pipeline.session.active and pipeline._tail_stream is None
                and not pipeline.command_stream.discarding):
            # VAD runs before speech_started assigns a UID. Bind its collector's
            # span at that event; a later busy-discarded collector cannot alter it.
            collector = pipeline.command_stream.collector
            now = monotonic()
            if collector not in speech_spans:
                span = {'first': now, 'last': now}
                speech_spans[collector] = span
                if not collector.started:
                    pending_spans.append(span)
            else:
                speech_spans[collector]['last'] = now
        return speech

    def publish(uid, text):
        now = monotonic()
        span = utterance_spans.get(uid)
        transcripts.append({
            'utterance_id': uid, 'text': text, 'at_s': now - origin,
            'first_vad_speech_at_s': None if span is None else span['first'] - origin,
            'last_vad_speech_at_s': None if span is None else span['last'] - origin,
            'vad_last_speech_to_text_s': None if span is None else now - span['last'],
        })

    try:
        pipeline = DialoguePipeline(
            recorder_factory=lambda: recorder,
            wake=LocalWakeRecognizer.from_transcriber(transcriber), transcriber=transcriber,
            is_speech=is_speech, publish_transcript=publish,
            publish_control=lambda *_: report('unexpected_playback_control'),
            publish_interruption=lambda *_: report('unexpected_addressee_request'),
            report=report,
        )
    except Exception:
        transcriber.model = original_model
        raise
    status, error = 'completed', None
    try:
        if not wake_required:
            pipeline.session.activate()
        pipeline.start()
        report('replay_started:' + ('waiting_for_wake' if wake_required else 'session_active'))
        while True:
            pipeline.poll()
            now = monotonic()
            # Allow the capture thread to enqueue its last returned frame before
            # accepting an empty queue as EOF completion.
            if (recorder.finished_at is not None and now - recorder.finished_at >= 0.1
                    and _idle(pipeline)):
                break
            if now - origin >= max_runtime_s:
                status = 'timed_out'
                report('replay_timeout')
                break
            recorder.stopped.wait(0.005)
    except KeyboardInterrupt:
        status = 'interrupted'
    except Exception as exception:
        status, error = 'failed', type(exception).__name__
    finally:
        try:
            pipeline.close()
        except Exception as exception:
            status, error = 'failed', type(exception).__name__
    clean = (not pipeline.capture_thread or not pipeline.capture_thread.is_alive()) and (
        not pipeline.asr_thread or not pipeline.asr_thread.is_alive())
    if clean:
        transcriber.model = original_model
    packages = {}
    for package in ('faster-whisper', 'ctranslate2', 'mlx-whisper', 'mlx', 'webrtcvad-wheels'):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None
    return {
        'schema_version': 1, 'status': status, 'error': error, 'clean_shutdown': clean,
        'source': source, 'transcripts': transcripts, 'transcript_count': len(transcripts),
        'model_transcribe_calls': len(calls), 'inference': deepcopy(calls), 'events': events,
        'replay': {
            'frame_samples': 512, 'tail_s': 4.0, 'pacing': 'real_time',
            'max_runtime_s': max_runtime_s, 'elapsed_s': monotonic() - origin,
            'all_input_emitted': recorder.finished_at is not None,
            'emitted_chunks': recorder.chunks, 'vad_mode': 2,
            'vad_timing_basis': ('UID-correlated owner-thread VAD processing; '
                                 'not physical speech-end truth'),
            'wake_required': wake_required, 'audio_input': 'local_wav_without_playback',
        },
        'runtime': {
            'python': platform.python_version(), 'platform': platform.platform(),
            'packages': packages, 'model_reused': reused, 'model_load_s': load_s,
            'backend': actual_backend,
            'requested_backend': None if reused else backend or 'faster-whisper',
            'compute_type': backend_compute_type,
            'requested_compute_type': (None if reused else 'float16' if backend == 'mlx'
                                       else compute_type or 'int8'),
            'model_path': str(Path(model_path).expanduser().resolve()) if model_path else None,
        },
    }


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wav', required=True)
    parser.add_argument('--model-path', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--backend', choices=('faster-whisper', 'mlx'), default='faster-whisper')
    parser.add_argument('--compute-type', choices=('int8', 'float32'))
    parser.add_argument('--wake', action='store_true',
                        help='require a wake word first (default: dialogue starts active)')
    parser.add_argument('--max-runtime-s', type=float, default=120.0,
                        help='replay observation limit after loading the model (default: 120)')
    options = parser.parse_args(args)
    if options.backend == 'mlx' and options.compute_type is not None:
        parser.error('--compute-type cannot be used with the fixed-fp16 MLX backend')
    try:
        result = replay_wav(options.wav, model_path=options.model_path,
                            compute_type=options.compute_type,
                            backend=options.backend,
                            wake_required=options.wake,
                            max_runtime_s=options.max_runtime_s)
    except Exception as error:
        result = {'schema_version': 1, 'status': 'failed', 'error': type(error).__name__}
    output = Path(options.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'status': result['status']}, ensure_ascii=False))
    return 0 if result['status'] == 'completed' and result.get('clean_shutdown') else 1


if __name__ == '__main__':
    raise SystemExit(main())
