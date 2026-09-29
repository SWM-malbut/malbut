#!/usr/bin/env python3
"""Summarize measured resources and same-frame semantic CPU/GPU differences."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def resources(root):
    rows = json.loads((root/'summary.json').read_text())
    raw = [json.loads((root/f"{r['case_id']}-{r['repeat']}.json").read_text())['raw'] for r in rows]
    times = [t for r in raw for t in r['inference_ms']]
    return dict(replays=len(rows), cpu_percent_median=statistics.median(r['cpu_percent'] for r in rows),
        inference_ms_median=statistics.median(times), inference_ms_p95=sorted(times)[int(.95*len(times))],
        fps_median=statistics.median(r['processed_fps'] for r in rows),
        conversions=sum(r['conversion_count'] for r in rows), inferences=sum(r['inference_count'] for r in rows),
        input_frames=sum(r['input_frames'] for r in rows),
        rss_max_mib=max(r['rss_max_mib'] for r in rows),
        gpu_median_range=[min(r['gpu_percent'].get('median',0) for r in rows),
                          max(r['gpu_percent'].get('median',0) for r in rows)],
        gpu_memory_max_mib=max(r['gpu_memory_mib'].get('max',0) for r in rows),
        dropped=sum(r['dropped_before_callback'] for r in rows))


def signature(row):
    return [(m['captureTimeSec'], [(c['candidateKind'], c['evidenceEndSec']) for c in m['candidates']])
            for m in row['candidate_messages'] if m['candidates']]


def compare(left, right):
    lm = json.loads((left/'manifest.json').read_text())
    rm = json.loads((right/'manifest.json').read_text())
    for field in ('model_sha256','media_sha256','cases','camera_input'):
        if lm.get(field) != rm.get(field):
            raise ValueError('comparison inputs differ: '+field)
    counts = Counter()
    different = []
    maximum = {'box': 0., 'keypoint_xy': 0., 'keypoint_confidence': 0., 'box_confidence': 0.}
    for case in lm['cases']:
        a = json.loads((left/(case+'-0.json')).read_text())
        b = json.loads((right/(case+'-0.json')).read_text())
        if a['raw']['inferred_frame_indices'] != b['raw']['inferred_frame_indices']:
            raise ValueError('different inference frames: '+case)
        counts['videos'] += 1
        counts['frames'] += a['inference_count']
        counts['left_flagged_videos'] += bool(signature(a))
        counts['right_flagged_videos'] += bool(signature(b))
        if signature(a) != signature(b):
            different.append(case)
        for ap, bp in zip(a['raw']['poses'], b['raw']['poses']):
            if len(ap['poses']) != len(bp['poses']):
                counts['pose_count_different_frames'] += 1
                continue
            for aa, bb in zip(ap['poses'], bp['poses']):
                counts['compared_poses'] += 1
                maximum['box'] = max(maximum['box'], *[abs(x-y) for x,y in zip(aa['box'],bb['box'])])
                maximum['box_confidence'] = max(maximum['box_confidence'], abs(aa['box_confidence']-bb['box_confidence']))
                for ak, bk in zip(aa['keypoints'],bb['keypoints']):
                    maximum['keypoint_xy'] = max(maximum['keypoint_xy'], abs(ak['x']-bk['x']),abs(ak['y']-bk['y']))
                    maximum['keypoint_confidence'] = max(maximum['keypoint_confidence'],abs(ak['confidence']-bk['confidence']))
    return dict(counts=counts, candidate_timing_or_kind_changed=different,
                max_absolute_delta=maximum, note='Not a ground-truth person identity/merging accuracy score')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    result = {'resources': {name: resources(args.root/name) for name in
              ['A-original','B-early-gate','C-cpu-tuned','D-cuda']},
              'same_frames': compare(args.root/'fixed-original-rgb84', args.root/'fixed-cuda-rgb84')}
    if (args.root/'E-no-spinning/summary.json').exists():
        result['resources']['E-no-spinning'] = resources(args.root/'E-no-spinning')
    print(json.dumps(result, indent=2))
