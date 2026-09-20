#!/usr/bin/env python3
"""Actual RGB/model/approved-GT audit; never interpolate boxes or redraw anatomy."""
import argparse
import html
import json
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from replay_fall_baseline import sha, write_json
from replay_pose_retention import verify_overlay
from review_fall_annotations import box_at, require
from review_fall_timing import decode_frames
from score_fall_baseline import normalized_box


def run(args):
    verify_overlay(args.spatial_final, args.parent_frozen)
    summary = json.loads((args.run/'summary.json').read_text())
    complete = json.loads((args.run/'completed.json').read_text())
    require(sha(args.run/'summary.json') == complete['summary_sha256'], 'changed summary')
    require(summary['spatial_freeze_sha256'] == sha(args.spatial_final/'freeze.json'),
            'wrong approved labels')
    folder = args.run/'bounded_pending'
    arm = json.loads((folder/'completed.json').read_text())
    for name, digest in arm['files'].items():
        require(Path(name).name == name and sha(folder/name) == digest, 'changed arm artifact')
    audit = json.loads((folder/'association-audit.json').read_text())
    bundle = json.loads((args.spatial_final/'evaluation_labels.json').read_text())
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    media = json.loads((args.spatial_final/'media.json').read_text())
    metas = {m['case_id']: m for m in media['cases']}
    rows = {(r['case_id'], r['frame_index']): r for r in map(
        json.loads, (folder/'frames.jsonl').read_text().splitlines())}
    selections = {}
    for item in audit['multi_person_cases']:
        cid = item['case_id']
        selections[cid] = sorted({b[0] for p in annotations[cid]['persons'] for b in p['boxes']})
    for item in audit['cross_track_updates']:
        e = item['event']
        cid = e['origin']['case_id']
        frames = {e['origin']['frame_index'], e['decision_frame_index']}
        frames.update(s['frameIndex'] for s in e['update']['deduplication']['actualSamples'])
        selections[cid] = sorted(set(selections.get(cid, [])) | frames)
    args.output.mkdir(mode=0o700, exist_ok=False)
    font = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 18,
                              index=1)
    manifest, cards = [], []
    for cid, selected in sorted(selections.items()):
        meta, ann = metas[cid], annotations[cid]
        path = args.parent_frozen/meta['source_path']
        require(sha(path) == meta['sha256'], 'changed media')
        frames = decode_frames(path, meta)
        name = labels[cid].get('review_case_id') or cid
        if name.startswith('SYN'):
            name = Path(labels[cid]['original_source_path']).stem
        sheet = Image.new('RGB', (1536, 88+360*math.ceil(len(selected)/3)), '#101923')
        draw = ImageDraw.Draw(sheet)
        draw.text((12, 10), f'{name} · 최종 검수 박스 / 실제 모델 출력', font=font, fill='white')
        draw.text((12, 42), '초록: 해당 프레임의 검수 박스  |  파랑/주황: 서로 다른 예측 ID',
                  font=font, fill='white')
        for i, f in enumerate(selected):
            r = rows[cid, f]
            tile = frames[f].copy()
            marks = ImageDraw.Draw(tile)
            thickness = max(2, meta['width']//320)
            gt = [p for p in ann['persons'] if box_at(p, f) is not None]
            for p in gt:
                marks.rectangle(box_at(p, f), outline='#41e779', width=thickness)
            for o in r['observations']:
                tid = o['track_id'].rsplit('-', 1)[-1] if o['track_id'] else '?'
                colors = ['#30c8ff', '#ff9a40', '#ca93ff', '#ffe26b']
                color = colors[(int(tid)-1) % len(colors)] if tid.isdigit() else '#ffffff'
                box = normalized_box(o, meta)
                marks.rectangle(box, outline=color, width=thickness)
                marks.text((box[0], box[1]), 'ID'+tid, font=font, fill=color,
                           stroke_width=1, stroke_fill='black')
            tile.thumbnail((512, 300))
            x, y = (i % 3)*512, 88+(i//3)*360
            sheet.paste(tile, (x+(512-tile.width)//2, y+48+(300-tile.height)//2))
            draw.text((x+8, y), f'f{f:03d} · 영상 {f/meta["fps"]:.3f}초', font=font,
                      fill='white')
            note = '검수 박스 표시' if gt else '이 프레임은 검수 박스 없음 (추정하지 않음)'
            draw.text((x+8, y+24), note, font=font, fill='#bcd0dc')
        filename = cid+'.jpg'
        sheet.save(args.output/filename, quality=94)
        manifest.append(dict(case_id=cid, name=name, frames=selected, image=filename,
                             media_sha256=meta['sha256'], image_sha256=sha(args.output/filename)))
        cards.append(f'<h2>{html.escape(name)}</h2><a href="{filename}">'
                     f'<img src="{filename}" alt="{html.escape(name)}"></a>')
        del frames
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8">
<title>중복 요청 개선 검토</title><style>
body{max-width:1540px;margin:24px auto;padding:16px;background:#101923;color:#eef4ff;
font:18px/1.6 sans-serif}img{max-width:100%}h2{margin-top:48px}a{color:#aee2ff}
</style><h1>108 중복 요청 개선 · 다인 영상 확인</h1>
<p>84개 / 2,224프레임. 낙상 17/25, 낙상 의심 15/25, 정상 10/34 유지.<br>
108의 요청 2→1. 첫 요청은 그대로이며 추가 후보만 영상 시각 기준 약 0.42초 확인 후 합침.<br>
실제 YOLO 출력 재생 결과이며, 모델 재추론·VLM 호출·실물 배포 결과가 아닙니다.</p>
<p>초록 박스는 최종 검수된 해당 프레임에만 표시합니다. ID는 실제 사람 수가 아닙니다.
검수된 다인 영상에서 새로 합친 요청은 없습니다. 이는 다른 환경의 오합병이 없다는 뜻은 아닙니다.</p>
'''+''.join(cards)+'</html>'
    (args.output/'index.html').write_text(page, encoding='utf-8')
    write_json(args.output/'evidence.json', dict(
        spatial_freeze_sha256=summary['spatial_freeze_sha256'],
        result_sha256=complete['summary_sha256'], script_sha256=sha(Path(__file__)),
        sheets=manifest, index_sha256=sha(args.output/'index.html')))
    print(args.output, flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'spatial-final', 'parent-frozen', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
