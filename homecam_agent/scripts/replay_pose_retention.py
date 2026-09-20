#!/usr/bin/env python3
"""Four-arm cached-Pose comparison using an approved spatial-only overlay."""
import argparse
from collections import Counter
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time

from experimental_pose_retention import PoseRetentionConfig, PoseRetentionExperiment
from experimental_request_dedup import RequestDedupExperiment
from replay_fall_baseline import AGENT, sha, verify_freeze, write_json
from replay_pose_stability import candidate_timing, compare, read_run, semantic
from replay_request_dedup import payloads
from review_fall_annotations import require
from score_fall_baseline import anchors, events, validate_rows


MODES = {
    'control': PoseRetentionConfig(reobserved_low=False, transition_followup=False),
    'reobserved_low': PoseRetentionConfig(reobserved_low=True, transition_followup=False),
    'transition_followup': PoseRetentionConfig(reobserved_low=False, transition_followup=True),
    'combined': PoseRetentionConfig(),
}


def verify_overlay(stage, parent):
    verify_freeze(parent)
    overlay = json.loads((stage/'freeze.json').read_text())
    require(overlay['parent_freeze_sha256'] == sha(parent/'freeze.json'), 'wrong overlay parent')
    require(overlay['parent_labels_sha256'] == sha(parent/'evaluation_labels.json'),
            'wrong parent labels')
    for name, digest in overlay['files'].items():
        require(Path(name).name == name and sha(stage/name) == digest,
                'changed final overlay: '+name)
    require(sha(stage/'media.json') == sha(parent/'media.json'),
            'media changed in spatial overlay')
    return overlay


def replay_rows(rows, metas, config):
    """Only Pose output and media dimensions enter decisions; no GT or case lists."""
    current = None
    for row in rows:
        cid = row['case_id']
        if current != cid:
            component = PoseRetentionExperiment(config)
            dedup, current = RequestDedupExperiment(), cid
        size = (metas[cid]['width'], metas[cid]['height'])
        start = time.perf_counter()
        extra, details = component.update(row, image_size=size)
        expanded = copy.deepcopy(row)
        expanded['fall_analysis']['candidates'].extend(extra)
        requests, updates, dedup_details = dedup.update(expanded, image_size=size)
        require(payloads(requests, updates) == payloads(
            expanded['fall_analysis']['candidates'], expanded['verification_updates']),
            'upstream evidence lost or rewritten')
        out = copy.deepcopy(row)
        out.update(retention_ms=(time.perf_counter()-start)*1000,
                   retention_details=details, retention_raw_candidates=extra,
                   retention_upstream_analysis=copy.deepcopy(row['fall_analysis']),
                   retention_upstream_updates=copy.deepcopy(row['verification_updates']),
                   request_dedup_details=dedup_details, verification_updates=updates,
                   verification_routes=dict(dedup.routes))
        out['fall_analysis']['candidates'] = requests
        yield out


def timing_stats(values):
    values = sorted(values)
    return dict(n=len(values), median=statistics.median(values),
                p95=values[math.ceil(.95*len(values))-1], maximum=values[-1]) if values else None


def additional_evidence(output, stage, overlay):
    # Scoring starts after all decisions for this arm are complete.
    bundle = json.loads((stage/'evaluation_labels.json').read_text())
    ann = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    metas = {c['case_id']: c for c in json.loads((stage/'media.json').read_text())['cases']}
    raw = copy.deepcopy(output)
    for r in raw:
        r['fall_analysis']['candidates'] = r['retention_raw_candidates']
    result, exact_by_case = [], {}
    for event in events(raw):
        cid, candidate = event['case_id'], event['candidate']
        if cid not in exact_by_case:
            exact_by_case[cid] = anchors(ann[cid], [r for r in raw if r['case_id'] == cid],
                                         metas[cid], overlay['match'])
        exact = exact_by_case[cid]
        target = [a for a in exact if a['frame_index'] == event['frame_index']
                  and a['person_id'] == ann[cid].get('target_person_id')]
        matched = any(a['status'] == 'matched' and a['track_id'] == candidate['targetTrackId']
                      for a in target)
        result.append(dict(case_id=cid, review_case_id=labels[cid].get('review_case_id'),
                           original_source=labels[cid].get('original_source_path'),
                           label=labels[cid]['label'],
                           frame_index=event['frame_index'], timestamp_s=event['timestamp_s'],
                           candidate=candidate, observation=event['observation'],
                           exact_frame_target_match=matched if target else None,
                           target_association_scope='only this reviewed frame; no ID propagation',
                           timing=candidate_timing(
                               ann[cid], event['timestamp_s'], metas[cid]['fps'])))
    return result


def run(args):
    require(not args.output.exists(), 'output exists; no overwrite')
    overlay = verify_overlay(args.spatial_final, args.parent_frozen)
    completed, meta, rows = read_run(args.upstream, args.parent_frozen)
    require(meta['robot_motion'] == 'unknown' and meta['depth'] is None,
            'requires RGB-only unknown motion')
    sources = json.loads(args.source_lock.read_text())
    for name, digest in sources.items():
        require(sha(Path(name)) == digest, 'changed upstream source: '+name)
    media = json.loads((args.spatial_final/'media.json').read_text())
    metas = {c['case_id']: c for c in media['cases']}
    required = {Path(k): v for k, v in sources.items()}
    for directory, marker in [(args.upstream, completed)]:
        required.update({directory/'frames.jsonl': marker['frames_sha256'],
                         directory/'run.json': marker['run_sha256']})
    required.update({args.source_lock: sha(args.source_lock),
                     args.spatial_final/'freeze.json': sha(args.spatial_final/'freeze.json')})
    experiment_paths = [
        Path(__file__),
        Path(__file__).with_name('experimental_pose_retention.py'),
        AGENT/'evaluations/synthetic_fall_v1/POSE_RETENTION_PROTOCOL.md']
    experiment_sources = {str(p): sha(p) for p in experiment_paths}
    args.output.mkdir(mode=0o700)
    summary = dict(created_utc=datetime.now(timezone.utc).isoformat(),
                   scope='offline cached Pose development comparison; not deployed',
                   videos=len(metas), sampled_frames=len(rows), vlm_calls=0, paid_calls=0,
                   model_inference_calls=0, observations_unchanged=True,
                   spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
                   upstream_frames_sha256=completed['frames_sha256'],
                   source_sha256=experiment_sources, arms={})
    write_json(args.output/'experiment-plan.json', dict(
        modes={k: asdict(v) for k, v in MODES.items()},
        **{k: v for k, v in summary.items() if k != 'arms'}))
    for mode, config in MODES.items():
        folder = args.output/mode
        folder.mkdir()
        settings = dict(upstream=meta['fall_config'], retention=asdict(config), mode=mode)
        digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        outmeta = dict(meta, fall_config=settings, fall_config_sha256=digest,
                       scope=summary['scope'], retention_source_sha256=experiment_sources,
                       spatial_freeze_sha256=summary['spatial_freeze_sha256'],
                       upstream_frames_sha256=completed['frames_sha256'],
                       timing_note='retention_ms: add-on/copy/dedup CPU only; '
                       'other timings inherited')
        write_json(folder/'run.json', outmeta)
        output = []
        with (folder/'frames.jsonl').open('x') as stream:
            for before, out in zip(rows, replay_rows(rows, metas, config)):
                require(out['observations'] == before['observations'], 'Pose observations changed')
                require(out['baseline_analysis'] == before['baseline_analysis'],
                        'baseline analysis changed')
                out['fall_analysis'].update(
                    configSha256=digest, algorithmVersion='pose-retention-experiment-v1')
                stream.write(json.dumps(out, allow_nan=False)+'\n')
                output.append(out)
            stream.flush()
            os.fsync(stream.fileno())
        require(len(output) == len(rows), 'coverage changed')
        validate_rows(output, media, outmeta)
        evidence = copy.deepcopy(output)
        for r in evidence:
            r['fall_analysis']['candidates'].extend(
                u['evidence'] for u in r['verification_updates'])
        events(evidence)
        if mode == 'control':
            require(all(semantic(a) == semantic(b) for a, b in zip(rows, output)),
                    'control differs from upstream')
        comparison = compare(rows, output, args.spatial_final)
        extra = additional_evidence(output, args.spatial_final, overlay)
        write_json(folder/'comparison.json', comparison)
        write_json(folder/'extra-events.json', extra)
        class_by_case = {c['case_id']: c['label'] for c in comparison['cases']}
        request_counts = Counter(class_by_case[e['case_id']] for e in events(output))
        arm = dict(groups=comparison['after']['groups'], requests=comparison['after']['requests'],
                   requests_by_label=dict(request_counts),
                   added_evidence=len(extra), changed_cases=comparison['changed_cases'],
                   new_candidate_cases=[c['case_id'] for c in comparison['cases']
                                        if c['before'] is None and c['after'] is not None],
                   lost_candidate_cases=[c['case_id'] for c in comparison['cases']
                                         if c['before'] is not None and c['after'] is None],
                   overhead_ms=timing_stats([r['retention_ms'] for r in output]),
                   control_parity=True if mode == 'control' else None,
                   timing_scope='clip first-request source time; '
                   'not verified target latency or wall-clock VLM time')
        require(not arm['lost_candidate_cases'], 'add-on removed existing candidate coverage')
        summary['arms'][mode] = arm
        write_json(folder/'completed.json', dict(frames_sha256=sha(folder/'frames.jsonl'),
                   run_sha256=sha(folder/'run.json'),
                   comparison_sha256=sha(folder/'comparison.json'),
                   extra_events_sha256=sha(folder/'extra-events.json'), observations_unchanged=True,
                   preserved_candidate_payloads=True, **arm))
        print(json.dumps(dict(mode=mode, **arm), ensure_ascii=False), flush=True)
    verify_overlay(args.spatial_final, args.parent_frozen)
    all_sources = {**required, **{Path(k): v for k, v in experiment_sources.items()}}
    for path, expected in all_sources.items():
        require(sha(path) == expected, 'source/input changed during run: '+str(path))
    write_json(args.output/'summary.json', summary)
    write_json(args.output/'completed.json', dict(summary_sha256=sha(args.output/'summary.json'),
               experiment_plan_sha256=sha(args.output/'experiment-plan.json'),
               inputs_unchanged=True,
               arms={m: sha(args.output/m/'completed.json') for m in MODES}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('upstream', 'spatial-final', 'parent-frozen', 'source-lock', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
