#!/usr/bin/env python3
"""Render actual RGB/Pose with before/after request counts; no invented missing boxes."""
import argparse
import json
import math
import os
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont

from replay_fall_baseline import sha, verify_freeze, write_json
from review_fall_annotations import require


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
    verify_freeze(args.frozen)
    metadata = json.loads((args.run / 'run.json').read_text())
    complete = json.loads((args.run / 'completed.json').read_text())
    require(sha(args.run / 'run.json') == complete['run_sha256'], 'changed run')
    require(sha(args.run / 'frames.jsonl') == complete['frames_sha256'], 'changed frames')
    require(metadata['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
    media = json.loads((args.frozen / 'media.json').read_text())
    meta, = [m for m in media['cases'] if m['case_id'] == args.case]
    source = (args.dataset / meta['source_path']).resolve()
    require(source.is_relative_to(args.dataset.resolve()), 'source path escape')
    require(sha(source) == meta['sha256'], 'video hash mismatch')
    rows = [json.loads(line) for line in (args.run / 'frames.jsonl').read_text().splitlines()]
    rows = [r for r in rows if r['case_id'] == args.case]
    lookup = {r['frame_index']: r for r in rows}
    require(len(set(args.frames)) == len(args.frames) and
            all(f in lookup for f in args.frames), 'invalid/duplicate source frames')
    args.output.mkdir(mode=0o700, exist_ok=False)
    w, h = meta['width'], meta['height']
    header = 80
    sheet = Image.new('RGB', (2*w, math.ceil(len(args.frames)/2)*(h+header)), '#171b22')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    cap = cv2.VideoCapture(str(source))
    try:
        for i, f in enumerate(args.frames):
            row = lookup[f]
            cap.set(cv2.CAP_PROP_POS_FRAMES, f)
            ok, frame = cap.read()
            require(ok, 'could not decode source frame')
            tile = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            marks = ImageDraw.Draw(tile)
            for o in row['observations']:
                tid = o['track_id'].rsplit('-', 1)[-1] if o['track_id'] else '?'
                color = '#35d9fa' if tid == '1' else '#ffb34b'
                b = o['pose']['box']
                box = b['left']*w, b['top']*h, b['right']*w, b['bottom']*h
                marks.rectangle(box, outline=color, width=2)
                marks.text((box[0], max(0, box[1]-20)),
                           f'ID{tid} score={o["pose"]["boxConfidence"]:.2f}',
                           font=font, fill=color, stroke_width=1, stroke_fill='black')
                for p in o['pose']['keypoints']:
                    if p['confidence'] >= .5:
                        x, y = p['x']*w, p['y']*h
                        marks.ellipse((x-2, y-2, x+2, y+2), fill=color)
            x, y = (i % 2)*w, (i//2)*(h+header)
            sheet.paste(tile, (x, y+header))
            before = sum(len(r['upstream_analysis']['candidates'])
                         for r in rows if r['frame_index'] <= f)
            after = sum(len(r['fall_analysis']['candidates'])
                        for r in rows if r['frame_index'] <= f)
            lines = [f'{args.case} frame {f} / {row["timestamp_s"]:.3f}s',
                     f'Requests so far: before {before} / after {after}']
            merged = [u for u in row['verification_updates'] if 'deduplication' in u]
            if merged:
                c = merged[0]['evidence']
                tid = c['targetTrackId'].rsplit('-', 1)[-1]
                ref = c.get('evidenceReference')
                lines.append(f'ID{tid}: existing request updated' + (
                    f' / last seen f{ref["frameIndex"]}' if ref else ''))
            else:
                lines.append('Only actual detections drawn; IDs are NOT a person count')
            for n, line in enumerate(lines):
                draw.text((x+6, y+4+n*24), line, font=font, fill='white')
    finally:
        cap.release()
    target = args.output / f'{args.case}-dedup.jpg'
    sheet.save(target, quality=92)
    write_json(args.output / 'review.json', dict(
        frames_sha256=complete['frames_sha256'], video_sha256=meta['sha256'],
        image_sha256=sha(target), renderer_sha256=sha(Path(__file__)),
        case_id=args.case, frames=args.frames,
        note='Actual RGB/Pose overlays; cumulative request counts, no synthetic missing poses'))
    print(target)


if __name__ == '__main__':
    main()
