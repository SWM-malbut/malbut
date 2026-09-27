#!/usr/bin/env python3
"""Replay frozen multi-view poses with/without cross-view duplicate suppression.

No new inference, API calls, GT-driven routing, threshold search or deployment.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import itertools
from pathlib import Path
import statistics
import time

from replay_reviewed_pose_cloud import REPO, digest, frozen_jpegs, read, replay_scene, require, save, score_tracks
from replay_box_association_ablation import outcome, replay_box_only, score_linked_people, verify_frozen
from replay_pose_view_comparison import ARMS, CASES, Pipeline, detection_score
from experimental_pose_duplicates import DuplicateConfig, retained_observations, suppress_duplicates


def replay_poses(rows, arm, treatment, image_directory):
    pipeline = Pipeline()
    result = []
    for old in rows:
        observations = retained_observations(old, arm)
        started = time.perf_counter()
        if treatment == 'joints':
            poses, evidence = suppress_duplicates(observations)
        else:
            require(treatment == 'baseline', 'unknown treatment')
            poses = tuple(o['pose'] for o in observations)
            evidence = dict(kept_indices=list(range(len(poses))), groups=[], pair_evidence=[],
                            input_sources=[o['source'] for o in observations])
        fusion_time = time.perf_counter()-started
        image_path = image_directory/f"{old['source_frame']:04d}.jpg"
        require(digest(image_path) == old['jpeg_sha256'], 'cached JPEG changed')
        row = pipeline.step(poses, old['source_frame'], old['captured_at'], image_path.read_bytes(),
                            old['cloud_input'], old['inference_s'], old['view_predictions'])
        row.update(duplicate_evidence=evidence, suppression_s=fusion_time,
                   inference_time_reused=True, original_pose_count=len(old['poses']))
        result.append(row)
    return result


def suppressed_person_score(rows, original, annotation, meta, criteria):
    """Sparse exact-frame geometry only, not proof of human identity/safety."""
    from audit_paid_vlm_localization import exact_gt
    from score_fall_baseline import match_boxes
    original_by_frame = {r['source_frame']: r for r in original}
    records = []
    for row in rows:
        frame = row['source_frame']
        gt = exact_gt(annotation, frame, meta)
        old = original_by_frame[frame]
        for group in row['duplicate_evidence']['groups']:
            if not group['suppressed']:
                continue
            people = []
            for index in group['indices']:
                pose = old['poses'][index]
                box = [pose['box'][k]*(640 if i%2 == 0 else 400)
                       for i, k in enumerate(('left', 'top', 'right', 'bottom'))]
                matched = match_boxes([g['box'] for g in gt], [box], criteria)
                exact = [g for g, (status, _) in zip(gt, matched) if status == 'matched']
                people.append(dict(index=index, person_id=exact[0]['person_id'] if len(exact) == 1 else None,
                                   role=exact[0]['role'] if len(exact) == 1 else None))
            known = {p['person_id'] for p in people if p['person_id'] is not None}
            status = ('different_people' if len(known) > 1 else 'same_person_on_exact_gt'
                      if len(known) == 1 and all(p['person_id'] is not None for p in people)
                      else 'unverified')
            records.append(dict(source_frame=frame, indices=group['indices'], kept=group['kept'],
                                suppressed=group['suppressed'], people=people, status=status))
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('baseline', 'audit', 'spatial', 'output'):
        parser.add_argument('--'+key, type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve earlier results')
    verify_frozen(args.baseline)
    args.output.mkdir(parents=True, mode=0o700)
    sources = dict(read(args.baseline/'provenance.json')['sources'])
    sources.update({str(args.baseline/name): h for name, h in read(args.baseline/'completed.json')['files'].items()})
    paths = [args.baseline/'completed.json', Path(__file__),
             Path(__file__).with_name('experimental_pose_duplicates.py'),
             REPO/'homecam_agent/test/test_pose_duplicate_comparison.py']
    sources.update({str(p): digest(p) for p in paths})
    save(args.output/'plan.json', dict(cases=CASES, views=ARMS, treatments=['baseline', 'joints'],
        duplicate_config=asdict(DuplicateConfig()),
        rule='after frozen .85 fusion; cross-view overlap + named anatomy; complete unique-view components only',
        representatives='highest actual confidence; full view wins tie; never averaged',
        unchanged=['pose values', 'tracker', 'candidate rules', 'association thresholds',
                   'all Cloud regions', 'Cloud JPEGs/replies', 'timestamps', 'unknown robot motion'],
        new_api_calls=0, new_inference_calls=0, ground_truth_used_only_posthoc=True,
        scope='9 known development scenes; instantaneous cached merge; no scheduler/ROS/latency'))
    metas = {m['case_id']: m for m in read(args.spatial/'media.json')['cases']}
    config = read(REPO/'malbut_agent_server/config/fall_runtime.example.json')
    jobs, inputs, originals = {}, {}, {}
    for model, cases in read(args.audit/'cases.json').items():
        for case in cases:
            cid = case['case_id']
            if cid not in CASES:
                continue
            path = Path(case['result_path']); input_path = path.parent.parent/'inputs'/f'{cid}.input.json'
            record, result = read(input_path), read(path)
            require(result['case_id'] == cid, 'wrong response case')
            if cid in inputs:
                require(record == inputs[cid], 'models used different input')
            inputs[cid] = record; jobs[model, cid] = result
            originals[cid] = frozen_jpegs(record, metas[cid])
    require(len(jobs) == 18 and set(inputs) == set(CASES), 'missing model/case')
    prior = {(r['arm'], r['mode'], r['model'], r['case_id']): r
             for r in read(args.baseline/'comparison.json')}
    rows, old_rows, comparisons, replays = {}, {}, [], {}
    for arm in ARMS:
        print('cached Pose replay', arm, flush=True)
        for cid in CASES:
            old_rows[arm, cid] = read(args.baseline/f'{arm}-{cid}.pose.json')
            for treatment in ('baseline', 'joints'):
                data = replay_poses(old_rows[arm, cid], arm, treatment, args.baseline/'images'/cid)
                rows[arm, treatment, cid] = data
                save(args.output/f'{arm}-{treatment}-{cid}.pose.json', data)
        for treatment, mode, model, cid in itertools.product(
                ('baseline', 'joints'), ('production', 'box_only'), ('gemma', 'gemini'), CASES):
            run = replay_scene if mode == 'production' else replay_box_only
            stem = f'{arm}-{treatment}-{mode}-{model}-{cid}'
            value = run(rows[arm, treatment, cid], originals[cid], inputs[cid], jobs[model, cid],
                        config, args.output/f'{stem}.sqlite')
            summary = outcome(value)
            if treatment == 'baseline':
                require(all(summary[k] == prior[arm, mode, model, cid][k] for k in summary),
                        'baseline control changed')
            save(args.output/f'{stem}.replay.json', value)
            replays[arm, treatment, mode, model, cid] = value
            comparisons.append(dict(arm=arm, treatment=treatment, mode=mode, model=model, case_id=cid, **summary))
    save(args.output/'comparison.json', comparisons)
    # No GT or classification loaded before all duplicate/track/merge decisions above.
    freeze = read(args.spatial/'freeze.json')
    for name, expected in freeze['files'].items():
        p = args.spatial/name; require(digest(p) == expected, 'GT changed'); sources[str(p)] = expected
    bundle = read(args.spatial/'evaluation_labels.json')
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c['label'] for c in bundle['classifications']['cases']}
    metrics, audits, links, detections = [], [], [], {}
    for (arm, treatment, cid), data in rows.items():
        score = detection_score(data, annotations[cid], metas[cid], freeze['match'])
        detections[f'{arm}-{treatment}-{cid}'] = score
        candidates = [c for r in data for c in r['candidate_payload']['candidates']]
        targets = [s for s in score if s['role'] == 'target']
        metrics.append(dict(arm=arm, treatment=treatment, case_id=cid, label=labels[cid], frames=len(data),
            input_boxes=sum(r['original_pose_count'] for r in data), output_boxes=sum(len(r['poses']) for r in data),
            suppressed=sum(r['original_pose_count']-len(r['poses']) for r in data),
            ambiguous_components=sum(g['reason'] == 'ambiguous_component' for r in data
                                     for g in r['duplicate_evidence']['groups']),
            unassigned_frames=sum(bool(r['unassigned']) for r in data), candidate_count=len(candidates),
            association_usable_observations=sum(t['associationUsable'] for r in data for t in r['candidate_payload']['tracks']),
            target_gt_samples=len(targets), target_matches=sum(s['all_status'] == 'matched' for s in targets),
            strong_target_matches=sum(s['strong_status'] == 'matched' for s in targets),
            suppression_median_ms=statistics.median(r['suppression_s']*1000 for r in data)))
        if treatment == 'joints':
            audits.append(dict(arm=arm, case_id=cid, groups=suppressed_person_score(
                data, old_rows[arm, cid], annotations[cid], metas[cid], freeze['match'])))
    for arm, treatment, mode, model in itertools.product(ARMS, ('baseline', 'joints'),
                                                        ('production', 'box_only'), ('gemma', 'gemini')):
        score = score_tracks({cid: replays[arm, treatment, mode, model, cid]['pose_timeline'] for cid in CASES},
                             annotations, metas, freeze['match'])
        save(args.output/f'{arm}-{treatment}-{mode}-{model}.gt-score.json', score)
        for cid in CASES:
            links.append(dict(arm=arm, treatment=treatment, mode=mode, model=model, case_id=cid,
                findings=score_linked_people(replays[arm, treatment, mode, model, cid], score[cid])))
    save(args.output/'metrics.json', metrics); save(args.output/'suppressed-person-score.json', audits)
    save(args.output/'linked-person-score.json', links); save(args.output/'detection-score.json', detections)
    require(all(digest(p) == h for p, h in sources.items()), 'frozen source changed')
    save(args.output/'provenance.json', dict(sources=sources, new_api_calls=0, new_inference_calls=0,
        production_changed=False, baseline_controls_reproduced=True, gt_used_for_routing=False))
    save(args.output/'completed.json', dict(files={str(p.relative_to(args.output)): digest(p)
        for p in sorted(args.output.rglob('*')) if p.is_file()}))
    for arm in ARMS:
        mm = [m for m in metrics if m['arm'] == arm and m['treatment'] == 'joints']
        print(arm, 'suppressed', sum(m['suppressed'] for m in mm),
              'unassigned_frames', sum(m['unassigned_frames'] for m in mm), flush=True)


if __name__ == '__main__':
    main()
