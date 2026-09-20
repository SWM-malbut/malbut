#!/usr/bin/env python3
"""Audit completed RGB-only misses; never run inference or change frozen labels.

Frame counts describe observations anywhere in a frame, not target recall.
Exact, sparse target-box matches are reported separately. No GT interpolation.
"""
import argparse
from collections import Counter
import copy
import json
import math
import os
from pathlib import Path

from diagnose_fall_misses import geometry_checks
from replay_fall_baseline import SOURCE, sha, verify_freeze, write_json
from replay_leg_change import canonical, tracking_snapshot
from replay_pose_stability import read_run
from review_fall_annotations import box_at, require
from review_fall_timing import decode_frames
from score_fall_baseline import anchors, events, normalized_box
from homecam_detector.fall_candidate import FallCandidateConfig, FallCandidateDetector


def window(annotation):
    """Use the last edge of a reviewed boundary; never invent a landing time."""
    for field in ('first_down_frames', 'onset_frames'):
        interval = annotation.get(field)
        if interval is not None:
            require(len(interval) == 2 and all(type(x) is int for x in interval)
                    and 0 <= interval[0] <= interval[1], 'invalid timing interval')
            return dict(start_frame=interval[1], basis=field,
                        source_interval=list(interval))
    return dict(start_frame=0, basis='whole_clip_no_reviewed_boundary',
                source_interval=None)


def low(feature):
    return bool(feature['usable'] and (feature['horizontal'] or feature['compact_body']))


def pattern(counts):
    """Disjoint observed bottlenecks, NOT exclusive physical causes."""
    if not counts['frames']:
        return 'no_samples_in_window'
    if not counts['any_pose']:
        return 'no_pose_in_window'
    if not counts['usable_pose']:
        return 'no_usable_pose_in_window'
    if not counts['low_pose']:
        return 'usable_but_no_low_pose'
    return 'low_pose_seen_no_request'


def frame_counts(rows):
    return dict(frames=len(rows),
                any_pose=sum(bool(r['observations']) for r in rows),
                usable_pose=sum(any(o['features']['usable'] for o in r['observations'])
                                for r in rows),
                low_pose=sum(any(low(o['features']) for o in r['observations']) for r in rows),
                unassigned_pose=sum(any(o['track_id'] is None for o in r['observations'])
                                    for r in rows),
                requests=sum(len(r['fall_analysis']['candidates']) for r in rows))


def low_runs(rows, maximum_gap):
    """Actual per-ID contiguous low samples, broken by missing/poor/other posture.

    This diagnostic does not grant target identity to an ID, nor bridge gaps.
    It is not the anchor-sensitive pose-loss/disagreement experiment state.
    """
    active, completed = {}, []
    for row in rows:
        present = {o['track_id']: o for o in row['observations']
                   if o['track_id'] is not None}
        for tid in list(active):
            obs = present.get(tid)
            reason = ('missing_or_unassigned' if obs is None else
                      'insufficient_pose' if not obs['features']['usable'] else
                      'other_posture' if not low(obs['features']) else
                      'sample_gap' if row['timestamp_s'] - active[tid][-1][1]
                      > maximum_gap else None)
            if reason:
                completed.append((tid, active.pop(tid), reason, row['frame_index']))
        for tid, obs in present.items():
            if low(obs['features']):
                active.setdefault(tid, []).append((row['frame_index'], row['timestamp_s']))
    completed.extend((tid, points, 'clip_end', None) for tid, points in active.items())
    return [dict(track_id=tid, frames=[p[0] for p in points], samples=len(points),
                 start_s=points[0][1], end_s=points[-1][1],
                 span_s=points[-1][1] - points[0][1],
                 ended_by=reason, ended_at_frame=end)
            for tid, points, reason, end in completed]


def inspect_case(case, annotation, meta, rows, match, config):
    selected_window = window(annotation)
    selected = [r for r in rows if r['frame_index'] >= selected_window['start_frame']]
    cfg = FallCandidateConfig(**config)
    quality, horizontal, compact = Counter(), Counter(), Counter()
    detailed = []
    for row in rows:
        observations = []
        for obs in row['observations']:
            check = geometry_checks(obs['pose'], (meta['width'], meta['height']), cfg)
            require(json.dumps(check['features'], sort_keys=True) ==
                    json.dumps(obs['features'], sort_keys=True), 'changed cached geometry')
            if row['frame_index'] >= selected_window['start_frame']:
                quality.update(check['quality_failures'])
                if check['features']['usable']:
                    horizontal.update(check['horizontal_failures'])
                    compact.update(check['compact_failures'])
            observations.append(dict(observation_index=obs['observation_index'],
                                     track_id=obs['track_id'], **check))
        detailed.append(dict(frame_index=row['frame_index'], timestamp_s=row['timestamp_s'],
                             in_window=row['frame_index'] >= selected_window['start_frame'],
                             observations=observations,
                             baseline_tracks=row['baseline_analysis']['tracks'],
                             pose_loss=row['experiment_details'],
                             disagreement=row['stability_details']))
    exact = anchors(annotation, rows, meta, match)
    target = [a for a in exact if a['role'] == 'target' and
              a['frame_index'] >= selected_window['start_frame']]
    counts = frame_counts(selected)
    return dict(case_id=case['case_id'], review_case_id=case.get('review_case_id'),
                original_source=case.get('original_source_path'), label=case['label'],
                window=selected_window, whole_clip=frame_counts(rows), in_window=counts,
                observation_pattern=pattern(counts),
                target_anchors=dict(count=len(target),
                                    statuses=dict(Counter(a['status'] for a in target)),
                                    matched_usable=sum(a['usable'] for a in target)),
                exact_anchors=exact, low_runs=low_runs(rows, cfg.max_frame_gap_sec),
                quality_failures=dict(quality), horizontal_failures=dict(horizontal),
                compact_failures=dict(compact), frames=detailed,
                interpretation='observation-stage evidence; not an exclusive root cause; '
                               'whole-frame observations are not target-verified')


def render_sheet(path, meta, annotation, rows, report, output):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 14)
    frames = decode_frames(path, meta)
    width, height, header = 384, 240, 55
    sheet = Image.new('RGB', (4 * width, 75 + math.ceil(len(rows) / 4) * (height + header)),
                      '#161a21')
    draw = ImageDraw.Draw(sheet)
    title = f'{report["case_id"]} / {report["review_case_id"]} / {report["label"]}'
    draw.text((8, 6), title, font=font, fill='white')
    draw.text((8, 28), 'Green: exact reviewed box. Blue: predicted box/joints >= .5. '
              'ID is NOT proof of a person.', font=font, fill='white')
    draw.text((8, 50), 'U=usable geometry; L=low-pose rule. All actual sampled frames; '
              'not continuous video.', font=font, fill='white')
    for i, row in enumerate(rows):
        raw = frames[row['frame_index']].copy()
        rd = ImageDraw.Draw(raw)
        lw = max(2, meta['width'] // 320)
        for person in annotation['persons']:
            gt = box_at(person, row['frame_index'])
            if gt:
                rd.rectangle(gt, outline='#39eb73', width=lw)
        for obs in row['observations']:
            rd.rectangle(normalized_box(obs, meta), outline='#25cbff', width=lw)
            for kp in obs['pose']['keypoints']:
                if kp['confidence'] >= .5:
                    x, y = kp['x'] * meta['width'], kp['y'] * meta['height']
                    rd.ellipse((x - lw, y - lw, x + lw, y + lw), fill='#ffff24')
        raw.thumbnail((width, height))
        x, y = (i % 4) * width, 75 + (i // 4) * (height + header)
        sheet.paste(raw, (x, y + header))
        draw.text((x + 5, y + 2), f'f{row["frame_index"]:03d} {row["timestamp_s"]:.3f}s '
                  f'boxes={len(row["observations"])}', font=font, fill='white')
        tags = [f'{str(o["track_id"]).rsplit("-", 1)[-1]}:'
                f'{o["pose"]["boxConfidence"]:.2f}'
                f'{" U" if o["features"]["usable"] else " -"}'
                f'{" L" if low(o["features"]) else ""}' for o in row['observations']]
        draw.text((x + 5, y + 22), ' / '.join(tags)[:58], font=font, fill='#9ddeee')
    sheet.save(output, quality=91)


def run(args):
    os.umask(0o077)
    require(not args.output.exists(), 'output exists; no overwrite')
    freeze = verify_freeze(args.frozen)
    complete, meta, rows = read_run(args.run, args.frozen)
    required = {args.run / 'frames.jsonl': complete['frames_sha256'],
                args.run / 'run.json': complete['run_sha256'],
                args.run / 'completed.json': sha(args.run / 'completed.json'),
                args.frozen / 'freeze.json': sha(args.frozen / 'freeze.json')}
    for key in ('source_sha256', 'experiment_source_sha256', 'stability_source_sha256'):
        for name, digest in meta.get(key, {}).items():
            p = SOURCE / name if key == 'source_sha256' else Path(name)
            required[p] = digest
    for name in ('audit_fall84_misses.py', 'diagnose_fall_misses.py',
                 'score_fall_baseline.py', 'replay_leg_change.py',
                 'replay_pose_stability.py', 'review_fall_timing.py',
                 'review_fall_annotations.py', 'replay_fall_baseline.py'):
        p = Path(__file__).with_name(name)
        required[p] = sha(p)
    for p, digest in required.items():
        require(sha(p) == digest, 'changed input/source: ' + str(p))
    require(meta['robot_motion'] == 'unknown' and meta['depth'] is None, 'not RGB-only')
    require(meta['fall_config']['strategy'] == 'disagreement_request', 'not current gate')
    bundle = json.loads((args.frozen / 'evaluation_labels.json').read_text())
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    metas = {c['case_id']: c for c in json.loads((args.frozen / 'media.json').read_text())['cases']}
    hits = {e['case_id'] for e in events(rows)}
    missed = sorted(cid for cid, c in labels.items()
                    if c['label'] != 'normal_activity' and cid not in hits)
    cfg = FallCandidateConfig(**meta['fall_config']['upstream']['baseline'])
    reports = []
    # Verify all baseline states, including non-missed videos, before reporting.
    by_case = {cid: [] for cid in labels}
    for row in rows:
        by_case[row['case_id']].append(row)
    for cid, case_rows in by_case.items():
        detector = FallCandidateDetector(cfg)
        for row in case_rows:
            base_row = dict(row, fall_analysis=row['baseline_analysis'])
            actual = detector.update(tracking_snapshot(base_row), capture_time=row['timestamp_s'],
                                     image_size=(metas[cid]['width'], metas[cid]['height']),
                                     robot_motion='unknown')
            require(canonical(actual) == canonical(row['baseline_analysis']),
                    f'baseline parity failed: {cid}/{row["frame_index"]}')
    args.output.mkdir(mode=0o700)
    (args.output / 'sheets').mkdir(mode=0o700)
    for cid in missed:
        report = inspect_case(labels[cid], annotations[cid], metas[cid], by_case[cid],
                              freeze['match'], meta['fall_config']['upstream']['baseline'])
        write_json(args.output / (cid + '.json'), report)
        reports.append({k: v for k, v in report.items() if k != 'frames'})
        path = (args.frozen / metas[cid]['source_path']).resolve()
        require(path.is_relative_to(args.frozen.resolve()), 'media escape')
        require(sha(path) == metas[cid]['sha256'], 'changed media: ' + cid)
        required[path] = metas[cid]['sha256']
        render_sheet(path, metas[cid], annotations[cid], by_case[cid], report,
                     args.output / 'sheets' / (cid + '.jpg'))
        print(cid + ' ' + report['observation_pattern'], flush=True)
    summary = dict(
        scope='cached actual Pose + original RGB review; no new inference or detector change',
        rows=len(rows), cases=len(labels), requested_cases=len(hits), missed_positive=len(missed),
        groups={label: dict(total=sum(c['label'] == label for c in labels.values()),
                            requested=sum(labels[cid]['label'] == label for cid in hits))
                for label in sorted({c['label'] for c in labels.values()})},
        patterns=dict(Counter(r['observation_pattern'] for r in reports)),
        caveat='Patterns use the defined window and any prediction, not proven target identity. '
               'Different underlying causes may coexist. No full-trajectory GT is invented.',
        baseline_parity_frames=len(rows), cases_detail=reports,
        input_sha256={str(p): h for p, h in required.items()})
    write_json(args.output / 'summary.json', summary)
    for p, digest in required.items():
        require(sha(p) == digest, 'changed during audit: ' + str(p))
    verify_freeze(args.frozen)
    write_json(args.output / 'completed.json', dict(
        files={str(p.relative_to(args.output)): sha(p)
               for p in sorted(args.output.rglob('*')) if p.is_file()},
        baseline_parity_frames=len(rows), missed_positive=len(missed)))
    print(json.dumps(summary['patterns'], sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'run', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())
