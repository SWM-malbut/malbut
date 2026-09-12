"""Test a spoken Korean wake phrase with local Whisper, without keys or ROS."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import struct
import sys
from time import monotonic
from typing import Optional, Sequence

from malbut_stt.audio import CaptureSettings, UtteranceCollector
from malbut_stt.wake import LocalWakeRecognizer, is_wake_phrase


def main(args: Optional[Sequence[str]] = None) -> int:
    """Recognize one short utterance after a pause; this is an ASR prototype."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--model-path', required=True, help='downloaded faster-whisper model directory',
    )
    parser.add_argument('--wav', help='test an audio file instead of opening the microphone')
    parser.add_argument('--device-index', type=int, default=-1)
    parser.add_argument('--once', action='store_true', help='stop after one capture attempt')
    options = parser.parse_args(args)
    model_path = Path(options.model_path).expanduser()
    if not model_path.is_dir():
        parser.error('--model-path must be a downloaded model directory')
    if options.wav and not Path(options.wav).expanduser().is_file():
        parser.error('--wav file does not exist')

    def emit(event, **fields):
        print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)

    phase = 'loading_local_model'
    try:
        recognizer = LocalWakeRecognizer(str(model_path))

        def recognize(audio, is_file=False):
            nonlocal phase
            phase = 'recognizing_locally'
            started = monotonic()
            text = (recognizer.transcribe_file(audio) if is_file
                    else recognizer.transcribe(audio, 16000))
            emit('wake_detected' if is_wake_phrase(text) else 'not_wake',
                 text=text, inference_s=round(monotonic() - started, 3))

        if options.wav:
            recognize(str(Path(options.wav).expanduser()), is_file=True)
            return 0

        from pvrecorder import PvRecorder
        import webrtcvad

        vad = webrtcvad.Vad(2)
        settings = CaptureSettings(silence_timeout_s=0.4, max_utterance_s=6.0)
        print('Enter를 누르면 로컬 호출어 감지를 시작합니다. 종료: Ctrl+C',
              file=sys.stderr, flush=True)
        input()
        while True:
            with ExitStack() as resources:
                phase = 'opening_microphone'
                recorder = PvRecorder(frame_length=512, device_index=options.device_index)
                resources.callback(recorder.delete)
                if recorder.sample_rate != 16000:
                    raise ValueError('local Whisper requires 16kHz PCM')
                collector = UtteranceCollector(16000, vad.is_speech, settings)
                phase = 'starting_microphone'
                recorder.start()
                resources.callback(recorder.stop)
                emit('waiting_for_wake', phrase='제이크야')
                result = None
                while result is None:
                    phase = 'reading_microphone'
                    samples = recorder.read()
                    result = collector.feed(struct.pack('<' + 'h' * len(samples), *samples))
            if result.status == 'complete':
                recognize(result.pcm)
            else:
                emit(result.status)
            if options.once:
                return 0
    except (KeyboardInterrupt, EOFError):
        return 0
    except Exception as error:
        emit('stopped', phase=phase, error=type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
