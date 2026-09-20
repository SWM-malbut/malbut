#!/usr/bin/env python3
"""Four-arm RGB/approved-box comparison. Never interpolate ground-truth boxes."""
import argparse
from collections import Counter
import html
import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from replay_fall_baseline import sha, write_json
from replay_pose_retention import verify_overlay
from review_fall_annotations import box_at, require
from score_fall_baseline import normalized_box


ARM_NAMES = {
    'n_stretch': '기존 n · 비율 변형',
    'n_letterbox': '기존 n · 비율 유지',
    's_stretch': '큰 s · 비율 변형',
    's_letterbox': '큰 s · 비율 유지',
}
LABELS = {'observed_fall': '낙상', 'suspected_fall': '낙상 의심', 'normal_activity': '정상'}


def aggregate(cases):
    groups = {}
    for label in LABELS:
        items = [c for c in cases if c['label'] == label]
        groups[label] = dict(total=len(items), requested=sum(c['request_count'] > 0 for c in items),
                             requests=sum(c['request_count'] for c in items))
    return dict(cases=len(cases), groups=groups,
                boxes={k: sum(c['boxes'][k] for c in cases)
                       for k in ('total', 'matched', 'ambiguous', 'missing', 'usable')})


def display_name(label):
    name = label.get('review_case_id', '')
    return name if name and not name.startswith('SYN') else Path(
        label['original_source_path']).stem


def run(args):
    import cv2

    require(not args.output.exists(), 'output exists')
    verify_overlay(args.spatial_final, args.parent_frozen)
    marker = json.loads((args.run/'completed.json').read_text())
    require(sha(args.run/'summary.json') == marker['summary_sha256'], 'changed results')
    summary = json.loads((args.run/'summary.json').read_text())
    require(summary['spatial_freeze_sha256'] == sha(args.spatial_final/'freeze.json'),
            'wrong approved GT')
    bundle = json.loads((args.spatial_final/'evaluation_labels.json').read_text())
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    metas = {m['case_id']: m for m in json.loads((args.spatial_final/'media.json').read_text())[
        'cases']}
    cases, rows = {}, {}
    for arm in ARM_NAMES:
        folder = args.run/arm
        require(sha(folder/'completed.json') == marker['arms'][arm], 'changed arm marker')
        for name, digest in json.loads((folder/'completed.json').read_text())['files'].items():
            require(Path(name).name == name and sha(folder/name) == digest, 'changed arm data')
        cases[arm] = {c['case_id']: c for c in json.loads((folder/'cases.json').read_text())}
        rows[arm] = {(r['case_id'], r['frame_index']): r for r in map(
            json.loads, (folder/'frames.jsonl').read_text().splitlines())}
    changed = sorted({cid for cid in metas if any(
        bool(cases[arm][cid]['request_count']) != bool(cases['n_stretch'][cid]['request_count'])
        for arm in ARM_NAMES)})
    # Every changed request/no-request case, plus previously discussed difficult cases.
    selected = sorted(set(changed) | {
        'SYN002', 'SYN007', 'SYN012', 'SYN016', 'SYN022', 'SYN050', 'SYN071', 'SYN076'})
    args.output.mkdir(mode=0o700)
    font = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 18,
                              index=1)
    small = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 15,
                               index=1)
    sheets, cards = [], []
    for cid in selected:
        ann, meta, label = annotations[cid], metas[cid], labels[cid]
        frames = {b[0] for p in ann['persons'] for b in p['boxes']}
        for arm in ARM_NAMES:
            req = cases[arm][cid]['requests']
            for event in req:
                f = event['origin']['frame_index']
                frames.add(f)
                c = event['origin']['candidate']
                if c.get('evidenceReference'):
                    frames.add(c['evidenceReference']['frameIndex'])
        if not frames:
            frames = {r[1] for r in rows['n_stretch'] if r[0] == cid}
        frames = sorted(frames)
        path = args.parent_frozen/meta['source_path']
        require(sha(path) == meta['sha256'], 'media changed')
        cap = cv2.VideoCapture(str(path))
        sheet = Image.new('RGB', (1920, 100+370*len(frames)), '#101923')
        draw = ImageDraw.Draw(sheet)
        title = (f'{display_name(label)} · {LABELS[label["label"]]} · '
                 f'{meta["width"]}×{meta["height"]}')
        draw.text((12, 8), title, font=font, fill='white')
        draw.text((12, 36), '초록: 최종 검수 박스 / 파랑: 실제 Pose 검출 / 점: 신뢰도 0.5 이상 관절',
                  font=font, fill='white')
        draw.text((12, 64), '미검수 프레임의 정답 박스는 추정하지 않음. ID는 사람 수가 아님.',
                  font=font, fill='#bcd0dc')
        try:
            for i, f in enumerate(frames):
                cap.set(cv2.CAP_PROP_POS_FRAMES, f)
                ok, bgr = cap.read()
                require(ok, 'decode failure')
                raw = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
                for j, arm in enumerate(ARM_NAMES):
                    row = rows[arm][cid, f]
                    tile = raw.copy()
                    marks = ImageDraw.Draw(tile)
                    thickness = max(2, meta['width']//320)
                    gt = [box_at(p, f) for p in ann['persons'] if box_at(p, f) is not None]
                    for b in gt:
                        marks.rectangle(b, outline='#41e779', width=thickness+1)
                    for o in row['observations']:
                        box = normalized_box(o, meta)
                        marks.rectangle(box, outline='#30baff', width=thickness)
                        for k in o['pose']['keypoints']:
                            if k['confidence'] >= .5:
                                x, y = k['x']*meta['width'], k['y']*meta['height']
                                r = thickness
                                marks.ellipse((x-r, y-r, x+r, y+r), fill='#ffd252')
                        tid = o['track_id'].rsplit('-', 1)[-1] if o['track_id'] else '?'
                        marks.text((box[0], box[1]), f'ID{tid} {o["pose"]["boxConfidence"]:.2f}',
                                   font=small, fill='#30baff', stroke_width=1, stroke_fill='black')
                    tile.thumbnail((476, 298))
                    x, y = j*480, 100+i*370
                    sheet.paste(tile, (x+(480-tile.width)//2, y+68+(298-tile.height)//2))
                    draw.text((x+6, y), ARM_NAMES[arm], font=font, fill='white')
                    draw.text((x+6, y+24), f'f{f:03d} / {f/meta["fps"]:.2f}초 / '
                              f'전체 요청 {cases[arm][cid]["request_count"]}회', font=small,
                              fill='#d2dfeb')
                    draw.text((x+6, y+45), '최종 검수 박스 있음' if gt else '이 프레임 검수 박스 없음',
                              font=small, fill='#87d8a0' if gt else '#ffe09c')
        finally:
            cap.release()
        filename = cid+'.jpg'
        sheet.save(args.output/filename, quality=94)
        sheets.append(dict(case_id=cid, name=display_name(label), frames=frames, image=filename,
                           image_sha256=sha(args.output/filename), media_sha256=meta['sha256']))
        cards.append(f'<h2>{html.escape(title)}</h2><a href="{filename}">'
                     f'<img loading="lazy" src="{filename}" alt="{html.escape(title)}"></a>')
    resolutions = sorted({(m['width'], m['height']) for m in metas.values()})
    grouped = {f'{w}x{h}': {arm: aggregate([
        c for cid, c in arm_cases.items() if (metas[cid]['width'], metas[cid]['height']) == (w, h)])
        for arm, arm_cases in cases.items()} for w, h in resolutions}
    table_rows = []
    for arm, name in ARM_NAMES.items():
        metric = summary['metrics'][arm]
        totals = aggregate(list(cases[arm].values()))['groups']
        values = [f'{totals[k]["requested"]}/{totals[k]["total"]}' for k in LABELS]
        table_rows.append('<tr><th>'+name+'</th>'+''.join(f'<td>{v}</td>' for v in values) +
                          f'<td>{metric["boxes"]["matched"]}/533</td>' +
                          f'<td>{metric["pose_ms"]["mean"]:.1f} ms</td></tr>')
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8">
<title>YOLO Pose 검출 비교</title><style>
body{max-width:1920px;margin:24px auto;padding:16px;background:#101923;color:#eef4ff;
font:18px/1.6 sans-serif}img{max-width:100%}h2{margin-top:48px}a{color:#aee2ff}
table{border-collapse:collapse}th,td{padding:12px;border:1px solid #61717b}
</style><h1>84개 · 검출 모델 / 비율 유지 비교</h1>
<p>최종 사용자 검수 GT 고정. 실제 2,224프레임 × 4조건 재추론.<br>
영상별 요청 발생 수이며 대상자·낙상 이후 확인을 마친 검출률은 아닙니다.
정상 영상 요청은 불필요한 확인 요청이지, 낙상 확정 알림이 아닙니다.<br>
박스 대응은 기존 기준(정답 영역 50%·예측 영역 25%·중심 포함·유일 대응)입니다.
관절 위치 정답이 없어 관절 정확도를 평가한 수치가 아닙니다.<br>
시간은 이 PC CPU에서 Pose 전처리·추론·좌표 복원만 측정. 실물 Jetson 지연이 아닙니다.</p>
<table><tr><th>조건</th><th>낙상 요청</th><th>의심 요청</th><th>정상 요청</th>
<th>검수 박스 대응</th><th>평균 처리 시간</th></tr>'''+''.join(table_rows) + \
        '</table><p>요청 유무가 달라진 모든 영상과 기존 주요 미탐 사례.</p>'+''.join(cards)+'</html>'
    (args.output/'index.html').write_text(page, encoding='utf-8')
    write_json(args.output/'review.json', dict(
        source_summary_sha256=marker['summary_sha256'], script_sha256=sha(Path(__file__)),
        spatial_freeze_sha256=summary['spatial_freeze_sha256'],
        dimensions=dict(Counter(f'{m["width"]}x{m["height"]}' for m in metas.values())),
        by_resolution=grouped, changed_cases=changed, sheets=sheets,
        index_sha256=sha(args.output/'index.html')))
    print(args.output, flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'spatial-final', 'parent-frozen', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
