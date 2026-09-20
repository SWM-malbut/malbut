#!/usr/bin/env python3
"""Export offline, frame-exact RGB timing review. No inference or auto-approval."""
import argparse
import base64
import csv
import hashlib
import html
import io
import json
from pathlib import Path

from review_fall_annotations import load_inputs, require, seconds, time_label, validate


FIELDS = (('onset', '동작 시작'), ('landing', '몸 첫 접촉'),
          ('first_down', '바닥 상태 첫 확인'))
FONT = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')


def timing_rows(case, fps):
    """Unknown times never become frame zero or a midpoint estimate."""
    rows = []
    for key, label in FIELDS:
        interval = case[key + '_frames']
        status = case.get(key + '_status')
        rows.append(dict(key=key, label=label, frames=interval,
                         text=time_label(seconds(interval, fps), status)))
    return rows


def jpeg_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=90)
    return buffer.getvalue()


def decode_frames(path, meta):
    import cv2
    from PIL import Image
    cap = cv2.VideoCapture(str(path))
    frames = []
    try:
        require(cap.isOpened(), f'cannot open {path.name}')
        for index in range(meta['frames']):
            ok, raw = cap.read()
            require(ok, f'{path.name}: missing frame {index}')
            require(raw.shape[:2] == (meta['height'], meta['width']), 'frame size changed')
            frames.append(Image.fromarray(cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)))
        require(not cap.read()[0], 'frame count changed')
    finally:
        cap.release()
    return frames


def render_timing_sheet(case, original, meta, frames, font_path):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(str(font_path), 22)
    small = ImageFont.truetype(str(font_path), 19)
    width, height = meta['width'], meta['height']
    header, cell = 66, height + 66
    sheet = Image.new('RGB', (width * 3, header + cell * 2), '#f1f2f4')
    draw = ImageDraw.Draw(sheet)
    title = f'{case["case_id"]} · {Path(original["source_path"]).name}'
    draw.text((12, 4), title, font=font, fill='#222222')
    draw.text((12, 34), '시각 검토 초안 · 윗줄: 범위 시작 / 아랫줄: 범위 끝 · 모델 결과 아님',
              font=small, fill='#444444')
    for column, row in enumerate(timing_rows(case, meta['fps'])):
        for edge in (0, 1):
            x, y = column * width, header + edge * cell
            interval = row['frames']
            draw.text((x + 10, y + 4), row['label'], font=font, fill='#222222')
            if interval is None:
                draw.text((x + 15, y + 125), row['text'], font=font, fill='#5d626b')
                draw.text((x + 15, y + 165), '시각과 대표 프레임을 추측하지 않음',
                          font=small, fill='#5d626b')
                continue
            frame = interval[edge]
            sheet.paste(frames[frame], (x, y + 33))
            draw.text((x + 10, y + height + 37),
                      f'{frame}프레임 · {frame / meta["fps"]:.3f}초',
                      font=small, fill='#222222')
    return jpeg_bytes(sheet)


def html_document(records):
    # Escaping '<' also prevents a source filename/note from closing the JSON script tag.
    data = json.dumps(records, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    options = ''.join(f'<option value="{i}">{html.escape(r["title"])}</option>'
                      for i, r in enumerate(records))
    rows = ''.join('<tr><td>' + html.escape(r['title']) + '</td>' +
                   ''.join('<td>' + html.escape(t['text']) + '</td>' for t in r['times']) +
                   '</tr>' for r in records)
    return ('''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>낙상 시각 검토</title>
<style>
body{font:16px/1.6 system-ui,sans-serif;max-width:1250px;margin:24px auto;padding:0 16px;
background:#fafaf8;color:#262626}button,select,input{font:inherit}button,select{padding:8px;
margin:4px;border:1px solid #bbb;border-radius:6px;background:white;color:#222}
#frame{display:block;width:640px;max-width:100%;height:auto;background:#333}
#sheet{width:100%;height:auto}.controls{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
#slider{width:min(640px,100%)}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:8px;text-align:left;border-bottom:1px solid #ddd}.table{overflow-x:auto}
.note{padding:12px;background:#fff4df;border-radius:8px}#times{white-space:pre-line}
</style>
<h1>낙상 시각 검토</h1>
<p>확인 대상 21개 영상. 기존 분류·박스는 유지하고 동작 시각만 검토합니다.</p>
<p class="note">표의 범위는 동작 지속 시간이 아니라 시작 경계가 걸친 프레임입니다.
몸 첫 접촉은 손·무릎이 아니라 몸통·골반이 바닥 또는 낮은 받침에 처음 닿는 때입니다.
시각은 아직 사용자 확인 전이며, 단일 정답 시각이나 모델 성능으로 확정하지 않았습니다.</p>
<label for="case">영상 선택</label><select id="case">''' + options + '''</select>
<p id="identity"></p><p id="times"></p><p id="notes"></p>
<img id="frame" alt="선택한 원본 RGB 프레임">
<div class="controls"><button id="prev">이전 프레임</button>
<button id="play">재생</button><button id="next">다음 프레임</button>
<label>속도 <select id="speed"><option value="1">1배</option>
<option value="0.5">0.5배</option><option value="0.25">0.25배</option></select></label>
<output id="position"></output></div>
<input id="slider" aria-label="프레임 선택" type="range" min="0" step="1" value="0">
<div id="jumps" class="controls"></div>
<p>키보드 ← → 로도 한 프레임씩 이동합니다. 사진은 보간 없이 원본에서 추출했습니다.
재생은 무음 프레임 미리보기이며, 감지 지연 측정에 쓰지 않습니다.</p>
<details><summary>시각 비교 사진 펼치기</summary><img id="sheet" alt="시각 범위 양 끝 비교"></details>
<h2>전체 시간표</h2><div class="table"><table><thead><tr><th>영상</th>
<th>동작 시작</th><th>몸 첫 접촉</th><th>바닥 상태 첫 확인</th></tr></thead><tbody>''' + rows + '''
</tbody></table></div><p>이미 쓰러진 영상의 0초는 처음 발견한 시각이지 낙상 시각이 아닙니다.
431의 후반 움직임은 같은 사람의 추가 동작이며 새 사고로 세지 않습니다.
정상 행동 20개는 이 시간표에 없으며 평가에서 제외된다는 뜻은 아닙니다.</p>
<script type="application/json" id="data">''' + data + '''</script>
<script>
const records=JSON.parse(document.getElementById('data').textContent);
const el=id=>document.getElementById(id);
let current=records[0],index=0,timer=null;
function stop(){if(timer!==null)clearInterval(timer);timer=null;el('play').textContent='재생';}
function show(n){index=Math.max(0,Math.min(current.frames.length-1,n));
 el('frame').src='data:image/jpeg;base64,'+current.frames[index];
 el('position').textContent=index+'프레임 / '+(current.frames.length-1)+' · '+
 (index/current.fps).toFixed(3)+'초';el('slider').value=index;}
function selectCase(){stop();current=records[Number(el('case').value)];
 el('identity').textContent='확인 대상 '+current.target;
 el('notes').textContent=current.notes;
 el('times').textContent=current.times.map(t=>t.label+': '+t.text+
 (t.frames?' ('+t.frames.join('–')+'프레임)':'')).join(String.fromCharCode(10));
 el('slider').max=current.frames.length-1;el('jumps').replaceChildren();
 for(const t of current.times){if(t.frames===null)continue;
  for(const n of [...new Set(t.frames)]){const b=document.createElement('button');
   b.textContent=t.label+' '+n+'f';b.onclick=()=>{stop();show(n);};el('jumps').append(b);}}
 el('sheet').src='data:image/jpeg;base64,'+current.sheet;show(0);}
el('case').onchange=selectCase;
el('prev').onclick=()=>{stop();show(index-1);};
el('next').onclick=()=>{stop();show(index+1);};
el('slider').oninput=()=>{stop();show(Number(el('slider').value));};
el('speed').onchange=stop;
el('play').onclick=()=>{if(timer!==null){stop();return;}
 if(index===current.frames.length-1)show(0);el('play').textContent='일시정지';
 timer=setInterval(()=>{if(index>=current.frames.length-1){stop();return;}show(index+1);},
 1000/(current.fps*Number(el('speed').value)));};
document.addEventListener('keydown',e=>{if(['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName))
 return;if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();stop();
 show(index+(e.key==='ArrowLeft'?-1:1));}});
document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
selectCase();
</script></html>''')


def export(dataset, annotations, output, font_path=FONT):
    require(not output.exists(), 'output already exists; choose a new folder')
    require(font_path.is_file(), 'Korean font missing; use --font')
    draft, labels, media = load_inputs(dataset, annotations)
    validation = validate(draft, labels, media)
    originals = {c['case_id']: c for c in labels['cases']}
    metadata = {c['case_id']: c for c in media['cases']}
    output.mkdir(mode=0o700, parents=True)
    records, rows = [], []
    for case in draft['cases']:
        if case['target_person_id'] is None:
            continue
        cid = case['case_id']
        original, meta = originals[cid], metadata[cid]
        frames = decode_frames(dataset / original['source_path'], meta)
        sheet = render_timing_sheet(case, original, meta, frames, font_path)
        (output / f'{cid}-timing.jpg').write_bytes(sheet)
        target = next(p for p in case['persons'] if p['person_id'] == case['target_person_id'])
        times = timing_rows(case, meta['fps'])
        record = dict(case_id=cid, title=f'{cid} · {Path(original["source_path"]).name}',
                      target=target['person_id']+' · '+target['description'], notes=case['notes'],
                      fps=meta['fps'], times=times,
                      frames=[base64.b64encode(jpeg_bytes(f)).decode('ascii') for f in frames],
                      sheet=base64.b64encode(sheet).decode('ascii'))
        records.append(record)
        row = dict(case_id=cid, filename=Path(original['source_path']).name,
                   approved_clip_label=original['label'], target_person_id=target['person_id'],
                   fps=meta['fps'], source_sha256=meta['sha256'])
        for entry in times:
            row[entry['key']+'_frames'] = json.dumps(entry['frames'])
            row[entry['key']+'_text'] = entry['text']
        row.update(review_status='pending_user_review', notes=case['notes'])
        rows.append(row)
    (output / 'review.html').write_text(html_document(records), encoding='utf-8')
    with (output / 'timing.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manifest = dict(schema_version='malbut.fall-timing-review.v1',
                    annotation_sha256=hashlib.sha256(annotations.read_bytes()).hexdigest(),
                    source_labels_sha256=media['source_labels_sha256'], validation=validation,
                    cases=rows, model_inference_executed=False,
                    original_media_modified=False, temporal_labels_user_approved=False)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    for path in output.iterdir():
        path.chmod(0o600)
    print(json.dumps(dict(target_cases=len(records), validation=validation), ensure_ascii=False))
    print(output / 'review.html')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--font', type=Path, default=FONT)
    args = parser.parse_args()
    export(args.dataset, args.annotations, args.output, args.font)


if __name__ == '__main__':
    main()
