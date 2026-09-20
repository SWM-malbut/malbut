#!/usr/bin/env python3
"""Fresh causal RGB -> Pose -> tracker -> candidate inference for VLM replay.

Writes each observed frame immediately. No saved predictions or labels drive inference.
The parent may invoke VLM before reading the next row. This is offline replay, not ROS.
"""
import argparse
import copy
from dataclasses import asdict
import json
from pathlib import Path
import time
from unittest.mock import patch

from replay_fall_baseline import SOURCE, sample_frames, sha, verify_freeze
from review_fall_annotations import require
from experimental_leg_change import merge_requests
from experimental_pose_gap import PoseGapConfig, PoseGapExperiment
from experimental_request_dedup import RequestDedupConfig, RequestDedupExperiment


def wait_until(deadline):
    """Bounded sleeps keep replay responsive; no future frames reach inference."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, .2))


def iter_rows(args):
    import cv2
    import onnxruntime as ort
    from homecam_detector.config import DetectorConfig
    from homecam_detector.fall_candidate import (
        FallCandidateConfig, FallCandidateDetector, extract_pose_features,
    )
    from homecam_detector.pose import PersonPoseEstimator
    from homecam_detector.pose_tracker import PersonPoseTracker

    verify_freeze(args.frozen)
    reference = json.loads((args.reference / 'run.json').read_text())
    complete = json.loads((args.reference / 'completed.json').read_text())
    require(sha(args.reference / 'run.json') == complete['run_sha256'], 'reference changed')
    require(sha(args.model) == reference['model_sha256'], 'Pose weights changed')
    for name, expected in reference['source_sha256'].items():
        require(sha(SOURCE / name) == expected, f'Pose source changed: {name}')
    cfg = DetectorConfig()
    settings = reference['fall_config']
    require(settings['upstream']['mode'] == 'pose-loss', 'unexpected candidate experiment')
    fall_cfg = FallCandidateConfig(**settings['upstream']['baseline'])
    gap_cfg = PoseGapConfig(**settings['upstream']['pose_gap'])
    dedup_cfg = RequestDedupConfig(**settings['request_dedup'])
    session = ort.InferenceSession

    def bounded_session(*pos, **kw):
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.add_session_config_entry('session.intra_op.allow_spinning', '0')
        return session(*pos, sess_options=options, **kw)

    with patch.object(ort, 'InferenceSession', bounded_session):
        estimator = PersonPoseEstimator(str(args.model), cfg.pose_confidence_threshold,
                                        cfg.pose_keypoint_threshold, input_size=640)
    cv2.setNumThreads(1)
    media = json.loads((args.frozen / 'media.json').read_text())['cases']
    for meta in media:
        path = (args.dataset / meta['source_path']).resolve()
        require(path.is_relative_to(args.dataset.resolve()), 'media outside dataset')
        require(sha(path) == meta['sha256'], 'media changed')
        tracker = PersonPoseTracker(**reference['tracker'])
        detector, gap, dedup = (FallCandidateDetector(fall_cfg), PoseGapExperiment(gap_cfg),
                                RequestDedupExperiment(dedup_cfg))
        requested = {}
        cap = cv2.VideoCapture(str(path))
        try:
            require(cap.isOpened(), 'video unavailable')
            realtime = getattr(args, 'realtime', False)
            replay_start = time.monotonic() if realtime else None
            for tick, index in enumerate(sample_frames(meta['frames'], meta['fps'], 5)):
                if realtime:
                    due = replay_start + index / meta['fps']
                    wait_until(due)
                    processing_started = time.monotonic()
                cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = cap.read()
                require(ok, 'cannot decode frame')
                require(frame.shape[:2] == (meta['height'], meta['width']), 'wrong image size')
                started = time.perf_counter()
                poses = estimator.estimate_all(
                    frame, confidence_threshold=reference['tracker']['candidate_threshold'])
                pose_ms = (time.perf_counter() - started) * 1000
                tracks = tracker.update(poses, now=100 + tick / 5)
                size, stamp = (meta['width'], meta['height']), index / meta['fps']
                base = detector.update(tracks, capture_time=stamp,
                                       image_size=size, robot_motion='unknown')
                observations = []
                for number, pose in enumerate(poses):
                    matches = [t for t in tracks.tracks if t.pose is pose]
                    observations.append(dict(
                        observation_index=number, pose=pose.as_dict(),
                        track_id=matches[0].track_id if matches else None,
                        features=asdict(extract_pose_features(pose, size, {}, fall_cfg))))
                extra, details = gap.update(tracks, base, capture_time=stamp, image_size=size,
                                            frame_index=index, observations=observations)
                requests, updates = merge_requests(base['candidates'], extra, requested)
                row = dict(case_id=meta['case_id'], frame_index=index, timestamp_s=stamp,
                           observations=observations, fall_analysis=copy.deepcopy(base),
                           verification_updates=updates, gap_details=details)
                row['fall_analysis']['candidates'] = requests
                requests, updates, details = dedup.update(row, image_size=size)
                row['fall_analysis']['candidates'] = requests
                row['verification_updates'] = updates
                row.update(dedup_details=details, pose_ms=pose_ms,
                           pipeline_ms=(time.perf_counter() - started) * 1000)
                if realtime:
                    row['replay_timing'] = dict(
                        clock='Linux controller time.monotonic', replay_start_s=replay_start,
                        frame_due_s=due, processing_started_s=processing_started,
                        candidate_ready_s=time.monotonic(),
                        processing_lateness_s=max(0., processing_started-due),
                        delivery='5Hz, 1x source timeline, no drops; blocking consumer may queue frames')
                yield row
        finally:
            cap.release()


def run(args):
    for row in iter_rows(args):
        print(json.dumps(row, allow_nan=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('dataset', 'frozen', 'reference', 'model'):
        parser.add_argument('--' + arg, type=Path, required=True)
    parser.add_argument('--realtime', action='store_true', help='1x source-time replay; not ROS capture')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
