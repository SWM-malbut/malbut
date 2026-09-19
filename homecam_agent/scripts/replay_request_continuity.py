#!/usr/bin/env python3
"""Probe duplicate scheduling on frozen actual requests; no model or network calls."""
import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path

from experimental_request_continuity import ContinuityProbe
from finalize_partial_pose_review import verify_arm
from replay_fall_baseline import sha
from replay_pose_retention import verify_overlay
from replay_vlm_frames import save
from review_fall_annotations import require


def read(path):
    return json.loads(path.read_text())


def run(args):
    require(not args.output.exists(), 'output exists')
    verify_overlay(args.spatial_final, args.dataset)
    cases = verify_arm(args.pose_run, 'partial_brief')
    metas = {m['case_id']: m for m in read(args.spatial_final/'media.json')['cases']}
    requests = {c['case_id']: c['requests'] for c in cases}
    source_rows = args.pose_run/'partial_brief/frames.jsonl'
    rows = [json.loads(s) for s in source_rows.read_text().splitlines()]
    current, proposals, seen = None, [], set()
    for row in rows:
        cid = row['case_id']
        if current != cid:
            engine, current = ContinuityProbe(), cid
        engine.observe(row, (metas[cid]['width'], metas[cid]['height']))
        for request in requests[cid]:
            if request['decision_frame_index'] == row['frame_index']:
                key = cid, request['request_id']
                require(key not in seen, 'duplicate scheduling')
                seen.add(key)
                proposal = engine.route(request)
                require(proposal['original'] == request, 'modified evidence')
                proposals.append(dict(case_id=cid, **proposal))
    require(len(proposals) == sum(len(r) for r in requests.values()) == 53,
            'missing request; delayed requests need separate handling')
    # Labels enter reporting only, never the continuity component above.
    labels = {c['case_id']: c['label'] for c in cases}
    first, changed = {}, []
    for p in proposals:
        cid = p['case_id']
        if cid not in first:
            require(p['action'] == 'request', 'first request was suppressed')
            first[cid] = p['original']['dispatch_time_s']
        if p['action'] != 'request':
            changed.append(p)
    require(len(first) == 52, 'lost requested video')
    summary = dict(
        requests_before=53, requests_after=sum(p['action'] == 'request' for p in proposals),
        requested_videos_before=52, requested_videos_after=len(first),
        raw_requested_groups=dict(Counter(labels[cid] for cid in first)),
        first_requests_unchanged=True, changed=changed,
        model_calls=0, provider_calls=0, config=asdict(engine.config),
        scope='offline routing proposal only; no operational incident/VLM response state',
        limitation='No proven identity; unknown motion allowed only under strict static '
                   '2D evidence. '
                   'Do not treat a match as a normal prediction or completed event.',
        source_marker_sha256=sha(args.pose_run/'partial_brief/completed.json'),
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'))
    args.output.mkdir(mode=0o700)
    save(args.output/'proposals.json', proposals)
    save(args.output/'summary.json', summary)
    save(args.output/'completed.json', dict(
        files={p.name: sha(p) for p in args.output.iterdir()},
        source_sha256={str(p): sha(p) for p in [Path(__file__), Path(__file__).with_name(
            'experimental_request_continuity.py')]}))
    print(json.dumps({k: v for k, v in summary.items() if k != 'changed'}), flush=True)
    for p in changed:
        print('CHANGED', p['case_id'], p['original']['request_id'],
              p['parent_request_id'], flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'spatial-final', 'pose-run', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())
