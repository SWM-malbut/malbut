"""Run STT with a triggered evidence recorder, or exercise it without hardware."""

import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from malbut_stt.incident import DEFAULT_CONTAINS, DEFAULT_EXACT, IncidentRecorder


def demo(options):
    """Create explicitly synthetic evidence without ROS, a microphone, or Whisper."""
    import numpy as np

    recorder = IncidentRecorder(**options, metadata={'synthetic': True, 'mode': 'demo'})
    pcm = (np.sin(np.arange(16000) * 0.05) * 2000).astype('<i2').tobytes()

    class Model:
        def transcribe(self, audio, **kwargs):
            return iter([SimpleNamespace(text='구독과 좋아요 부탁드려요!',
                                         start=0.0, end=1.0)]), None

    try:
        recorder.audio(pcm, captured_at=recorder.clock(), blocked=False, busy=False)
        recorder.event('wake_detected')
        recorder.wrap_vad(lambda frame, rate: True, kind='command')(pcm[:640], 16000)
        recorder.event('speech_started', utterance_id='synthetic-utterance')
        for event in ('job_enqueued', 'job_dequeued'):
            recorder.event(event, kind='command', generation=0,
                           utterance_id='synthetic-utterance')
        with recorder.context(kind='command', utterance_id='synthetic-utterance', final=True):
            segments, _ = recorder.wrap_model(Model()).transcribe(
                np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768,
                language='ko', initial_prompt=None)
            text = ''.join(s.text for s in segments)
            recorder.event('inference_result', text=text, error=None)
        for event in ('result_enqueued', 'result_dequeued', 'result_accepted'):
            recorder.event(event, kind='command', generation=0,
                           utterance_id='synthetic-utterance', text=text)
        recorder.event('transcript_published', utterance_id='synthetic-utterance', text=text)
        for event in ('speech_transcript', 'dialogue_response_published'):
            recorder.ros_log(SimpleNamespace(
                name='malbut_agent_communication', level=20,
                msg=json.dumps(dict(event=event, status='received' if event == 'speech_transcript'
                                    else None, utterance_id='synthetic-utterance')),
                stamp=SimpleNamespace(sec=0, nanosec=0), file='synthetic-demo',
                function='demo', line=0))
        recorder.event('tts_request', request_id='synthetic-utterance',
                       playback_id='synthetic-playback', text='합성 응답')
        recorder.event('tts_playback', request_id='synthetic-utterance',
                       playback_id='synthetic-playback', state='finished')
    finally:
        recorder.close()
    if recorder.error or recorder.last_incident is None:
        print('Demo failed: ' + str(recorder.error or 'trigger did not match'), file=sys.stderr)
        return 1
    print(str(recorder.last_incident))
    return 0


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='~/.ros/malbut/stt_incidents')
    parser.add_argument('--pre-seconds', type=float, default=30.0)
    parser.add_argument('--post-seconds', type=float, default=15.0)
    parser.add_argument('--buffer-mib', type=int, default=32)
    parser.add_argument('--disk-mib', type=int, default=512)
    parser.add_argument('--contains', action='append', help='Replace default substring triggers.')
    parser.add_argument('--exact', action='append', help='Replace default whole-text triggers.')
    parser.add_argument('--no-default-triggers', action='store_true')
    parser.add_argument('--demo', action='store_true', help='Synthetic storage test; no hardware.')
    parser.add_argument('--no-audio', action='store_true',
                        help='Save execution logs and decode metadata without PCM/WAV files.')
    # Everything after -- belongs to the existing STT/ROS entry point.
    arguments = list(sys.argv[1:] if args is None else args)
    split = arguments.index('--') if '--' in arguments else len(arguments)
    options = parser.parse_args(arguments[:split])
    ros_args = arguments[split + 1:]
    config = dict(
        directory=str(Path(options.output).expanduser()), pre_seconds=options.pre_seconds,
        post_seconds=options.post_seconds, max_buffer_bytes=options.buffer_mib * 1024 * 1024,
        max_bytes=options.disk_mib * 1024 * 1024, save_audio=not options.no_audio,
        contains=options.contains if options.contains is not None else (
            [] if options.no_default_triggers else list(DEFAULT_CONTAINS)),
        exact=options.exact if options.exact is not None else (
            [] if options.no_default_triggers else list(DEFAULT_EXACT)),
    )
    try:
        if options.demo:
            return demo(config)
        # Validate before loading models; this creates no files or microphone.
        probe = IncidentRecorder(**config)
        probe.close()
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    if os.environ.get('MALBUT_STT_DIAGNOSTIC_DIR', '').strip():
        parser.error('unset MALBUT_STT_DIAGNOSTIC_DIR before selecting incident capture')
    from malbut_stt.node import main as run_stt

    previous = os.environ.get('MALBUT_STT_INCIDENT_CONFIG')
    os.environ['MALBUT_STT_INCIDENT_CONFIG'] = json.dumps(config, ensure_ascii=False)
    try:
        return run_stt(args=ros_args)
    finally:
        if previous is None:
            os.environ.pop('MALBUT_STT_INCIDENT_CONFIG', None)
        else:
            os.environ['MALBUT_STT_INCIDENT_CONFIG'] = previous


if __name__ == '__main__':
    raise SystemExit(main())
