#!/usr/bin/env python3
"""Offline, fixed-checklist scoring and inspectable HTML for patrol experiments."""

import argparse
import base64
from html import escape
import json
from pathlib import Path

from patrol_method_comparison import digest, read, save, standard_cost


ABSENT = {2: ['bed', 'ac'], 3: ['ball'], 4: ['bed', 'ac'], 7: ['ball'],
          9: ['ball'], 10: ['fridge'], 13: ['fridge'], 21: ['ball'], 24: ['bed']}
LABELS = {'original': '기존 24장 격자', 'separated': '구분 강화 24장 격자',
          'grid6': 'IG-VLM 응용: 6장 격자 × 4',
          'separate24': '개별 이미지 24개', 'som24': 'SoM 응용: SAM 번호 24장'}


def matches(category, name, coarse=False):
    # Frozen from the previous two runs; do not broaden after seeing new output.
    if category == 'ball':
        return '공' in name and '공간' not in name
    if category == 'barbell':
        return any(term in name for term in ('바벨', '덤벨', '원판', '역기'))
    if category == 'bin':
        return any(term in name for term in ('휴지통', '쓰레기통')) or (
            coarse and '하늘색' in name and '통' in name)
    return {'fridge': '냉장고', 'bed': '침대', 'ac': '에어컨'}[category] in name


def score(rows, ground):
    scores = []
    for category, target in ground['targets'].items():
        found, coarse, missing = [], [], []
        for number in target['frames']:
            names = [item['name'] for item in rows[number-1]['analysis']['objects']]
            if any(matches(category, name) for name in names):
                found.append(number)
            elif any(matches(category, name, True) for name in names):
                coarse.append(number)
            else:
                missing.append(number)
        scores.append(dict(category=category, name=target['name'],
                           expected=len(target['frames']), recognized=found,
                           coarse_only=coarse, missed_or_mislabeled=missing))
    false_cases = []
    for number, categories in ABSENT.items():
        names = [o['name'] for o in rows[number-1]['analysis']['objects']]
        for category in categories:
            offending = [name for name in names if matches(category, name)]
            if offending:
                false_cases.append(dict(frame_id=f'F{number:02d}', category=category,
                                        names=offending))
    objects = [o for r in rows for o in r['analysis']['objects']]
    return dict(expected_presences=35,
                recognized_presences=sum(len(s['recognized']) for s in scores),
                coarse_only_presences=sum(len(s['coarse_only']) for s in scores),
                scores=scores, false_presence_cases=false_cases,
                false_presence_case_count=len(false_cases), total_reported_objects=len(objects),
                null_bbox_count=sum(o['image_bbox'] is None for o in objects),
                scope='Selected names only; not exhaustive recall, precision, or bbox accuracy')


def image_uri(path):
    return 'data:image/jpeg;base64,'+base64.b64encode(path.read_bytes()).decode('ascii')


def build(root, original, separated):
    ground = read(separated / 'ground-truth.json')
    sources = dict(original=original, separated=separated,
                   **{m: root/m for m in ('grid6', 'separate24', 'som24')})
    data = {}
    for method, folder in sources.items():
        rows = read(folder / 'observations.json')
        if len(rows) != 24 or any(row['status'] != 'ready' for row in rows):
            raise ValueError('cannot compare incomplete experiment')
        response = read(folder / 'response.json') if (folder / 'response.json').exists() else (
            read(folder / 'vision-calls.json')[0]['response'])
        quality = score(rows, ground)
        cost = standard_cost(response['usage'])
        data[method] = dict(label=LABELS[method], usage=response['usage'], quality=quality,
                            usd=cost, krw_at_1500=cost*1500, rows=rows,
                            api_calls=1, cached_reads=response['usage'][
                                'input_tokens_details'].get('cached_tokens', 0),
                            folder=str(folder))
        if method in ('grid6', 'separate24', 'som24'):
            save(folder / 'quality.json', quality)
            verification = read(folder / 'verification.json')
            verification.update(estimated_usd=cost, estimated_krw_at_1500=cost*1500,
                                includes_cache_write_cost=True)
            save(folder / 'verification.json', verification)
        if (folder / 'timing.json').exists():
            data[method]['api_seconds'] = read(folder / 'timing.json')['api_seconds']
    reference = [r['metadata']['image_sha256'] for r in data['separated']['rows']]
    if any([r['metadata']['image_sha256'] for r in d['rows']] != reference
           for d in data.values()):
        raise ValueError('comparison source mismatch')
    associations = read(root / 'som24/associations.json')
    sam = read(root / 'marks/marks.json')
    data['som24']['sam'] = dict(
        total_seconds=sam['total_seconds'], total_marks=sum(
            len(f['marks']) for f in sam['frames'].values()),
        unknown_mark_ids=sum(a['unknown_id'] for a in associations),
        selected_marks=sum(a['valid'] for a in associations))
    save(root / 'comparison.json', dict(
        methods={k: {key: value for key, value in d.items() if key != 'rows'}
                 for k, d in data.items()}, source_sha256=reference,
        ground_truth_sha256=digest(separated / 'ground-truth.json'),
        independent_trials_per_method=1, source_frame_count=24,
        pricing=dict(input=.20, cached_input=.02, cache_write_30m=.25,
                     cache_write_12h=.50, output=1.20, per_tokens=1000000)))
    table = ''
    for d in data.values():
        q, u = d['quality'], d['usage']
        cells = [escape(d['label']), f"{q['recognized_presences']}/35",
                 str(q['coarse_only_presences']), f"{q['false_presence_case_count']}/11",
                 f"{q['null_bbox_count']}/{q['total_reported_objects']}",
                 str(u['input_tokens']), str(u['output_tokens']),
                 f"${d['usd']:.7f}", f"{d['krw_at_1500']:.2f}원"]
        table += '<tr>'+''.join('<td>'+cell+'</td>' for cell in cells)+'</tr>'
    photos = [image_uri(separated / 'memory/captures' / (r['id']+'.jpg'))
              for r in data['separated']['rows']]
    marked = [image_uri(root / 'marks' / f'F{n:02d}.jpg') for n in range(1, 25)]
    payload = json.dumps(dict(methods=data, photos=photos, marked=marked),
                         ensure_ascii=False).replace('<', '\\u003c')
    html = Path(__file__).with_name('patrol_methods_report.html').read_text()
    (root / 'report.html').write_text(html.replace('TABLE', table).replace('PAYLOAD', payload))
    for method, d in data.items():
        print(method, d['quality']['recognized_presences'], '/35',
              'false', d['quality']['false_presence_case_count'], '/11',
              'USD', round(d['usd'], 7), 'KRW', round(d['krw_at_1500'], 4))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--separated', type=Path, required=True)
    args = parser.parse_args()
    build(args.root, args.original, args.separated)
