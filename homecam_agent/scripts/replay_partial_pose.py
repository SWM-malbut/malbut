#!/usr/bin/env python3
"""Offline ablations on frozen s-letterbox poses. No network or model calls."""
import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time

from compare_pose_detectors import load_reference, score
from experimental_partial_pose import PartialConfig, PartialPoseExperiment
from experimental_pose_disagreement import PoseDisagreementConfig
from experimental_pose_retention import PoseRetentionConfig
from experimental_request_coalescing import CoalescingConfig
from replay_fall_baseline import sha, write_json
from replay_pose_stability import replay_rows as disagreement_rows
from replay_pose_retention import replay_rows as retention_rows, verify_overlay
from replay_request_coalescing import replay, source_aligned
from review_fall_annotations import require
from replay_roi_pose import same_json


ARMS = dict(control=None, partial=PartialConfig(observed_descent=False),
            descent=PartialConfig(partial_upper=False), combined=PartialConfig(),
            brief_descent=PartialConfig(
                partial_upper=False, descent_samples=2, descent_span_sec=.15),
            partial_brief=PartialConfig(descent_samples=2, descent_span_sec=.15))


def read(path):
    return json.loads(path.read_text())


def jsonl(path):
    return [json.loads(s) for s in path.read_text().splitlines()]


def run(args):
    require(not args.output.exists(), 'output exists')
    overlay = verify_overlay(args.spatial_final, args.parent_frozen)
    marker = read(args.source/'completed.json')
    require(sha(args.source/'summary.json') == marker['summary_sha256'], 'changed source')
    arm = args.source/'s_letterbox'
    require(sha(arm/'completed.json') == marker['arms']['s_letterbox'], 'changed arm')
    for name, digest in read(arm/'completed.json')['files'].items():
        require(Path(name).name == name and sha(arm/name) == digest, 'changed data')
    source_plan = read(args.source/'plan.json')
    for p, digest in source_plan['source_sha256'].items():
        require(sha(Path(p)) == digest, 'changed frozen source '+p)
    _, meta, _ = load_reference(Path(source_plan['source_reference']))
    raw, baseline = jsonl(arm/'raw-frames.jsonl'), jsonl(arm/'frames.jsonl')
    for r in raw:
        r['fall_analysis'] = copy.deepcopy(r['upstream_analysis'])
        r['verification_updates'] = copy.deepcopy(r['upstream_verification_updates'])
    metas = {m['case_id']: m for m in read(args.spatial_final/'media.json')['cases']}
    require(len(metas) == 84 and len(raw) == 2224, 'wrong evaluation coverage')
    stable = meta['fall_config']['upstream']['upstream']
    a = list(disagreement_rows(raw, metas, PoseDisagreementConfig(**stable['pose_stability']),
                               True, 'disagreement_request'))
    retained = list(retention_rows(a, metas, PoseRetentionConfig(
        **meta['fall_config']['upstream']['retention'])))
    args.output.mkdir(mode=0o700)
    own_files = [
        Path(__file__), Path(__file__).with_name('experimental_partial_pose.py'),
        Path(__file__).parents[1]/'evaluations/synthetic_fall_v1/S_PARTIAL_POSE_PROTOCOL.md']
    sources = dict(source_plan['source_sha256'], **{str(p): sha(p) for p in own_files})
    snapshot = args.output/'code-snapshot'
    snapshot.mkdir(mode=0o700)
    for source in own_files:
        shutil.copy2(source, snapshot/source.name)
    write_json(args.output/'plan.json', dict(
        created_utc=datetime.now(timezone.utc).isoformat(),
        source=str(args.source), source_marker_sha256=sha(args.source/'completed.json'),
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
        source_sha256=sources, arms={n: asdict(c) if c else None for n, c in ARMS.items()},
        model_calls=0, provider_calls=0,
        scope='cached real poses; development replay, not holdout'))
    metrics, all_cases, completions = {}, {}, {}
    for name, config in ARMS.items():
        folder = args.output/name
        folder.mkdir(mode=0o700)
        rows = copy.deepcopy(retained)
        engine, current, times, extra_count = None, None, [], 0
        for r in rows:
            cid = r['case_id']
            if cid != current:
                engine, current = PartialPoseExperiment(config) if config else None, cid
            start = time.perf_counter()
            size = metas[cid]['width'], metas[cid]['height']
            extras, details = engine.update(r, image_size=size) if engine else ([], [])
            times.append((time.perf_counter()-start)*1000)
            r['partial_experiment_candidates'] = extras
            r['partial_experiment_details'] = details
            r['retention_raw_candidates'].extend(copy.deepcopy(extras))
            extra_count += len(extras)
        dispatches = []
        routing = replay(rows, metas, CoalescingConfig(**meta['fall_config']['coalescing']),
                         dispatches.append)
        final = source_aligned(rows, dispatches, routing)
        require(all(a['observations'] == b['observations'] for a, b in zip(final, baseline)),
                'changed poses/IDs')
        if config is None:
            require(same_json(dispatches, jsonl(arm/'dispatches.jsonl')),
                    'control dispatch changed')
            after_candidates = [r['fall_analysis']['candidates'] for r in final]
            before_candidates = [r['fall_analysis']['candidates'] for r in baseline]
            require(same_json(after_candidates, before_candidates), 'control candidates changed')
        metric, cases, boxes = score(final, dispatches, baseline,
                                     args.spatial_final, overlay['match'])
        metric.update(extra_evidence_count=extra_count, extra_rule_mean_ms=sum(times)/len(times))
        for filename, items in [('frames.jsonl', final), ('dispatches.jsonl', dispatches),
                                ('routing.jsonl', routing)]:
            with (folder/filename).open('x') as f:
                for item in items:
                    f.write(json.dumps(item, ensure_ascii=False, allow_nan=False)+'\n')
        for filename, obj in [('cases.json', cases), ('box-matches.json', boxes),
                              ('metrics.json', metric)]:
            write_json(folder/filename, obj)
        write_json(folder/'completed.json', dict(files={p.name: sha(p) for p in folder.iterdir()}))
        completions[name] = sha(folder/'completed.json')
        metrics[name], all_cases[name] = metric, cases
        print(name, json.dumps(dict(groups=metric['candidate_groups'], requests=metric['requests'],
              new=metric['new_candidate_cases'], lost=metric['lost_candidate_cases'],
              extras=extra_count)), flush=True)
    for p, digest in sources.items():
        require(sha(Path(p)) == digest, 'source changed during replay')
    verify_overlay(args.spatial_final, args.parent_frozen)
    write_json(args.output/'summary.json', dict(
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'), metrics=metrics,
        model_calls=0, provider_calls=0, control_dispatch_exact_match=True))
    write_json(args.output/'completed.json', dict(summary_sha256=sha(args.output/'summary.json'),
                                                  arms=completions))
    print('COMPLETED', args.output, flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'spatial-final', 'parent-frozen', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    run(p.parse_args())
