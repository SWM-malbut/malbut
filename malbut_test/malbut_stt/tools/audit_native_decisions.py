"""Audit recorded Whisper decisions without a model, audio, ROS, or network."""

import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


def audit(root):
    rows, seen, incidents, unavailable, conflicts = [], {}, [], [], []
    for path in sorted(Path(root).rglob('events.jsonl')):
        manifest = path.parent / 'incident.json'
        if not manifest.exists():
            continue
        incident = json.loads(manifest.read_text())
        incidents.append(dict(path=str(manifest.resolve()),
                              audio_saved=incident.get('audio_saved'),
                              model=incident.get('metadata', {}).get('model'),
                              triggers=incident.get('triggers', [])))
        for number, line in enumerate(path.read_text().splitlines(), 1):
            event = json.loads(line)
            if event.get('event') != 'decode_result':
                continue
            # Incident windows overlap. Count one decoder call only once.
            key = event.get('decode_id') or (str(path), number)
            if key in seen:
                if seen[key] != event:
                    conflicts.append(dict(decode_id=key, source=str(path), line=number))
                continue
            seen[key] = event
            native = event.get('native_decode') or {}
            context = event.get('context') or {}
            text = ''.join(s.get('text', '') for s in event.get('segments', [])).strip()
            base = dict(decode_id=event.get('decode_id'), source=str(path.resolve()), line=number,
                        at_kst=datetime.fromtimestamp(event['wall_ns'] / 1e9,
                                                      ZoneInfo('Asia/Seoul')).isoformat(),
                        kind=context.get('kind'), utterance_id=context.get('utterance_id'),
                        text=text, voiced_s=context.get('voiced_s'),
                        audio_s=event.get('sample_count', 0) / event.get('sample_rate', 16000),
                        return_code=native.get('return_code'),
                        windows_truncated=native.get('windows_truncated', False))
            if native.get('status') != 'observed':
                unavailable.append(dict(base, status=native.get('status', 'missing'),
                                        reason=native.get('reason')))
            for index, window in enumerate(native.get('windows', [])):
                p, score = window.get('no_speech_prob'), window.get('avg_logprobs')
                pt, st = window.get('no_speech_threshold'), window.get('logprob_threshold')
                valid = all(type(v) in (int, float) and math.isfinite(v)
                            for v in (p, score, pt, st))
                row = dict(base, window_index=index, **window)
                row.update(probability_condition=p > pt if valid else None,
                           score_condition=score < st if valid else None,
                           expected_skip=p > pt and score < st if valid else None,
                           decision_matches=(
                               (p > pt and score < st) == window.get('skipped_no_speech')
                               if valid else None))
                rows.append(row)
    return dict(incidents=incidents, windows=rows, unavailable=unavailable,
                duplicate_conflicts=conflicts,
                summary=dict(incidents=len(incidents), unique_decodes=len(seen),
                             observed_windows=len(rows), unavailable_decodes=len(unavailable),
                             skipped_windows=sum(r.get('skipped_no_speech') is True for r in rows),
                             decision_mismatches=sum(r['decision_matches'] is False for r in rows),
                             uncheckable_windows=sum(r['decision_matches'] is None for r in rows),
                             duplicate_conflicts=len(conflicts)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incidents', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(args.incidents)
    if not report['incidents']:
        parser.error('no incident manifests and events found')
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    fields = ['at_kst', 'decode_id', 'kind', 'utterance_id', 'text', 'audio_s', 'voiced_s',
              'no_speech_prob', 'avg_logprobs', 'no_speech_threshold', 'logprob_threshold',
              'probability_condition', 'score_condition', 'expected_skip', 'skipped_no_speech',
              'decision_matches', 'single_timestamp_ending', 'next_seek_s',
              'window_index', 'source', 'line']
    with (args.output / 'windows.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['windows'])
    print(json.dumps(report['summary']))
    return int(bool(report['summary']['decision_mismatches'] or report['duplicate_conflicts']))


if __name__ == '__main__':
    raise SystemExit(main())
