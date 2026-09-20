#!/usr/bin/env python3
"""Real 2x2 Pose inference comparison; frozen downstream logic and approved GT."""
import argparse
from collections import Counter, defaultdict
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import time
from unittest.mock import patch

from replay_fall_baseline import AGENT, sha, write_json
from experimental_pose_input import letterbox, restore
from experimental_pose_disagreement import PoseDisagreementConfig
from experimental_pose_retention import PoseRetentionConfig
from experimental_request_coalescing import CoalescingConfig
from replay_roi_pose import CandidatePipeline, same_json
from replay_pose_stability import replay_rows as disagreement_rows, semantic, compare
from replay_pose_retention import replay_rows as retention_rows, verify_overlay, timing_stats
from replay_request_coalescing import replay as coalescing_replay, source_aligned
from review_fall_annotations import box_at, require
from score_fall_baseline import anchors, events, overlap_score, normalized_box
from homecam_detector.pose import PersonPoseEstimator


ARMS = dict(n_stretch=('n', False), n_letterbox=('n', True),
            s_stretch=('s', False), s_letterbox=('s', True))


def load_reference(root):
    marker = json.loads((root/'completed.json').read_text())
    require(sha(root/'summary.json') == marker['summary_sha256'], 'changed reference summary')
    summary = json.loads((root/'summary.json').read_text())
    folder = root/'bounded_pending'
    require(sha(folder/'completed.json') == marker['arms']['bounded_pending'], 'changed reference')
    complete = json.loads((folder/'completed.json').read_text())
    for name, digest in complete['files'].items():
        require(Path(name).name == name and sha(folder/name) == digest, 'changed reference file')
    meta = json.loads((folder/'run.json').read_text())
    rows = [json.loads(s) for s in (folder/'frames.jsonl').read_text().splitlines()]
    return summary, meta, rows


def frozen_downstream(raw, metas, meta, emit):
    retained = meta['fall_config']['upstream']
    stable = retained['upstream']
    a = list(disagreement_rows(raw, metas, PoseDisagreementConfig(**stable['pose_stability']),
                               True, 'disagreement_request'))
    b = list(retention_rows(a, metas, PoseRetentionConfig(**retained['retention'])))
    dispatches = []

    def save(event):
        dispatches.append(event)
        emit(event)

    diagnostics = coalescing_replay(b, metas, CoalescingConfig(**meta['fall_config']['coalescing']),
                                    save)
    scored = source_aligned(b, dispatches, diagnostics)
    return scored, dispatches, diagnostics


def box_summary(records):
    count = Counter(r['status'] for r in records)
    return dict(total=len(records), matched=count['matched'], ambiguous=count['ambiguous'],
                missing=count['unlinked'], usable=sum(r['usable'] for r in records))


def score(rows, dispatches, reference, stage, match):
    # This is the first point where label/box content enters computation.
    bundle = json.loads((stage/'evaluation_labels.json').read_text())
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    metas = {c['case_id']: c for c in json.loads((stage/'media.json').read_text())['cases']}
    by_case = defaultdict(list)
    for r in rows:
        by_case[r['case_id']].append(r)
    records, cases = [], []
    requests = [e for e in dispatches if e['dispatch_kind'] == 'request']
    for cid, ann in annotations.items():
        exact = anchors(ann, by_case[cid], metas[cid], match)
        records.extend(exact)
        target = [r for r in exact if r['person_id'] == ann.get('target_person_id')]
        unlinked, reviewed_predictions = 0, 0
        reviewed = {b[0] for p in ann['persons'] for b in p['boxes']}
        for row in by_case[cid]:
            if row['frame_index'] not in reviewed:
                continue
            gt = [box_at(p, row['frame_index']) for p in ann['persons']
                  if box_at(p, row['frame_index']) is not None]
            for obs in row['observations']:
                reviewed_predictions += 1
                pred = normalized_box(obs, metas[cid])
                unlinked += not any(overlap_score(b, pred, match) is not None for b in gt)
        req = [e for e in requests if e['origin']['case_id'] == cid]
        # No-person labels are a dedicated annotation attribute, not guessed from a filename.
        no_person = not ann['persons']
        cases.append(dict(case_id=cid, review_case_id=labels[cid].get('review_case_id'),
                          original_source=labels[cid].get('original_source_path'),
                          label=labels[cid]['label'], boxes=box_summary(exact),
                          target_boxes=box_summary(target), request_count=len(req),
                          first_dispatch_sec=req[0]['dispatch_time_s'] if req else None,
                          first_evidence_frame=req[0]['origin']['frame_index'] if req else None,
                          no_person_annotation=no_person,
                          predictions=sum(len(r['observations']) for r in by_case[cid]),
                          frames_with_predictions=sum(
                              bool(r['observations']) for r in by_case[cid]),
                          reviewed_predictions=reviewed_predictions,
                          unlinked_at_reviewed_frames=unlinked,
                          requests=req))
    comparison = compare(reference, rows, stage)
    candidate_groups = comparison['after']['groups']
    stats = timing_stats([r['pose_ms'] for r in rows])
    stats['mean'] = statistics.mean(r['pose_ms'] for r in rows)
    new_cases = [c['case_id'] for c in comparison['cases']
                 if c['before'] is None and c['after'] is not None]
    lost_cases = [c['case_id'] for c in comparison['cases']
                  if c['before'] is not None and c['after'] is None]
    return dict(boxes=box_summary(records), candidate_groups=candidate_groups,
                requests=len(requests), normal_requests=sum(
                    c['request_count'] for c in cases if c['label'] == 'normal_activity'),
                pose_ms=stats, new_candidate_cases=new_cases, lost_candidate_cases=lost_cases,
                maximum_routing_delay=max((e['delay_sec'] for e in dispatches), default=0),
                reviewed_predictions=sum(c['reviewed_predictions'] for c in cases),
                unlinked_at_reviewed_frames=sum(c['unlinked_at_reviewed_frames'] for c in cases),
                no_person_cases=[c for c in cases if c['no_person_annotation']],
                interpretation='video-level requests, not target-verified recall; '
                'unlinked boxes are not automatically background false positives'), cases, records


def run(args):
    import cv2
    import numpy as np
    import onnxruntime as ort

    require(not args.output.exists(), 'output exists; not overwriting')
    overlay = verify_overlay(args.spatial_final, args.parent_frozen)
    summary, meta, reference = load_reference(args.reference)
    require(summary['spatial_freeze_sha256'] == sha(args.spatial_final/'freeze.json'),
            'wrong approved spatial overlay')
    expected_runtime = meta['runtime']
    require([cv2.__version__, np.__version__, ort.__version__] ==
            [expected_runtime[k] for k in ('cv2', 'numpy', 'ort')], 'wrong runtime')
    sources = {Path(p): d for p, d in json.loads(args.source_lock.read_text()).items()}
    sources.update({Path(p): d for p, d in summary['source_sha256'].items()})
    sources.update({Path(p): d for p, d in meta['retention_source_sha256'].items()})
    own_paths = [Path(__file__), Path(__file__).with_name('experimental_pose_input.py'),
                 Path(__file__).with_name('replay_roi_pose.py'),
                 AGENT/'evaluations/synthetic_fall_v1/DETECTION_COMPARISON_PROTOCOL.md']
    sources.update({p: sha(p) for p in own_paths})
    for p, digest in sources.items():
        require(sha(p) == digest, 'changed source: '+str(p))
    s_export = json.loads(args.s_model.with_name('export.json').read_text())
    models = {'n': args.n_model, 's': args.s_model}
    model_hashes = dict(n=sha(args.n_model), s=sha(args.s_model))
    require(model_hashes['n'] == meta['model_sha256'], 'wrong control model')
    require(model_hashes['s'] == s_export['model_sha256'], 'wrong s export')
    media = json.loads((args.spatial_final/'media.json').read_text())
    selected = media['cases'][:args.max_cases] if args.max_cases else media['cases']
    metas = {m['case_id']: m for m in selected}
    reference = [r for r in reference if r['case_id'] in metas]
    ref_by_case = defaultdict(list)
    for r in reference:
        ref_by_case[r['case_id']].append(r)
    args.output.mkdir(mode=0o700)
    plan = dict(created_utc=datetime.now(timezone.utc).isoformat(), arms=ARMS,
                source_sha256={str(p): d for p, d in sources.items()}, model_sha256=model_hashes,
                source_reference=str(args.reference), decision_config=meta['fall_config'],
                spatial_freeze_sha256=summary['spatial_freeze_sha256'], runtime=expected_runtime,
                cases=list(metas), smoke_only=bool(args.max_cases), vlm_calls=0, paid_calls=0,
                decode_excluded=True, sampling_fps=meta['sample_fps'])
    write_json(args.output/'plan.json', plan)
    original_session = ort.InferenceSession

    def bounded(*a, **kw):
        opt = ort.SessionOptions()
        opt.intra_op_num_threads, opt.inter_op_num_threads = 2, 1
        opt.add_session_config_entry('session.intra_op.allow_spinning', '0')
        return original_session(*a, sess_options=opt, **kw)

    with patch.object(ort, 'InferenceSession', bounded):
        estimators = {name: PersonPoseEstimator(str(path), .45, .5, input_size=640)
                      for name, path in models.items()}
    cv2.setNumThreads(1)
    for estimator in estimators.values():
        estimator.estimate_all(np.zeros((640, 640, 3), np.uint8), confidence_threshold=.10)
    stable_cfg = meta['fall_config']['upstream']['upstream']
    legacy_meta = dict(tracker=meta['tracker'], fall_config=dict(
        upstream=stable_cfg['upstream'], request_dedup=stable_cfg['request_dedup']))
    streams, outputs = {}, {name: [] for name in ARMS}
    for arm in ARMS:
        folder = args.output/arm
        folder.mkdir()
        streams[arm] = (folder/'raw-frames.jsonl').open('x', buffering=1)
    try:
        for case_index, m in enumerate(selected):
            cid = m['case_id']
            oldrows = ref_by_case[cid]
            tids = [o['track_id'] for r in oldrows for o in r['observations'] if o['track_id']]
            prefix = tids[0].rsplit('-', 1)[0] if tids else 'empty-'+cid
            dprefix = oldrows[0]['baseline_analysis']['observationId'].rsplit('-frame-', 1)[0]
            pipelines = {
                arm: CandidatePipeline(cid, legacy_meta,
                                       prefix if arm == 'n_stretch' else arm+'-'+cid,
                                       dprefix if arm == 'n_stretch' else arm+'-'+cid)
                for arm in ARMS}
            path = (args.parent_frozen/m['source_path']).resolve()
            require(path.is_relative_to(args.parent_frozen.resolve()) and sha(path) == m['sha256'],
                    'source video changed')
            cap = cv2.VideoCapture(str(path))
            try:
                for tick, old in enumerate(oldrows):
                    f, stamp = old['frame_index'], old['timestamp_s']
                    cap.set(cv2.CAP_PROP_POS_FRAMES, f)
                    ok, frame = cap.read()
                    require(ok and frame.shape[:2] == (m['height'], m['width']), 'decode failure')
                    order = list(ARMS)
                    offset = (case_index+tick) % len(order)
                    for arm in order[offset:]+order[:offset]:
                        model, padded = ARMS[arm]
                        start = time.perf_counter()
                        image, g = letterbox(frame) if padded else (frame, None)
                        predicted = estimators[model].estimate_all(image, confidence_threshold=.10)
                        poses = restore(predicted, g) if padded else predicted
                        elapsed = (time.perf_counter()-start)*1000
                        if arm == 'n_stretch':
                            require(same_json([p.as_dict() for p in poses],
                                              [o['pose'] for o in old['observations']]),
                                    f'control inference mismatch: {cid}:{f}')
                        begin = time.perf_counter()
                        row = pipelines[arm].step(poses, f, stamp, tick,
                                                  (m['width'], m['height']), meta['sample_fps'])
                        # Stability stage consumes pose-loss BEFORE the legacy dedup stage.
                        row['fall_analysis'] = copy.deepcopy(row['upstream_analysis'])
                        row['verification_updates'] = copy.deepcopy(
                            row['upstream_verification_updates'])
                        pipeline_ms = elapsed+(time.perf_counter()-begin)*1000
                        row.update(pose_ms=elapsed, pipeline_ms=pipeline_ms,
                                   input_transform=asdict(g) if g else dict(mode='stretch'),
                                   model_poses_before_restore=[p.as_dict() for p in predicted])
                        if arm == 'n_stretch':
                            require(same_json(row['observations'], old['observations']),
                                    f'control tracking mismatch: {cid}:{f}')
                            require(same_json(row['baseline_analysis'], old['baseline_analysis']),
                                    f'control decision mismatch: {cid}:{f}')
                        streams[arm].write(json.dumps(row, allow_nan=False)+'\n')
                        outputs[arm].append(row)
            finally:
                cap.release()
            for stream in streams.values():
                stream.flush()
                os.fsync(stream.fileno())
            print(f'{case_index+1}/{len(selected)} {cid}: {len(oldrows)} frames x 4 arms',
                  flush=True)
    finally:
        for stream in streams.values():
            stream.close()
    all_summary = dict(plan, metrics={})
    for arm, raw in outputs.items():
        folder = args.output/arm
        with (folder/'dispatches.jsonl').open('x') as stream:
            def emit(e):
                stream.write(json.dumps(e, allow_nan=False)+'\n')
            rows, dispatches, diagnostics = frozen_downstream(raw, metas, meta, emit)
        if arm == 'n_stretch':
            require(all(same_json(semantic(a), semantic(b)) for a, b in zip(rows, reference)),
                    'control downstream mismatch')
        require(len(rows) == len(reference), 'incomplete replay')
        evidence = copy.deepcopy(rows)
        for r in evidence:
            r['fall_analysis']['candidates'].extend(
                u['evidence'] for u in r['verification_updates'])
        events(evidence)
        for name, records in [('frames.jsonl', rows), ('routing.jsonl', diagnostics)]:
            with (folder/name).open('x') as stream:
                for record in records:
                    stream.write(json.dumps(record, allow_nan=False)+'\n')
        if not args.max_cases:
            metrics, cases, exact = score(rows, dispatches, reference, args.spatial_final,
                                          overlay['match'])
            all_summary['metrics'][arm] = metrics
            write_json(folder/'cases.json', cases)
            write_json(folder/'box-matches.json', exact)
            print(json.dumps(dict(arm=arm, **metrics), ensure_ascii=False), flush=True)
        write_json(folder/'completed.json', dict(frames=len(rows),
                   files={p.name: sha(p) for p in folder.iterdir() if p.is_file()},
                   control_parity=True if arm == 'n_stretch' else None))
    for p, digest in sources.items():
        require(sha(p) == digest, 'source changed during run: '+str(p))
    for name, path in models.items():
        require(sha(path) == model_hashes[name], 'model changed during run')
    verify_overlay(args.spatial_final, args.parent_frozen)
    write_json(args.output/'summary.json', all_summary)
    write_json(args.output/'completed.json', dict(
        summary_sha256=sha(args.output/'summary.json'), plan_sha256=sha(args.output/'plan.json'),
        cases=len(metas), frames=len(reference), inference_calls=len(reference)*len(ARMS),
        warmup_calls=2, unchanged_sources=True,
        arms={arm: sha(args.output/arm/'completed.json') for arm in ARMS}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('reference', 'spatial-final', 'parent-frozen', 'source-lock',
                 'n-model', 's-model', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--max-cases', type=int, default=0, help='smoke only, no scoring')
    args = parser.parse_args()
    require(args.max_cases >= 0, 'negative case limit')
    run(args)


if __name__ == '__main__':
    main()
