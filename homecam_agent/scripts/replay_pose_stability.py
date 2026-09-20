#!/usr/bin/env python3
"""Isolated cached-Pose stability comparison. No model, network, ROS or deployment."""
import argparse
from collections import Counter
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from experimental_pose_stability import PoseStabilityConfig, PoseStabilityExperiment
from experimental_pose_disagreement import PoseDisagreementConfig, PoseDisagreementExperiment
from experimental_request_dedup import RequestDedupConfig, RequestDedupExperiment
from replay_fall_baseline import AGENT, SOURCE, sha, verify_freeze, write_json
from replay_request_dedup import payloads
from review_fall_annotations import require
from score_fall_baseline import events, validate_rows


def read_run(directory, frozen):
    complete = json.loads((directory / 'completed.json').read_text())
    for name, key in [('run.json', 'run_sha256'), ('frames.jsonl', 'frames_sha256')]:
        require(sha(directory / name) == complete[key], 'changed input: ' + name)
    meta = json.loads((directory / 'run.json').read_text())
    require(meta['freeze_sha256'] == sha(frozen / 'freeze.json'), 'wrong freeze')
    require(meta['media_sha256'] == sha(frozen / 'media.json'), 'wrong media')
    rows = [json.loads(s) for s in (directory / 'frames.jsonl').read_text().splitlines()]
    validate_rows(rows, json.loads((frozen / 'media.json').read_text()), meta)
    return complete, meta, rows


def semantic(row):
    analysis = {k: v for k, v in row['fall_analysis'].items()
                if k not in {'configSha256', 'algorithmVersion'}}
    return dict(case_id=row['case_id'], frame_index=row['frame_index'],
                timestamp_s=row['timestamp_s'], observations=row['observations'],
                analysis=analysis, updates=row['verification_updates'],
                routes=row['verification_routes'])


def candidate_timing(annotation, stamp, fps):
    """Old labels have entry_state; new approved intervals need no invented state."""
    if annotation.get('entry_state') == 'already_down':
        basis, origin = 'first_down', annotation.get('first_down_frames')
    elif annotation.get('onset_frames') is not None:
        basis, origin = 'onset', annotation['onset_frames']
    else:
        basis, origin = 'first_down', annotation.get('first_down_frames')
    if origin is None:
        return dict(basis='unknown', delay_s=None, position='unknown')
    require(len(origin) == 2 and 0 <= origin[0] <= origin[1], 'invalid annotated interval')
    low, high = stamp - origin[1] / fps, stamp - origin[0] / fps
    return dict(basis=basis, delay_s=[low, high],
                position='early' if high < 0 else 'boundary_uncertain' if low < 0 else 'after')


def replay_rows(rows, metas, config, enabled, strategy='stability'):
    """No annotations accepted here; retain every upstream candidate payload."""
    current = None
    for row in rows:
        cid = row['case_id']
        if cid != current:
            component = (PoseDisagreementExperiment if strategy == 'disagreement_request'
                         else PoseStabilityExperiment)
            stability, dedup = component(config), RequestDedupExperiment()
            current = cid
        size = metas[cid]['width'], metas[cid]['height']
        begin = time.perf_counter()
        extra, details = stability.update(row, image_size=size) if enabled else ([], [])
        expanded = copy.deepcopy(row)
        expanded['fall_analysis']['candidates'].extend(extra)
        requests, updates, dedup_details = dedup.update(expanded, image_size=size)
        require(payloads(requests, updates) == payloads(
            expanded['fall_analysis']['candidates'], expanded['verification_updates']),
            'candidate evidence lost or rewritten')
        out = copy.deepcopy(row)
        out.update(stability_ms=(time.perf_counter() - begin) * 1000,
                   stability_details=details, stability_raw_candidates=extra,
                   stability_upstream_analysis=copy.deepcopy(row['fall_analysis']),
                   stability_upstream_updates=copy.deepcopy(row['verification_updates']),
                   request_dedup_details=dedup_details,
                   verification_updates=updates, verification_routes=dict(dedup.routes))
        out['fall_analysis']['candidates'] = requests
        yield out


def compare(before, after, frozen):
    # GT enters only AFTER all candidate decisions have been produced.
    bundle = json.loads((frozen / 'evaluation_labels.json').read_text())
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    metas = {c['case_id']: c for c in json.loads((frozen / 'media.json').read_text())['cases']}
    result, first = {}, {}
    for name, rows in [('before', before), ('after', after)]:
        found = events(rows)
        first[name] = {}
        for e in found:
            first[name].setdefault(e['case_id'], e)
        groups = {}
        for label in sorted({c['label'] for c in labels.values()}):
            ids = sorted(cid for cid, c in labels.items() if c['label'] == label)
            groups[label] = dict(total=len(ids), with_candidate=sum(cid in first[name] for cid in ids),
                                 missing=[cid for cid in ids if cid not in first[name]])
        result[name] = dict(groups=groups, requests=len(found))
    cases = []
    for cid, label in sorted(labels.items()):
        def summary(name):
            e = first[name].get(cid)
            if not e:
                return None
            return dict(frame_index=e['frame_index'], timestamp_s=e['timestamp_s'],
                        evidence_timestamp_s=e.get('evidence_timestamp_s', e['timestamp_s']),
                        target_track_id=e['candidate']['targetTrackId'],
                        candidate_kind=e['candidate']['candidateKind'],
                        reasons=e['candidate']['reasons'],
                        timing=candidate_timing(annotations[cid], e['timestamp_s'], metas[cid]['fps']))
        a, b = summary('before'), summary('after')
        cases.append(dict(case_id=cid, label=label['label'],
                          review_case_id=label.get('review_case_id'),
                          original_source=label.get('original_source_path'),
                          before=a, after=b, changed=a != b,
                          association='not_automatically_inferred_from_candidate_or_sparse_GT'))
    result.update(cases=cases, changed_cases=[c['case_id'] for c in cases if c['changed']],
                  metric='candidate_anywhere_in_video_not_target_verified_recall',
                  timing_scope='video-source first request time, not VLM or wall-clock latency',
                  dataset_usage='previously_inspected_synthetic_development_only')
    return result


def run(args):
    verify_freeze(args.frozen)
    require(not args.output.exists(), 'output exists; no overwrite')
    complete, meta, rows = read_run(args.upstream, args.frozen)
    ref_complete, ref_meta, reference = read_run(args.reference, args.frozen)
    require(meta['fall_config']['mode'] == 'pose-loss', 'requires original pose-loss input')
    require(ref_meta.get('upstream_frames_sha256') == complete['frames_sha256'],
            'reference is not derived from this upstream')
    require(meta['robot_motion'] == 'unknown' and meta['depth'] is None, 'requires RGB-only input')
    required = {SOURCE / name: digest for name, digest in meta['source_sha256'].items()}
    required.update({Path(p): h for p, h in meta['experiment_source_sha256'].items()})
    for directory, marker in [(args.upstream, complete), (args.reference, ref_complete)]:
        required.update({directory / 'frames.jsonl': marker['frames_sha256'],
                         directory / 'run.json': marker['run_sha256']})
    for p, digest in required.items():
        require(sha(p) == digest, 'source/input changed: ' + str(p))
    sources = {str(p): sha(p) for p in [Path(__file__),
        Path(__file__).with_name('experimental_pose_stability.py'),
        Path(__file__).with_name('experimental_pose_disagreement.py'),
        Path(__file__).with_name('experimental_request_dedup.py'),
        Path(__file__).with_name('replay_request_dedup.py'),
        AGENT / 'evaluations/synthetic_fall_v1/POSE_DISAGREEMENT_PROTOCOL.md',
        AGENT / 'evaluations/synthetic_fall_v1/POSE_STABILITY_PROTOCOL.md']}
    strategy = 'disagreement_request' if args.disagreement_request else 'stability'
    config = PoseDisagreementConfig() if args.disagreement_request else PoseStabilityConfig()
    settings = dict(upstream=meta['fall_config'], pose_stability=asdict(config),
                    strategy=strategy, geometry=asdict(PoseStabilityConfig()),
                    stability_enabled=not args.control, request_dedup=asdict(RequestDedupConfig()))
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    outmeta = dict(meta, created_utc=datetime.now(timezone.utc).isoformat(),
                   upstream_frames_sha256=complete['frames_sha256'],
                   reference_frames_sha256=ref_complete['frames_sha256'],
                   stability_source_sha256=sources, fall_config=settings,
                   fall_config_sha256=digest, scope='offline cached Pose only; not deployed',
                   timing_note='stability_ms includes add-on and dedup; old timing fields inherited')
    metas = {c['case_id']: c for c in json.loads((args.frozen / 'media.json').read_text())['cases']}
    args.output.mkdir(mode=0o700)
    write_json(args.output / 'run.json', outmeta)
    output, counts = [], Counter()
    with (args.output / 'frames.jsonl').open('x') as stream:
        for row in replay_rows(rows, metas, config, not args.control, strategy):
            row['fall_analysis'].update(configSha256=digest,
                                        algorithmVersion='pose-loss-stability-dedup-experiment-v1')
            stream.write(json.dumps(row, allow_nan=False) + '\n')
            output.append(row)
            counts.update(frames=1, new_candidates=len(row['stability_raw_candidates']),
                          requests=len(row['fall_analysis']['candidates']),
                          evidence_updates=len(row['verification_updates']))
        stream.flush()
        os.fsync(stream.fileno())
    validate_rows(output, dict(cases=list(metas.values())), outmeta)
    evidence = copy.deepcopy(output)
    for row in evidence:
        row['fall_analysis']['candidates'].extend(u['evidence'] for u in row['verification_updates'])
    events(evidence)  # Validate all evidence references, including suppressed requests.
    if args.control:
        require(len(reference) == len(output), 'control coverage changed')
        require(all(semantic(a) == semantic(b) for a, b in zip(reference, output)),
                'control differs from baseline reference')
    report = compare(reference, output, args.frozen)
    write_json(args.output / 'comparison.json', report)
    for p, expected in {**required, **{Path(k): v for k, v in sources.items()}}.items():
        require(sha(p) == expected, 'source/input changed during run: ' + str(p))
    verify_freeze(args.frozen)
    write_json(args.output / 'completed.json', dict(
        counts, cases=len(metas), control_parity=args.control,
        frames_sha256=sha(args.output / 'frames.jsonl'),
        run_sha256=sha(args.output / 'run.json'), comparison_sha256=sha(args.output / 'comparison.json')))
    print(json.dumps(dict(counts), sort_keys=True))


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'upstream', 'reference', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--control', action='store_true')
    p.add_argument('--disagreement-request', action='store_true',
                   help='separate weak-evidence request experiment, not combined with stability')
    run(p.parse_args())


if __name__ == '__main__':
    main()
