"""Exercise the robot's microphone/STT pipeline on a laptop without ROS."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from time import monotonic
from typing import Optional, Sequence

from malbut_stt.audio import CaptureSettings
from malbut_stt.pipeline import SpeechPipeline
from malbut_stt.transcription import LocalWhisperTranscriber, OpenAITranscriber
from malbut_stt.wake import LocalWakeRecognizer


def main(args: Optional[Sequence[str]] = None) -> int:
    """Print final transcripts locally; manual mode replaces only wake detection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list-devices', action='store_true')
    parser.add_argument('--manual', action='store_true', help='press Enter instead of a wake word')
    parser.add_argument('--wake-only', action='store_true', help='detect wake words without STT')
    parser.add_argument('--local', action='store_true', help='transcribe commands locally without API')
    parser.add_argument('--dialogue', action='store_true',
                        help='continuous local dialogue; no TTS events to test its 5s timeout')
    parser.add_argument('--model-path', help='downloaded model directory for the local backend')
    parser.add_argument('--backend', choices=('faster-whisper', 'mlx'),
                        help='local inference backend (default: faster-whisper)')
    parser.add_argument('--compute-type', choices=('int8', 'float32'),
                        help='local Whisper compute type (default: int8)')
    parser.add_argument('--device-index', type=int, default=-1)
    parser.add_argument('--once', action='store_true',
                        help='stop after one command attempt (wake attempt with --wake-only)')
    options = parser.parse_args(args)

    if options.manual and options.wake_only:
        parser.error('--manual cannot be combined with --wake-only')
    if options.dialogue and (not options.local or options.wake_only or options.once):
        parser.error('--dialogue requires --local and cannot use --wake-only or --once')
    if options.backend == 'mlx':
        if not options.local and not options.wake_only:
            parser.error('--backend mlx requires --local or --wake-only')
        if options.compute_type is not None:
            parser.error('--compute-type cannot be used with the fixed-fp16 MLX backend')
    if not options.list_devices:
        required_keys = [] if options.wake_only or options.local else ['OPENAI_API_KEY']
        if options.manual and not options.local:
            if options.backend is not None:
                parser.error('--backend requires a local wake or transcription model')
            if options.compute_type is not None:
                parser.error('--compute-type requires a local wake or transcription model')
            if options.model_path:
                parser.error('--manual requires --local to use --model-path')
        elif not options.model_path or not Path(options.model_path).expanduser().is_dir():
            parser.error('--model-path must be a downloaded model directory')
        for name in required_keys:
            if not os.environ.get(name, '').strip():
                parser.error('missing environment variable: ' + name)

    pipeline = None
    phase = 'loading_runtime_dependencies'
    done = False
    failed = False
    transcription_started = 0.0
    last_vad_speech_at = None

    def emit(event, **fields):
        print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)

    def report(event):
        nonlocal done, failed, transcription_started, last_vad_speech_at
        if event == 'listening':
            last_vad_speech_at = None
        if event == 'transcribing':
            transcription_started = monotonic()
        if event.startswith('transcription_failed:') or event == 'empty_transcript':
            failed = True
        if options.once and (
            event in ('no_speech', 'too_long', 'empty_transcript')
            or event.startswith('transcription_failed:')
        ):
            done = True
        if options.wake_only and (
            event == 'wake_detected' or options.once and event in (
                'not_wake', 'wake_no_speech', 'wake_too_long',
            )
        ):
            done = True
        if event.startswith('published:'):
            return  # Output is local; never imply ROS publication or Agent receipt.
        emit(event)

    def publish(utterance_id, text):
        nonlocal done
        published_at = monotonic()
        emit('transcript', utterance_id=utterance_id, text=text,
             transcription_s=round(published_at - transcription_started, 3),
             vad_last_speech_to_text_s=(
                 round(published_at - last_vad_speech_at, 3)
                 if last_vad_speech_at is not None else None
             ))
        done = options.once

    try:
        from pvrecorder import PvRecorder

        if options.list_devices:
            for index, name in enumerate(PvRecorder.get_available_devices()):
                emit('device', index=index, name=name)
            return 0

        with ExitStack() as resources:
            compute_type = options.compute_type or 'int8'
            backend = options.backend or 'faster-whisper'
            transcriber = None
            if options.local and not options.wake_only or backend == 'mlx':
                phase = 'loading_local_model'
                if backend == 'mlx':
                    from malbut_stt.mlx_transcription import MlxWhisperTranscriber

                    transcriber = MlxWhisperTranscriber(options.model_path)
                    compute_type = 'float16'
                else:
                    transcriber = LocalWhisperTranscriber(
                        str(Path(options.model_path).expanduser()),
                        compute_type=compute_type,
                    )
            phase = 'initializing_wake'
            if options.manual:
                wake = None
            elif transcriber is not None:
                wake = LocalWakeRecognizer.from_transcriber(transcriber)
            else:
                wake = LocalWakeRecognizer(str(Path(options.model_path).expanduser()),
                                           compute_type=compute_type)
            if options.wake_only:
                transcriber = None

            phase = 'loading_runtime_dependencies'
            import webrtcvad

            if not options.local and not options.wake_only:
                from openai import OpenAI

                phase = 'creating_api_client'
                client = OpenAI(
                    api_key=os.environ['OPENAI_API_KEY'],
                    base_url='https://api.openai.com/v1', timeout=30.0, max_retries=0,
                )
                resources.callback(client.close)
                transcriber = OpenAITranscriber(client)
            phase = 'initializing_vad'
            vad = webrtcvad.Vad(2)

            def is_speech(pcm, sample_rate):
                """Time VAD frame processing, not the physical speech-end ground truth."""
                nonlocal last_vad_speech_at
                speech = vad.is_speech(pcm, sample_rate)
                if speech:
                    last_vad_speech_at = monotonic()
                return speech

            def recorder_factory():
                if options.manual:
                    print('Enter를 누른 뒤 listening이 나오면 말하세요. 종료: Ctrl+C',
                          file=sys.stderr, flush=True)
                    input()
                return PvRecorder(frame_length=512, device_index=options.device_index)

            if options.dialogue:
                from threading import Event
                from malbut_stt.dialogue_pipeline import DialoguePipeline

                pipeline = DialoguePipeline(
                    recorder_factory=recorder_factory,
                    wake=wake or LocalWakeRecognizer.from_transcriber(transcriber),
                    transcriber=transcriber, is_speech=vad.is_speech,
                    publish_transcript=lambda uid, text: emit(
                        'transcript', utterance_id=uid, text=text),
                    publish_control=lambda *_: None, publish_interruption=lambda *_: None,
                    report=lambda event: report(
                        'listening' if options.manual and event == 'waiting_for_wake' else event),
                )
                resources.callback(pipeline.close)
                if options.manual:
                    pipeline.session.activate()
                emit('ready', mode='dialogue', wake_required=not options.manual,
                     model='small', backend='local', local_backend=backend,
                     local_compute_type=compute_type, device_index=options.device_index,
                     output='terminal_only', tts_completion_events=False,
                     tts_5s_timeout_testable=False)
                pipeline.start()
                wait = Event()
                while True:
                    pipeline.poll()
                    wait.wait(0.01)

            pipeline = SpeechPipeline(
                recorder_factory=recorder_factory, wake=wake,
                is_speech=is_speech, transcriber=transcriber,
                publish=publish, should_stop=lambda: done, report=report,
                settings=CaptureSettings(),
            )
            mode = 'wake_only' if options.wake_only else 'manual' if options.manual else 'wake'
            emit('ready', mode=mode,
                 model='small' if options.wake_only or options.local else 'gpt-transcribe',
                 backend='local' if options.wake_only or options.local else 'openai',
                 local_backend=backend if wake is not None or options.local else None,
                 local_compute_type=compute_type if wake is not None or options.local else None,
                 device_index=options.device_index)
            while True:
                pipeline.run()
                if not options.wake_only or options.once:
                    break
                done = False
        return 1 if failed else 0
    except (KeyboardInterrupt, EOFError):
        return 0
    except Exception as error:
        emit('stopped', phase=pipeline.phase if pipeline is not None else phase,
             error=type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
