#!/usr/bin/env python3
"""Posthoc exact-instant review of frozen streaming trials; no inference/network."""
import argparse
from collections import Counter
from pathlib import Path
import statistics

from replay_reviewed_pose_cloud import digest, read, require, save
from replay_deferred_location_ablation import audit_links


def audit_timing(result):
    delivered = {d['captured_at']: d for d in result['delivered']}
    samples = result['samples']
    for i, sample in enumerate(samples):
        source = delivered[sample['observed_at']]
        require(source['source_frame'] == sample['source_frame'], 'frame changed')
        require(sample['observed_at'] <= source['delivered_at'] <= sample['started_at']
                <= sample['gpu_ready_at'] <= sample['delivered_at'], 'noncausal processing')
        require(sample['available_input_count'] == i + 1, 'future model input count')
        require(abs(sample['age_s'] - (sample['delivered_at'] - sample['observed_at'])) < 1e-9,
                'capture age changed')
    links = [e for e in result['events'] if e['kind'] == 'cloud_discovery_linked']
    if result['mode'] in ('cached_causal', 'late_delivery', 'camera_off'):
        require(not links, 'negative control linked stale or canceled evidence')
    if result['mode'] == 'camera_off':
        require(result['queue_closed'] == 'camera_off' and len(samples) == 1,
                'camera stop did not cancel queued work')
        first = samples[0]
        require(first['started_at'] <= result['camera_off_at'] < first['gpu_ready_at'],
                'control missed in-flight GPU work')
        require(first['decision']['reason'] == 'unknown_tracking_session',
                'late GPU result survived camera reset')
        require(all(d['delivered_at'] <= result['camera_off_at'] for d in result['delivered']),
                'RGB accepted after camera off')
        if 'final_history_frames' in result:
            require(result['final_history_frames'] == result['final_queue_bytes']
                    == result['final_buffer_bytes'] == 0 and result['sam_state_released'],
                    'camera reset retained buffered data')
    return dict(timing_consistent=True, gpu_frames=len(samples),
                canceled_gpu_result_rejected=result['mode'] == 'camera_off')


def attachment_types(result):
    existing, created = 0, 0
    for e in result['events']:
        if e['kind'] != 'cloud_discovery_linked':
            continue
        sample = next(s for s in result['samples']
                      if s['observed_at'] == e['discovery']['association_link']['confirmed_at'])
        opened = [v for v in result['events']
                  if v['kind'] == 'incident_opened' and v['incident_id'] == e['incident_id']]
        require(len(opened) == 1, 'target incident opening missing or duplicated')
        if opened[0]['received_at'] < sample['delivered_at']:
            existing += 1
        else:
            created += 1
    return dict(attached_existing=existing, created_person_case=created)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'cached', 'spatial', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    completed = read(args.run / 'completed.json')
    require(completed['complete'], 'unfinished inference must not be scored')
    for name, expected in completed['files'].items():
        require(digest(args.run / name) == expected, 'frozen output changed')
    require(not args.output.exists(), 'preserve previous report')
    plan = read(args.cached / 'plan.json')
    freeze = read(args.spatial / 'freeze.json')
    for name, expected in freeze['files'].items():
        require(digest(args.spatial / name) == expected, 'review changed')
    annotations = {a['case_id']: a for a in read(
        args.spatial / 'evaluation_labels.json')['annotations']['cases']}
    cases, per_frame, startup, latency = [], [], [], []
    for summary in read(args.run / 'summary.json'):
        cid, mode = summary['case_id'], summary['mode']
        result = read(args.run / mode / cid / 'result.json')
        checks = audit_timing(result)
        samples = [dict(s, captured_at=s['observed_at']) for s in result['samples']]
        links = audit_links(dict(link_events=result['events']),
            read(args.cached / 'run' / cid / 'pose.json'), dict(samples=samples),
            annotations[cid], plan['cases'][cid]['meta'], freeze['match'])
        if mode == 'early_fixture' and samples:
            startup.append(samples[0]['gpu_ready_at'] - samples[0]['started_at'])
            per_frame.extend(s['gpu_ready_at'] - s['started_at'] for s in samples[1:])
            latency.extend(s['age_s'] for s in samples)
        cases.append(dict(**summary, **attachment_types(result), checks=checks, link_audit=links,
            decision_counts=dict(Counter(s['decision']['reason'] for s in samples)),
            journal_reopen_verified=result['journal_reopen_verified']))
    verified = [a for c in cases for a in c['link_audit']]
    output = dict(cases=cases, scope='controlled streaming plumbing, NOT Cloud accuracy or deployment',
        pose_cached=True, inference_reads_human_review=False, new_api_calls=0,
        exact_review_available=sum(a['exact_review_available'] for a in verified),
        correct_reviewed_instant=sum(a['correct_at_reviewed_instant'] for a in verified),
        wrong_reviewed_person=sum(a['wrong_person_at_reviewed_instant'] for a in verified),
        unreviewed_links=sum(not a['exact_review_available'] for a in verified),
        startup_worker_median_s=statistics.median(startup) if startup else None,
        frame_worker_median_s=statistics.median(per_frame) if per_frame else None,
        frame_worker_max_s=max(per_frame) if per_frame else None,
        observation_age_median_s=statistics.median(latency) if latency else None,
        max_dispatch_lag_s=max(c['max_dispatch_lag_s'] for c in cases),
        source_manifest_sha256=digest(args.run / 'completed.json'),
        review_sha256=digest(args.spatial / 'evaluation_labels.json'))
    save(args.output, output)
    import json
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
