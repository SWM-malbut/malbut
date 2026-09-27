#!/usr/bin/env python3
"""Cached SAM/Pose -> actual monitor attachment and SQLite, with NO API calls.

The saved Cloud reply is introduced AFTER the whole clip, not at its seed.
Tracking samples are then replayed retrospectively. This exercises transitions,
not live Cloud/SAM latency, the scheduler, ROS or real-world identity accuracy.
No annotations enter inference or the attachment boundary.
"""

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from replay_reviewed_pose_cloud import (
    REPO, Clock, NoNetworkProvider, cached_reply, digest, event_metadata,
    frozen_jpegs, incident_metadata, read, require, save, stamp,
    CloudFallMonitor, CloudFallRequest, FallDetectorInput, FallFrameBuffer,
    FallRuntimePolicy, FrameWindow, RgbFrame, SqliteFallJournal,
)


def replay(case, folder, config, destination, *, subject_frame_factory=None):
    rows, tracking = read(folder / 'pose.json'), read(folder / 'result.json')
    record, result = read(case['input']), read(case['response'])
    originals = frozen_jpegs(record, case['meta'])
    clock = Clock()
    path = destination / 'events.sqlite'
    journal = SqliteFallJournal(path, device_id='offline-replay', wall_clock=clock)
    monitor = CloudFallMonitor(
        device_id='offline-replay', boot_id='offline',
        policy=FallRuntimePolicy.agreed(**config['policy']),
        buffer=FallFrameBuffer(retention_s=config['retention_s'],
                               max_bytes=config['buffer_bytes'], max_frames=config['buffer_frames']),
        provider=NoNetworkProvider(), journal=journal, clock=clock)
    adapter = FallDetectorInput(monitor, max_source_age_s=config['max_source_age_s'])
    adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    location_timeline = []
    for row in rows:
        clock.value = row['captured_at']
        image = folder / 'images' / f'{row["source_frame"]:05d}.jpg'
        require(digest(image) == row['jpeg_sha256'], 'tracking image changed')
        # Actual saved 4 Hz tracker RGB, NOT replacement Cloud request frames.
        monitor.ingest_rgb(RgbFrame(clock.value, image.read_bytes()))
        payload = row['candidate_payload']
        if subject_frame_factory is not None:
            # Explicit offline location-only boundary, not an accepted ROS
            # contract. Never alter features.usable or candidate generation.
            payload = copy.deepcopy(payload)
            payload.pop('subjectCheckVersion')
            payload.pop('subjectCheckMaxGapSec')
        adapter.candidates(json.dumps(payload), source_now=clock.value, now=clock.value)
        if subject_frame_factory is not None:
            frame, decisions = subject_frame_factory(row)
            require(all(p.state.value == 'unknown' for p in frame.subjects),
                    'location-only evidence must never clear an incident')
            monitor.ingest_subject_frame(frame)
            location_timeline.append(dict(source_frame=row['source_frame'], subjects=decisions))
    before = incident_metadata(monitor)
    pose_events = event_metadata(monitor.drain_events())
    frames = tuple(RgbFrame(stamp(index, case['meta']['fps']), jpeg)
                   for index, jpeg in sorted(originals.items()))
    require([hashlib.sha256(f.jpeg).hexdigest() for f in frames] == record['evidence']['jpeg_sha256'],
            'cached Cloud images changed')
    clock.value = max(clock.value, frames[-1].captured_at)
    window = FrameWindow(frames, max(0, frames[-1].captured_at - 5), frames[-1].captured_at, False)
    request = CloudFallRequest('cached-' + result['case_id'], 'crosscheck', 'offline-replay',
                               'offline', None, None, 0, window, None)
    monitor._record_crosscheck(request, cached_reply(result),
                              monitor._subject_evidence.snapshot(window), monitor._scene_incident_versions())
    initial_events = monitor.drain_events()
    discoveries = [e.discovery for e in initial_events if e.discovery]
    require(len(discoveries) <= 1, 'one-object cached SAM pilot only')
    progress, replay_idempotent = [], True
    if discoveries and discoveries[0].subject_key is None:
        sid = monitor.begin_discovery_tracking(discoveries[0].discovery_id)
        for sample in tracking['samples']:
            require(digest(folder / 'masks' / f'{sample["source_frame"]:05d}.png') == sample['mask_sha256'],
                    'cached mask changed')
            outcome = monitor.ingest_discovery_track(
                sid, observed_at=sample['captured_at'],
                box=tuple(sample['box']) if sample['box'] is not None else None)
            progress.append(dict(source_frame=sample['source_frame'], **asdict(outcome)))
        linked = [r for r in progress if r['reason'] == 'matched_after_tracking']
        if linked:
            after = incident_metadata(monitor)
            sample = tracking['samples'][-1]
            duplicate = monitor.ingest_discovery_track(sid, observed_at=sample['captured_at'],
                                                       box=tuple(sample['box']))
            replay_idempotent = duplicate.reason == 'already_linked' and incident_metadata(monitor) == after
            require(replay_idempotent, 'repeat changes linked case')
    new_events = monitor.drain_events()
    links = [e.discovery for e in new_events if e.kind == 'cloud_discovery_linked']
    existing = {i['incident_id'] for i in before}
    saved = journal.discoveries()
    unresolved = journal.unresolved()
    journal.close()
    reopened = SqliteFallJournal(path, device_id='offline-replay', wall_clock=clock)
    require(reopened.discoveries() == saved and reopened.unresolved() == unresolved,
            'journal changed after reopen')
    reopened.close()
    output = dict(case_id=result['case_id'], group=case['group'],
        discoveries=len(discoveries), direct_links=sum(d.subject_key is not None for d in discoveries),
        deferred_links=len(links), attached_existing=sum(d.incident_id in existing for d in links),
        created_person_cases=sum(d.incident_id not in existing for d in links),
        final_reason=progress[-1]['reason'] if progress else 'no_deferred_tracking',
        before=before, after=incident_metadata(monitor), pose_events=pose_events,
        initial_events=event_metadata(initial_events), link_events=event_metadata(new_events),
        progress=progress, journal_reopen_verified=True, replay_idempotent=replay_idempotent,
        experimental_location_boundary=subject_frame_factory is not None,
        location_timeline=location_timeline,
        new_api_calls=0, simulated_inference_latency=False, annotations_used=False,
        scope='cached post-clip association, measured 4Hz tracking vs original Cloud samples; no scheduler/ROS')
    save(destination / 'result.json', output)
    return {k: output[k] for k in ('case_id', 'group', 'discoveries', 'direct_links', 'deferred_links',
        'attached_existing', 'created_person_cases', 'final_reason', 'journal_reopen_verified', 'replay_idempotent')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cached', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve previous results')
    plan_path = args.cached / 'plan.json'
    plan = read(plan_path)
    done_path = args.cached / 'run/completed.json'
    done = read(done_path)
    require(done['complete'], 'cached inference incomplete')
    # Original inference code hashes are intentionally old; immutable INPUT and
    # RESULT hashes must still agree. Record new core code separately.
    for p, expected in plan['sources'].items():
        require(digest(p) == expected, 'cached input changed: ' + p)
    sources = {str(plan_path): digest(plan_path), str(done_path): digest(done_path)}
    config_path = REPO / 'malbut_agent_server/config/fall_runtime.example.json'
    sources[str(config_path)] = digest(config_path)
    config = read(config_path)
    code = {str(p): digest(p) for p in (REPO / 'malbut_agent_server/malbut_agent_server').rglob('*.py')}
    for p in (Path(__file__), Path(__file__).with_name('replay_reviewed_pose_cloud.py')):
        code[str(p)] = digest(p)
    args.output.mkdir(mode=0o700, parents=True)
    summary = []
    for row in done['results']:
        cid = row['case_id']
        folder = args.cached / 'run' / cid
        for kind in ('pose', 'result'):
            path = folder / f'{kind}.json'
            require(digest(path) == row[f'{kind}_sha256'], 'cached result changed')
            sources[str(path)] = digest(path)
        dest = args.output / cid
        dest.mkdir(mode=0o700)
        summary.append(replay(plan['cases'][cid], folder, config, dest))
    for p, expected in {**sources, **code}.items():
        require(digest(p) == expected, 'source changed during replay')
    save(args.output / 'summary.json', summary)
    save(args.output / 'provenance.json', dict(sources=sources, code=code, new_api_calls=0,
         cached_plan_sha256=digest(plan_path), compared_with_old_7_cases=False))
    # Only AFTER every transition is frozen: reuse exact reviewed instants.
    # No nearest-frame labels or interpolated person identities are permitted.
    score_path = args.cached / 'scores.json'
    scores = {c['case_id']: c for c in read(score_path)['cases']}
    audits = []
    for row in summary:
        cid = row['case_id']
        replayed = read(args.output / cid / 'result.json')
        samples = read(args.cached / 'run' / cid / 'result.json')['samples']
        for event in replayed['link_events']:
            if event['kind'] != 'cloud_discovery_linked':
                continue
            discovery = event['discovery']
            when = discovery['association_link']['confirmed_at']
            sample = next(s for s in samples if s['captured_at'] == when)
            reviewed = next((a for a in scores[cid]['audited']
                             if a['source_frame'] == sample['source_frame']), None)
            original_alias = sample['bridge']['active']
            comparable = bool(reviewed and original_alias and
                discovery['subject_key'] == 'pose:0:' + original_alias['pose_id'])
            audits.append(dict(case_id=cid, source_frame=sample['source_frame'],
                incident_id=discovery['incident_id'], exact_review_available=comparable,
                correct_at_reviewed_instant=bool(comparable and reviewed['linked_correct']),
                wrong_person_at_reviewed_instant=bool(comparable and reviewed['linked_wrong_person'])))
    save(args.output / 'review-audit.json', dict(links=audits,
        previous_scores_sha256=digest(score_path), previous_labels_sha256=read(score_path)['labels_sha256'],
        scope='existing exact-instant box review after replay; not complete trajectory or held-out accuracy'))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
