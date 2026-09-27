#!/usr/bin/env python3
"""Compare frozen baseline and box-only association without API/inference/GT input.

This tests the proposed input boundary, not a deployment or ROS contract change.
See replay_reviewed_pose_cloud.py for the cached-dispatch/sampling limitations.
"""
import argparse
from collections import Counter
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from replay_reviewed_pose_cloud import (
    REPO, Clock, NoNetworkProvider, cached_reply, digest, event_metadata,
    frozen_jpegs, incident_metadata, read, replay_scene, require, save, score_tracks,
)
from experimental_box_association import BoxAssociationConfig, box_subject_frame
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
from malbut_agent_server.application.fall_cloud_association import associate_finding
from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import CloudFallRequest, FallRuntimePolicy


def replay_box_only(rows, originals, record, result, config, journal_path):
    """Same real candidates and monitor; explicit experimental subject boundary.

    Never lie to the production adapter by setting features.usable=True. That
    adapter remains unchanged and still rejects the proposed box-only schema.
    """
    clock = Clock()
    journal = SqliteFallJournal(journal_path, device_id='offline-replay', wall_clock=clock)
    monitor = CloudFallMonitor(device_id='offline-replay', boot_id='offline',
        policy=FallRuntimePolicy.agreed(**config['policy']),
        buffer=FallFrameBuffer(retention_s=config['retention_s'], max_bytes=config['buffer_bytes'],
                               max_frames=config['buffer_frames']),
        provider=NoNetworkProvider(), journal=journal, clock=clock)
    adapter = FallDetectorInput(monitor, max_source_age_s=config['max_source_age_s'])
    adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    timeline = []
    try:
        for row in rows:
            clock.value = row['captured_at']
            subject_frame, decisions = box_subject_frame(row)
            if row['source_frame'] in originals:
                adapter.rgb(originals[row['source_frame']], capture=clock.value, frame_id='offline_rgb',
                            source_now=clock.value, now=clock.value)
            # Same candidates pass their production validation unchanged.
            # The proposed location observation is ingested separately, not
            # disguised as a validated production ROS message.
            payload = copy.deepcopy(row['candidate_payload'])
            payload.pop('subjectCheckVersion')
            payload.pop('subjectCheckMaxGapSec')
            ids = adapter.candidates(json.dumps(payload), source_now=clock.value, now=clock.value)
            monitor.ingest_subject_frame(subject_frame)
            for decision in decisions:
                decision['token'] = monitor._subject_evidence.token_at(decision['subject_key'], clock.value)
            timeline.append(dict(source_frame=row['source_frame'], incidents=list(ids), subjects=decisions))
        window = monitor.buffer.window(end=clock.value, duration_s=config['policy']['clip_window_s'],
                                       max_images=12, max_age_s=config['policy']['max_frame_age_s'])
        require([hashlib.sha256(f.jpeg).hexdigest() for f in window.frames]
                == record['evidence']['jpeg_sha256'], 'frozen Cloud frames changed')
        snapshot = monitor._subject_evidence.snapshot(window)
        before = incident_metadata(monitor)
        pose_events = event_metadata(monitor.drain_events())
        reply = cached_reply(result)
        findings, events, repeated = [], [], []
        if reply is not None:
            require(all(r.frame_index < len(window.frames) for f in reply.findings for r in f.regions),
                    'region index outside frozen window')
            findings = [dict(association=asdict(associate_finding(f, snapshot)),
                             regions=[dict(frame_index=r.frame_index,
                                 source_frame=record['evidence']['frame_indices'][r.frame_index],box=r.box)
                                      for r in f.regions]) for f in reply.findings]
            request = CloudFallRequest('cached-'+result['case_id'], 'crosscheck', 'offline-replay', 'offline',
                                      None, None, 0, window, None)
            monitor._record_crosscheck(request, reply, snapshot, monitor._scene_incident_versions())
            events = event_metadata(monitor.drain_events())
            after = incident_metadata(monitor)
            monitor._record_crosscheck(request, reply, snapshot, monitor._scene_incident_versions())
            repeated = event_metadata(monitor.drain_events())
            require(incident_metadata(monitor) == after, 'repeat changed incident state/budget')
        else:
            after = before
        persisted, unresolved = journal.discoveries(), journal.unresolved()
    finally:
        journal.close()
    reopened = SqliteFallJournal(journal_path, device_id='offline-replay', wall_clock=clock)
    try:
        require(reopened.discoveries() == persisted and reopened.unresolved() == unresolved,
                'journal reopen mismatch')
    finally:
        reopened.close()
    return dict(case_id=result['case_id'], outcome=result['outcome'], reply_usable=reply is not None,
        pose_timeline=timeline, findings=findings, events=events, repeat_events=repeated,
        before=before, after=after, pose_events=pose_events, repeat_incidents_unchanged=True,
        journal_reopen_verified=True, persisted_discoveries=len(persisted), new_api_calls=0,
        scope='experimental location boundary; production adapter unchanged; no ROS/scheduler/delay',
        snapshot=[[dict(key=k, token=t, box=p.box, usable=p.association_usable,
                        clearance=p.state.value) for k,t,p in entries] for entries in snapshot])


def outcome(replay):
    discoveries = [e['discovery'] for e in replay['events'] if e['discovery']]
    return dict(reply_usable=replay['reply_usable'],
                reasons=dict(Counter(d['reason'] for d in discoveries)),
                linked=sum(d['incident_id'] is not None and d['subject_key'] is not None
                           for d in discoveries),
                before_incidents=len(replay['before']), after_incidents=len(replay['after']))


def verify_frozen(directory):
    completed = read(directory/'completed.json')
    for name, expected in completed['files'].items():
        require(digest(directory/name) == expected, 'frozen run output changed: '+name)
    provenance = read(directory/'provenance.json')
    for path, expected in provenance['sources'].items():
        require(digest(path) == expected, 'frozen source changed: '+path)


def score_linked_people(replay, scored):
    """Posthoc correctness per actual returned region, never a routing input."""
    frames = {f['source_frame']:f['people'] for f in scored}
    output = []
    for event in replay['events']:
        discovery = event['discovery']
        if not discovery or discovery['incident_id'] is None or discovery['subject_key'] is None:
            continue
        finding = replay['findings'][discovery['finding_index']]
        region_people = []
        for region in finding['regions']:
            exact = [p for p in frames.get(region['source_frame'], [])
                     if p['subject'] and p['subject']['subject_key'] == discovery['subject_key']]
            region_people.append(dict(source_frame=region['source_frame'],
                person_id=exact[0]['person_id'] if len(exact)==1 else None,
                role=exact[0]['role'] if len(exact)==1 else None))
        ids = {p['person_id'] for p in region_people}
        status = ('verified_target_on_returned_regions' if None not in ids and len(ids)==1
                  and all(p['role']=='target' for p in region_people) else
                  'wrong_person' if any(p['role']=='other' for p in region_people) else 'unverified')
        output.append(dict(finding_index=discovery['finding_index'],status=status,regions=region_people))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline','audit','spatial','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve previous outputs')
    verify_frozen(args.baseline)
    args.output.mkdir(parents=True,mode=0o700)
    source_paths = set(args.baseline.glob('*.pose.json')) | {
        args.baseline/'completed.json', args.baseline/'provenance.json', args.baseline/'summary.json',
        args.audit/'cases.json', args.spatial/'media.json',args.spatial/'freeze.json',
        REPO/'malbut_agent_server/config/fall_runtime.example.json', Path(__file__),
        Path(__file__).with_name('experimental_box_association.py')}
    sources = {str(p):digest(p) for p in source_paths}
    sources.update(read(args.baseline/'provenance.json')['sources'])
    plan = read(args.baseline/'plan.json')
    cases = plan['cases']
    save(args.output/'plan.json',dict(cases=cases,arms=['baseline','box_only'],
        config=asdict(BoxAssociationConfig()),change='remove anatomy gate for location only',
        unchanged=['tracked state','strong confidence','global unassigned gate','IoU 0.60',
                   'IoU margin 0.15','all returned regions','continuity token','latest target',
                   'candidate generation','frame schedule','Cloud replies'],
        new_api_calls=0,new_inference=0,gt_only_posthoc=True,production_code_changed=False,
        normal_clearance_from_boxes=False,scope='offline component ablation on selected development scenes'))
    metas = {m['case_id']:m for m in read(args.spatial/'media.json')['cases']}
    config = read(REPO/'malbut_agent_server/config/fall_runtime.example.json')
    prior = {(r['model'],r['case_id']):r for r in read(args.baseline/'summary.json')}
    replays, comparisons = {}, []
    for model, entries in read(args.audit/'cases.json').items():
        for entry in entries:
            cid = entry['case_id']
            if cid not in cases:
                continue
            result_path = Path(entry['result_path'])
            input_path = result_path.parent.parent/'inputs'/f'{cid}.input.json'
            sources.update({str(p):digest(p) for p in (result_path,input_path)})
            rows = read(args.baseline/f'{cid}.pose.json')
            record, result = read(input_path), read(result_path)
            require(result['case_id']==cid, 'cached response case mismatch')
            originals = frozen_jpegs(record,metas[cid])
            controls = {}
            for arm, run in [('baseline',replay_scene),('box_only',replay_box_only)]:
                value = run(rows,originals,record,result,config,args.output/f'{arm}-{model}-{cid}.sqlite')
                save(args.output/f'{arm}-{model}-{cid}.json',value)
                replays[arm,model,cid] = value
                controls[arm] = outcome(value)
            require(all(controls['baseline'][k] == prior[model,cid][k]
                        for k in ('reply_usable','reasons','linked','before_incidents','after_incidents')),
                    'control differs from frozen baseline')
            comparisons.append(dict(model=model,case_id=cid,**controls))
    save(args.output/'comparison.json',comparisons)
    # Labels and person identities are first read here, after both arms finish.
    freeze = read(args.spatial/'freeze.json')
    for name, expected in freeze['files'].items():
        path=args.spatial/name
        require(digest(path)==expected,'approved GT changed')
        sources[str(path)]=expected
    annotations={c['case_id']:c for c in read(args.spatial/'evaluation_labels.json')['annotations']['cases']}
    scored = {}
    for arm in ('baseline','box_only'):
        for model in sorted({m for a,m,c in replays}):
            score = score_tracks({cid:replays[arm,model,cid]['pose_timeline'] for cid in cases},
                                 annotations,metas,freeze['match'])
            scored[arm,model]=score
            save(args.output/f'{arm}-{model}-gt-score.json',score)
    link_scores=[]
    for (arm,model,cid),replay in replays.items():
        link_scores.append(dict(arm=arm,model=model,case_id=cid,
            linked_findings=score_linked_people(replay,scored[arm,model][cid])))
    save(args.output/'linked-person-score.json',link_scores)
    eligibility=[]
    # Count the shared Pose frames once, not once per Cloud provider.
    model=sorted({m for a,m,c in replays})[0]
    for cid in cases:
        frames=replays['box_only',model,cid]['pose_timeline']
        changed=[dict(source_frame=f['source_frame'],**s) for f in frames for s in f['subjects']
                 if s['usable']!=s['baseline_usable']]
        require(all(s['usable'] and not s['feature_usable'] for s in changed),
                'ablation changed more than anatomy eligibility')
        eligibility.append(dict(case_id=cid,track_observations=sum(len(f['subjects']) for f in frames),
            baseline_usable=sum(s['baseline_usable'] for f in frames for s in f['subjects']),
            box_only_usable=sum(s['usable'] for f in frames for s in f['subjects']),changes=changed))
    save(args.output/'eligibility.json',eligibility)
    require(all(digest(path)==expected for path,expected in sources.items()),'sources changed')
    save(args.output/'provenance.json',dict(sources=sources,new_api_calls=0,new_inference=0,
        gt_used_for_routing=False,baseline_control_reproduced=True,production_code_changed=False))
    save(args.output/'completed.json',dict(files={p.name:digest(p) for p in sorted(args.output.iterdir())
                                               if p.is_file()}))
    print(json.dumps(comparisons,ensure_ascii=False,indent=2))
    print('eligibility',[(r['case_id'],r['baseline_usable'],r['box_only_usable']) for r in eligibility])


if __name__ == '__main__':
    main()
