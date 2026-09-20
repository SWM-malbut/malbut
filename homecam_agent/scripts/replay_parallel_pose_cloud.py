#!/usr/bin/env python3
"""Replay independent measured YOLO/Cloud outputs with a clip-level OR rule.

No inference/network calls, no GT in the combining function, no person association,
no production events/notifications. A flag means needs checking, not confirmed fall.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path

import evaluate_runtime_cloud_frames as cloud
from finalize_partial_pose_review import verify_arm
from replay_fall_baseline import sha
from replay_pose_retention import verify_overlay
from replay_vlm_frames import save
import score_pose_comparison_review as review


def read(path):
    return json.loads(path.read_text())


def require(value, message):
    if not value:
        raise ValueError(message)


def merge_clip_signals(requests, cloud_result):
    """No labels/boxes/audits accepted. Do not invent a shared person or severity."""
    require(isinstance(requests, list), 'requests must be a list')
    keys = [r['request_id'] for r in requests]
    require(len(keys) == len(set(keys)), 'duplicate source request')
    yolo_positive = bool(requests)
    prediction = cloud_result.get('prediction')
    usable = (cloud_result.get('status') == 'responded' and cloud_result.get('valid') is True
              and not cloud.scoring.validate_prediction(prediction)
              and not cloud_result.get('schema_errors')
              and not cloud_result.get('semantic_errors'))
    cloud_positive = None
    if usable and prediction['outcome'] == 'classified':
        cloud_positive = prediction['label'] in {'observed_fall', 'suspected_fall'}
    flag = True if yolo_positive else cloud_positive
    sources = (["yolo_pose"] if yolo_positive else []) + (
        ['cloud_vlm'] if cloud_positive is True else [])
    return dict(yolo_positive=yolo_positive, cloud_positive=cloud_positive,
                needs_check=flag, sources=sources,
                yolo_cloud_disagree=(cloud_positive is not None
                                     and yolo_positive != cloud_positive),
                cloud_assessment=prediction['label'] if usable else None,
                source_request_ids=keys,
                source_track_ids=sorted({r['track_id'] for r in requests}),
                person_merge_verified=False, final_fall_assessment=None,
                scope='one video flag; not one person/incident or guardian alert')


def summarize(rows):
    groups = {}
    for label in cloud.scoring.LABELS:
        selected = [r for r in rows if r['label'] == label]
        groups[label] = dict(total=len(selected),
            yolo_requested=sum(r['yolo_positive'] for r in selected),
            yolo_target_supported=sum(r['yolo_review']['target_coverage'] for r in selected),
            cloud_flagged=sum(r['cloud_positive'] is True for r in selected),
            combined_flagged=sum(r['needs_check'] is True for r in selected),
            both=sum(r['yolo_positive'] and r['cloud_positive'] is True for r in selected),
            yolo_only=sum(r['yolo_positive'] and r['cloud_positive'] is False for r in selected),
            cloud_only=sum(not r['yolo_positive'] and r['cloud_positive'] is True for r in selected),
            no_flag=sum(r['needs_check'] is False for r in selected),
            unresolved=sum(r['needs_check'] is None for r in selected))
    return dict(cases=len(rows), groups=groups,
        combined_flagged_videos=sum(r['needs_check'] is True for r in rows),
        new_yolo_inference_calls=0, new_cloud_calls=0,
        source_yolo_requests=sum(len(r['source_request_ids']) for r in rows),
        cloud_positive_without_yolo_request=[r['case_id'] for r in rows
            if r['label'] != 'normal_activity' and not r['yolo_positive']
            and r['cloud_positive'] is True],
        cloud_positive_without_verified_yolo_target=[r['case_id'] for r in rows
            if r['label'] != 'normal_activity' and not r['yolo_review']['target_coverage']
            and r['cloud_positive'] is True],
        yolo_only_positive_cases=[r['case_id'] for r in rows
            if r['yolo_positive'] and r['cloud_positive'] is False],
        normal_flags=[r['case_id'] for r in rows
            if r['label'] == 'normal_activity' and r['needs_check'] is True],
        not_three_class_accuracy=True,
        scope='cached independent outputs, scene-level verification flags, not live E2E',
        limitation='Cloud identifies no target; GT cannot authorize person/incident merging.',
        coverage='Cloud once per complete ~5s clip, NOT a 60s/300s periodic schedule',
        latency='Cloud request times only; no simulated min(YOLO time, Cloud time) latency')


def validate_review(args, raw_cases):
    audit_summary = read(args.audit / 'scored-final/summary.json')
    completed = read(args.audit / 'scored-final/completed.json')
    require(sha(args.audit / 'scored-final/summary.json') == completed['summary_sha256'],
            'changed target review summary')
    require(sha(Path(review.__file__)) == completed['script_sha256'], 'changed target scorer')
    decisions_doc = read(args.decisions)
    require(Path(decisions_doc['audit_path']).resolve() == args.audit.resolve(), 'wrong review')
    require(sha(args.decisions) == audit_summary['review_sha256'], 'changed decisions')
    require(sha(args.audit / 'events.json') == audit_summary['events_sha256']
            == decisions_doc['events_sha256'], 'changed review events')
    require(sha(args.audit / 'completed.json') == audit_summary['audit_marker_sha256'],
            'changed review marker')
    for name, expected in read(args.audit / 'completed.json')['files'].items():
        path = (args.audit / name).resolve()
        require(path.is_relative_to(args.audit.resolve()) and sha(path) == expected,
                'changed reviewed image/data')
    source = read(args.audit / 'events.json')
    for name, expected in source['sources'].items():
        require(sha(Path(name)) == expected, 'changed reviewed detector output')
    require(sha(args.spatial / 'freeze.json') == audit_summary['spatial_freeze_sha256'],
            'not final approved boxes')
    decisions = review.validate_decisions(source['events'], decisions_doc)
    events = {e['audit_id']: e for e in source['events']}
    bundle = read(args.spatial / 'evaluation_labels.json')
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    metas = {m['case_id']: m for m in read(args.spatial / 'media.json')['cases']}
    refs = [r for r in source['stage_requests'] if r['stage'] == 's_improved']
    require(len(refs) == source['stage_counts']['s_improved'], 'incomplete reviewed requests')
    recalculated = []
    for case in raw_cases:
        cid = case['case_id']
        found = [r for r in refs if r['case_id'] == cid]
        require([r['request'] for r in found] == case['requests'], 'different YOLO requests')
        results = []
        for ref in found:
            event = events[ref['audit_id']]
            result = review.score_request(event, ref['request'], decisions[ref['audit_id']],
                                          annotations[cid], metas[cid]['fps'])
            result.update(audit_id=ref['audit_id'], stage_request_index=ref['stage_request_index'])
            results.append(result)
        recalculated.append(review.summarize_case(cid, labels[cid]['label'], results,
                                                 annotations[cid]))
    saved = audit_summary['stages']['s_improved']['cases']
    require({r['case_id']: r for r in recalculated} == {r['case_id']: r for r in saved},
            'target scoring does not reproduce')
    return {r['case_id']: r for r in recalculated}


def run(args):
    require(not args.output.exists(), 'output already exists')
    verify_overlay(args.spatial, args.frozen)
    media = read(args.frozen / 'media.json')['cases']
    require(len(media) == len({m['case_id'] for m in media}) == 84, 'wrong media')
    spec = cloud.verify_baseline(args.cloud, args.frozen, media)
    require(spec['frames'] == 12 and not spec['yolo_gate']
            and spec['purpose'] == 'crosscheck_scene_level_not_target_specific_incident'
            and spec['freeze_sha256'] == sha(args.frozen / 'freeze.json')
            and spec['label_sha256'] == sha(args.frozen / 'evaluation_labels.json'),
            'not independent 12-frame Cloud results')
    raw_cases = verify_arm(args.pose, 'partial_brief')
    raw = {c['case_id']: c for c in raw_cases}
    require(len(raw) == len(raw_cases) == 84 and set(raw) == {m['case_id'] for m in media},
            'incomplete/duplicate pose cases')
    # Combining uses only raw model signals. Reviewed targets/labels enter scoring later.
    combined = []
    cloud_rows = []
    for meta in media:
        cid = meta['case_id']
        requests = [dict(request_id=r['request_id'],
                         track_id=r['origin']['candidate']['targetTrackId'],
                         dispatch_s=r['dispatch_time_s']) for r in raw[cid]['requests']]
        model_result = read(args.cloud / f'{cid}.result.json')
        cloud_rows.append(model_result)
        combined.append(dict(case_id=cid, **merge_clip_signals(requests, model_result)))
    reviewed = validate_review(args, raw_cases)
    labels = {c['case_id']: c for c in read(args.frozen / 'evaluation_labels.json')[
        'classifications']['cases']}
    require(Counter(r['label'] for r in labels.values()) == dict(
        observed_fall=25, suspected_fall=25, normal_activity=34), 'labels changed')
    require(cloud.scoring.score(cloud_rows, labels, 'full')
            == read(args.cloud / 'summary.json'), 'Cloud score changed')
    for row in combined:
        cid = row['case_id']
        require(reviewed[cid]['label'] == labels[cid]['label'] == raw[cid]['label'],
                'mismatched class labels')
        row.update(label=labels[cid]['label'], yolo_review=reviewed[cid],
                   name=labels[cid].get('video', labels[cid].get('original_source_path', cid)))
    summary = summarize(combined)
    args.output.mkdir(mode=0o700)
    save(args.output / 'cases.json', combined)
    save(args.output / 'summary.json', summary)
    paths = [Path(__file__), Path(cloud.__file__), Path(review.__file__), args.decisions,
        args.frozen / 'freeze.json', args.spatial / 'freeze.json',
        args.cloud / 'completed.json', args.pose / 'partial_brief/completed.json',
        args.audit / 'scored-final/completed.json']
    save(args.output / 'completed.json', dict(
        sources={str(p.resolve()): sha(p) for p in paths},
        files={p.name: sha(p) for p in args.output.iterdir() if p.is_file()}))
    print('COMPLETED', json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'spatial', 'pose', 'cloud', 'audit', 'decisions', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
