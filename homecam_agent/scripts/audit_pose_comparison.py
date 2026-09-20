#!/usr/bin/env python3
"""Uniform, source-bound visual request audit across all five reported stages.

No inference, GT edits, interpolation, remote calls, or automatic identity labels.
Equivalent visual evidence is reviewed once only when every selected observation
and request time is identical; every original stage request remains in the ledger.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from audit_pose_requests import prepare, render
from replay_fall_baseline import sha, write_json
from replay_pose_retention import verify_overlay
from review_fall_annotations import require
from review_pose_detection_comparison import display_name
from score_fall_baseline import events


ROOT = Path('/home/jisanggeun/.local/share/malbut-evaluations')
SPATIAL = ROOT/'fall84-user-boxes-yolo-20260917.9UdJDF/spatial-final'
MEDIA = ROOT/'eval84-gemma-20260917.fiHWKe/fall84-v2-r1-20260917'
RETENTION = ROOT/'fall84-pose-retention-20260917-r3'
COMPARISON = ROOT/'fall84-detection-comparison-20260917-r2'
FINAL = ROOT/'fall84-s-partial-20260918-r4'
STAGES = {
    'baseline': (RETENTION, 'control'),
    'n_stretch': (COMPARISON, 'n_stretch'),
    'n_letterbox': (COMPARISON, 'n_letterbox'),
    's_letterbox': (COMPARISON, 's_letterbox'),
    's_improved': (FINAL, 'partial_brief'),
}
POLICY = {
    'scope': 'nonblind assistant RGB output audit; not new user GT or clinical validation',
    'spatial': 'Request evidence must depict the labelled subject, not another person, '
               'furniture, pet, or unresolvable covered region. Partial visible body may '
               'establish identity; this is not an IoU or keypoint accuracy metric.',
    'temporal': 'Observed-fall success additionally needs visible relevant fall/down '
                'evidence and no evidence/dispatch before the earliest approved onset. '
                'Within onset interval is explicitly boundary-uncertain, not exact latency.',
    'missing_time': 'Never invent time labels. Suspected cases with no onset/first-down '
                    'annotation get target-associated coverage only, no validated latency.',
    'unresolved': 'Keep all 25/25/34 denominators. Unresolved requests are not successes.',
    'negative': 'Every request on normal video is an unnecessary verification request, '
                'even if it points to furniture/pet. Never remove these from cost/FPR.',
    'unit': 'Clip covered if at least one qualifying dispatched request exists; '
            'duplicate requests separately counted. Updates are not new requests.',
    'no_tuning': 'No detector/threshold changes while performing this audit.',
}


def read(path):
    return json.loads(path.read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def baseline_events(ann, labels):
    folder = RETENTION/'control'
    marker = read(RETENTION/'completed.json')
    require(sha(RETENTION/'summary.json') == marker['summary_sha256'], 'retention changed')
    require(sha(folder/'completed.json') == marker['arms']['control'], 'control changed')
    complete = read(folder/'completed.json')
    for file, key in [('frames.jsonl', 'frames_sha256'), ('run.json', 'run_sha256')]:
        require(sha(folder/file) == complete[key], 'changed baseline '+file)
    rows = [json.loads(line) for line in (folder/'frames.jsonl').read_text().splitlines()]
    lookup = {}
    for row in rows:
        lookup.setdefault(row['case_id'], {})[row['frame_index']] = row
    result = []
    for item in events(rows):
        cid, candidate = item['case_id'], item['candidate']
        frame = item.get('evidence_frame_index', item['frame_index'])
        start = min(lookup[cid], key=lambda f: abs(
            lookup[cid][f]['timestamp_s']-candidate['evidenceStartSec']))
        reviewed = sorted({b[0] for p in ann[cid]['persons'] for b in p['boxes']})
        nearby = min(reviewed, key=lambda f: abs(f-frame)) if reviewed else None
        request = dict(origin=dict(case_id=cid, frame_index=item['frame_index'],
                                   timestamp_s=item['timestamp_s'], candidate=candidate),
                       dispatch_time_s=item['timestamp_s'],
                       decision_frame_index=item['frame_index'])
        result.append(dict(case_id=cid, name=display_name(labels[cid]), label=labels[cid]['label'],
                           request=request,
                           selected_frames=[start, frame, frame if nearby is None else nearby],
                           actual_evidence_frame=frame, exact_gt_frame=nearby,
                           onset_frames=ann[cid].get('onset_frames'),
                           first_down_frames=ann[cid].get('first_down_frames'),
                           target_person_id=ann[cid].get('target_person_id')))
    return lookup, result


def run(args):
    require(not args.output.exists(), 'output already exists')
    verify_overlay(SPATIAL, MEDIA)
    bundle = read(SPATIAL/'evaluation_labels.json')
    ann = {a['case_id']: a for a in bundle['annotations']['cases']}
    labels = {a['case_id']: a for a in bundle['classifications']['cases']}
    metas = {m['case_id']: m for m in read(SPATIAL/'media.json')['cases']}
    args.output.mkdir(mode=0o700)
    source_hashes = {str(SPATIAL/'freeze.json'): sha(SPATIAL/'freeze.json')}
    unique, references, stage_counts, visual_rows = {}, [], {}, {}
    for stage, (root, arm) in STAGES.items():
        if stage == 'baseline':
            rows, records = baseline_events(ann, labels)
        else:
            _, _, rows, records = prepare(SimpleNamespace(
                run=root, arm=arm, spatial_final=SPATIAL, parent_frozen=MEDIA))
        source_hashes.update({str(root/arm/name): sha(root/arm/name)
                             for name in ('completed.json', 'frames.jsonl')})
        stage_counts[stage] = len(records)
        for index, record in enumerate(records, 1):
            cid = record['case_id']
            candidate = record['request']['origin']['candidate']
            tid = candidate['targetTrackId']
            observations = {
                str(f): [o['pose'] for o in rows[cid].get(f, {}).get('observations', [])
                         if o['track_id'] == tid] for f in record['selected_frames']}
            require(len(observations[str(record['actual_evidence_frame'])]) == 1,
                    'missing evidence pose')
            key = digest(dict(case_id=cid, frames=record['selected_frames'], poses=observations,
                              dispatch=record['request']['dispatch_time_s'],
                              kind=candidate['candidateKind']))
            if key not in unique:
                uid = f'U{len(unique)+1:03d}'
                event = dict(record, index=len(unique)+1, audit_id=uid, stages=[],
                             source_sha256=metas[cid]['sha256'], equivalence_sha256=key)
                unique[key] = event
                # render accepts a case-keyed lookup; use audit ID to preserve each model output.
                visual_rows[uid] = rows[cid]
            event = unique[key]
            event['stages'].append(stage)
            references.append(dict(stage=stage, stage_request_index=index,
                                   audit_id=event['audit_id'], case_id=cid,
                                   request=record['request']))
    unique_events = list(unique.values())
    # Render each page with audit IDs as temporary lookup keys, retaining real case IDs in ledger.
    for offset in range(0, len(unique_events), 5):
        group = unique_events[offset:offset+5]
        page = args.output/f'batch-{offset//5+1:02d}'
        page.mkdir()
        page_ann, page_meta, display = {}, {}, []
        for event in group:
            uid, cid = event['audit_id'], event['case_id']
            page_ann[uid], page_meta[uid] = ann[cid], metas[cid]
            display.append(dict(event, case_id=uid,
                                name=event['audit_id']+' '+event['name']))
        render(SimpleNamespace(output=page, parent_frozen=MEDIA, arm='uniform-audit'),
               page_ann, page_meta, visual_rows, display)
        print('RENDERED', offset+len(group), '/', len(unique_events), flush=True)
    for path, expected in source_hashes.items():
        require(sha(Path(path)) == expected, 'source changed during audit')
    write_json(args.output/'events.json', dict(policy=POLICY, sources=source_hashes,
               stage_counts=stage_counts, unique_count=len(unique_events),
               events=unique_events, stage_requests=references))
    write_json(args.output/'review-template.json', dict(
        events_sha256=sha(args.output/'events.json'), entries=[dict(
            audit_id=e['audit_id'], case_id=e['case_id'], association='pending',
            scene='pending', note='') for e in unique_events]))
    (args.output/'index.html').write_text(
        '<!doctype html><meta charset="utf-8"><title>Uniform YOLO request audit</title>'
        '<style>body{background:#121923;color:white;max-width:1440px;margin:auto}'
        'img{width:100%}</style><h1>All stages — same review criteria</h1>'+''.join(
            f'<h2>Batch {i:02d}</h2><img loading="lazy" src="batch-{i:02d}/page-01.jpg">'
            for i in range(1, (len(unique_events)+4)//5+1)))
    write_json(args.output/'completed.json', dict(script_sha256=sha(Path(__file__)), files={
        str(p.relative_to(args.output)): sha(p) for p in args.output.rglob('*') if p.is_file()}))
    print('COMPLETE', stage_counts, 'unique', len(unique_events), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())
