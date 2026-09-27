#!/usr/bin/env python3
"""Offline exact-frame spatial audit. Never calls a provider or modifies annotations."""
import argparse
import base64
from collections import Counter, defaultdict
import csv
import html
import io
import json
import math
from pathlib import Path
import statistics

from paid_vlm.inputs import assess, load_bundle, private_dir, require, save, sha
from score_fall_baseline import match_boxes


def read(path):
    return json.loads(path.read_bytes())


def canvas_box(box, meta, dimensions=(640, 400)):
    """Apply the same resize rounding/center padding as the frozen JPEG extractor."""
    width, height = meta['width'], meta['height']
    cw, ch = dimensions
    require(len(box) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in box),
            'invalid GT box')
    left, top, right, bottom = box
    require(0 <= left < right <= width and 0 <= top < bottom <= height, 'invalid GT bounds')
    scale = min(cw / width, ch / height)
    rw, rh = round(width * scale), round(height * scale)
    dx, dy = (cw - rw) // 2, (ch - rh) // 2
    return [left * rw/width + dx, top * rh/height + dy,
            right * rw/width + dx, bottom * rh/height + dy]


def exact_gt(case, source_frame, meta):
    people = []
    for person in case['persons']:
        boxes = [box[1:] for box in person['boxes'] if box[0] == source_frame]
        require(len(boxes) <= 1, 'duplicate GT frame')
        if boxes:
            people.append(dict(person_id=person['person_id'], role=person['role'],
                               box=canvas_box(boxes[0], meta)))
    return people


def overlap(gt, prediction):
    intersection = max(0, min(gt[2], prediction[2])-max(gt[0], prediction[0])) * max(
        0, min(gt[3], prediction[3])-max(gt[1], prediction[1]))
    area_gt = (gt[2]-gt[0])*(gt[3]-gt[1])
    area_pred = (prediction[2]-prediction[0])*(prediction[3]-prediction[1])
    require(area_gt > 0 and area_pred > 0, 'invalid area')
    return dict(iou=intersection/(area_gt+area_pred-intersection),
                gt_box_coverage=intersection/area_gt, prediction_box_coverage=intersection/area_pred)


def audit_case(row, record, annotation, meta, criteria):
    evidence = record['evidence']
    indices = evidence['frame_indices']
    require(evidence['source_sha256'] == meta['sha256'], 'different source video')
    require(evidence['dimensions'] == [640, 400], 'unknown canvas')
    require(len(indices) == len(set(indices)) == len(record['common']['images']), 'frame mapping')
    require(all(type(f) is int and 0 <= f < meta['frames'] for f in indices), 'source frame bounds')
    require(len(indices) == len(evidence['source_times_s']) and all(
        abs(frame/meta['fps']-stamp) < 1e-9 for frame, stamp in zip(indices, evidence['source_times_s'])),
        'source frame time mismatch')
    normalized = row.get('normalized_response')
    require(row['outcome'] == 'classified' or normalized is None, 'invalid response was salvaged')
    predictions = defaultdict(list)
    for fi, finding in enumerate((normalized or {}).get('findings', [])):
        for region in finding['regions']:
            index = region['frame_index']
            require(type(index) is int and 0 <= index < len(indices), 'invalid supplied index')
            left, top, right, bottom = region['box']
            require(0 <= left < right <= 1 and 0 <= top < bottom <= 1, 'invalid normalized prediction')
            predictions[index].append(dict(finding_index=fi,
                box=[left*640, top*400, right*640, bottom*400]))
    regions, frames = [], []
    for index, source_frame in enumerate(indices):
        gt = exact_gt(annotation, source_frame, meta)
        pred = predictions[index]
        matches = match_boxes([g['box'] for g in gt], [p['box'] for p in pred], criteria)
        matched = {pi: gi for gi, (status, pi) in enumerate(matches) if status == 'matched'}
        target_index = next((i for i, p in enumerate(gt)
                            if p['person_id'] == annotation['target_person_id']), None)
        frame = dict(image_index=index, source_frame=source_frame, gt=gt, predictions=pred,
                     unannotated_person_ids=[p['person_id'] for p in annotation['persons']
                                             if p['person_id'] not in {g['person_id'] for g in gt}],
                     target_status=None)
        if target_index is not None:
            if row['outcome'] != 'classified':
                frame['target_status'] = 'unusable_response'
            elif not pred:
                frame['target_status'] = 'no_region_for_this_frame'
            else:
                frame['target_status'] = matches[target_index][0]
        frames.append(frame)
        for pi, prediction in enumerate(pred):
            measurements = [dict(person_id=g['person_id'], role=g['role'],
                                 **overlap(g['box'], prediction['box'])) for g in gt]
            person = gt[matched[pi]] if pi in matched else None
            target = next((m for m in measurements if m['person_id'] == annotation['target_person_id']), None)
            regions.append(dict(image_index=index, source_frame=source_frame,
                finding_index=prediction['finding_index'], box=prediction['box'],
                exact_gt_count=len(gt), all_persons_annotated=not frame['unannotated_person_ids'],
                association='matched' if person else ('unlinked_or_ambiguous' if gt else 'no_exact_gt'),
                matched_person=person['person_id'] if person else None,
                matched_role=person['role'] if person else None,
                target=target, comparisons=measurements,
                best_iou=max((m['iou'] for m in measurements), default=None)))
    identities = []
    for fi, finding in enumerate((normalized or {}).get('findings', [])):
        matches = [r for r in regions if r['finding_index'] == fi and r['matched_person'] is not None]
        pids = sorted({r['matched_person'] for r in matches})
        identities.append(dict(finding_index=fi, exact_matched_samples=len(matches), person_ids=pids,
            status='different_persons_in_exact_samples' if len(pids)>1 else
                   ('same_person_in_at_least_two_exact_samples' if len(matches)>=2 else 'insufficient_exact_samples')))
    return dict(case_id=row['case_id'], outcome=row['outcome'], label=row.get('reported_assessment'),
        issues=row.get('response_issue_codes'), target_person_id=annotation['target_person_id'],
        annotated_person_count=len(annotation['persons']), regions=regions, frames=frames,
        findings=identities, returned_regions=len(regions),
        exact_input_frames=sum(bool(f['gt']) for f in frames))


def describe(values):
    return dict(n=len(values), median=statistics.median(values) if values else None,
                minimum=min(values) if values else None)


def summarize(cases):
    regions = [r for c in cases for r in c['regions']]
    exact = [r for r in regions if r['exact_gt_count']]
    target = [r for r in regions if r['target'] is not None]
    findings = [f for c in cases for f in c['findings']]
    return dict(cases=len(cases), usable_responses=sum(c['outcome']=='classified' for c in cases),
        invalid_cases=[c['case_id'] for c in cases if c['outcome']!='classified'],
        returned_regions=len(regions), with_exact_gt=len(exact), without_exact_gt=len(regions)-len(exact),
        cases_with_exact_region_gt=sum(any(r['exact_gt_count'] for r in c['regions']) for c in cases),
        matched_any_person=sum(r['matched_person'] is not None for r in exact),
        matched_roles=dict(Counter(r['matched_role'] for r in exact if r['matched_role'])),
        best_iou_to_any_exact_gt=describe([r['best_iou'] for r in exact]),
        exact_target_regions=len(target), target_iou=describe([r['target']['iou'] for r in target]),
        target_gt_box_coverage=describe([r['target']['gt_box_coverage'] for r in target]),
        target_iou_at_least_05=sum(r['target']['iou'] >= .5 for r in target),
        target_gt_box_coverage_below_05=sum(r['target']['gt_box_coverage'] < .5 for r in target),
        exact_target_frames=dict(Counter(f['target_status'] for c in cases for f in c['frames']
                                        if f['target_status'] is not None)),
        finding_identity_checks=dict(Counter(f['status'] for f in findings)),
        notes=['Counts are conditional on exact reviewed frames, not whole84 localization accuracy.',
               'No region on a frame is not an error: model is asked for only 2-4 samples.',
               'Missing GT is unknown, never an absent person or an incorrect prediction.',
               'Same-person matches at sparse samples do not prove continuous tracking.'])


def render(case, record, destination, title):
    from PIL import Image, ImageDraw
    # Display every supplied frame, including normal replies and invalid responses.
    canvas = Image.new('RGB', (1280, 6*440+38), '#151a22')
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 8), title+' | cyan: model, green: target GT, yellow: other GT', fill='white')
    for frame, encoded in zip(case['frames'], record['common']['images']):
        index = frame['image_index']
        img = Image.open(io.BytesIO(base64.b64decode(encoded))).convert('RGB')
        require(img.size == (640, 400), 'unexpected JPEG dimensions')
        overlay = ImageDraw.Draw(img)
        for gt in frame['gt']:
            color = '#72ff67' if gt['role']=='target' else '#ffe060'
            overlay.rectangle(gt['box'], outline=color, width=2)
            overlay.text((gt['box'][0], max(0, gt['box'][1]-12)), 'GT '+gt['person_id'], fill=color)
        for prediction in frame['predictions']:
            overlay.rectangle(prediction['box'], outline='#00e5ff', width=3)
            overlay.text((prediction['box'][0]+3, prediction['box'][1]+3),
                         'model '+str(prediction['finding_index']), fill='#00e5ff')
        x, y = index%2*640, index//2*440+38
        draw.text((x+8, y+8), f'image {index}, source f{frame["source_frame"]}, '
                  + ('exact GT' if frame['gt'] else 'NO EXACT GT; NOT SCORED'), fill='white')
        canvas.paste(img, (x, y+28))
    canvas.save(destination, quality=92)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison', type=Path, required=True)
    parser.add_argument('--spatial', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    comparison = read(args.comparison)
    spatial = args.spatial
    freeze = read(spatial/'freeze.json')
    baseline_hashes = {str(args.comparison): sha(args.comparison), str(spatial/'freeze.json'): sha(spatial/'freeze.json')}
    for name, digest in freeze['files'].items():
        require(Path(name).name == name and sha(spatial/name) == digest, 'changed spatial overlay')
        baseline_hashes[str(spatial/name)] = digest
    require(read(spatial/'approval.json')['user_confirmed_complete'], 'GT approval missing')
    bundle = read(spatial/'evaluation_labels.json')
    annotations = {r['case_id']:r for r in bundle['annotations']['cases']}
    labels = {r['case_id']:r for r in bundle['classifications']['cases']}
    metas = {r['case_id']:r for r in read(spatial/'media.json')['cases']}
    require(len(metas)==84 and set(metas)==set(annotations)==set(labels), 'incomplete GT')
    require(all(c['spatial_review_status'] in ('user_approved', 'user_approved_20260917')
                for c in annotations.values()), 'unapproved annotation')
    private_dir(args.output)
    sheets = args.output/'sheets'
    sheets.mkdir(mode=0o700)
    reports, summaries, csv_rows, links = {}, {}, [], []
    bundles = {}
    for model, model_comparison in comparison.items():
        require(model in ('gemma','gemini') and model_comparison['complete_full84'], 'unexpected model/scope')
        pairs = {p['case_id']:p for p in model_comparison['pairs']}
        cases = []
        for path_str in model_comparison['result_files']:
            path = Path(path_str)
            row = read(path)
            cid = row['case_id']
            root = path.parent.parent
            if root not in bundles:
                manifest, _ = load_bundle(root/'inputs')
                require(manifest['freeze_sha256']==freeze['parent_freeze_sha256'] and
                        manifest['labels_sha256']==freeze['parent_labels_sha256'], 'different frozen dataset')
                bundles[root] = manifest
                baseline_hashes[str(root/'inputs/manifest.json')] = sha(root/'inputs/manifest.json')
            manifest = bundles[root]
            require(row['model']==model_comparison['model'] and
                    manifest['labels'][cid]==labels[cid]['label']==pairs[cid]['expected'], 'model/label mismatch')
            source = root/'inputs'/(cid+'.input.json')
            record = read(source)
            require(sha(source)==manifest['files'][source.name], 'changed input')
            baseline_hashes[str(path)], baseline_hashes[str(source)] = sha(path), sha(source)
            rescored = assess(row['text'], len(record['common']['images']), 'native_boxes_v4')
            require(all(row.get(k)==v for k,v in rescored.items()), 'score changed')
            require(all(row.get(k)==v for k,v in pairs[cid]['v4'].items()), 'comparison changed')
            case = audit_case(row, record, annotations[cid], metas[cid], freeze['match'])
            case.update(expected=labels[cid]['label'], original_source=labels[cid].get('original_source_path',
                        labels[cid].get('source_path')), result_path=str(path))
            sheet = f'{model}-{cid}.jpg'
            render(case, record, sheets/sheet, f'{model}/{cid}: '+str(case['label']))
            case['sheet'] = 'sheets/'+sheet
            cases.append(case)
            for r in case['regions']:
                csv_rows.append(dict(model=model, case_id=cid, source_frame=r['source_frame'],
                    image_index=r['image_index'], finding_index=r['finding_index'],
                    exact_gt_count=r['exact_gt_count'], association=r['association'], matched_person=r['matched_person'],
                    matched_role=r['matched_role'], best_iou=r['best_iou'],
                    target_iou=(r['target'] or {}).get('iou'),
                    target_coverage=(r['target'] or {}).get('gt_box_coverage')))
            links.append((model,cid,case['label'],case['sheet'],case['original_source']))
        require(len(cases)==len({c['case_id'] for c in cases})==84, 'missing/duplicate results')
        reports[model], summaries[model] = cases, summarize(cases)
    require(all(sha(Path(p))==h for p,h in baseline_hashes.items()), 'source changed during audit')
    save(args.output/'cases.json', reports)
    save(args.output/'summary.json', dict(models=summaries, api_calls=0, cost_usd=0,
        ground_truth_boxes=sum(len(p['boxes']) for c in annotations.values() for p in c['persons']),
        gt_interpolation=False, match_criteria=freeze['match'], source_files_unchanged=True,
        whole_dataset_target_and_timing_metrics_ready=bundle['annotations'].get('whole_dataset_target_and_timing_metrics_ready'),
        annotation_provenance='Frozen user-reviewed spatial overlay; no new GT or target IDs inferred.'))
    save(args.output/'provenance.json', dict(source_files=baseline_hashes, script_sha256=sha(Path(__file__))))
    with (args.output/'regions.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(csv_rows[0]))
        writer.writeheader(); writer.writerows(csv_rows)
    rows = ''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (m,cid,label,source))+
                   '<td><a href="'+link+'">12 frames</a></td></tr>' for m,cid,label,link,source in sorted(links))
    page = '<!doctype html><meta charset="utf-8"><title>VLM exact-frame box audit</title>'
    page += '<h1>84 cases × 2 models: saved replies only</h1><p>Cyan: model; green: target GT; yellow: other GT. '
    page += 'GT is drawn only on the exact reviewed frame. No interpolation. Missing GT does not mean no person.</p>'
    page += '<p>This index is review material, not human approval of every prediction.</p><table border="1">'
    page += '<tr><th>Model</th><th>ID</th><th>Reply</th><th>Original source</th><th>Sheet</th></tr>'+rows+'</table>'
    (args.output/'index.html').write_text(page, encoding='utf-8')
    save(args.output/'completed.json', dict(files={str(p.relative_to(args.output)):sha(p)
        for p in args.output.rglob('*') if p.is_file()}, source_files_unchanged=True))
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
