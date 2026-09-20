#!/usr/bin/env python3
"""Render every dispatched request against RGB and immutable, sparse approved GT.

Association requires visual review. Track IDs and sparse matches do not establish
person identity. This script does not infer boxes, time labels or fall truth.
"""
import argparse
import html
import json
import os
from pathlib import Path

from replay_fall_baseline import sha, write_json
from replay_pose_retention import verify_overlay
from review_fall_annotations import box_at, require
from review_pose_detection_comparison import display_name
from score_fall_baseline import normalized_box


def read(path):
    return json.loads(path.read_text())


def prepare(args):
    verify_overlay(args.spatial_final, args.parent_frozen)
    marker = read(args.run/'completed.json')
    require(sha(args.run/'summary.json') == marker['summary_sha256'], 'changed summary')
    require(read(args.run/'summary.json')['spatial_freeze_sha256'] ==
            sha(args.spatial_final/'freeze.json'), 'wrong approved boxes')
    arm = args.run/args.arm
    require(sha(arm/'completed.json') == marker['arms'][args.arm], 'changed arm')
    for name, digest in read(arm/'completed.json')['files'].items():
        require(Path(name).name == name and sha(arm/name) == digest, 'changed artifact')
    bundle = read(args.spatial_final/'evaluation_labels.json')
    ann = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    metas = {m['case_id']: m for m in read(args.spatial_final/'media.json')['cases']}
    rows = {}
    for r in map(json.loads, (arm/'frames.jsonl').read_text().splitlines()):
        rows.setdefault(r['case_id'], {})[r['frame_index']] = r
    events = []
    for case in read(arm/'cases.json'):
        cid = case['case_id']
        for request in case['requests']:
            origin = request['origin']
            candidate = origin['candidate']
            evidence_frame = candidate.get('evidenceReference', {}).get(
                'frameIndex', origin['frame_index'])
            lookup = rows[cid]
            start = min(lookup, key=lambda f: abs(
                lookup[f]['timestamp_s']-candidate['evidenceStartSec']))
            reviewed = sorted({b[0] for p in ann[cid]['persons'] for b in p['boxes']})
            nearby = min(reviewed, key=lambda f: abs(f-evidence_frame)) if reviewed else None
            evidence_row = lookup[evidence_frame]
            observations = [o for o in evidence_row['observations']
                            if o['track_id'] == candidate['targetTrackId']]
            require(len(observations) == 1, 'missing/ambiguous actual evidence observation')
            events.append(dict(
                index=len(events)+1, case_id=cid, name=display_name(labels[cid]),
                label=labels[cid]['label'], request=request,
                selected_frames=[start, evidence_frame,
                                 nearby if nearby is not None else evidence_frame],
                exact_gt_frame=nearby, actual_evidence_frame=evidence_frame,
                source_sha256=metas[cid]['sha256'],
                onset_frames=ann[cid].get('onset_frames'),
                first_down_frames=ann[cid].get('first_down_frames'),
                temporal_annotations_available=ann[cid].get('temporal_annotations_available'),
                target_person_id=ann[cid].get('target_person_id'),
                review_status='pending', association='unknown', review_note=''))
    return ann, metas, rows, events


def render(args, ann, metas, rows, events):
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
    cell_w, cell_h = 480, 355
    pages = []
    for offset in range(0, len(events), 5):
        group = events[offset:offset+5]
        sheet = Image.new('RGB', (3*cell_w, len(group)*cell_h), '#121923')
        draw = ImageDraw.Draw(sheet)
        for line, event in enumerate(group):
            cid = event['case_id']
            meta = metas[cid]
            path = args.parent_frozen/meta['source_path']
            require(sha(path) == meta['sha256'], 'changed media')
            cap = cv2.VideoCapture(str(path))
            candidate = event['request']['origin']['candidate']
            tid = candidate['targetTrackId']
            try:
                for col, frame in enumerate(event['selected_frames']):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
                    ok, bgr = cap.read()
                    require(ok, 'decode failure')
                    tile = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
                    marks = ImageDraw.Draw(tile)
                    for p in ann[cid]['persons']:
                        box = box_at(p, frame)
                        if box is not None:
                            marks.rectangle(box, outline='#42ec81', width=3)
                            marks.text((box[0], box[1]), 'GT '+p['person_id'], fill='#42ec81',
                                       font=font, stroke_width=1, stroke_fill='black')
                    for o in rows[cid].get(frame, {}).get('observations', []):
                        if o['track_id'] != tid:
                            continue
                        box = normalized_box(o, meta)
                        marks.rectangle(box, outline='#22caff', width=3)
                        for point in o['pose']['keypoints']:
                            if point['confidence'] >= .5:
                                x, y = point['x']*meta['width'], point['y']*meta['height']
                                marks.ellipse((x-3, y-3, x+3, y+3), fill='#ffcc55')
                    tile.thumbnail((cell_w-4, 278))
                    x, y = col*cell_w, line*cell_h
                    sheet.paste(tile, (x+(cell_w-tile.width)//2, y+73))
                    captions = [f'{event["index"]:02d} {event["name"]} {event["label"]}',
                                f'f{frame:03d} ID{tid.rsplit("-", 1)[-1]} '
                                f'emit={event["request"]["dispatch_time_s"]:.3f}s',
                                ('evidence start', 'actual request evidence',
                                 'nearest reviewed GT; no interpolation')[col]]
                    for n, text in enumerate(captions):
                        draw.text((x+4, y+4+n*22), text, font=font, fill='white')
            finally:
                cap.release()
        name = f'page-{offset//5+1:02d}.jpg'
        sheet.save(args.output/name, quality=95)
        pages.append(name)
    title = f'{args.arm}: all {len(events)} requests / association review'
    page = ('<!doctype html><meta charset="utf-8"><title>'+html.escape(title) +
            '</title><style>body{background:#121923;color:white;font:18px sans-serif;'
            'max-width:1440px;margin:24px auto}img{width:100%}</style><h1>'+title +
            '</h1><p>Green: exact approved GT only. Blue: requesting track. '
            'Sparse annotations do not prove identity between frames. '
            'Pending non-blind assistant review, not new human ground truth.</p>'+''.join(
                f'<a href="{p}"><img src="{p}" loading="lazy"></a>' for p in pages))
    (args.output/'index.html').write_text(page)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'spatial-final', 'parent-frozen', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--arm', default='s_letterbox')
    args = parser.parse_args()
    require(not args.output.exists(), 'output exists')
    ann, metas, rows, events = prepare(args)
    args.output.mkdir(mode=0o700)
    render(args, ann, metas, rows, events)
    write_json(args.output/'events.json', dict(
        method='pending_nonblind_assistant_RGB_association_review',
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
        source_arm_marker_sha256=sha(args.run/args.arm/'completed.json'),
        script_sha256=sha(Path(__file__)), events=events))
    write_json(args.output/'completed.json', dict(files={
        p.name: sha(p) for p in args.output.iterdir()}))
    print('AUDIT_READY', len(events), args.output, flush=True)


if __name__ == '__main__':
    main()
