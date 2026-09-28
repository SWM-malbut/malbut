#!/usr/bin/env python3
"""Frozen three-view Pose sequences with baseline vs temporal appearance costs.

Only real observed boxes are encoded. No new Pose or Cloud inference, training,
threshold search, GT input, retroactive token repair, runtime or ROS changes.
"""
import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time

from replay_reviewed_pose_cloud import REPO, digest, frozen_jpegs, read, replay_scene, require, save, score_tracks
from replay_box_association_ablation import outcome, score_linked_people, verify_frozen
from replay_pose_view_comparison import ARMS as VIEWS, CASES, Pipeline
from replay_reid_association import FrozenFeatures
from experimental_pose_duplicates import pose_from_dict
from experimental_temporal_reid import TemporalReIDConfig, TemporalReIDTracker
from malbut_reid.models import BoundingBox, ImageDetection
from malbut_reid.reid.osnet_encoder import OsNetPersonEncoder

TREATMENTS = ('baseline', 'temporal_reid')


def replay_poses(old_rows, features, treatment):
    require(treatment in TREATMENTS, 'unknown treatment')
    frame_to_index = {f:i for i,f in enumerate(features.record['evidence']['frame_indices'])}
    time_to_index = {r['captured_at']:frame_to_index[r['source_frame']] for r in old_rows}
    pipeline = Pipeline()
    pipeline.tracker = TemporalReIDTracker(
        lambda pose,now:features(time_to_index[now], pose.box), use_reid=treatment=='temporal_reid')
    # UUID prefixes affect labels only, never decisions. Reuse the frozen prefix
    # so the baseline can be checked field-for-field, including candidate JSON.
    ids = [t['track_id'] for r in old_rows for t in r['tracks']]
    if ids:
        pipeline.tracker._prefix = ids[0].rsplit('-',1)[0]
    pipeline.detector._prefix = old_rows[0]['candidate_payload']['observationId'].rsplit('-frame-',1)[0]
    rows = []
    for old in old_rows:
        poses = tuple(pose_from_dict(p) for p in old['poses'])
        started = time.perf_counter()
        row = pipeline.step(poses, old['source_frame'], old['captured_at'],
            features.originals[old['source_frame']], old['cloud_input'], old['inference_s'], old['view_predictions'])
        elapsed = time.perf_counter()-started
        if treatment == 'baseline':
            # The source JSON has lists where runtime dataclasses use tuples.
            # Compare wire values, without masking IDs/numbers/fields.
            require(json.loads(json.dumps(row)) == json.loads(json.dumps({k:old[k] for k in row})),
                    'baseline Pose/candidate replay changed')
        row.update(tracker_diagnostic=pipeline.tracker.frame_diagnostic,
                   track_and_candidate_s=elapsed, reused_pose_inference_time=True)
        rows.append(row)
    return rows


def score_temporal(rows, annotation, meta, criteria):
    """Audit sparse exact-frame correspondences only, not MOTA/IDF1 claims."""
    from audit_paid_vlm_localization import exact_gt
    from score_fall_baseline import match_boxes

    by_time = {r['captured_at']:r for r in rows}

    def identity(box, source_frame):
        gt = exact_gt(annotation, source_frame, meta)
        pixel_box = [v*(640 if i%2==0 else 400) for i,v in enumerate(box)]
        matched = match_boxes([g['box'] for g in gt], [pixel_box], criteria)
        people = [g for g,(status,_) in zip(gt,matched) if status=='matched']
        return people[0]['person_id'] if len(people)==1 else None

    edges = []
    for row in rows:
        for edge in row['tracker_diagnostic']['accepted']:
            previous = by_time[edge['previous_at']]
            a = identity(edge['previous_box'], previous['source_frame'])
            b = identity(edge['current_box'], row['source_frame'])
            relation = ('unverified' if a is None or b is None else
                        'same_gt_person' if a==b else 'different_gt_people')
            edges.append(dict(**edge, previous_frame=previous['source_frame'],
                source_frame=row['source_frame'], previous_person=a, current_person=b, relation=relation))
    # A separate joint matching across all observed tracks avoids awarding every
    # duplicate box its own successful person in the sparse anchor summary.
    timeline = [dict(source_frame=r['source_frame'], subjects=[dict(
        subject_key='pose:0:'+t['targetTrackId'], box=t['box'], usable=t['associationUsable'],
        state=t['trackingState']) for t in r['candidate_payload']['tracks']]) for r in rows]
    cid = annotation['case_id']
    anchors = score_tracks({cid:timeline}, {cid:annotation}, {cid:meta}, criteria)[cid]
    persons, tracks = defaultdict(list), defaultdict(set)
    target_samples = target_matches = 0
    for anchor in anchors:
        for p in anchor['people']:
            if p['role']=='target':
                target_samples += 1
                target_matches += p['status']=='matched'
            if p['status']!='matched':
                continue
            key = p['subject']['subject_key']
            persons[p['person_id']].append(dict(frame=anchor['source_frame'], track_id=key, role=p['role']))
            tracks[key].add(p['person_id'])
    sequences = [dict(person_id=person, samples=samples,
        matched_anchor_id_changes=sum(a['track_id']!=b['track_id'] for a,b in zip(samples,samples[1:])),
        distinct_ids=len({s['track_id'] for s in samples})) for person,samples in persons.items()]
    mixed = [dict(track_id=key,person_ids=sorted(people)) for key,people in tracks.items() if len(people)>1]
    return dict(edges=edges, edge_counts=dict(Counter(e['relation'] for e in edges)),
        anchors=anchors, person_sequences=sequences, mixed_identity_tracks=mixed,
        target_anchor_samples=target_samples, target_anchor_matches=target_matches,
        matched_anchor_id_changes=sum(p['matched_anchor_id_changes'] for p in sequences))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('baseline','audit','spatial','model','output'):
        parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args()
    require(not args.output.exists(),'preserve previous outputs; choose new directory')
    verify_frozen(args.baseline)
    require(args.model.is_file(),'missing cached model; no automatic download')
    args.output.mkdir(parents=True,mode=0o700)
    sources=dict(read(args.baseline/'provenance.json')['sources'])
    sources.update({str(args.baseline/n):h for n,h in read(args.baseline/'completed.json')['files'].items()})
    paths=[Path(__file__),Path(__file__).with_name('experimental_temporal_reid.py'),
        Path(__file__).with_name('replay_reid_association.py'),
        Path(__file__).with_name('experimental_reid_association.py'),
        Path(__file__).with_name('experimental_pose_duplicates.py'),
        REPO/'homecam_agent/test/test_temporal_reid.py',args.model,args.baseline/'completed.json',
        args.audit/'cases.json',args.spatial/'media.json',args.spatial/'freeze.json']
    paths+=list((REPO/'malbut_reid/malbut_reid').rglob('*.py'))
    paths.append(REPO/'malbut_reid/scripts/prepare_osnet_model.sh')
    sources.update({str(p):digest(p) for p in paths})
    save(args.output/'plan.json',dict(cases=CASES,views=VIEWS,treatments=TREATMENTS,
        appearance_config=asdict(TemporalReIDConfig()),
        rule='same geometric eligibility; reject cosine<.80; otherwise original cost+(1-cosine)',
        reference='last accepted actual observation only; max gap 1s; no long-term gallery',
        missing_crop='whole-frame geometry fallback retaining all competitors',
        unchanged=['all original Pose values','tracker confirmation/expiry/mutual .10 margin',
            'candidate rules','all Cloud-returned regions','IoU .60/.15 cloud association',
            'continuity tokens/latest subject/incident guards','Cloud JPEGs/replies','GT'],
        no_gt_for_routing=True,no_threshold_search=True,new_api_calls=0,new_pose_inference_calls=0,
        model_sha256=digest(args.model),scope='short-lived temporal tracking; offline known development scenes'))
    import cv2
    import numpy as np
    import onnxruntime as ort
    print('loading cached OSNet',flush=True)
    encoder=OsNetPersonEncoder(str(args.model),dnn_target='cpu',inference_backend='onnxruntime')
    started=time.perf_counter()
    encoder.encode(np.zeros((400,640,3),np.uint8),[ImageDetection(BoundingBox(0,0,128,256),1.)])
    save(args.output/'environment.json',dict(python=sys.version,platform=platform.platform(),
        opencv=cv2.__version__,numpy=np.__version__,onnxruntime=ort.__version__,
        resolved_target=encoder.resolved_target,warmup_s=time.perf_counter()-started,
        scope='PC CPU, no Jetson throughput; shared per-frame/box feature cache'))
    config=read(REPO/'malbut_agent_server/config/fall_runtime.example.json')
    metas={m['case_id']:m for m in read(args.spatial/'media.json')['cases']}
    prior={(r['arm'],r['model'],r['case_id']):r for r in read(args.baseline/'comparison.json')
           if r['mode']=='production'}
    inputs,jobs,originals={}, {}, {}
    for model,entries in read(args.audit/'cases.json').items():
        for entry in entries:
            cid=entry['case_id']
            if cid not in CASES: continue
            path=Path(entry['result_path']); ipath=path.parent.parent/'inputs'/f'{cid}.input.json'
            record,result=read(ipath),read(path)
            require(result['case_id']==cid,'wrong cached response')
            if cid in inputs: require(inputs[cid]==record,'different provider input')
            inputs[cid]=record; jobs[model,cid]=result
            originals[cid]=frozen_jpegs(record,metas[cid])
            sources.update({str(p):digest(p) for p in (path,ipath)})
    require(len(jobs)==18 and set(inputs)==set(CASES),'missing input')
    all_rows,features,replays,comparisons={}, {}, {}, []
    for cid in CASES:
        # The extended schedule is only for local Pose tracking/embeddings.
        # Each Cloud replay still receives exactly its original 12 JPEGs.
        old=read(args.baseline/f'full-{cid}.pose.json')
        images={r['source_frame']:(args.baseline/'images'/cid/f"{r['source_frame']:04d}.jpg").read_bytes()
                for r in old}
        record=dict(evidence=dict(frame_indices=[r['source_frame'] for r in old],
                                 jpeg_sha256=[r['jpeg_sha256'] for r in old]))
        features[cid]=FrozenFeatures(images,record,encoder)
        for view in VIEWS:
            print('tracking',cid,view,flush=True)
            old=read(args.baseline/f'{view}-{cid}.pose.json')
            for treatment in TREATMENTS:
                rows=replay_poses(old,features[cid],treatment)
                all_rows[view,treatment,cid]=rows
                save(args.output/f'{view}-{treatment}-{cid}.pose.json',rows)
                for model in ('gemma','gemini'):
                    stem=f'{view}-{treatment}-{model}-{cid}'
                    replay=replay_scene(rows,originals[cid],inputs[cid],jobs[model,cid],config,
                                        args.output/f'{stem}.sqlite')
                    summary=outcome(replay)
                    if treatment=='baseline':
                        require(all(summary[k]==prior[view,model,cid][k] for k in summary),
                                'baseline merge changed')
                    save(args.output/f'{stem}.replay.json',replay)
                    replays[view,treatment,model,cid]=replay
                    comparisons.append(dict(view=view,treatment=treatment,model=model,case_id=cid,**summary))
        save(args.output/f'{cid}.features.json',features[cid].entries)
        print(cid,'unique local crops',len(features[cid].entries),flush=True)
    save(args.output/'comparison.json',comparisons)
    # Only now load ground-truth identities/classifications, after all decisions.
    freeze=read(args.spatial/'freeze.json')
    for name,expected in freeze['files'].items():
        path=args.spatial/name; require(digest(path)==expected,'approved labels changed')
        sources[str(path)]=expected
    bundle=read(args.spatial/'evaluation_labels.json')
    annotations={c['case_id']:c for c in bundle['annotations']['cases']}
    labels={c['case_id']:c['label'] for c in bundle['classifications']['cases']}
    metrics,links=[],[]
    for (view,treatment,cid),rows in all_rows.items():
        scored=score_temporal(rows,annotations[cid],metas[cid],freeze['match'])
        save(args.output/f'{view}-{treatment}-{cid}.temporal-score.json',scored)
        metrics.append(dict(view=view,treatment=treatment,case_id=cid,label=labels[cid],frames=len(rows),
            unassigned_frames=sum(bool(r['unassigned']) for r in rows),
            candidate_count=sum(len(r['candidate_payload']['candidates']) for r in rows),
            usable_observations=sum(t['associationUsable'] for r in rows for t in r['candidate_payload']['tracks']),
            created_tracks=sum(len(r['tracker_diagnostic']['births']) for r in rows),
            missing_feature_fallback_frames=sum(bool(r['tracker_diagnostic']['fallback_reason']) for r in rows),
            appearance_rejections=sum(p['reason']=='appearance_gate' for r in rows
                                      for p in r['tracker_diagnostic']['pair_costs']),
            edge_counts=scored['edge_counts'],mixed_identity_tracks=len(scored['mixed_identity_tracks']),
            matched_anchor_id_changes=scored['matched_anchor_id_changes'],
            target_anchor_samples=scored['target_anchor_samples'],target_anchor_matches=scored['target_anchor_matches']))
    for view in VIEWS:
        for treatment in TREATMENTS:
            for model in ('gemma','gemini'):
                scored=score_tracks({c:replays[view,treatment,model,c]['pose_timeline'] for c in CASES},
                                    annotations,metas,freeze['match'])
                for cid in CASES:
                    links.append(dict(view=view,treatment=treatment,model=model,case_id=cid,
                        findings=score_linked_people(replays[view,treatment,model,cid],scored[cid])))
    save(args.output/'metrics.json',metrics);save(args.output/'linked-person-score.json',links)
    entries=[e for f in features.values() for e in f.entries]
    times=[e['crop_encode_s'] for e in entries if e['feature'] is not None]
    summary=dict(new_api_calls=0,new_pose_inference_calls=0,local_replays=len(replays),
        unique_crop_requests=len(entries),osnet_calls=len(times),warmup_calls=1,
        insufficient_crops=len(entries)-len(times),crop_encode_sum_s=sum(times),
        crop_encode_median_ms=statistics.median(times)*1000 if times else None,conditions=[])
    for view in VIEWS:
        for treatment in TREATMENTS:
            group=[m for m in metrics if m['view']==view and m['treatment']==treatment]
            cc=[c for c in comparisons if c['view']==view and c['treatment']==treatment
                and c['case_id'] in CASES[:4]]
            summary['conditions'].append(dict(view=view,treatment=treatment,
                valid_multiperson_replies=sum(c['reply_usable'] for c in cc),
                linked=sum(c['linked'] for c in cc),
                reasons=dict(sum((Counter(c['reasons']) for c in cc),Counter())),
                unassigned_frames=sum(m['unassigned_frames'] for m in group),
                created_tracks=sum(m['created_tracks'] for m in group),
                normal_candidate_count=sum(m['candidate_count'] for m in group if m['label']=='normal_activity'),
                normal_cases_with_candidates=sum(m['candidate_count']>0 for m in group if m['label']=='normal_activity'),
                edge_counts=dict(sum((Counter(m['edge_counts']) for m in group),Counter())),
                mixed_identity_tracks=sum(m['mixed_identity_tracks'] for m in group),
                matched_anchor_id_changes=sum(m['matched_anchor_id_changes'] for m in group),
                target_anchor_matches=sum(m['target_anchor_matches'] for m in group),
                target_anchor_samples=sum(m['target_anchor_samples'] for m in group)))
    for path in args.output.glob('*.sqlite'):
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as connection:
            require(connection.execute('PRAGMA integrity_check').fetchone()==('ok',),'SQLite corruption')
    require(all(digest(p)==h for p,h in sources.items()),'frozen source changed')
    save(args.output/'summary.json',summary)
    save(args.output/'provenance.json',dict(sources=sources,production_code_changed=False,
        baseline_pose_and_candidate_fields_reproduced=True,baseline_merges_reproduced=True,
        no_gt_for_routing=True,new_api_calls=0))
    save(args.output/'completed.json',dict(files={str(p.relative_to(args.output)):digest(p)
        for p in sorted(args.output.rglob('*')) if p.is_file()}))
    print(summary,flush=True)


if __name__=='__main__':
    main()
