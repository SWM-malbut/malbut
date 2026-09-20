#!/usr/bin/env python3
"""Bind completed visual review and a future VLM call list to frozen artifacts.

No inference, network access, relabeling or automated person-identity assessment.
The explicit review below was performed on r3 RGB contact sheets; r4 must match
every request byte-for-byte semantically before those decisions may be reused.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path

from replay_fall_baseline import sha, write_json
from replay_pose_retention import verify_overlay
from review_fall_annotations import require
from run_ollama_fall_suite import prefix_indices


CHANGED_REVIEW = {
    'SYN008': '405: 일찍 요청한 f019에서도 바닥의 실제 대상자와 대응.',
    'SYN010': '412: f019에서 쓰러져 몸을 낮춘 실제 대상자와 대응.',
    'SYN013': '421: f022에서 바닥으로 넘어진 실제 대상자와 대응.',
    'SYN021': '443: f036에서 옆으로 쓰러지고 있는 실제 대상자와 대응.',
    'SYN067': 'V027: f019에서 서 있는 두 명 아닌 오른쪽 누운 대상자의 머리/팔.',
    'SYN074': 'V034: f019에서 가구 뒤 엎드린 실제 대상자와 대응.',
    'SYN082': 'V048: f058에서 실제 대상자가 앞으로 쓰러져 바닥에 도달한 시점.',
    'SYN083': 'V049: f086은 대상자가 손/무릎을 짚은 단계. 옆으로 완전히 '
              '쓰러지기 전이므로 이때까지의 영상만 본 VLM이 놓치는지 별도 확인 필요.',
}


def read(path):
    return json.loads(path.read_text())


def verify_arm(root, name):
    marker = read(root/'completed.json')
    require(sha(root/'summary.json') == marker['summary_sha256'], 'changed summary')
    folder = root/name
    require(sha(folder/'completed.json') == marker['arms'][name], 'changed arm')
    for filename, digest in read(folder/'completed.json')['files'].items():
        require(Path(filename).name == filename and sha(folder/filename) == digest,
                'changed artifact')
    return read(folder/'cases.json')


def run(args):
    require(not args.output.exists(), 'output exists')
    verify_overlay(args.spatial_final, args.parent_frozen)
    reviewed = verify_arm(args.reviewed_run, 'partial_brief')
    final = verify_arm(args.run, 'partial_brief')
    require(reviewed == final, 'visual review cannot be reused: requests/boxes changed')
    sheet_marker = read(args.reviewed_run/'audit/completed.json')
    for filename, digest in sheet_marker['files'].items():
        require(sha(args.reviewed_run/'audit'/filename) == digest, 'changed review sheet')
    decision = read(args.decisions)
    old_source = Path(decision['events_path'])
    require(sha(old_source) == decision['events_sha256'], 'changed original audit')
    old_events = read(old_source)['events']
    require(len(decision['entries']) == len(old_events) == 51, 'incomplete original review')
    prior = {}
    for index, cid, association, note in decision['entries']:
        event = old_events[index-1]
        require(event['case_id'] == cid and index not in prior, 'wrong review mapping')
        prior[index] = dict(event=event, association=association, note=note)
    metas = {m['case_id']: m for m in read(args.spatial_final/'media.json')['cases']}
    bundle = read(args.spatial_final/'evaluation_labels.json')
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    ledger, changed = [], set()
    planned = []
    for case in final:
        cid, meta = case['case_id'], metas[case['case_id']]
        media_path = (args.parent_frozen/meta['source_path']).resolve()
        require(media_path.is_relative_to(args.parent_frozen.resolve()) and
                sha(media_path) == meta['sha256'], 'media changed')
        for n, request in enumerate(case['requests'], 1):
            previous = [p for p in prior.values() if p['event']['request'] == request]
            if previous:
                require(len(previous) == 1, 'ambiguous old audit mapping')
                association, note = previous[0]['association'], previous[0]['note']
            else:
                require(cid in CHANGED_REVIEW, 'new unreviewed request '+cid)
                association, note = 'target', CHANGED_REVIEW[cid]
                changed.add(cid)
            interval = annotations[cid].get('onset_frames')
            if interval is None:
                interval = annotations[cid].get('first_down_frames')
            delay = None
            if interval is not None and association == 'target':
                delay = [request['dispatch_time_s']-interval[1]/meta['fps'],
                         request['dispatch_time_s']-interval[0]/meta['fps']]
            ledger.append(dict(
                case_id=cid, request=request, label=case['label'],
                association=association, note=note, video_time_delay_interval_s=delay,
                interpretation='video time, not wall-clock/Jetson latency'))
            last = request['decision_frame_index']
            require(type(last) is int and 0 <= last < meta['frames'], 'invalid prefix')
            require(last/meta['fps'] <= request['dispatch_time_s']+1e-9, 'future image')
            planned.append(dict(
                call_id=f'{cid}-request-{n:02d}', case_id=cid,
                mode='gated_primary' if n == 1 else 'gated_additional_request',
                media_path=str(media_path), media_sha256=meta['sha256'],
                available_through_frame=last, frame_indices=prefix_indices(last),
                dispatch_time_s=request['dispatch_time_s']))
        last = meta['frames']-1
        planned.append(dict(
            call_id=cid+'-full', case_id=cid, mode='full',
            media_path=str(media_path), media_sha256=meta['sha256'],
            available_through_frame=last, frame_indices=prefix_indices(last)))
    require(changed == set(CHANGED_REVIEW), 'reviewed change set differs')
    counts = Counter(item['association'] for item in ledger)
    for request in planned:
        require(max(request['frame_indices']) <= request['available_through_frame'],
                'future sampling')
        require(not {'label', 'reason', 'ground_truth'}.intersection(request), 'GT in call list')
    args.output.mkdir(mode=0o700)
    write_json(args.output/'association-review.json', dict(
        method='nonblind_assistant_RGB_review_not_new_human_ground_truth',
        source_summary_sha256=sha(args.run/'summary.json'),
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
        original_review_sha256=sha(args.decisions),
        changed_visual_review_sha256=sha(args.reviewed_run/'audit/completed.json'),
        r3_r4_requests_equal=True, counts=dict(counts), entries=ledger))
    write_json(args.output/'vlm-call-plan.json', dict(
        status='prepared_not_executed', actual_calls=0, paid_calls=0,
        source_summary_sha256=sha(args.run/'summary.json'),
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
        counts=dict(Counter(p['mode'] for p in planned)), calls=planned,
        policy='Original RGB only; no GT overlays, labels or user reasons sent. '
               'Use same existing prompt/schema/preprocessing. Verify free access before calls. '
               'Primary gated clip metrics use first request per clip; additional requests '
               'are separately evaluated and counted as cost. '
               'YOLO misses remain in E2E denominator.',
        blocker='Mac Ollama host offline; no server or model availability assumed'))
    write_json(args.output/'completed.json', dict(
        script_sha256=sha(Path(__file__)),
        files={p.name: sha(p) for p in args.output.iterdir()}))
    print('REVIEW', dict(counts), 'PLANNED', len(planned), 'ACTUAL_CALLS=0', flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'reviewed-run', 'spatial-final', 'parent-frozen', 'decisions', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    run(p.parse_args())
