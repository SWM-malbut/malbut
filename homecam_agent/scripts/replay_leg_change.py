#!/usr/bin/env python3
"""Compare isolated candidate experiments using hash-bound, actual cached Pose results.

No inference, tracker changes, labels in the candidate loop, network or ROS.
Reconstruct only fields consumed by the baseline, and assert full baseline parity.
"""
import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from experimental_leg_change import LegChangeConfig, LegChangeExperiment, merge_requests
from experimental_pose_gap import PoseGapConfig, PoseGapExperiment
from replay_fall_baseline import AGENT, SOURCE, sha, verify_freeze, write_json
from review_fall_annotations import require
from score_fall_baseline import validate_rows
from homecam_detector.fall_candidate import FallCandidateConfig, FallCandidateDetector
from homecam_detector.pose import PersonPose, PoseKeypoint
from homecam_detector.pose_tracker import PoseTrackingResult, TrackedPose, UnassignedPose


def tracking_snapshot(row):
    poses, by_track, unassigned = [], {}, []
    for obs in row['observations']:
        value = obs['pose']
        pose = PersonPose(value['boxConfidence'], tuple(
            value['box'][key] for key in ('left', 'top', 'right', 'bottom')),
            tuple(PoseKeypoint(**k) for k in value['keypoints']), value['visibleKeypoints'])
        poses.append(pose)
        tid = obs['track_id']
        if tid is None:
            unassigned.append(UnassignedPose(pose, 'cached_unassigned'))
        else:
            require(tid not in by_track, 'two observations assigned to one track')
            by_track[tid] = pose
    tracks = []
    for diag in row['fall_analysis']['tracks']:
        tid = diag['targetTrackId']
        pose = by_track.pop(tid, None)
        # Historical strong_seen is not serialized. For emitted baseline
        # candidates, its only effect (weak warning) is preserved exactly.
        emitted = [c for c in row['fall_analysis']['candidates'] if c['targetTrackId'] == tid]
        weak = (('weak_pose_detection' in emitted[0]['uncertainties']) if emitted
                else bool(pose and pose.box_confidence < .45))
        tracks.append(TrackedPose(tid, diag['trackingState'], pose,
                                  'weak' if weak else 'strong', 0, 0, 0, ()))
    require(not by_track, 'assigned observations absent from diagnostics')
    require(len(unassigned) == row['fall_analysis']['unassignedCount'], 'unassigned count')
    return PoseTrackingResult(tuple(tracks), tuple(unassigned), tuple(
        row['fall_analysis']['expiredTrackIds']))


def canonical(value):
    """Only random detector prefixes differ; retain IDs' sequence numbers."""
    value = json.loads(json.dumps(value))

    def visit(item):
        if isinstance(item, dict):
            return {k: (v.rsplit('-', 1)[-1] if v is not None and k in {
                'candidateId', 'activeCandidateId', 'observationId'} else visit(v))
                    for k, v in item.items()}
        return [visit(v) for v in item] if isinstance(item, list) else item

    return visit(value)


def run(args):
    verify_freeze(args.frozen)
    completed = json.loads((args.baseline / 'completed.json').read_text())
    for key, name in (('frames_sha256', 'frames.jsonl'), ('run_sha256', 'run.json')):
        require(sha(args.baseline / name) == completed[key], f'changed baseline {name}')
    metadata = json.loads((args.baseline / 'run.json').read_text())
    require(metadata['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
    require(metadata['media_sha256'] == sha(args.frozen / 'media.json'), 'wrong media')
    for name, digest in metadata['source_sha256'].items():
        require(sha(SOURCE / name) == digest, f'baseline source changed: {name}')
    require(metadata['robot_motion'] == 'unknown' and metadata['depth'] is None,
            'this comparison requires the RGB-only baseline')
    media = json.loads((args.frozen / 'media.json').read_text())
    rows = [json.loads(line) for line in (args.baseline / 'frames.jsonl').read_text().splitlines()]
    validate_rows(rows, media, metadata)
    baseline_cfg = FallCandidateConfig(**metadata['fall_config'])
    mode = getattr(args, 'experiment', 'leg-change')
    cfg = LegChangeConfig()
    gap_cfg = PoseGapConfig(resume_after_missing=(mode != 'pose-loss'))
    protocols = AGENT / 'evaluations/synthetic_fall_v1'
    source_files = [Path(__file__), Path(__file__).with_name('experimental_leg_change.py'),
                    Path(__file__).with_name('experimental_pose_gap.py'),
                    Path(__file__).with_name('replay_fall_baseline.py'),
                    Path(__file__).with_name('score_fall_baseline.py'),
                    protocols / 'LEG_CHANGE_PROTOCOL.md', protocols / 'POSE_GAP_PROTOCOL.md',
                    protocols / 'POSE_GAP_ABLATION.md']
    sources = {str(p): sha(p) for p in source_files}
    settings = dict(baseline=asdict(baseline_cfg), mode=mode)
    if mode in {'leg-change', 'combined'}:
        settings['leg_change'] = asdict(cfg)
    if mode in {'pose-gap', 'pose-loss', 'combined'}:
        settings['pose_gap'] = asdict(gap_cfg)
    config_sha = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    args.output.mkdir(mode=0o700, exist_ok=False)
    outmeta = dict(metadata, created_utc=datetime.now(timezone.utc).isoformat(),
                   scope='offline cached Pose experiment; not deployed; not new inference',
                   baseline_frames_sha256=completed['frames_sha256'],
                   baseline_run_sha256=completed['run_sha256'],
                   experiment_source_sha256=sources, fall_config=settings,
                   fall_config_sha256=config_sha,
                   timing_note='pose_ms/pipeline_ms inherited from baseline; '
                               'experiment_ms is the newly measured add-on only')
    write_json(args.output / 'run.json', outmeta)
    metas = {m['case_id']: m for m in media['cases']}
    current, count, candidate_count = None, 0, 0
    with (args.output / 'frames.jsonl').open('x', buffering=1) as stream:
        for row in rows:
            cid = row['case_id']
            if current != cid:
                detector = FallCandidateDetector(baseline_cfg)
                components = []
                if mode in {'leg-change', 'combined'}:
                    components.append(('leg_change', LegChangeExperiment(cfg)))
                if mode in {'pose-gap', 'pose-loss', 'combined'}:
                    components.append(('pose_gap', PoseGapExperiment(gap_cfg)))
                current, requested = cid, {}
            tracks = tracking_snapshot(row)
            size = (metas[cid]['width'], metas[cid]['height'])
            actual = detector.update(tracks, capture_time=row['timestamp_s'],
                                     image_size=size, robot_motion='unknown')
            require(canonical(actual) == canonical(row['fall_analysis']),
                    f'baseline parity failed: {cid} frame {row["frame_index"]}')
            # Use original IDs for transparent preservation of all baseline outputs.
            base = row['fall_analysis']
            start = time.perf_counter()
            extra, details = [], []
            for name, component in components:
                kw = (dict(frame_index=row['frame_index'], observations=row['observations'])
                      if name == 'pose_gap' else {})
                candidates, records = component.update(
                    tracks, base, capture_time=row['timestamp_s'], image_size=size, **kw)
                extra.extend(candidates)
                details.extend(dict(d, rule=name) for d in records)
            requests, updates = merge_requests(base['candidates'], extra, requested)
            elapsed = (time.perf_counter() - start) * 1000
            out = copy.deepcopy(row)
            out['baseline_analysis'] = base
            out['experiment_ms'] = elapsed
            out['experiment_details'] = details
            out['experiment_raw_candidates'] = extra
            out['verification_updates'] = updates
            out['fall_analysis'].update(algorithmVersion=f'pose-v2-plus-{mode}-experiment-v1',
                                        configSha256=config_sha, candidates=requests)
            emitted_tracks = {c['targetTrackId'] for c in requests}
            for diag in out['fall_analysis']['tracks']:
                tid = diag['targetTrackId']
                if tid in requested:
                    diag['activeCandidateId'] = requested[tid]
                if tid in emitted_tracks:
                    diag['status'] = 'verification_candidate'
            stream.write(json.dumps(out, allow_nan=False) + '\n')
            candidate_count += len(requests)
            count += 1
        stream.flush()
        os.fsync(stream.fileno())
    for path, digest in sources.items():
        require(sha(Path(path)) == digest, 'experiment source changed during run')
    for name, digest in metadata['source_sha256'].items():
        require(sha(SOURCE / name) == digest, 'baseline source changed during run')
    write_json(args.output / 'completed.json', dict(
        cases=len(metas), frames=count, outputs=candidate_count, baseline_parity_frames=count,
        frames_sha256=sha(args.output / 'frames.jsonl'), run_sha256=sha(args.output / 'run.json')))
    print(f'COMPLETE frames={count} baseline_parity={count} requests={candidate_count}')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--experiment', choices=('leg-change', 'pose-gap', 'pose-loss', 'combined'),
                        default='leg-change')
    args = parser.parse_args()
    run(args)


if __name__ == '__main__':
    main()
