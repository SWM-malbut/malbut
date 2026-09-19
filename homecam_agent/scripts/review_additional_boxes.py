#!/usr/bin/env python3
"""Source frames, box draft JSON and contact sheets; no detector/provider calls."""
import argparse
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import shutil


FRAMES = [0, 24, 48, 72, 96, 120]
SCHEMA = 'malbut.additional-person-box-review.v1'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def selected(metadata):
    frozen = json.loads((metadata/'freeze.json').read_text())
    for name in ('media.json', 'evaluation_labels.json'):
        require(sha(metadata/name) == frozen['files'][name], 'changed frozen metadata')
    labels = json.loads((metadata/'evaluation_labels.json').read_text())['classifications']['cases']
    media = {c['case_id']: c for c in json.loads((metadata/'media.json').read_text())['cases']}
    cases = [dict(label=c, media=media[c['case_id']]) for c in labels if c['source_group'] == 'additional36']
    require(len(cases) == 36, 'expected 36 additional accepted videos')
    return cases


def contact_sheet(case, folder, boxed=None):
    from PIL import Image, ImageDraw, ImageFont
    sheet = Image.new('RGB', (1280, 1194), '#f6f5f1')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 18)
    box_font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 28)
    title = case['video'] + ' | ' + case['case_id'] + ' | BOX DRAFT' if boxed else case['video'] + ' | RGB SOURCE'
    draw.text((12, 10), title, fill='#22332a', font=font)
    colors = ['#ff8b16', '#22c8ef', '#d964db', '#68dc69']
    for i, frame in enumerate(FRAMES):
        tile = Image.open(folder/'frames'/f'{case["video"]}-f{frame:03d}.jpg').convert('RGB')
        if boxed:
            ink = ImageDraw.Draw(tile)
            for j, p in enumerate(boxed['persons']):
                box = next((b[1:] for b in p['boxes'] if b[0] == frame), None)
                if box:
                    ink.rectangle(box, outline=colors[j % len(colors)], width=5)
                    ink.text((box[0]+4, max(2, box[1]-32)), p['person_id'], font=box_font,
                             fill=colors[j % len(colors)], stroke_width=1, stroke_fill='black')
        tile = tile.resize((640, 360))
        x, y = i % 2 * 640, 42 + i // 2 * 384
        caption = f'f{frame:03d} | {frame/24:.1f}s'
        if boxed and not any(b[0] == frame for p in boxed['persons'] for b in p['boxes']):
            caption += ' | No visible person' if all(
                frame in p.get('not_visible_frames', []) for p in boxed['persons']
            ) else ' | Box unavailable'
        draw.text((x+10, y+2), caption, fill='#243d32', font=font)
        sheet.paste(tile, (x, y+24))
    return sheet


def extract(metadata, videos, output):
    import cv2
    require(not output.exists(), 'output exists')
    pairs = selected(metadata)
    # Do not create a partial review directory when transfer is incomplete.
    for pair in pairs:
        path = videos/(pair['label']['case_id']+'.mp4')
        require(path.is_file(), 'missing source video: '+path.name)
        require(sha(path) == pair['media']['sha256'], 'video changed: '+path.name)
    output.mkdir(mode=0o700)
    (output/'frames').mkdir(mode=0o700)
    (output/'source_sheets').mkdir(mode=0o700)
    index = dict(freeze_sha256=sha(metadata/'freeze.json'), cases=[])
    for pair in pairs:
        label, meta = pair['label'], pair['media']
        cid = label['case_id']; video = label['review_case_id'].removeprefix('GAP-')
        path = videos/(cid+'.mp4')
        require(sha(path) == meta['sha256'], 'video changed: '+cid)
        require((meta['width'], meta['height'], meta['fps']) == (1280, 720, 24.), 'unexpected geometry')
        cap = cv2.VideoCapture(str(path))
        require(cap.isOpened(), 'cannot decode '+cid)
        try:
            for frame in FRAMES:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
                ok, rgb = cap.read()
                require(ok and rgb.shape[:2] == (720, 1280), 'frame decode failed')
                require(cv2.imwrite(str(output/'frames'/f'{video}-f{frame:03d}.jpg'), rgb,
                                    [cv2.IMWRITE_JPEG_QUALITY, 95]), 'JPEG write failed')
        finally:
            cap.release()
        case = dict(case_id=cid, video=video, source_sha256=meta['sha256'],
                    width=1280, height=720, fps=24, frames=FRAMES, label=label['label'],
                    label_ko=label['label_ko'])
        index['cases'].append(case)
        contact_sheet(case, output).save(output/'source_sheets'/f'{video}.jpg', quality=94)
    index['frame_sha256'] = {p.name:sha(p) for p in sorted((output/'frames').glob('*.jpg'))}
    save(output/'frames_index.json', index)
    print('EXTRACTED', len(index['cases']), 'videos', len(index['frame_sha256']), 'frames')


def validate(data, index):
    require(data['schema_version'] == SCHEMA and data['new_labels_user_approved'] is False,
            'draft schema/approval mismatch')
    require(data['freeze_sha256'] == index['freeze_sha256'], 'wrong evaluation set')
    require(data['box_format'] == 'source_pixel_xyxy_visible_extent'
            and data['interpolation'] == 'forbidden' and data['temporal_labels_changed'] is False
            and data['classification_labels_changed'] is False,
            'box-only scope changed')
    expected = {c['video']: c for c in index['cases']}
    cases = {c['video']: c for c in data['cases']}
    require(len(cases) == len(data['cases']) == 36 and set(cases) == set(expected), 'case set changed')
    count = 0
    for video, case in cases.items():
        meta = expected[video]
        require(case['case_id'] == meta['case_id'] and case['source_sha256'] == meta['source_sha256'], 'source changed')
        require(all(case[key] == meta[key] for key in
                    ('width', 'height', 'fps', 'frames', 'label', 'label_ko')), 'source metadata changed')
        require(case['reviewed_frames'] == FRAMES, 'representative frame set changed')
        pids = [p['person_id'] for p in case['persons']]
        require(len(set(pids)) == len(pids) and bool(pids), 'invalid people')
        require(case['target_person_id'] is None or case['target_person_id'] in pids, 'missing target')
        for person in case['persons']:
            require(person['role'] in ('target', 'other', 'normal_activity'), 'invalid role')
            require((person['role'] == 'target') == (person['person_id'] == case['target_person_id']), 'target role mismatch')
            known = []
            for box in person['boxes']:
                require(len(box) == 5 and all(type(n) is int for n in box), 'integer xyxy required')
                frame, x1, y1, x2, y2 = box
                require(frame in FRAMES and 0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= 720, 'box outside image')
                known.append(frame); count += 1
            absent = person.get('not_visible_frames', [])
            unknown = person.get('spatial_unknown_frames', [])
            require(len(set(known+absent+unknown)) == len(known+absent+unknown)
                    and set(known+absent+unknown) == set(FRAMES), 'frame box/state coverage')
    return dict(cases=36, frames=216, people=sum(len(c['persons']) for c in cases.values()), boxes=count)


def compile_draft(manual, folder):
    index = json.loads((folder/'frames_index.json').read_text())
    seeds = json.loads(manual.read_text())
    require(set(seeds) == {c['video'] for c in index['cases']}, 'manual draft incomplete')
    data = dict(schema_version=SCHEMA, annotation_status='assistant_draft_pending_user_review',
                new_labels_user_approved=False, freeze_sha256=index['freeze_sha256'],
                annotation_basis='Assistant visual review of source frames; no YOLO/VLM prediction outputs imported. Not blinded to clip labels.',
                box_format='source_pixel_xyxy_visible_extent', interpolation='forbidden',
                temporal_labels_changed=False, classification_labels_changed=False, cases=[])
    for meta in index['cases']:
        seed = copy.deepcopy(seeds[meta['video']])
        for p in seed['persons']:
            coords_by_frame = p.pop('boxes_640')
            require(len(coords_by_frame) == len(FRAMES), 'expected six draft frames')
            require(all(coords is None or (len(coords) == 4 and all(
                type(v) is int for v in coords)) for coords in coords_by_frame),
                'integer half-size xyxy required')
            p['boxes'] = [[f, *[v*2 for v in coords]] for f, coords in zip(FRAMES, coords_by_frame) if coords is not None]
        data['cases'].append(dict(meta, **seed, reviewed_frames=FRAMES, user_reviewed_frames=[]))
    summary = validate(data, index)
    for name, expected in index['frame_sha256'].items():
        require(sha(folder/'frames'/name) == expected, 'source frame changed: '+name)
    save(folder/'annotations.json', data)
    (folder/'boxed_sheets').mkdir(mode=0o700)
    for case in data['cases']:
        contact_sheet(case, folder, case).save(folder/'boxed_sheets'/f'{case["video"]}.jpg', quality=94)
    save(folder/'summary.json', summary)
    print(json.dumps(summary))


def review_html(data):
    """An offline viewer, not an annotation approval or automatic GT editor."""
    cases_count = len(data['cases'])
    frames_count = sum(len(c['frames']) for c in data['cases'])
    boxes_count = sum(len(p['boxes']) for c in data['cases'] for p in c['persons'])
    options = ''.join(f'<option value="{i}">{html.escape(c["video"])}</option>'
                      for i, c in enumerate(data['cases']))
    payload = json.dumps(data['cases'], ensure_ascii=False).replace('<', '\\u003c')
    return '''<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>추가 ''' + str(cases_count) + '''개 영상 · 사람 박스 검토</title>
<style>
body{font:16px/1.6 system-ui,sans-serif;margin:0;background:#f6f5f1;color:#24352f}
main{max-width:1280px;margin:auto;padding:20px}h1{font-size:24px;margin:0}
nav{position:sticky;top:0;background:#f6f5f1;padding:12px 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap;z-index:2}
button,select{font:inherit;padding:7px 12px;border:1px solid #aab8b0;border-radius:6px;background:white;color:inherit}
button:disabled{opacity:.4}a{color:#245d88}#sheet{display:block;width:100%;height:auto;border:1px solid #d3d9d5}
.hint{font-size:14px;color:#55645c}.legend{display:flex;gap:16px;flex-wrap:wrap}#frames a{margin-right:14px}
#error{color:#a12b24}button:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid #406bd3}
</style><main>
<h1>추가 ''' + str(cases_count) + '''개 영상 · 사람 박스 검토</h1>
<p>검토용 초안 · ''' + str(frames_count) + '''개 프레임 · ''' + str(boxes_count) + '''개 박스. 라벨과 낙상 시작 시각은 이 작업에서 정하거나 바꾸지 않았습니다.</p>
<p class="hint">보이는 몸의 외곽을 표시했습니다. 가구에 가려진 몸이나 화면 밖 머리는 추정하지 않았습니다.
P1·P2·P3은 이 영상 안에서 사람을 구분하는 표시이며, 낙상 확정이나 모델의 검출 결과를 뜻하지 않습니다.</p>
<nav><button id="prev">← 이전 영상</button><select id="cases" aria-label="영상 선택">''' + options + '''</select>
<button id="next">다음 영상 →</button><label><input type="checkbox" id="original"> 박스 없는 원본 보기</label>
<a id="jpg" target="_blank" rel="noopener">JPG 크게 보기</a><span id="position"></span></nav>
<h2 id="heading"></h2><div id="people" class="legend"></div><p id="note"></p>
<p class="hint">수정할 곳은 “V008 · 120프레임 · P1 왼손 포함”처럼 알려주세요. 좌우 화살표 키로 영상 이동도 가능합니다.</p>
<p id="error" role="alert"></p><img id="sheet" width="1280" height="1194" alt="선택한 영상의 여섯 프레임">
<p>개별 원본 프레임(1280×720): <span id="frames"></span></p>
<p class="hint">JPG 폴더에는 검토 이미지 ''' + str(cases_count) + '''장만 있습니다. 좌표와 원본 프레임은 자료 폴더에 따로 보관했습니다.
이 페이지는 검토용이며 좌표 수정·승인·서버 전송을 하지 않습니다.</p>
<script id="data" type="application/json">''' + payload + '''</script><script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent);
const el=id=>document.getElementById(id);
const colors=['#bb5300','#007a92','#944594','#427b42'];
const role={target:'확인 대상',other:'다른 사람',normal_activity:'정상 행동의 사람',observed_person:'화면 속 사람'};
function render(){
  const i=Number(el('cases').value), c=data[i];
  el('prev').disabled=i===0;el('next').disabled=i===data.length-1;
  el('position').textContent=`${i+1} / ${data.length}`;
  el('heading').textContent=c.video+' · '+c.case_id+(c.label_ko ? ' · 기존 라벨: '+c.label_ko : ' · 라벨 작성과 별도');
  el('note').textContent=c.notes;el('people').replaceChildren();
  c.persons.forEach((p,j)=>{const span=document.createElement('span');span.style.color=colors[j%colors.length];
    span.textContent=`${p.person_id} (${role[p.role]}): ${p.description}`;el('people').appendChild(span);});
  const src=(el('original').checked?'자료/source_sheets/':'JPG/')+c.video+'.jpg';
  el('error').textContent='';el('sheet').src=src;el('jpg').href=src;
  el('sheet').alt=c.video+'의 0·24·48·72·96·120프레임';el('frames').replaceChildren();
  c.frames.forEach(f=>{const a=document.createElement('a');a.href=`자료/frames/${c.video}-f${String(f).padStart(3,'0')}.jpg`;
    a.target='_blank';a.rel='noopener';a.textContent=f+'프레임';el('frames').appendChild(a);});
}
function move(delta){const i=Number(el('cases').value);el('cases').value=String(Math.max(0,Math.min(data.length-1,i+delta)));render();}
el('prev').onclick=()=>move(-1);el('next').onclick=()=>move(1);
el('cases').onchange=render;el('original').onchange=render;
el('sheet').onerror=()=>{el('error').textContent='이미지를 찾을 수 없습니다. HTML과 JPG·자료 폴더를 함께 보관해주세요.';};
document.addEventListener('keydown',e=>{if(['SELECT','INPUT','BUTTON'].includes(e.target.tagName))return;
  if(e.key==='ArrowLeft'){e.preventDefault();move(-1);}if(e.key==='ArrowRight'){e.preventDefault();move(1);}});
render();
</script></main></html>'''


def publish(folder, output):
    index = json.loads((folder/'frames_index.json').read_text())
    data = json.loads((folder/'annotations.json').read_text())
    summary = validate(data, index)
    for name, expected in index['frame_sha256'].items():
        require(sha(folder/'frames'/name) == expected, 'source frame changed: '+name)
    require(not output.exists(), 'bundle output exists')
    output.mkdir(mode=0o700)
    assets = output/'자료'
    assets.mkdir(mode=0o700)
    # Regenerate sheets from the validated coordinates to avoid stale overlays.
    (output/'JPG').mkdir(mode=0o700)
    for case in data['cases']:
        contact_sheet(case, folder, case).save(output/'JPG'/f'{case["video"]}.jpg', quality=95)
    shutil.copytree(folder/'frames', assets/'frames')
    shutil.copytree(folder/'source_sheets', assets/'source_sheets')
    for name in ('annotations.json', 'frames_index.json', 'summary.json'):
        shutil.copy2(folder/name, assets/name)
    with (output/'열어보기.html').open('x', encoding='utf-8') as stream:
        stream.write(review_html(data))
    manifest = {str(p.relative_to(output)): sha(p) for p in sorted(output.rglob('*')) if p.is_file()}
    save(output/'자료'/'bundle_manifest.json', dict(files=manifest, summary=summary,
                                                 status='draft_pending_user_review'))
    print('PUBLISHED', output, json.dumps(summary))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['extract', 'compile', 'publish'])
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--videos', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manual', type=Path)
    parser.add_argument('--review', type=Path)
    args = parser.parse_args()
    if args.action == 'extract': extract(args.metadata, args.videos, args.output)
    elif args.action == 'compile': compile_draft(args.manual, args.output)
    else: publish(args.review, args.output)


if __name__ == '__main__':
    main()
