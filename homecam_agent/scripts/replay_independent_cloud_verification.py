#!/usr/bin/env python3
"""Replay frozen Pose/Cloud inputs, scoring verification separately from identity.

No models, API calls, inference or robot devices. The existing replay injects
saved responses at the crosscheck boundary, not through the live scheduler.
"""

import argparse
from collections import Counter
import json
from pathlib import Path

from replay_reviewed_pose_cloud import (
    REPO, digest, frozen_jpegs, read, replay_scene, require, save,
)
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'audit', 'spatial', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'preserve previous results')
    # Frozen result/input files must match. Old implementation source hashes
    # intentionally differ: this run tests the changed verification workflow.
    completed = read(args.baseline / 'completed.json')
    for name, expected in completed['files'].items():
        require(digest(args.baseline / name) == expected, 'baseline result changed: ' + name)
    previous_sources = read(args.baseline / 'provenance.json')['sources']
    cases = read(args.baseline / 'plan.json')['cases']
    audit = read(args.audit / 'cases.json')
    metas = {c['case_id']: c for c in read(args.spatial / 'media.json')['cases']}
    config_path = REPO / 'malbut_agent_server/config/fall_runtime.example.json'
    config = read(config_path)
    sources = {config_path: digest(config_path), Path(__file__): digest(__file__)}
    for folder in ('malbut_agent_server/malbut_agent_server',
                   'malbut_fall_coordinator/malbut_fall_coordinator'):
        sources.update({p: digest(p) for p in (REPO / folder).rglob('*.py')})
    helper = Path(__file__).with_name('replay_reviewed_pose_cloud.py')
    sources[helper] = digest(helper)
    args.output.mkdir(mode=0o700, parents=True)
    summary = []
    for model, entries in audit.items():
        for entry in entries:
            cid = entry['case_id']
            if cid not in cases:
                continue
            result_path = Path(entry['result_path'])
            input_path = result_path.parent.parent / 'inputs' / f'{cid}.input.json'
            pose_path = args.baseline / f'{cid}.pose.json'
            for path in (result_path, input_path, config_path, args.spatial / 'media.json'):
                require(str(path) in previous_sources and digest(path) == previous_sources[str(path)],
                        'original replay input changed: ' + str(path))
                sources[path] = digest(path)
            sources[pose_path] = digest(pose_path)
            record, result = read(input_path), read(result_path)
            replay = replay_scene(
                read(pose_path), frozen_jpegs(record, metas[cid]), record, result, config,
                args.output / f'{model}-{cid}.sqlite')
            coordinator = FallConfirmationCoordinator(runtime_id='offline')
            for event in replay['events']:
                coordinator.receive(json.dumps(dict(event, boot_id='offline', runtime_id='offline')))
            discoveries = [e['discovery'] for e in replay['events'] if e['discovery']]
            row = dict(
                model=model, case_id=cid, reply_usable=replay['reply_usable'],
                association_cases=dict(Counter(d.get('association_case', 'unknown')
                                               for d in discoveries)),
                person_linked=any(d['incident_id'] and d['subject_key'] for d in discoveries),
                scene_questions=sum(r.subject_key is None for r in coordinator.requests.values()),
                subject_questions=sum(r.subject_key is not None for r in coordinator.requests.values()),
                repeat_questions=sum(e['kind'] == 'question_requested' for e in replay['repeat_events']),
                repeat_incidents_unchanged=replay['repeat_incidents_unchanged'],
                journal_reopen_verified=replay['journal_reopen_verified'], new_api_calls=0)
            summary.append(row)
            save(args.output / f'{model}-{cid}.replay.json', replay)
    require(len(summary) == len(audit) * len(cases), 'missing model/case')
    require(all(digest(p) == expected for p, expected in sources.items()), 'source changed during replay')
    save(args.output / 'summary.json', summary)
    save(args.output / 'provenance.json', dict(
        sources={str(p): h for p, h in sources.items()}, new_api_calls=0,
        scope='cached crosscheck boundary plus fall coordinator routing; no scheduler, ROS, inference or GT'))
    save(args.output / 'completed.json', dict(
        files={p.name: digest(p) for p in args.output.iterdir() if p.is_file()}))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
