"""Operator-only command line for local microphone transcript capture."""

import argparse
import json
import sys
import unicodedata

from malbut_voice.config import load_protected_config
from malbut_voice.errors import VoiceBoundaryError, clear_exception_details
from malbut_voice.transcript_source import MicrophoneTranscriptSource


def _positive_integer(value):
    try:
        result = int(value, 10)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('must be an integer')
    if result < 1 or result > 30:
        raise argparse.ArgumentTypeError('must be between 1 and 30')
    return result


def _safe_terminal_text(value):
    result = []
    for character in value:
        category = unicodedata.category(character)
        if category in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}:
            result.append(' ')
        else:
            result.append(character)
    return ''.join(result)


def _parser():
    parser = argparse.ArgumentParser(
        prog='malbut-microphone-stt',
        description='Explicit one-shot local hardware microphone transcript',
    )
    parser.add_argument('--config', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        '--check',
        action='store_true',
        help='verify fixed device/model resources without microphone capture',
    )
    action.add_argument(
        '--microphone',
        action='store_true',
        help='explicitly authorize one bounded microphone capture',
    )
    parser.add_argument('--duration-seconds', type=_positive_integer)
    parser.add_argument(
        '--show-transcript',
        action='store_true',
        help='opt in to printing recognized text on this terminal',
    )
    return parser


def _run(argv, source_factory):
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.check and arguments.show_transcript:
        parser.error('--show-transcript requires --microphone')
    if arguments.check and arguments.duration_seconds is not None:
        parser.error('--duration-seconds requires --microphone')
    try:
        config = load_protected_config(arguments.config)
        source = source_factory(config)
        if arguments.check:
            source.prepare()
            output = {
                'device_attested': True,
                'execution_authority': False,
                'microphone_opened': False,
                'model_attested': True,
                'speaker_identity_verified': False,
                'status': 'ready',
            }
        else:
            result = source.capture_final(arguments.duration_seconds)
            if not source.verify_final(result):
                raise VoiceBoundaryError('final_capability_rejected')
            output = {
                'audit': result.audit.to_dict(),
                'status': 'final_transcript_ready',
            }
            if arguments.show_transcript:
                output['transcript'] = _safe_terminal_text(result.event.text)
        print(
            json.dumps(output, sort_keys=True, ensure_ascii=False),
            flush=True,
        )
        return 0
    except VoiceBoundaryError as exc:
        code = exc.code
        clear_exception_details(exc)
        print(
            json.dumps({'error': code, 'status': 'rejected'}),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except KeyboardInterrupt as exc:
        clear_exception_details(exc)
        print(
            json.dumps({'error': 'interrupted', 'status': 'rejected'}),
            file=sys.stderr,
            flush=True,
        )
        return 130
    except Exception:
        print(
            json.dumps(
                {'error': 'voice_internal_error', 'status': 'rejected'}
            ),
            file=sys.stderr,
            flush=True,
        )
        return 3


def microphone_stt_main(argv=None):
    """Run the content-safe check or explicit one-shot capture command."""
    return _run(argv, MicrophoneTranscriptSource)


def _microphone_stt_main_for_test(argv, source_factory):
    """Run deterministic CLI tests without hardware or optional STT imports."""
    return _run(argv, source_factory)


if __name__ == '__main__':
    raise SystemExit(microphone_stt_main())
