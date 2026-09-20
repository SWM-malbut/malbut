#!/usr/bin/env python3
"""Offline RGB replay of current production classes, without labels in inference.

prepare verifies/fixes the evaluation inputs; run consumes only the media manifest.
No ROS callbacks, robot controls, provider calls or detector threshold tuning here.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from review_fall_annotations import load_inputs, require, validate


AGENT = Path(__file__).resolve().parents[1]
REPO = AGENT.parent
SOURCE = AGENT / 'homecam_detector/homecam_detector'
sys.path.insert(0, str(SOURCE.parent))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def sample_frames(count, fps, rate=5):
    """Nearest actual source frame to each 5 Hz tick, never duplicate or extrapolate."""
    require(count > 0 and math.isfinite(fps) and fps >= rate > 0, 'invalid sampling')
    return sorted({round(i * fps / rate) for i in range(math.ceil(count / fps * rate))
                   if round(i * fps / rate) < count})


def prepare(args):
    draft, labels, media = load_inputs(args.dataset, args.annotations)
    validate(draft, labels, media)
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_json(args.output / 'media.json', media)
    write_json(args.output / 'evaluation_labels.json', dict(
        annotations=draft, classifications=labels,
        review_use='user-reviewed working labels for development baseline, 2026-09-10',
        original_annotations_sha256=sha(args.annotations),
    ))
    protocol = AGENT / 'evaluations/synthetic_fall_v1/BASELINE_PROTOCOL.md'
    (args.output / 'protocol.md').write_bytes(protocol.read_bytes())
    write_json(args.output / 'freeze.json', dict(
        created_utc=datetime.now(timezone.utc).isoformat(),
        files={name: sha(args.output / name) for name in
               ('media.json', 'evaluation_labels.json', 'protocol.md')},
        match=dict(visible_coverage=0.5, prediction_coverage=0.25, margin=0.1),
    ))
    print(f'FROZEN cases={len(media["cases"])} output={args.output}', flush=True)


def verify_freeze(directory):
    frozen = json.loads((directory / 'freeze.json').read_text())
    if frozen.get('schema_version') == 'malbut.fall-evaluation-freeze.v2':
        from prepare_fall_evaluation_v2 import verify
        return verify(directory)
    for name, digest in frozen['files'].items():
        require(name in {'media.json', 'evaluation_labels.json', 'protocol.md'}, 'freeze path')
        require(sha(directory / name) == digest, f'changed frozen file: {name}')
    return frozen


def run(args):
    import cv2
    import numpy as np
    import onnxruntime as ort
    from homecam_detector.config import DetectorConfig
    from homecam_detector.fall_candidate import FallCandidateConfig, FallCandidateDetector
    from homecam_detector.fall_candidate import extract_pose_features
    from homecam_detector.pose import PersonPoseEstimator
    from homecam_detector.pose_tracker import PersonPoseTracker

    verify_freeze(args.frozen)
    # Only media metadata reaches the inference loop. No classifications, boxes or times.
    media = json.loads((args.frozen / 'media.json').read_text())
    cfg, fall_cfg = DetectorConfig(), FallCandidateConfig()
    settings = dict(strong_threshold=cfg.pose_confidence_threshold,
                    candidate_threshold=cfg.pose_candidate_confidence_threshold,
                    max_gap_sec=cfg.pose_track_max_gap_sec,
                    min_observations=cfg.pose_track_min_observations,
                    max_people=cfg.pose_track_max_people)
    require(sha(args.model) == args.model_sha256, 'unexpected model bytes')
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    source_hashes = {n: sha(SOURCE / n) for n in (
        'pose.py', 'pose_tracker.py', 'fall_candidate.py', 'config.py', 'detector_node.py')}
    metadata = dict(
        created_utc=datetime.now(timezone.utc).isoformat(),
        scope='development RGB replay, current production classes; not ROS or robot E2E',
        git_head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO,
                                         text=True).strip(),
        source_sha256=source_hashes, script_sha256=sha(Path(__file__)),
        model_sha256=sha(args.model), media_sha256=sha(args.frozen / 'media.json'),
        freeze_sha256=sha(args.frozen / 'freeze.json'),
        runtime=dict(cv2=cv2.__version__, ort=ort.__version__, numpy=np.__version__,
                     device='CPU', ort_intra_threads=2, ort_inter_threads=1, cv2_threads=1),
        input_size=[640, 640], resize='stretch', sample_fps=cfg.pose_inference_fps,
        tracker=settings, fall_config=asdict(fall_cfg), fall_config_sha256=fall_cfg.sha256,
        robot_motion='unknown', depth=None,
        clocks='source frame/fps for evidence; synthetic 100+i/5 receive time for tracking',
    )
    write_json(args.output / 'run.json', metadata)
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
    total, outputs = 0, 0
    with (args.output / 'frames.jsonl').open('x', buffering=1, encoding='utf-8') as stream:
        for meta in media['cases']:
            path = (args.dataset / meta['source_path']).resolve()
            require(path.is_relative_to(args.dataset.resolve()), 'media path escapes dataset')
            require(sha(path) == meta['sha256'], f'{meta["case_id"]}: media changed')
            tracker, detector = PersonPoseTracker(**settings), FallCandidateDetector(fall_cfg)
            indices = sample_frames(meta['frames'], meta['fps'], cfg.pose_inference_fps)
            cap = cv2.VideoCapture(str(path))
            emitted = 0
            try:
                for tick, index in enumerate(indices):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                    ok, frame = cap.read()
                    require(ok, f'{meta["case_id"]}: decode failed at {index}')
                    require(frame.shape[:2] == (meta['height'], meta['width']), 'image size')
                    begin = time.perf_counter()
                    poses = estimator.estimate_all(
                        frame, confidence_threshold=cfg.pose_candidate_confidence_threshold)
                    pose_ms = (time.perf_counter() - begin) * 1000
                    tracks = tracker.update(poses, now=100 + tick / cfg.pose_inference_fps)
                    result = detector.update(tracks, capture_time=index / meta['fps'],
                                             image_size=(meta['width'], meta['height']),
                                             robot_motion='unknown')
                    pipeline_ms = (time.perf_counter() - begin) * 1000
                    require(result['status'] == 'ok', 'fall analysis unavailable')
                    observations = []
                    for number, pose in enumerate(poses):
                        matches = [t for t in tracks.tracks if t.pose is pose]
                        observations.append(dict(
                            observation_index=number, pose=pose.as_dict(),
                            track_id=matches[0].track_id if matches else None,
                            features=asdict(extract_pose_features(
                                pose, (meta['width'], meta['height']), {}, fall_cfg)),
                        ))
                    row = dict(case_id=meta['case_id'], frame_index=index,
                               timestamp_s=index / meta['fps'], pose_ms=pose_ms,
                               pipeline_ms=pipeline_ms, observations=observations,
                               fall_analysis=result)
                    stream.write(json.dumps(row, allow_nan=False) + '\n')
                    total += 1
                    emitted += len(result['candidates'])
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                cap.release()
            outputs += emitted
            print(f'{meta["case_id"]} samples={len(indices)} outputs={emitted}', flush=True)
    for name, digest in source_hashes.items():
        require(sha(SOURCE / name) == digest, f'source changed during run: {name}')
    require(sha(args.model) == metadata['model_sha256'], 'model changed during run')
    write_json(args.output / 'completed.json', dict(
        cases=len(media['cases']), frames=total, outputs=outputs,
        frames_sha256=sha(args.output / 'frames.jsonl'), run_sha256=sha(args.output / 'run.json'),
    ))
    print(f'COMPLETE cases={len(media["cases"])} frames={total}', flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    freeze = modes.add_parser('prepare')
    freeze.add_argument('--annotations', type=Path, required=True)
    execute = modes.add_parser('run')
    execute.add_argument('--frozen', type=Path, required=True)
    execute.add_argument('--model', type=Path, required=True)
    execute.add_argument('--model-sha256', required=True)
    for mode in (freeze, execute):
        mode.add_argument('--dataset', type=Path, required=True)
        mode.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        (prepare if args.command == 'prepare' else run)(args)
    except (ValueError, OSError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
