#!/usr/bin/env python3
"""Summarize completed, immutable device runs; never infer or call providers."""

import argparse
import json
from pathlib import Path
import statistics

from replay_reviewed_pose_cloud import digest, read, require, save


def summarize(root):
    done = read(root / 'run/completed.json')
    require(done['complete'], 'inference incomplete')
    elapsed, propagation, cases = [], [], []
    for record in done['results']:
        folder = root / 'run' / record['case_id']
        for name in ('result', 'pose'):
            require(digest(folder / f'{name}.json') == record[f'{name}_sha256'],
                    'result hash mismatch')
        value = read(folder / 'result.json')
        if value['samples']:
            elapsed.append(value['sam_elapsed_s'])
            # The seed frame was computed in initialization, not propagation.
            propagation.extend(s['propagate_s'] for s in value['samples'][1:]
                               if 'propagate_s' in s)
        cases.append(dict(case_id=value['case_id'], seconds=value['sam_elapsed_s'],
            frames=len(value['samples']), gpu_peak_bytes=value.get('gpu_peak_allocated_bytes')))
    return dict(root=str(root), clips=len(elapsed), frames=sum(c['frames'] for c in cases),
        elapsed_min_s=min(elapsed), elapsed_median_s=statistics.median(elapsed),
        elapsed_max_s=max(elapsed), cases=cases,
        propagation_median_s=statistics.median(propagation) if propagation else None,
        propagation_max_s=max(propagation) if propagation else None,
        environment=read(root / 'run/environment.json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--comparison', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve old report')
    rows = [dict(profile='cpu-fp32-prior', **summarize(args.baseline))]
    for profile in ('cuda-fp32', 'cuda-bf16'):
        root = args.comparison / profile
        row = dict(profile=profile, **summarize(root))
        attachment = args.comparison / ('attachment-' + profile)
        summary = read(attachment / 'summary.json')
        audit = read(attachment / 'review-audit.json')['links']
        row.update(attached_existing=sum(c['attached_existing'] for c in summary),
                   reviewed_correct=sum(c['correct_at_reviewed_instant'] for c in audit),
                   reviewed_wrong=sum(c['wrong_person_at_reviewed_instant'] for c in audit))
        # Both devices must consume exactly the same measured Pose output.
        for case in row['cases']:
            name = Path('run') / case['case_id'] / 'pose.json'
            require(digest(root / name) == digest(args.baseline / name), 'Pose inputs differ')
        row['ratio_of_clip_medians_vs_prior_cpu'] = rows[0]['elapsed_median_s'] / row['elapsed_median_s']
        rows.append(row)
    save(args.output, dict(profiles=rows, new_api_calls=0,
        caveat='Single device runs on development clips; prior CPU run had concurrent ROS load. '
               'Not Jetson, live streaming, end-to-end latency or multi-object throughput.'))
    print(json.dumps([{k: v for k, v in row.items() if k not in ('environment', 'cases')}
                      for row in rows], indent=2))


if __name__ == '__main__':
    main()
