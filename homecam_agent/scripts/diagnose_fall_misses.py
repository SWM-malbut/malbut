#!/usr/bin/env python3
"""Read-only detector diagnosis from frozen actual Pose outputs and source RGB.

Writes only a new review artifact directory. No new inference, threshold changes,
label changes, missing-pose interpolation or product calls. Geometry eligibility
is NOT proof that a prediction depicts the labelled target or even a human.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import json
import math
import os
from pathlib import Path

from replay_fall_baseline import SOURCE, sha, verify_freeze, write_json
from replay_leg_change import canonical, tracking_snapshot
from review_fall_annotations import box_at, require
from review_fall_timing import decode_frames
from score_fall_baseline import anchors, normalized_box, validate_rows
from homecam_detector.fall_candidate import (
    BODY_NAMES, FallCandidateConfig, FallCandidateDetector, extract_pose_features,
)
from homecam_detector.pose import PersonPose, PoseKeypoint


def geometry_checks(value, size, cfg):
    pose = PersonPose(value['boxConfidence'], tuple(
        value['box'][k] for k in ('left', 'top', 'right', 'bottom')),
        tuple(PoseKeypoint(**k) for k in value['keypoints']), value['visibleKeypoints'])
    feature = asdict(extract_pose_features(pose, size, {}, cfg))
    # Match the implementation's valid, named body joints, not facial landmarks.
    points = {}
    for p in pose.keypoints:
        if (p.name in BODY_NAMES and p.confidence >= cfg.keypoint_threshold
                and all(math.isfinite(v) and 0 <= v <= 1 for v in (p.x, p.y, p.confidence))):
            if p.name not in points or p.confidence > points[p.name].confidence:
                points[p.name] = p
    shoulders = [p for p in points.values() if p.name.endswith('_shoulder')]
    hips = [p for p in points.values() if p.name.endswith('_hip')]
    torso_length = None
    if shoulders and hips:
        def center(items):
            return (sum(p.x for p in items)/len(items)*size[0]/size[1],
                    sum(p.y for p in items)/len(items))
        torso_length = math.dist(center(shoulders), center(hips))
    minimum_length = max(.015, .1*feature['box_height'])
    quality = dict(enough_body_joints=len(points) >= cfg.minimum_body_points,
                   shoulder_present=bool(shoulders), hip_present=bool(hips),
                   torso_length_sufficient=torso_length is not None and
                   torso_length >= minimum_length)
    horizontal = dict(
        usable_pose=feature['usable'],
        torso_angle=feature['torso_angle_deg'] is not None and
        feature['torso_angle_deg'] >= cfg.horizontal_torso_deg,
        box_aspect=feature['box_aspect'] >= cfg.minimum_box_aspect,
        body_axis=feature['body_axis_angle_deg'] is None or
        feature['body_axis_angle_deg'] >= cfg.horizontal_torso_deg)
    compact = dict(
        usable_pose=feature['usable'],
        enough_body_joints=len(points) >= cfg.compact_minimum_body_points,
        both_shoulders_and_hips=all(f'{side}_{j}' in points
                                    for side in ('left', 'right') for j in ('shoulder', 'hip')),
        leg_joint_present=any(n.endswith(('_knee', '_ankle')) for n in points),
        box_aspect=feature['box_aspect'] >= cfg.compact_box_aspect,
        body_spread=feature['body_spread_aspect'] is not None and
        feature['body_spread_aspect'] >= cfg.compact_body_aspect,
        vertical_fraction=feature['body_vertical_fraction'] is not None and
        feature['body_vertical_fraction'] <= cfg.compact_max_vertical_fraction)
    require(all(quality.values()) == feature['usable'], 'quality explanation mismatch')
    require(all(horizontal.values()) == feature['horizontal'], 'horizontal explanation mismatch')
    require(all(compact.values()) == feature['compact_body'], 'compact explanation mismatch')
    return dict(features=feature, reliable_body_joint_names=sorted(points),
                quality_failures=[k for k, v in quality.items() if not v],
                horizontal_failures=[k for k, v in horizontal.items() if not v],
                compact_failures=[k for k, v in compact.items() if not v],
                torso_length=torso_length, minimum_torso_length=minimum_length,
                shoulder_hip_confidences={
                    p.name: p.confidence for p in pose.keypoints
                    if p.name.endswith(('_shoulder', '_hip'))})


def render(case, meta, rows, source, selected, output):
    """All sampled frames, plus selected original/zoomed pairs for close inspection."""
    from PIL import Image, ImageDraw, ImageFont
    frames = decode_frames(source, meta)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    w, h = meta['width'], meta['height']
    lookup = {r['frame_index']: r for r in rows}
    # Label-based crop is used only for human review, NEVER as a detector input.
    boxes = [b[1:] for p in case['persons'] for b in p['boxes']]
    crop = (max(0, min(b[0] for b in boxes)-40), max(0, min(b[1] for b in boxes)-30),
            min(w, max(b[2] for b in boxes)+40), min(h, max(b[3] for b in boxes)+30))

    def tile(frame_index):
        r = lookup[frame_index]
        image = frames[frame_index].copy()
        draw = ImageDraw.Draw(image)
        for person in case['persons']:
            gt = box_at(person, frame_index)
            if gt:
                draw.rectangle(gt, outline='#38e37c', width=2)
                draw.text((gt[0], max(0, gt[1]-19)), 'GT '+person['person_id'],
                          font=font, fill='#38e37c', stroke_width=1, stroke_fill='black')
        for o in r['observations']:
            pred = normalized_box(o, meta)
            color = '#2ad8ff' if o['track_id'] else '#ffaf40'
            tag = o['track_id'].rsplit('-', 1)[-1] if o['track_id'] else '?'
            draw.rectangle(pred, outline=color, width=2)
            draw.text((pred[0], pred[1]), f'ID{tag} {o["pose"]["boxConfidence"]:.2f}',
                      font=font, fill=color, stroke_width=1, stroke_fill='black')
            for p in o['pose']['keypoints']:
                if p['confidence'] >= .5:
                    x, y = p['x']*w, p['y']*h
                    draw.ellipse((x-2, y-2, x+2, y+2), fill=color)
        return image

    sampled = sorted(lookup)
    pages = [(f'all-{i//6+1}', sampled[i:i+6], False) for i in range(0, len(sampled), 6)]
    pages += [('key', selected, False)]
    pages += [(f'detail-{i//2+1}', selected[i:i+2], True)
              for i in range(0, len(selected), 2)]
    for label, indices, zoom in pages:
        header = 75
        panels = [(f, z) for f in indices for z in ([False, True] if zoom else [False])]
        sheet = Image.new('RGB', (2*w, math.ceil(len(panels)/2)*(h+header)), '#171b22')
        draw = ImageDraw.Draw(sheet)
        for i, (f, is_crop) in enumerate(panels):
            image = tile(f)
            x, y = i % 2*w, i//2*(h+header)
            if is_crop:
                image = image.crop(crop)
                image.thumbnail((w, h))
                scale = min(w/image.width, h/image.height)
                image = image.resize((round(image.width*scale), round(image.height*scale)))
            sheet.paste(image, (x, y+header))
            r = lookup[f]
            usable = sum(o['features']['usable'] for o in r['observations'])
            lines = [f'{case["case_id"]} f{f} / {f/meta["fps"]:.3f}s',
                     f'Boxes={len(r["observations"])} usable-geometry={usable}; NOT person count',
                     'Review zoom ONLY; model input unchanged' if is_crop else
                     'Green: exact GT only / cyan: ID / orange: unassigned']
            for n, line in enumerate(lines):
                draw.text((x+6, y+3+23*n), line, font=font, fill='white')
        sheet.save(output / f'{case["case_id"]}-{label}.jpg', quality=92)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', required=True)
    parser.add_argument('--frames', nargs='+', type=int, required=True)
    args = parser.parse_args()
    freeze = verify_freeze(args.frozen)
    m = json.loads((args.run / 'run.json').read_text())
    c = json.loads((args.run / 'completed.json').read_text())
    for name, key in [('frames.jsonl', 'frames_sha256'), ('run.json', 'run_sha256')]:
        require(sha(args.run / name) == c[key], f'changed {name}')
    require(m['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
    for name, digest in m['source_sha256'].items():
        require(sha(SOURCE / name) == digest, f'changed detector source: {name}')
    media = json.loads((args.frozen / 'media.json').read_text())
    bundle = json.loads((args.frozen / 'evaluation_labels.json').read_text())
    meta, = [r for r in media['cases'] if r['case_id'] == args.case]
    case, = [r for r in bundle['annotations']['cases'] if r['case_id'] == args.case]
    all_rows = [json.loads(line) for line in (args.run / 'frames.jsonl').read_text().splitlines()]
    validate_rows(all_rows, media, m)
    rows = [r for r in all_rows if r['case_id'] == args.case]
    require(set(args.frames) <= {r['frame_index'] for r in rows}, 'unsampled review frame')
    cfg = FallCandidateConfig(**m['fall_config']['upstream']['baseline'])
    detector = FallCandidateDetector(cfg)
    records, counts = [], Counter()
    size = meta['width'], meta['height']
    for row in rows:
        replay_row = dict(row, fall_analysis=row['baseline_analysis'])
        actual = detector.update(tracking_snapshot(replay_row), capture_time=row['timestamp_s'],
                                 image_size=size, robot_motion='unknown')
        require(canonical(actual) == canonical(row['baseline_analysis']),
                'baseline replay mismatch')
        observations = []
        for o in row['observations']:
            check = geometry_checks(o['pose'], size, cfg)
            require(json.dumps(check['features'], sort_keys=True) ==
                    json.dumps(o['features'], sort_keys=True), 'geometry cache mismatch')
            observations.append(dict(observation_index=o['observation_index'],
                                     track_id=o['track_id'], box=o['pose']['box'], **check))
        counts.update(frames=1, frames_with_any_pose=bool(observations),
                      frames_with_any_usable_geometry=any(
                          o['features']['usable'] for o in observations),
                      frames_with_unassigned=any(o['track_id'] is None for o in observations),
                      actual_requests=len(row['fall_analysis']['candidates']))
        records.append(dict(frame_index=row['frame_index'], timestamp_s=row['timestamp_s'],
                            observations=observations, baseline_diagnostics=actual['tracks'],
                            pose_loss_diagnostics=row['experiment_details']))
    source = (args.dataset / meta['source_path']).resolve()
    require(source.is_relative_to(args.dataset.resolve()), 'source escape')
    require(sha(source) == meta['sha256'], 'source video changed')
    args.output.mkdir(mode=0o700, exist_ok=False)
    write_json(args.output / 'diagnostic.json', dict(
        case_id=args.case, scope='read-only cached inference diagnosis; not a detector change',
        warning='Whole-frame Pose counts do NOT identify the target. RGB review is separate.',
        input_frames_sha256=c['frames_sha256'], run_sha256=c['run_sha256'],
        freeze_sha256=m['freeze_sha256'], video_sha256=meta['sha256'],
        script_sha256=sha(Path(__file__)), config=asdict(cfg),
        counts=dict(counts), baseline_parity_frames=len(rows),
        sparse_target_anchors=anchors(case, rows, meta, freeze['match']), frames=records))
    render(case, meta, rows, source, args.frames, args.output)
    write_json(args.output / 'completed.json', dict(
        files={p.name: sha(p) for p in sorted(args.output.iterdir())},
        review_frames=args.frames, source_rgb_decoded_frames=meta['frames']))
    print(json.dumps(dict(case_id=args.case, **counts)))


if __name__ == '__main__':
    main()
