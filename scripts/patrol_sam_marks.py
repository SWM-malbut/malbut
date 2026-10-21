#!/usr/bin/env python3
"""Make class-agnostic automatic SAM ViT-B marks for an offline patrol replay."""

import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import torch
from segment_anything import SamAutomaticMaskGenerator, sam_model_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700)
    torch.set_num_threads(4)
    model = sam_model_registry['vit_b'](checkpoint=str(args.checkpoint)).to('cpu')
    generator = SamAutomaticMaskGenerator(
        model, points_per_side=16, points_per_batch=32, pred_iou_thresh=.86,
        stability_score_thresh=.92, crop_n_layers=0, min_mask_region_area=0)
    rows = json.loads((args.source / 'observations.json').read_text())
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 12)
    colors = ['#00ffff', '#ffff00', '#ff80ff', '#80ff80', '#ffad55', '#a0bfff']
    record = dict(model='SAM ViT-B', device='cpu', threads=4, points_per_side=16,
                  points_per_batch=32, pred_iou_thresh=.86, stability_score_thresh=.92,
                  crop_n_layers=0, max_marks=40, minimum_area_pixels=120,
                  maximum_area_fraction=.75, mask_iou_dedup=.85,
                  checkpoint_sha256=hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
                  labels_used=False, frames={})
    total = time.monotonic()
    for n, row in enumerate(rows, 1):
        frame_id = f'F{n:02d}'
        source = args.source / 'memory/captures' / (row['id'] + '.jpg')
        original = Image.open(source).convert('RGB')
        rgb = np.array(original)
        start = time.monotonic()
        with torch.inference_mode():
            masks = generator.generate(rgb)
        selected = []
        for mask in sorted(masks, key=lambda m: m['area'], reverse=True):
            if not 120 <= mask['area'] <= rgb.shape[0]*rgb.shape[1]*.75:
                continue
            seg = mask['segmentation']
            if any(np.logical_and(seg, old['segmentation']).sum() /
                   np.logical_or(seg, old['segmentation']).sum() > .85 for old in selected):
                continue
            selected.append(mask)
            if len(selected) == 40:
                break
        marked = original.copy()
        draw = ImageDraw.Draw(marked)
        marks = []
        for idx, mask in enumerate(selected, 1):
            seg = mask['segmentation']
            # Draw a one-pixel mask boundary; preserve scene colors inside objects.
            inside = seg.copy()
            inside[1:] &= seg[:-1]
            inside[:-1] &= seg[1:]
            inside[:, 1:] &= seg[:, :-1]
            inside[:, :-1] &= seg[:, 1:]
            outline = Image.fromarray(((seg & ~inside)*255).astype('uint8'))
            color = colors[(idx-1) % len(colors)]
            marked.paste(color, (0, 0, *marked.size), outline)
            x, y, w, h = mask['bbox']
            # Pick the mask pixel nearest its centroid, avoiding labels in holes.
            ys, xs = np.nonzero(seg)
            center = np.argmin((xs-xs.mean())**2+(ys-ys.mean())**2)
            tx, ty = int(xs[center]), int(ys[center])
            label = str(idx)
            width = draw.textbbox((0, 0), label, font=font)[2]+4
            tx = min(max(tx-width//2, 0), marked.width-width)
            ty = min(max(ty-8, 0), marked.height-17)
            draw.rectangle((tx, ty, tx+width, ty+17), fill='black', outline=color)
            draw.text((tx+2, ty+1), label, font=font, fill=color)
            bbox = [round(v/d*1000) for v, d in zip(
                [x, y, x+w, y+h], [marked.width, marked.height]*2)]
            marks.append(dict(id=idx, bbox_1000=bbox, area=mask['area'],
                              predicted_iou=float(mask['predicted_iou']),
                              stability_score=float(mask['stability_score'])))
        marked.save(args.output / (frame_id+'.jpg'), quality=90)
        np.savez_compressed(args.output / (frame_id+'.npz'),
                            masks=np.array([m['segmentation'] for m in selected]))
        record['frames'][frame_id] = dict(
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), marks=marks,
            segmentation_seconds=time.monotonic()-start, raw_masks=len(masks))
        (args.output / 'marks.json').write_text(json.dumps(record, indent=2))
        print(frame_id, 'marks', len(marks), 'seconds',
              round(record['frames'][frame_id]['segmentation_seconds'], 2), flush=True)
    record['total_seconds'] = time.monotonic()-total
    (args.output / 'marks.json').write_text(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
