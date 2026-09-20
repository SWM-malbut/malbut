#!/usr/bin/env python3
"""Paired full-frame / full-frame+ROI real ONNX inference, offline only."""
import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from replay_fall_baseline import AGENT, SOURCE, sha, verify_freeze, write_json
from experimental_leg_change import merge_requests
from experimental_pose_gap import PoseGapConfig, PoseGapExperiment
from experimental_request_dedup import RequestDedupConfig, RequestDedupExperiment
from experimental_roi_pose import RoiPlanner, RoiPoseConfig, fuse_poses, project_pose
from review_fall_annotations import require
from score_fall_baseline import events, validate_rows
from homecam_detector.fall_candidate import (
    FallCandidateConfig, FallCandidateDetector, extract_pose_features,
)
from homecam_detector.pose import PersonPoseEstimator
from homecam_detector.pose_tracker import PersonPoseTracker


def same_json(left, right):
    """Compare the stored contract (JSON arrays), not Python tuple/list types."""
    return (json.dumps(left, sort_keys=True, allow_nan=False) ==
            json.dumps(right, sort_keys=True, allow_nan=False))


class CandidatePipeline:
    def __init__(self, cid, metadata, tracker_prefix, detector_prefix):
        self.cid = cid
        settings = metadata['fall_config']['upstream']
        self.cfg = FallCandidateConfig(**settings['baseline'])
        self.tracker = PersonPoseTracker(**metadata['tracker'])
        self.detector = FallCandidateDetector(self.cfg)
        # Deterministic replay IDs only; production constructors/files are unchanged.
        self.tracker._prefix = tracker_prefix
        self.detector._prefix = detector_prefix
        self.gap = PoseGapExperiment(PoseGapConfig(**settings['pose_gap']))
        self.dedup = RequestDedupExperiment(RequestDedupConfig(
            **metadata['fall_config']['request_dedup']))
        self.requested = {}

    def step(self, poses, frame, stamp, tick, size, sample_fps):
        tracks = self.tracker.update(poses, now=100+tick/sample_fps)
        base = self.detector.update(tracks, capture_time=stamp, image_size=size,
                                    robot_motion='unknown')
        observations = []
        for index, pose in enumerate(poses):
            assigned = [t for t in tracks.tracks if t.pose is pose]
            observations.append(dict(
                observation_index=index, pose=pose.as_dict(),
                track_id=assigned[0].track_id if assigned else None,
                features=asdict(extract_pose_features(pose, size, {}, self.cfg))))
        extra, details = self.gap.update(tracks, base, capture_time=stamp, image_size=size,
                                         frame_index=frame, observations=observations)
        requests, updates = merge_requests(base['candidates'], extra, self.requested)
        row = dict(case_id=self.cid, frame_index=frame, timestamp_s=stamp,
                   observations=observations, baseline_analysis=copy.deepcopy(base),
                   fall_analysis=copy.deepcopy(base), experiment_raw_candidates=extra,
                   experiment_details=details, verification_updates=updates)
        row['fall_analysis']['candidates'] = requests
        for d in row['fall_analysis']['tracks']:
            tid = d['targetTrackId']
            if tid in self.requested:
                d['activeCandidateId'] = self.requested[tid]
            if any(c['targetTrackId'] == tid for c in requests):
                d['status'] = 'verification_candidate'
        row['upstream_analysis'] = copy.deepcopy(row['fall_analysis'])
        row['upstream_verification_updates'] = copy.deepcopy(updates)
        out, updated, paired = self.dedup.update(row, image_size=size)
        row['fall_analysis']['candidates'] = out
        row['verification_updates'] = updated
        row['request_dedup_details'] = paired
        row['verification_routes'] = dict(self.dedup.routes)
        return row


def run(args):
    import cv2
    import numpy as np
    import onnxruntime as ort

    verify_freeze(args.frozen)
    completed = json.loads((args.reference / 'completed.json').read_text())
    for name, key in [('frames.jsonl', 'frames_sha256'), ('run.json', 'run_sha256')]:
        require(sha(args.reference / name) == completed[key], 'changed reference')
    m = json.loads((args.reference / 'run.json').read_text())
    require(m['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
    require(m['media_sha256'] == sha(args.frozen / 'media.json'), 'wrong media')
    require(sha(args.model) == m['model_sha256'], 'wrong model')
    require(m['runtime']['cv2'] == cv2.__version__ and m['runtime']['ort'] == ort.__version__
            and m['runtime']['numpy'] == np.__version__, 'runtime differs from frozen baseline')
    for name, digest in m['source_sha256'].items():
        require(sha(SOURCE / name) == digest, f'changed source {name}')
    media = json.loads((args.frozen / 'media.json').read_text())
    reference = [json.loads(line) for line in
                 (args.reference / 'frames.jsonl').read_text().splitlines()]
    validate_rows(reference, media, m)
    cfg = RoiPoseConfig()
    files = [Path(__file__), Path(__file__).with_name('experimental_roi_pose.py'),
             Path(__file__).with_name('experimental_leg_change.py'),
             Path(__file__).with_name('experimental_pose_gap.py'),
             Path(__file__).with_name('experimental_request_dedup.py'),
             Path(__file__).with_name('score_fall_baseline.py'),
             AGENT / 'evaluations/synthetic_fall_v1/ROI_POSE_PROTOCOL.md']
    source_hashes = {str(p): sha(p) for p in files}
    session = ort.InferenceSession

    def bounded(*pos, **kw):
        opt = ort.SessionOptions()
        opt.intra_op_num_threads, opt.inter_op_num_threads = 2, 1
        opt.add_session_config_entry('session.intra_op.allow_spinning', '0')
        return session(*pos, sess_options=opt, **kw)

    with patch.object(ort, 'InferenceSession', bounded):
        estimator = PersonPoseEstimator(str(args.model), .45, .5, input_size=640)
    cv2.setNumThreads(1)
    estimator.estimate_all(np.zeros((400, 640, 3), np.uint8), confidence_threshold=.10)
    args.output.mkdir(mode=0o700, exist_ok=False)
    streams, metadata, saved_rows = {}, {}, {'control': [], 'roi': []}
    for name in saved_rows:
        out = args.output / name
        out.mkdir(mode=0o700)
        settings = dict(m['fall_config'], roi_pose=asdict(cfg) if name == 'roi' else None)
        config_sha = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        md = dict(m, created_utc=datetime.now(timezone.utc).isoformat(),
                  scope=f'offline real ONNX paired {name}; not ROS/Jetson/E2E',
                  roi_source_sha256=source_hashes, fall_config=settings,
                  fall_config_sha256=config_sha,
                  reference_frames_sha256=completed['frames_sha256'],
                  warmup_calls=1, decode_excluded=True,
                  timing_note='New CPU measurement. Full-frame inference shared once per pair. '
                              'pipeline_ms=full_pose_ms+own extra/analysis stages; '
                              'excludes decode, '
                              'model load, warmup and the other comparison branch.')
        metadata[name] = md
        write_json(out / 'run.json', md)
        streams[name] = (out / 'frames.jsonl').open('x', buffering=1)
    full_count, roi_count = 0, 0
    try:
        for meta in media['cases']:
            cid = meta['case_id']
            rows = [r for r in reference if r['case_id'] == cid]
            tids = [o['track_id'] for r in rows for o in r['observations'] if o['track_id']]
            track_prefix = tids[0].rsplit('-', 1)[0] if tids else f'empty-{cid}'
            det_prefix = rows[0]['baseline_analysis']['observationId'].rsplit('-frame-', 1)[0]
            pipelines = dict(control=CandidatePipeline(cid, m, track_prefix, det_prefix),
                             roi=CandidatePipeline(cid, m, f'roi-{cid}', f'roi-{cid}'))
            planner = RoiPlanner(cfg)
            path = (args.dataset / meta['source_path']).resolve()
            require(path.is_relative_to(args.dataset.resolve()) and sha(path) == meta['sha256'],
                    'source video changed')
            cap = cv2.VideoCapture(str(path))
            case_calls = 0
            try:
                for tick, old in enumerate(rows):
                    frame_index, stamp = old['frame_index'], old['timestamp_s']
                    size = meta['width'], meta['height']
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                    ok, frame = cap.read()
                    require(ok and frame.shape[:2] == (size[1], size[0]), 'decode mismatch')
                    start = time.perf_counter()
                    full = estimator.estimate_all(frame, confidence_threshold=.10)
                    full_ms = (time.perf_counter()-start)*1000
                    require([p.as_dict() for p in full] == [o['pose'] for o in old['observations']],
                            f'new full-frame inference differs: {cid}:{frame_index}')
                    start = time.perf_counter()
                    control = pipelines['control'].step(
                        full, frame_index, stamp, tick, size, m['sample_fps'])
                    control_ms = (time.perf_counter()-start)*1000
                    for key in ('observations', 'baseline_analysis', 'verification_updates',
                                'request_dedup_details', 'verification_routes'):
                        require(same_json(control[key], old[key]),
                                f'control pipeline differs {key}: {cid}:{frame_index}')
                    require(same_json(control['fall_analysis']['candidates'],
                                      old['fall_analysis']['candidates']),
                            f'control requests differ: {cid}:{frame_index}')
                    start = time.perf_counter()
                    rois, reason = planner.update(control, size)
                    crop_obs, attempts, roi_ms = [], [], 0.0
                    for roi_index, spec in enumerate(rois):
                        l, t, r, b = spec['roi']
                        begin = time.perf_counter()
                        detected = estimator.estimate_all(frame[t:b, l:r], confidence_threshold=.10)
                        elapsed = (time.perf_counter()-begin)*1000
                        roi_ms += elapsed
                        attempts.append(dict(spec, roi_index=roi_index, inference_ms=elapsed,
                                             raw_pose_count=len(detected)))
                        for p in detected:
                            crop_obs.append(dict(pose=project_pose(p, spec['roi'], size),
                                                 source='roi', roi_index=roi_index, **spec))
                    fused, provenance, raw = fuse_poses(full, crop_obs, cfg)
                    roi_extra_ms = (time.perf_counter()-start)*1000
                    start = time.perf_counter()
                    enhanced = pipelines['roi'].step(
                        fused, frame_index, stamp, tick, size, m['sample_fps'])
                    enhanced_ms = (time.perf_counter()-start)*1000
                    control.update(pose_ms=full_ms, pipeline_ms=full_ms+control_ms,
                                   full_pose_ms=full_ms, analysis_ms=control_ms)
                    enhanced.update(pose_ms=full_ms+roi_ms,
                                    pipeline_ms=full_ms+roi_extra_ms+enhanced_ms,
                                    full_pose_ms=full_ms, roi_inference_ms=roi_ms,
                                    roi_extra_ms=roi_extra_ms, analysis_ms=enhanced_ms,
                                    roi_attempts=attempts, roi_scheduling_reason=reason,
                                    roi_raw_observations=raw, observation_provenance=provenance)
                    for name, row in [('control', control), ('roi', enhanced)]:
                        row['fall_analysis'].update(
                            algorithmVersion=f'paired-roi-pose-{name}-experiment-v1',
                            configSha256=metadata[name]['fall_config_sha256'])
                        streams[name].write(json.dumps(row, allow_nan=False)+'\n')
                        saved_rows[name].append(row)
                    full_count += 1
                    roi_count += len(rois)
                    case_calls += len(rois)
            finally:
                cap.release()
            counts = {n: sum(len(r['fall_analysis']['candidates']) for r in saved_rows[n]
                             if r['case_id'] == cid) for n in saved_rows}
            print(f'{cid}: full={len(rows)} ROI calls={case_calls} '
                  f'requests control={counts["control"]} roi={counts["roi"]}', flush=True)
    finally:
        for stream in streams.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
    for path, digest in source_hashes.items():
        require(sha(Path(path)) == digest, 'experiment source changed during execution')
    for name, digest in m['source_sha256'].items():
        require(sha(SOURCE / name) == digest, 'detector source changed during execution')
    require(sha(args.model) == m['model_sha256'], 'model changed during execution')
    verify_freeze(args.frozen)
    for name, rows in saved_rows.items():
        validate_rows(rows, media, metadata[name])
        events(rows)
        out = args.output / name
        write_json(out / 'completed.json', dict(
            cases=len(media['cases']), frames=len(rows),
            fresh_full_inference_parity_frames=full_count,
            control_request_parity_frames=full_count,
            roi_inference_calls=roi_count if name == 'roi' else 0,
            outputs=sum(len(r['fall_analysis']['candidates']) for r in rows),
            frames_sha256=sha(out / 'frames.jsonl'), run_sha256=sha(out / 'run.json')))
    write_json(args.output / 'completed.json', dict(
        full_inference_calls=full_count, roi_inference_calls=roi_count,
        branches={n: sha(args.output / n / 'completed.json') for n in saved_rows}))
    print(f'COMPLETE full={full_count} roi={roi_count}', flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'reference', 'dataset', 'model', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
