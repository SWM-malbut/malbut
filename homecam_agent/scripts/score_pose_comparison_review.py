#!/usr/bin/env python3
"""Apply one association/time policy to every frozen comparison-stage request."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path

from audit_pose_comparison import MEDIA, SPATIAL, STAGES, read
from replay_fall_baseline import sha, write_json
from replay_pose_retention import verify_overlay
from review_fall_annotations import require


ASSOCIATIONS = {'target', 'normal_person', 'other_person', 'non_person', 'unresolved'}
SCENES = {'down', 'descent', 'risk_posture', 'normal', 'non_person', 'unresolved', 'pre_event'}


def validate_decisions(events, review):
    require(review['columns'] == ['audit_id', 'association', 'scene', 'note'], 'wrong columns')
    entries = review['entries']
    require(all(len(e) == 4 for e in entries), 'invalid entry')
    decisions = {uid: dict(association=a, scene=s, note=n) for uid, a, s, n in entries}
    require(len(decisions) == len(entries), 'duplicate review decision')
    require(set(decisions) == {e['audit_id'] for e in events}, 'incomplete/extra review')
    for event in events:
        d = decisions[event['audit_id']]
        require(d['association'] in ASSOCIATIONS and d['scene'] in SCENES, 'pending/invalid review')
        require(isinstance(d['note'], str) and bool(d['note'].strip()), 'missing rationale')
        require(not (event['label'] == 'normal_activity' and d['association'] == 'target'),
                'normal video has no positive target')
    return decisions


def time_position(ann, timestamp, fps):
    interval = ann.get('onset_frames')
    basis = 'onset'
    if interval is None:
        interval, basis = ann.get('first_down_frames'), 'first_down'
    if interval is None:
        return dict(position='unlabelled', delay_interval_s=None, basis=None)
    low, high = timestamp-interval[1]/fps, timestamp-interval[0]/fps
    return dict(position='early' if high < -1e-9 else 'boundary' if low < -1e-9 else 'after',
                delay_interval_s=[low, high], basis=basis)


def score_request(event, request, decision, ann, fps):
    evidence_time = event['actual_evidence_frame']/fps
    dispatched = time_position(ann, request['dispatch_time_s'], fps)
    evidence = time_position(ann, evidence_time, fps)
    relevant_scene = decision['scene'] in {'down', 'descent'} or (
        event['label'] == 'suspected_fall' and decision['scene'] == 'risk_posture')
    associated = decision['association'] == 'target'
    target_scene = associated and relevant_scene
    early = 'early' in (evidence['position'], dispatched['position'])
    unlabelled = 'unlabelled' in (evidence['position'], dispatched['position'])
    return dict(**decision, evidence_time_s=evidence_time,
                dispatch_time_s=request['dispatch_time_s'], evidence_timing=evidence,
                dispatch_timing=dispatched, target_scene=target_scene,
                target_coverage=target_scene and not early,
                time_validated=target_scene and not early and not unlabelled,
                timing_unlabelled=unlabelled, early=early,
                latency_note='Video time only; no new time label or wall-clock latency.')


def summarize_case(cid, label, results, ann):
    coverage = any(r['target_coverage'] for r in results)
    time_validated = any(r['time_validated'] for r in results)
    if label == 'normal_activity':
        outcome = 'unnecessary_request' if results else 'no_request'
    elif coverage:
        outcome = 'verified_target_and_time' if time_validated else 'target_only_time_unlabelled'
    elif not results:
        outcome = 'no_request'
    elif any(r['association'] == 'unresolved' or r['scene'] == 'unresolved' for r in results):
        outcome = 'unresolved_evidence'
    elif any(r['association'] == 'target' for r in results):
        outcome = 'early_or_unrelated_posture_only'
    else:
        outcome = 'wrong_target_only'
    return dict(case_id=cid, label=label, request_count=len(results),
                target_coverage=coverage, time_validated=time_validated, outcome=outcome,
                temporal_gt_available=(ann.get('onset_frames') is not None or
                                       ann.get('first_down_frames') is not None),
                requests=results)


def group_metrics(cases, label):
    selected = [c for c in cases if c['label'] == label]
    return dict(total=len(selected), requested=sum(c['request_count'] > 0 for c in selected),
                requests=sum(c['request_count'] for c in selected),
                target_coverage=sum(c['target_coverage'] for c in selected),
                time_validated=sum(c['time_validated'] for c in selected),
                temporal_gt_available=sum(c['temporal_gt_available'] for c in selected),
                outcomes=dict(Counter(c['outcome'] for c in selected)))


def run(args):
    require(not args.output.exists(), 'output exists')
    verify_overlay(SPATIAL, MEDIA)
    review = read(args.decisions)
    audit = Path(review['audit_path'])
    require(sha(audit/'events.json') == review['events_sha256'], 'review source changed')
    marker = read(audit/'completed.json')
    for name, expected in marker['files'].items():
        require((audit/name).resolve().is_relative_to(audit.resolve()), 'invalid audit path')
        require(sha(audit/name) == expected, 'reviewed image or data changed')
    source = read(audit/'events.json')
    for path, expected in source['sources'].items():
        require(sha(Path(path)) == expected, 'stage data changed')
    decisions = validate_decisions(source['events'], review)
    events = {e['audit_id']: e for e in source['events']}
    bundle = read(SPATIAL/'evaluation_labels.json')
    ann = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    metas = {m['case_id']: m for m in read(SPATIAL/'media.json')['cases']}
    require(Counter(c['label'] for c in labels.values()) == dict(
        observed_fall=25, suspected_fall=25, normal_activity=34), 'denominators changed')
    stages = {}
    for stage in STAGES:
        by_case = {cid: [] for cid in labels}
        refs = [r for r in source['stage_requests'] if r['stage'] == stage]
        require(len(refs) == source['stage_counts'][stage], 'missing stage requests')
        require(len({r['stage_request_index'] for r in refs}) == len(refs), 'duplicate reference')
        for ref in refs:
            event, cid = events[ref['audit_id']], ref['case_id']
            require(event['case_id'] == cid and stage in event['stages'], 'mismatched reference')
            require(ref['request']['dispatch_time_s'] == event['request']['dispatch_time_s'],
                    'shared review with different dispatch time')
            result = score_request(event, ref['request'], decisions[ref['audit_id']],
                                   ann[cid], metas[cid]['fps'])
            result.update(audit_id=ref['audit_id'], stage_request_index=ref['stage_request_index'])
            by_case[cid].append(result)
        cases = [summarize_case(cid, labels[cid]['label'], rs, ann[cid])
                 for cid, rs in by_case.items()]
        stages[stage] = dict(groups={label: group_metrics(cases, label) for label in (
            'observed_fall', 'suspected_fall', 'normal_activity')}, cases=cases)
    args.output.mkdir(mode=0o700)
    summary = dict(method=review['method'], policy=source['policy'], unique_reviews=len(events),
                   original_stage_requests=len(source['stage_requests']),
                   review_sha256=sha(args.decisions),
                   audit_marker_sha256=sha(audit/'completed.json'),
                   events_sha256=sha(audit/'events.json'),
                   spatial_freeze_sha256=sha(SPATIAL/'freeze.json'), stages=stages,
                   model_inference_calls=0, vlm_calls=0, gt_changed=False)
    write_json(args.output/'summary.json', summary)
    write_json(args.output/'completed.json', dict(
        script_sha256=sha(Path(__file__)), summary_sha256=sha(args.output/'summary.json')))
    for stage, data in stages.items():
        print(stage, json.dumps(data['groups'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--decisions', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    run(p.parse_args())
