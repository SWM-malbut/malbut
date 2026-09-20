#!/usr/bin/env python3
"""Render hash-bound actual candidate evidence for non-blind RGB association review.

Sparse box matches are hints, never automatic target ground truth. No interpolation,
inference, provider calls, or writes to annotations/the source evaluation run.
"""
import argparse
import json
import math
import os
from pathlib import Path

from replay_fall_baseline import sha, verify_freeze, write_json
from review_fall_annotations import box_at, require
from score_fall_baseline import anchors, events, normalized_box, validate_rows


def prepare(frozen, run):
    freeze = verify_freeze(frozen)
    complete = json.loads((run/'completed.json').read_text())
    for name, key in [('frames.jsonl','frames_sha256'), ('run.json','run_sha256')]:
        require(sha(run/name) == complete[key], 'run changed: '+name)
    metadata = json.loads((run/'run.json').read_text())
    require(metadata['freeze_sha256'] == sha(frozen/'freeze.json'), 'wrong freeze')
    media = json.loads((frozen/'media.json').read_text())
    bundle = json.loads((frozen/'evaluation_labels.json').read_text())
    rows = [json.loads(s) for s in (run/'frames.jsonl').read_text().splitlines()]
    validate_rows(rows, media, metadata)
    cases = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    metas = {c['case_id']: c for c in media['cases']}
    by_case = {cid: [r for r in rows if r['case_id'] == cid] for cid in metas}
    reviewed = []
    for event in events(rows):
        cid = event['case_id']
        aa = anchors(cases[cid], by_case[cid], metas[cid], freeze['match'])
        tid = event['candidate']['targetTrackId']
        support = [a for a in aa if a['track_id'] == tid]
        reviewed.append(dict(event, label=labels[cid]['label'],
                             review_case_id=labels[cid].get('review_case_id'),
                             original_source=labels[cid].get('original_source_path'),
                             sparse_support=support,
                             association='unknown', note='RGB review not yet completed'))
    return complete, metas, cases, by_case, reviewed


def render(frozen, output, metas, cases, rows, records):
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    w, h, header = 440, 250, 66
    for page in range(math.ceil(len(records)/5)):
        group = records[page*5:(page+1)*5]
        sheet = Image.new('RGB', (w*3, len(group)*(h+header)), '#171b22')
        title = ImageDraw.Draw(sheet)
        for line, event in enumerate(group):
            cid = event['case_id']
            meta, case = metas[cid], cases[cid]
            video = frozen/meta['source_path']
            require(sha(video) == meta['sha256'], 'video changed')
            lookup = {r['frame_index']: r for r in rows[cid]}
            ev_frame = event.get('evidence_frame_index', event['frame_index'])
            start = event['candidate']['evidenceStartSec']
            start_frame = min(lookup, key=lambda f: abs(f/meta['fps']-start))
            annotated = sorted({b[0] for p in case['persons'] for b in p['boxes']})
            nearby = min(annotated, key=lambda f: abs(f-ev_frame)) if annotated else event['frame_index']
            selected = [start_frame, ev_frame, nearby]
            cap = cv2.VideoCapture(str(video))
            try:
                for col, frame_index in enumerate(selected):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                    ok, frame = cap.read()
                    require(ok, 'decode failed')
                    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    draw = ImageDraw.Draw(image)
                    for p in case['persons']:
                        gt = box_at(p, frame_index)
                        if gt:
                            draw.rectangle(gt, outline='#38e37c', width=3)
                            draw.text((gt[0],max(0,gt[1]-20)), 'GT '+p['person_id']+' '+p['role'],
                                      font=font, fill='#38e37c', stroke_width=1, stroke_fill='black')
                    row = lookup.get(frame_index)
                    if row:
                        for o in row['observations']:
                            if o['track_id'] != event['candidate']['targetTrackId']:
                                continue
                            pred = normalized_box(o, meta)
                            draw.rectangle(pred, outline='#21cfff', width=4)
                            for k in o['pose']['keypoints']:
                                if k['confidence'] >= .5:
                                    x,y=k['x']*meta['width'],k['y']*meta['height']
                                    draw.ellipse((x-3,y-3,x+3,y+3),fill='#ff6060')
                    image.thumbnail((w-4,h))
                    x,y=col*w,line*(h+header)
                    sheet.paste(image,(x,y+header))
                    caption=[f'{page*5+line+1}: {cid} {event["review_case_id"]}',
                             f'f{frame_index} / emit f{event["frame_index"]} / {event["label"]}',
                             ['evidence start','last/current evidence','nearest exact GT (no interpolation)'][col]]
                    for n,text in enumerate(caption):
                        title.text((x+3,y+3+n*20),text,font=font,fill='white')
            finally:
                cap.release()
        sheet.save(output/f'page-{page+1:02d}.jpg', quality=92)


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('frozen','run','output'):
        p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args()
    complete,metas,cases,rows,records=prepare(args.frozen,args.run)
    args.output.mkdir(mode=0o700,exist_ok=False)
    write_json(args.output/'events.json', dict(
        method='pending_RGB_association_review',frames_sha256=complete['frames_sha256'],
        freeze_sha256=sha(args.frozen/'freeze.json'),script_sha256=sha(Path(__file__)),
        warning='Sparse support does not prove identity between labelled frames.', events=records))
    render(args.frozen,args.output,metas,cases,rows,records)
    write_json(args.output/'completed.json',dict(files={p.name:sha(p) for p in args.output.iterdir()}))
    print('REVIEW_READY',len(records),args.output)


if __name__ == '__main__':
    main()
