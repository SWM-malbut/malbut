"""Run the 20 quiet-room spoken STT cases, saving text events without audio."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


PACKAGE_DIR = Path(__file__).resolve().parents[1]


def run_case(case, device_index, stream):
    """Inherit terminal input and stop after exactly one smoke subprocess."""
    child = subprocess.Popen(
        [sys.executable, '-m', 'malbut_stt.smoke', '--manual', '--once',
         '--device-index', str(device_index)],
        cwd=PACKAGE_DIR, stdout=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1,
    )
    transcript_count = 0
    try:
        for line in child.stdout:
            payload = json.loads(line)
            if not isinstance(payload, dict) or not isinstance(payload.get('event'), str):
                raise ValueError('invalid smoke event')
            record = dict(payload, case_id=case['id'], expected_text=case['text'],
                          recorded_at=datetime.now(timezone.utc).isoformat())
            encoded = json.dumps(record, ensure_ascii=False)
            stream.write(encoded + '\n')
            stream.flush()
            print(encoded, flush=True)
            transcript_count += payload['event'] == 'transcript'
        return_code = child.wait()
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        child.stdout.close()
    # smoke returns zero on EOF/Ctrl+C or a discarded capture; never advance then.
    return return_code if return_code else (0 if transcript_count == 1 else 1)


def main(args=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='transcription_s는 API 처리 시간이며 실제 발화 종료부터의 지연이 아닙니다.',
    )
    parser.add_argument('--device-index', required=True, type=int)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--start-case', type=int, choices=range(1, 21), default=1,
                        help='resume at this 1-based case number (default: 1)')
    options = parser.parse_args(args)
    if not sys.stdin.isatty():
        parser.error('터미널에서 실행하세요. 각 문장은 Enter 입력 후 녹음합니다.')
    cases = json.loads((PACKAGE_DIR / 'test/fixtures/stt_manual_ko.json').read_text(encoding='utf-8'))
    directory = options.output_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    print('조용한 환경에서 아래 문장을 읽으세요. Enter 후 listening 표시를 기다립니다.', flush=True)
    print('실패·무음·입력 종료 시 멈춥니다. Ctrl+C로 전체 종료합니다.', flush=True)
    print('transcription_s는 API 처리 시간이며 발화 종료부터 결과까지의 전체 지연이 아닙니다.', flush=True)
    try:
        with (directory / 'events.jsonl').open('a', encoding='utf-8') as stream:
            for index in range(options.start_case - 1, len(cases)):
                case = cases[index]
                print(f"\n[{index + 1}/{len(cases)}] {case['id']}: {case['text']}", flush=True)
                code = run_case(case, options.device_index, stream)
                if code:
                    print(f'중단했습니다. 같은 문장부터 재개: --start-case {index + 1}',
                          file=sys.stderr, flush=True)
                    return code if code > 0 else 128 - code
    except KeyboardInterrupt:
        print('\n사용자가 전체 시험을 종료했습니다.', file=sys.stderr, flush=True)
        return 130
    except (OSError, ValueError) as error:
        print('시험 중단: ' + type(error).__name__, file=sys.stderr, flush=True)
        return 1
    print(f'완료: {directory / "events.jsonl"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
