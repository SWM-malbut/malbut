#!/usr/bin/env python3
"""Offline anatomy gate ablation with cached SAM and actual incident attachment.

Strong confidence, tracked state, ambiguity, continuity and current-observation
gates stay unchanged. Location-only observations are UNKNOWN, never CLEAR.
Inference and incident replay finish before exact-frame human boxes are read.
"""

import argparse
import json
from pathlib import Path

from experimental_box_association import box_subject_frame
from replay_deferred_discovery_links import replay
from replay_reviewed_pose_cloud import REPO, digest, read, require, save


def audit_links(result, rows, tracking, annotation, meta, criteria):
    from audit_paid_vlm_localization import exact_gt
    from evaluate_visual_person_linking import identify

    samples = {s['captured_at']: s for s in tracking['samples']}
    lookup = {r['captured_at']: r for r in rows}
    output = []
    for event in result['link_events']:
        if event['kind'] != 'cloud_discovery_linked':
            continue
        discovery = event['discovery']
        when = discovery['association_link']['confirmed_at']
        sample, row = samples[when], lookup[when]
        candidates = [t for t in row['candidate_payload']['tracks']
                      if discovery['subject_key'] == 'pose:0:' + t['targetTrackId']]
        require(len(candidates) == 1, 'attached ID absent from observed Pose')
        gt = exact_gt(annotation, sample['source_frame'], meta)
        target = annotation['target_person_id']
        visual_person = identify(sample['box'], gt, criteria) if gt else None
        pose_person = identify(candidates[0]['box'], gt, criteria) if gt else None
        target_reviewed = bool(gt and target in {g['person_id'] for g in gt})
        output.append(dict(
            source_frame=sample['source_frame'], subject_key=discovery['subject_key'],
            target=target, visual_person=visual_person, pose_person=pose_person,
            exact_review_available=target_reviewed,
            correct_at_reviewed_instant=bool(target_reviewed and visual_person == pose_person == target),
            wrong_person_at_reviewed_instant=bool(target_reviewed and (
                (visual_person is not None and visual_person != target)
                or (pose_person is not None and pose_person != target)))))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cached', 'baseline_replay', 'spatial', 'output'):
        parser.add_argument('--' + name.replace('_', '-'), type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve old results')
    plan = read(args.cached / 'plan.json')
    done = read(args.cached / 'run/completed.json')
    require(done['complete'], 'inference incomplete')
    sources = dict(plan['sources'])
    for name in ('plan.json', 'run/completed.json'):
        sources[str(args.cached / name)] = digest(args.cached / name)
    config_path = REPO / 'malbut_agent_server/config/fall_runtime.example.json'
    config = read(config_path)
    sources[str(config_path)] = digest(config_path)
    code = {str(p): digest(p) for p in (REPO / 'malbut_agent_server/malbut_agent_server').rglob('*.py')}
    for name in ('replay_deferred_discovery_links.py', 'experimental_box_association.py',
                 'replay_deferred_location_ablation.py'):
        p = Path(__file__).with_name(name)
        code[str(p)] = digest(p)
    for p, expected in sources.items():
        require(digest(p) == expected, 'source changed: ' + p)
    args.output.mkdir(mode=0o700, parents=True)
    save(args.output / 'plan.json', dict(
        baseline=str(args.cached), code=code, sources=sources,
        arms=['production_gate', 'location_only'], strong_score=.45,
        change='separate location quality from fall-anatomy quality; UNKNOWN only',
        new_api_calls=0, new_inference=0, production_changed=False,
        live_latency_simulated=False, gt_for_routing=False))
    comparisons = []
    for entry in done['results']:
        cid = entry['case_id']
        folder = args.cached / 'run' / cid
        for name in ('pose', 'result'):
            path = folder / f'{name}.json'
            require(digest(path) == entry[f'{name}_sha256'], 'cached result changed')
            sources[str(path)] = digest(path)
        prior_path = args.baseline_replay / cid / 'result.json'
        prior = read(prior_path)
        sources[str(prior_path)] = digest(prior_path)
        summaries = {}
        for arm, factory in [('production_gate', None), ('location_only', box_subject_frame)]:
            destination = args.output / arm / cid
            destination.mkdir(mode=0o700, parents=True)
            summaries[arm] = replay(plan['cases'][cid], folder, config, destination,
                                    subject_frame_factory=factory)
        require(all(summaries['production_gate'][k] == prior[k]
                    for k in summaries['production_gate']), 'production control changed')
        comparisons.append(dict(case_id=cid, **summaries))
    save(args.output / 'comparison.json', comparisons)
    # Exact approved boxes are loaded only after both arms are frozen.
    freeze = read(args.spatial / 'freeze.json')
    for name, expected in freeze['files'].items():
        path = args.spatial / name
        require(digest(path) == expected, 'review changed')
        sources[str(path)] = expected
    annotations = {c['case_id']: c for c in read(
        args.spatial / 'evaluation_labels.json')['annotations']['cases']}
    audits = []
    for entry in comparisons:
        cid = entry['case_id']
        folder = args.cached / 'run' / cid
        for arm in ('production_gate', 'location_only'):
            result = read(args.output / arm / cid / 'result.json')
            audits.append(dict(case_id=cid, arm=arm, links=audit_links(
                result, read(folder / 'pose.json'), read(folder / 'result.json'),
                annotations[cid], plan['cases'][cid]['meta'], freeze['match'])))
    save(args.output / 'review-audit.json', dict(
        cases=audits, scope='exact reviewed attachment instant, not complete identity trajectory'))
    for p, expected in {**sources, **code}.items():
        require(digest(p) == expected, 'source changed during ablation: ' + p)
    save(args.output / 'provenance.json', dict(sources=sources, code=code,
        baseline_control_reproduced=True, gt_for_routing=False, new_api_calls=0))
    save(args.output / 'completed.json', dict(complete=True,
        files={str(p.relative_to(args.output)): digest(p)
               for p in args.output.rglob('*') if p.is_file()}))
    print(json.dumps(comparisons, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
