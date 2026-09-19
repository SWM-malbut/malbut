#!/usr/bin/env python3
"""Validate/export independent RGB annotation drafts. Never run a detector/provider.

Artifacts are review material, not approved ground truth or measured model scores.
Only explicitly annotated frames have boxes; interpolation is deliberately absent.
"""
import argparse
import base64
import copy
import csv
import hashlib
import html
import json
import math
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def frame_interval(value, count, name, nullable=True):
    if value is None and nullable:
        return
    require(isinstance(value, list) and len(value) == 2, f'{name}: interval required')
    require(all(type(n) is int for n in value), f'{name}: integer frames required')
    require(0 <= value[0] <= value[1] < count, f'{name}: out of range/reversed')


def box_at(person, frame):
    """None means unannotated, NOT person absent; never interpolate or carry forward."""
    return next((item[1:] for item in person['boxes'] if item[0] == frame), None)


def validate(draft, labels, media):
    require(draft['schema_version'] == 'malbut.fall-video-spatial-temporal.v1', 'schema')
    require(draft['annotation_status'] == 'assistant_draft_pending_user_review', 'draft status')
    require(draft['new_labels_user_approved'] is False, 'cannot self-approve draft')
    require(draft['dataset_usage'] == 'development_only_previously_inspected_and_tuned',
            'previously used dataset cannot become a held-out test set')
    require(draft['box_format'] == 'source_pixel_xyxy_visible_extent', 'box format')
    require(draft['box_interpolation'] == 'forbidden_for_scoring', 'interpolation policy')
    require(draft['time_format'] == 'inclusive_zero_based_frame_interval', 'time format')
    require(draft['source_labels_sha256'] == media['source_labels_sha256'], 'label hash')
    originals = {case['case_id']: case for case in labels['cases']}
    metadata = {case['case_id']: case for case in media['cases']}
    cases = draft['cases']
    identifiers = [case['case_id'] for case in cases]
    require(len(set(identifiers)) == len(identifiers), 'duplicate cases')
    require(set(identifiers) == set(originals) == set(metadata), 'missing/extra cases')
    for case in cases:
        cid = case['case_id']
        meta, original = metadata[cid], originals[cid]
        count, width, height = meta['frames'], meta['width'], meta['height']
        require(meta['sha256'] == original['source_sha256'], f'{cid}: video hash')
        require(meta['source_path'] == original['source_path'], f'{cid}: video path')
        require(type(count) is int and count > 0 and math.isfinite(meta['fps'])
                and meta['fps'] > 0, f'{cid}: media timing')
        reviewed = case['reviewed_frames']
        require(reviewed == 'all' or (isinstance(reviewed, list) and reviewed
                and all(type(f) is int and 0 <= f < count for f in reviewed)
                and reviewed == sorted(set(reviewed))), f'{cid}: reviewed frames')
        people = case['persons']
        pids = [p['person_id'] for p in people]
        require(len(pids) == len(set(pids)), f'{cid}: duplicate person IDs')
        target = case['target_person_id']
        expected = original['expected_candidate_detection']
        require((target in pids) if expected else target is None, f'{cid}: target mismatch')
        for key in ('onset_frames', 'landing_frames', 'first_down_frames'):
            frame_interval(case[key], count, f'{cid}.{key}')
        for key in ('onset', 'landing'):
            status, interval = case[f'{key}_status'], case[f'{key}_frames']
            unknown = {'not_visible_before_clip', 'not_applicable', 'occluded',
                       'covered_and_occluded'}
            known = {'visible_interval', 'suspected_motion_interval',
                     'occlusion_limited_interval', 'uncertain_staged_descent'}
            require(status in unknown | known, f'{cid}: unknown temporal status')
            require((interval is None) == (status in unknown), f'{cid}: status/time mismatch')
        if not expected:
            require(all(case[k] is None for k in
                        ('onset_frames', 'landing_frames', 'first_down_frames')),
                    f'{cid}: normal activity cannot invent a fall event')
        if case['entry_state'] == 'already_down':
            require(case['first_down_frames'] == [0, 0] and case['onset_frames'] is None
                    and case['landing_frames'] is None
                    and case['onset_status'] == 'not_visible_before_clip',
                    f'{cid}: cannot invent initial fall time')
        for key in ('landing_frames', 'first_down_frames'):
            if case['onset_frames'] and case[key]:
                require(case[key][1] >= case['onset_frames'][0], f'{cid}: temporal order')
        extra = case.get('additional_motion')
        if extra:
            frame_interval(extra['onset_frames'], count, f'{cid}.additional onset', False)
            frame_interval(extra['settled_low_frames'], count, f'{cid}.additional end', False)
            require(extra['settled_low_frames'][1] >= extra['onset_frames'][0],
                    f'{cid}: additional temporal order')
        for person in people:
            require(person['role'] in {'target', 'other', 'normal_activity'}, f'{cid}: role')
            require((person['role'] == 'target') == (person['person_id'] == target),
                    f'{cid}: inconsistent target role')
            require(person['occlusion'] in {'none', 'partial', 'heavy'}, f'{cid}: occlusion')
            frame_interval(person['first_visible_frames'], count, f'{cid}.first visible', False)
            seen = []
            require(person['boxes'], f'{cid}: person without annotated positions')
            for box in person['boxes']:
                require(isinstance(box, list) and len(box) == 5
                        and all(type(n) is int for n in box), f'{cid}: integer box required')
                frame, x1, y1, x2, y2 = box
                require(0 <= frame < count and 0 <= x1 < x2 <= width
                        and 0 <= y1 < y2 <= height, f'{cid}: box bounds')
                require(reviewed == 'all' or frame in reviewed, f'{cid}: unreviewed box')
                require(frame >= person['first_visible_frames'][0], f'{cid}: box before visible')
                seen.append(frame)
            require(seen == sorted(set(seen)), f'{cid}: duplicate/unordered box frames')
            unknown = person.get('spatial_unknown_frames', [])
            require(isinstance(unknown, list)
                    and all(type(f) is int and 0 <= f < count for f in unknown)
                    and unknown == sorted(set(unknown)), f'{cid}: unknown spatial frames')
            require(not set(unknown) & set(seen), f'{cid}: box also marked unknown')
            require(reviewed == 'all' or set(unknown) <= set(reviewed),
                    f'{cid}: unreviewed unknown positions')
            require(not unknown or bool(person.get('spatial_unknown_reason', '').strip()),
                    f'{cid}: missing unknown position reason')
    return dict(cases=len(cases),
                target_cases=sum(c['target_person_id'] is not None for c in cases),
                person_tracks=sum(len(c['persons']) for c in cases),
                explicit_boxes=sum(len(p['boxes']) for c in cases for p in c['persons']),
                initial_fall_not_visible=sum(c['onset_status'] == 'not_visible_before_clip'
                                             for c in cases),
                landing_occluded=sum(c['landing_status'] in {'occluded', 'covered_and_occluded'}
                                     for c in cases),
                approved_new_spatial_temporal_labels=0, formal_metrics_ready=False)


def seconds(interval, fps):
    return None if interval is None else [round(frame / fps, 4) for frame in interval]


def load_inputs(dataset, draft_path):
    import cv2
    draft = json.loads(draft_path.read_text())
    label_path = (dataset / draft['source_labels_relative_path']).resolve()
    require(label_path.is_relative_to(dataset.resolve()), 'label path escapes dataset')
    label_bytes = label_path.read_bytes()
    label_hash = hashlib.sha256(label_bytes).hexdigest()
    require(label_hash == draft['source_labels_sha256'], 'source label file changed')
    labels = json.loads(label_bytes)
    media = dict(source_labels_sha256=label_hash, cases=[])
    for case in labels['cases']:
        path = (dataset / case['source_path']).resolve()
        require(path.is_relative_to(dataset.resolve()), 'media path escapes dataset')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(digest == case['source_sha256'], f"{case['case_id']}: media hash changed")
        cap = cv2.VideoCapture(str(path))
        require(cap.isOpened(), f'cannot open {path.name}')
        fps = cap.get(cv2.CAP_PROP_FPS)
        count, width, height = 0, None, None
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            height, width = frame.shape[:2]
            count += 1
        cap.release()
        media['cases'].append(dict(case_id=case['case_id'], source_path=case['source_path'],
                                   sha256=digest, fps=fps, frames=count,
                                   width=width, height=height))
    return draft, labels, media


def render_case(dataset, original, case, meta, output):
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 20)
    indices = sorted(set([0, 12, 24, 36, 48, 60] +
                         [b[0] for p in case['persons'] for b in p['boxes']]))
    indices = [f for f in indices if f < meta['frames']]
    width, height = meta['width'], meta['height']
    size = (width * 3, 40 + (height + 34) * math.ceil(len(indices) / 3))
    sheet = Image.new('RGB', size, '#f3f3f3')
    draw = ImageDraw.Draw(sheet)
    title = (f"{case['case_id']} | {Path(original['source_path']).name}"
             ' | DRAFT / NOT MODEL OUTPUT')
    draw.text((10, 8), title, fill='black', font=font)
    cap = cv2.VideoCapture(str(dataset / original['source_path']))
    selected = {}
    for f in range(meta['frames']):
        ok, frame = cap.read()
        require(ok, f"decode failed {case['case_id']} frame {f}")
        if f in indices:
            selected[f] = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    cap.release()
    for i, frame in enumerate(indices):
        tile = selected[frame]
        ink = ImageDraw.Draw(tile)
        for person in case['persons']:
            box = box_at(person, frame)
            if box is None:
                if frame in person.get('spatial_unknown_frames', []):
                    ink.text((8, 8), person['person_id'] + ' POSITION UNKNOWN (covered)',
                             fill='#ff9b27', font=font, stroke_width=1, stroke_fill='black')
                continue
            color = '#ff9b27' if person['role'] == 'target' else '#44c5ff'
            ink.rectangle(box, outline=color, width=3)
            x, y = box[:2]
            label = person['person_id'] + (' TARGET' if person['role'] == 'target' else '')
            ink.text((x, max(0, y-24)), label, fill=color, font=font,
                     stroke_width=1, stroke_fill='black')
        x, y = i % 3 * width, 40 + i // 3 * (height+34)
        sheet.paste(tile, (x, y))
        draw.text((x+8, y+height+5), f'frame {frame} | {frame/meta["fps"]:.3f}s',
                  fill='black', font=font)
    path = output / f"{case['case_id']}-labels.jpg"
    sheet.save(path, quality=92)
    return path


def export(dataset, draft_path, output):
    draft, labels, media = load_inputs(dataset, draft_path)
    summary = validate(draft, labels, media)
    require(not output.exists(), 'output already exists; use a new review version')
    output.mkdir(mode=0o700, parents=True)
    originals = {c['case_id']: c for c in labels['cases']}
    metadata = {c['case_id']: c for c in media['cases']}
    compiled = copy.deepcopy(draft)
    compiled['source_annotation_sha256'] = hashlib.sha256(draft_path.read_bytes()).hexdigest()
    compiled['summary'] = summary
    rows, cards = [], []
    for case in compiled['cases']:
        cid = case['case_id']
        meta, original = metadata[cid], originals[cid]
        case['media'] = meta
        case['approved_clip_classification'] = original
        for key in ('onset', 'landing', 'first_down'):
            case[key+'_seconds'] = seconds(case[key+'_frames'], meta['fps'])
        row = dict(case_id=cid, filename=Path(original['source_path']).name,
                   approved_clip_label=original['label'],
                   target_person_id=case['target_person_id'],
                   visible_person_tracks=len(case['persons']), entry_state=case['entry_state'],
                   onset_seconds=case['onset_seconds'], onset_status=case['onset_status'],
                   landing_seconds=case['landing_seconds'], landing_status=case['landing_status'],
                   first_down_seconds=case['first_down_seconds'],
                   review_status=draft['annotation_status'], notes=case['notes'])
        rows.append(row)
        image_path = render_case(dataset, original, case, meta, output)
        encoded = base64.b64encode(image_path.read_bytes()).decode('ascii')
        details = '\n'.join(f'{p["person_id"]}: {p["description"]}' for p in case['persons'])
        timings = ' / '.join(
            title + ': ' + time_label(case[key+'_seconds'], case.get(key+'_status'))
            for title, key in [('동작 시작', 'onset'), ('바닥 접촉', 'landing'),
                               ('바닥의 사람 확인', 'first_down')])
        extra = case.get('additional_motion')
        if extra:
            timings += (' / 이후 별도 동작: '
                        + time_label(seconds(extra['onset_frames'], meta['fps']))
                        + ' — ' + extra['meaning'])
        cards.append(f'<section id="{cid}"><h2>{html.escape(cid+" · "+row["filename"])}</h2>'
                     f'<p>기존 분류: {html.escape(original["label_ko"])} · 새 라벨: 사용자 검수 전</p>'
                     f'<p>{html.escape(timings)}</p><p>{html.escape(case["notes"])}</p>'
                     f'<pre>{html.escape(details)}</pre>'
                     f'<img alt="{cid} 위치 라벨 초안" loading="lazy" '
                     f'src="data:image/jpeg;base64,{encoded}"></section>')
    (output/'annotations.json').write_text(json.dumps(compiled, ensure_ascii=False, indent=2)+'\n')
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    with (output/'review.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    navigation = ''.join(f'<a href="#{r["case_id"]}">'
                         f'{r["filename"].removesuffix(".mp4")}</a>' for r in rows)
    document = ('<!doctype html><html lang="ko"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>낙상 영상 위치·시간 라벨 검토</title>'
                '<style>body{font:16px/1.6 system-ui;margin:24px auto;'
                'padding:0 16px;max-width:1400px}'
                'img{width:100%;height:auto}section{border-top:1px solid #aaa;margin-top:32px}'
                'pre{white-space:pre-wrap}nav a{margin-right:12px}</style>'
                f'<h1>{len(rows)}개 영상 — 위치·시간 라벨 초안</h1>'
                '<p>주황: 확인 대상 · 파랑: 다른 사람/일반 행동. 모델 검출 결과가 아닙니다.</p>'
                '<p>기존 영상 분류는 유지했습니다. 새 위치·시각은 사용자 검수 전이며 최종 정답으로 쓰지 않습니다. '
                'null은 시각을 알 수 없거나 해당하지 않음을 뜻합니다. 박스 없는 중간 프레임은 미라벨이며 자동 보간하지 않습니다.</p>'
                '<nav>' + navigation + '</nav>' + ''.join(cards) + '</html>')
    (output/'review.html').write_text(document)
    for path in output.iterdir():
        path.chmod(0o600)
    print(json.dumps(summary, ensure_ascii=False))
    print(output/'review.html')


def time_label(value, status=None):
    if value is None:
        return {'not_visible_before_clip': '영상에 없음(촬영 전)',
                'occluded': '가려져 확인 불가',
                'covered_and_occluded': '이불에 가려 확인 불가',
                'not_applicable': '해당 없음'}.get(status, '해당 없음/미정')
    start, end = value
    return f'{start:g}초' if start == end else f'{start:g}–{end:g}초 사이'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output:
        export(args.dataset, args.annotations, args.output)
    else:
        draft, labels, media = load_inputs(args.dataset, args.annotations)
        print(json.dumps(validate(draft, labels, media), ensure_ascii=False))


if __name__ == '__main__':
    main()
