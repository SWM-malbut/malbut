#!/usr/bin/env python3
"""Render paired actual RGB detections. Crop outline is not a person box."""
import argparse
import json
import os
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont

from replay_fall_baseline import sha, verify_freeze, write_json
from review_fall_annotations import require


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'run', 'dataset', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--case', required=True)
    parser.add_argument('--frames', nargs='+', type=int, required=True)
    args = parser.parse_args()
    verify_freeze(args.frozen)
    require(1 <= len(args.frames) <= 4 and len(set(args.frames)) == len(args.frames),
            'choose 1 to 4 distinct actual sampled frames')
    completed = json.loads((args.run / 'completed.json').read_text())
    rows, hashes = {}, {}
    for branch in ('control', 'roi'):
        directory = args.run / branch
        require(sha(directory / 'completed.json') == completed['branches'][branch],
                'changed branch')
        done = json.loads((directory / 'completed.json').read_text())
        for name, key in [('frames.jsonl', 'frames_sha256'), ('run.json', 'run_sha256')]:
            require(sha(directory / name) == done[key], 'changed run')
        metadata = json.loads((directory / 'run.json').read_text())
        require(metadata['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
        rows[branch] = {r['frame_index']: r for r in map(json.loads,
                        (directory / 'frames.jsonl').read_text().splitlines())
                        if r['case_id'] == args.case}
        hashes[branch] = done['frames_sha256']
        require(all(f in rows[branch] for f in args.frames), 'not an actual sampled frame')
    meta, = [m for m in json.loads((args.frozen / 'media.json').read_text())['cases']
             if m['case_id'] == args.case]
    video = (args.dataset / meta['source_path']).resolve()
    require(video.is_relative_to(args.dataset.resolve()) and sha(video) == meta['sha256'],
            'changed media')
    w, h = meta['width'], meta['height']
    header = 80
    sheet = Image.new('RGB', (2*w, len(args.frames)*(h+header)), '#171b22')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    cap = cv2.VideoCapture(str(video))
    try:
        for panel, frame_index in enumerate(args.frames):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, rgb = cap.read()
            require(ok, 'decode failed')
            for side, branch in enumerate(('control', 'roi')):
                row = rows[branch][frame_index]
                tile = Image.fromarray(cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB))
                marks = ImageDraw.Draw(tile)
                for attempt in row.get('roi_attempts', []):
                    marks.rectangle(attempt['roi'], outline='#ffffff', width=1)
                for index, obs in enumerate(row['observations']):
                    source = (row['observation_provenance'][index]['source']
                              if branch == 'roi' else 'full_frame')
                    color = '#ff75cf' if source == 'roi' else '#36d4ff'
                    b = obs['pose']['box']
                    box = b['left']*w, b['top']*h, b['right']*w, b['bottom']*h
                    marks.rectangle(box, outline=color, width=2)
                    tid = obs['track_id'].rsplit('-', 1)[-1] if obs['track_id'] else '?'
                    low = obs['features']['horizontal'] or obs['features']['compact_body']
                    label = f'ID{tid} {obs["pose"]["boxConfidence"]:.2f}'
                    label += ' LOW' if low else ' U' if obs['features']['usable'] else ' weak'
                    marks.text((box[0], max(0, box[1]-20)), label, fill=color, font=font,
                               stroke_width=1, stroke_fill='black')
                    for point in obs['pose']['keypoints']:
                        if point['confidence'] >= .5:
                            x, y = point['x']*w, point['y']*h
                            marks.ellipse((x-2, y-2, x+2, y+2), fill=color)
                x, y = side*w, panel*(h+header)
                sheet.paste(tile, (x, y+header))
                total = sum(len(r['fall_analysis']['candidates'])
                            for f, r in rows[branch].items() if f <= frame_index)
                lines = [f'{args.case} f{frame_index} {row["timestamp_s"]:.3f}s | {branch}',
                         f'Requests so far {total} | Actual poses {len(row["observations"])}',
                         'Cyan: full / pink: ROI / white: search area (not a person)']
                for line_index, line in enumerate(lines):
                    draw.text((x+5, y+3+line_index*24), line, font=font, fill='white')
    finally:
        cap.release()
    args.output.mkdir(mode=0o700, exist_ok=False)
    target = args.output / f'{args.case}-roi.jpg'
    sheet.save(target, quality=93)
    write_json(args.output / 'review.json', dict(
        frames_sha256=hashes, video_sha256=meta['sha256'], case_id=args.case,
        frames=args.frames, image_sha256=sha(target), renderer_sha256=sha(Path(__file__)),
        note='Same actual RGB, actual detections only; '
             'no inferred GT, old boxes or artificial poses'))
    print(target)


if __name__ == '__main__':
    main()
