#!/usr/bin/env python3
"""Score frozen sparse labels against a completed offline replay, never interpolate GT."""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics

from replay_fall_baseline import sample_frames, sha, verify_freeze, write_json
from review_fall_annotations import box_at, require


def normalized_box(observation, meta):
    b = observation['pose']['box']
    return [b['left'] * meta['width'], b['top'] * meta['height'],
            b['right'] * meta['width'], b['bottom'] * meta['height']]


def overlap_score(gt, pred, criteria):
    area_gt = (gt[2] - gt[0]) * (gt[3] - gt[1])
    area_pred = (pred[2] - pred[0]) * (pred[3] - pred[1])
    require(area_gt > 0 and area_pred > 0, 'non-positive box area')
    overlap = max(0, min(gt[2], pred[2]) - max(gt[0], pred[0])) * max(
        0, min(gt[3], pred[3]) - max(gt[1], pred[1]))
    a, b = overlap / area_gt, overlap / area_pred
    center = ((gt[0] + gt[2]) / 2, (gt[1] + gt[3]) / 2)
    eligible = (a >= criteria['visible_coverage'] and b >= criteria['prediction_coverage']
                and pred[0] <= center[0] <= pred[2] and pred[1] <= center[1] <= pred[3])
    return math.sqrt(a * b) if eligible else None


def match_boxes(ground_truth, predictions, criteria):
    """Return GT-aligned (status, index) pairs; uncertain ties never borrow an identity."""
    scores = {(g, p): overlap_score(gt, pred, criteria)
              for g, gt in enumerate(ground_truth) for p, pred in enumerate(predictions)}
    rows = {g: sorted(((s, p) for (i, p), s in scores.items() if i == g and s is not None),
                      reverse=True) for g in range(len(ground_truth))}
    cols = {p: sorted(((s, g) for (g, j), s in scores.items() if j == p and s is not None),
                      reverse=True) for p in range(len(predictions))}

    def unique(options):
        return len(options) == 1 or options[0][0] - options[1][0] >= criteria['margin']

    result = []
    for g, options in rows.items():
        if not options:
            result.append(('unlinked', None))
            continue
        _, p = options[0]
        if cols[p][0][1] == g and unique(options) and unique(cols[p]):
            result.append(('matched', p))
        else:
            result.append(('ambiguous', None))
    return result


def anchors(case, rows, meta, criteria):
    records = []
    lookup = {row['frame_index']: row for row in rows}
    for frame in sorted({b[0] for p in case['persons'] for b in p['boxes']}):
        require(frame in lookup, f'{case["case_id"]}: labelled frame not sampled: {frame}')
        row = lookup[frame]
        people = [p for p in case['persons'] if box_at(p, frame) is not None]
        observations = row['observations']
        gt = [box_at(p, frame) for p in people]
        predictions = [normalized_box(o, meta) for o in observations]
        matches = match_boxes(gt, predictions, criteria)
        strong = [i for i, o in enumerate(observations) if o['pose']['boxConfidence'] >= .45]
        strong_matches = match_boxes(gt, [predictions[i] for i in strong], criteria)
        diagnostics = {d['targetTrackId']: d for d in row['fall_analysis']['tracks']}
        for person, (status, index), (strong_status, _) in zip(people, matches, strong_matches):
            obs = observations[index] if index is not None else None
            diag = diagnostics.get(obs['track_id']) if obs else None
            records.append(dict(
                case_id=case['case_id'], person_id=person['person_id'], role=person['role'],
                frame_index=frame, status=status, strong_status=strong_status,
                usable=bool(obs and obs['features']['usable']),
                track_id=obs['track_id'] if obs else None,
                candidate_status=diag['status'] if diag else None,
                observation_index=index,
            ))
    return records


def event_key(case_id, frame, candidate):
    return f'{case_id}:{frame}:{candidate["candidateId"]}:{candidate["revision"]}'


def events(rows):
    output = []
    lookup = {(r['case_id'], r['frame_index']): r for r in rows}
    latest = {}
    for row in rows:
        for candidate in row['fall_analysis']['candidates']:
            associated = [o for o in row['observations']
                          if o['track_id'] == candidate['targetTrackId']]
            historical = {}
            if candidate.get('observationAvailability') == 'missing':
                require(not associated, 'missing claim despite current observation')
                tid = candidate['targetTrackId']
                diagnostic = [d for d in row['fall_analysis']['tracks']
                              if d['targetTrackId'] == tid]
                require(len(diagnostic) == 1 and diagnostic[0]['trackingState'] == 'missing',
                        'historical request requires explicitly missing track')
                ref = candidate['evidenceReference']
                source = lookup.get((row['case_id'], ref['frameIndex']))
                require(source is not None and source['frame_index'] < row['frame_index'],
                        'historical evidence must refer to an earlier actual frame')
                require(latest.get((row['case_id'], tid)) == ref['frameIndex'],
                        'historical evidence is not the latest actual observation')
                require(0 < row['timestamp_s'] - source['timestamp_s'] <= .5 + 1e-9,
                        'historical evidence exceeds the short missing window')
                require(ref['timestampSec'] == source['timestamp_s']
                        and candidate['evidenceEndSec'] == source['timestamp_s'],
                        'historical timestamp must be the actual observation time')
                require(candidate['observationId'] == source['fall_analysis']['observationId'],
                        'historical observation ID mismatch')
                associated = [o for o in source['observations'] if o['track_id'] == tid]
                require(len(associated) == 1 and
                        associated[0]['observation_index'] == ref['observationIndex'],
                        'historical observation index mismatch')
                historical = dict(association_mode='last_observed_not_current',
                                  evidence_frame_index=source['frame_index'],
                                  evidence_timestamp_s=source['timestamp_s'])
            require(len(associated) == 1, 'candidate without unique current observation')
            output.append(dict(
                key=event_key(row['case_id'], row['frame_index'], candidate),
                case_id=row['case_id'], frame_index=row['frame_index'],
                timestamp_s=row['timestamp_s'], candidate=candidate,
                observation=associated[0], **historical,
            ))
        for obs in row['observations']:
            if obs['track_id'] is not None:
                latest[row['case_id'], obs['track_id']] = row['frame_index']
    require(len({e['key'] for e in output}) == len(output), 'duplicate event record')
    return output


def timing(case, timestamp, fps):
    origin = (case['first_down_frames'] if case['entry_state'] == 'already_down'
              else case['onset_frames'])
    if origin is None:
        return dict(basis='unknown', delay_s=None, position='unknown')
    low, high = timestamp - origin[1] / fps, timestamp - origin[0] / fps
    return dict(basis='first_down' if case['entry_state'] == 'already_down' else 'onset',
                delay_s=[low, high],
                position='early' if high < 0 else 'boundary_uncertain' if low < 0 else 'after')


def validate_audit(audit, all_events, frames_sha):
    require(audit['frames_sha256'] == frames_sha, 'audit belongs to another replay')
    require(audit['method'] == 'manual_RGB_output_association_not_blind', 'audit method')
    mapped = {e['key']: e for e in audit['events']}
    require(len(mapped) == len(audit['events']), 'duplicate audit event')
    require(set(mapped) == {e['key'] for e in all_events}, 'missing/extra audit events')
    for item in mapped.values():
        require(item['association'] in {'target', 'other_person', 'background', 'unknown'},
                'unknown audit association')
        require(bool(item['note'].strip()), 'audit note missing')
    return mapped


def case_score(case, classification, case_events, audit, fps):
    evaluated = []
    for event in case_events:
        judgment = audit.get(event['key'], dict(association='unknown', note='not audited'))
        require(classification['expected_candidate_detection']
                or judgment['association'] != 'target', 'normal case has no GT target')
        evidence_time = event.get('evidence_timestamp_s', event['timestamp_s'])
        evaluated.append(dict(
            key=event['key'], frame_index=event['frame_index'], timestamp_s=event['timestamp_s'],
            kind=event['candidate']['candidateKind'], association=judgment['association'],
            note=judgment['note'], **timing(case, event['timestamp_s'], fps),
            association_mode=event.get('association_mode', 'current_observation'),
            evidence_frame_index=event.get('evidence_frame_index', event['frame_index']),
            evidence_timestamp_s=evidence_time,
            evidence_position=timing(case, evidence_time, fps)['position'],
        ))
    hits = [e for e in evaluated if e['association'] == 'target'
            and e['position'] in {'after', 'boundary_uncertain'}
            and e['evidence_position'] in {'after', 'boundary_uncertain'}]
    if not classification['expected_candidate_detection']:
        outcome = 'unnecessary_candidate' if evaluated else 'no_candidate_on_negative'
    elif hits:
        outcome = 'target_candidate'
    elif not evaluated:
        outcome = 'no_output'
    elif any(e['association'] == 'unknown' or
             (e['association'] == 'target' and 'unknown' in
              (e['position'], e['evidence_position'])) for e in evaluated):
        outcome = 'unresolved_association_or_time'
    elif any(e['association'] == 'target' for e in evaluated):
        outcome = 'early_only'
    else:
        outcome = 'wrong_target_only'
    return dict(case_id=case['case_id'], source_path=classification['source_path'],
                classification=classification['label'], entry_state=case['entry_state'],
                evaluation_role=('descriptive_only' if classification['label'] in
                                 {'suspected_fall', 'unobservable'} else 'scored'),
                expected=classification['expected_candidate_detection'], outcome=outcome,
                output_count=len(evaluated), events=evaluated,
                first_target_candidate=min(hits, key=lambda e: e['timestamp_s']) if hits else None)


def percent(n, d):
    return None if d == 0 else n / d


def separated_groups(case_reports, evaluation_version='v1'):
    """Keep ambiguous cases visible without calling them fall FN or normal TN."""
    names = dict(observed_fall='낙상 동작 관찰', found_down='누운 사람 발견',
                 normal_activity='정상 동작', suspected_fall='낙상 의심·확정 불가',
                 unobservable='관찰 불가')
    require(evaluation_version in ('v1', 'v2'), 'unknown evaluation version')
    if evaluation_version == 'v2':
        from fall_evaluation_v2 import NAMES
        names = NAMES
    require(all(c['classification'] in names for c in case_reports), 'unknown label group')
    groups = {}
    for label, name in names.items():
        cases = [c for c in case_reports if c['classification'] == label]
        group = dict(name=name, total=len(cases),
                     output_count=sum(c['output_count'] for c in cases),
                     multiple_output_cases=[c['case_id'] for c in cases if c['output_count'] > 1],
                     cases_with_output=[c['case_id'] for c in cases if c['output_count']],
                     cases_without_output=[c['case_id'] for c in cases if not c['output_count']])
        if label in {'observed_fall', 'found_down'} or (
                evaluation_version == 'v2' and label == 'suspected_fall'):
            hits = [c for c in cases if c['outcome'] == 'target_candidate']
            unknown = [c for c in cases if c['outcome'] == 'unresolved_association_or_time']
            group.update(
                evaluation_role='scored_verification_candidates',
                target_candidate_cases=[c['case_id'] for c in hits],
                missed_cases=[c['case_id'] for c in cases if c not in hits + unknown],
                unresolved_cases=[c['case_id'] for c in unknown],
                candidate_coverage_bounds=[percent(len(hits), len(cases)),
                                           percent(len(hits) + len(unknown), len(cases))],
                last_observed_request_cases=[c['case_id'] for c in hits if
                                             c['first_target_candidate']['association_mode']
                                             == 'last_observed_not_current'],
            )
        elif label == 'normal_activity':
            group.update(evaluation_role='scored_unnecessary_candidates',
                         unnecessary_candidate_cases=group['cases_with_output'],
                         unnecessary_candidate_rate=percent(len(group['cases_with_output']),
                                                            len(cases)))
        else:
            group.update(evaluation_role='descriptive_only',
                         interpretation='Output presence only; not fall FN, normal TN, or accuracy')
        groups[label] = group
    return groups


def aggregate(case_reports, anchor_records, rows, evaluation_version='v1'):
    targets = [c for c in case_reports if c['expected']]
    negatives = [c for c in case_reports if not c['expected']]
    target_anchors = [r for r in anchor_records if r['role'] == 'target']
    outcome = Counter(c['outcome'] for c in targets)
    matched = sum(r['status'] == 'matched' for r in target_anchors)
    times = sorted(r['pipeline_ms'] for r in rows)
    hits = outcome['target_candidate']
    unknown = outcome['unresolved_association_or_time']
    return dict(
        grouping_version=2 if evaluation_version == 'v1' else 3,
        evaluation_version=evaluation_version,
        groups=separated_groups(case_reports, evaluation_version),
        legacy_candidate_bookkeeping=dict(
            interpretation='Original expected_candidate_detection flags; not fall accuracy',
            target_cases=len(targets), negative_cases=len(negatives),
            target_outcomes=dict(outcome),
            target_candidate_rate_bounds=[percent(hits, len(targets)),
                                          percent(hits + unknown, len(targets))]),
        target_annotated_boxes=len(target_anchors), target_box_matches=matched,
        target_box_match_rate=percent(matched, len(target_anchors)),
        target_strong_box_matches=sum(r['strong_status'] == 'matched' for r in target_anchors),
        target_box_ambiguous=sum(r['status'] == 'ambiguous' for r in target_anchors),
        target_usable_pose_boxes=sum(r['usable'] for r in target_anchors),
        target_tracked_boxes=sum(r['track_id'] is not None for r in target_anchors),
        sampled_frames=len(rows),
        pipeline_ms=dict(median=statistics.median(times),
                         p95=times[math.ceil(len(times) * .95) - 1], maximum=times[-1]),
    )


def validate_rows(rows, media, run):
    expected = {(m['case_id'], i): (i / m['fps']) for m in media['cases']
                for i in sample_frames(m['frames'], m['fps'], run['sample_fps'])}
    seen = set()
    last = {}
    for row in rows:
        key = row['case_id'], row['frame_index']
        require(key in expected and key not in seen, 'duplicate/unexpected sampled frame')
        require(row['frame_index'] > last.get(row['case_id'], -1), 'non-increasing frames')
        require(abs(row['timestamp_s'] - expected[key]) < 1e-8, 'wrong source timestamp')
        require(row['fall_analysis']['status'] == 'ok', 'failed analysis is not a miss')
        require(row['fall_analysis']['configSha256'] == run['fall_config_sha256'],
                'fall settings mismatch')
        require(all(math.isfinite(row[k]) and row[k] >= 0 for k in ('pose_ms', 'pipeline_ms')),
                'invalid runtime measurement')
        seen.add(key)
        last[row['case_id']] = row['frame_index']
    require(seen == set(expected), 'incomplete replay cannot be scored as FN')


def render(dataset, output, cases, metas, rows, all_events):
    """Deterministic overlays of actual predictions, not AI-generated imagery."""
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    for case in cases:
        cid = case['case_id']
        meta = metas[cid]
        lookup = {r['frame_index']: r for r in rows if r['case_id'] == cid}
        cap = cv2.VideoCapture(str(dataset / meta['source_path']))
        selected_events = [e for e in all_events if e['case_id'] == cid]
        panels = [(frame, None) for frame in (0, 12, 24, 36, 48, 60) if frame in lookup]
        for event in selected_events:
            start = event['candidate']['evidenceStartSec']
            first = min(lookup, key=lambda f: abs(f / meta['fps'] - start))
            panels.extend([(first, event), (event['frame_index'], event)])
            if event.get('association_mode') == 'last_observed_not_current':
                # Show actual evidence separately; never draw the old pose onto the missing frame.
                panels.insert(len(panels) - 1, (event['evidence_frame_index'], event))
        w, h = meta['width'], meta['height']
        sheet = Image.new('RGB', (w * 2, (h + 55) * math.ceil(len(panels) / 2)), '#171b22')
        draw = ImageDraw.Draw(sheet)
        try:
            for panel, (frame_index, event) in enumerate(panels):
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = cap.read()
                require(ok, 'review image decode failed')
                tile = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                marks = ImageDraw.Draw(tile)
                for person in case['persons']:
                    box = box_at(person, frame_index)
                    if box is not None:
                        marks.rectangle(box, outline='#20d473', width=3)
                        marks.text((box[0], max(0, box[1] - 18)), person['person_id'],
                                   font=font, fill='#20d473', stroke_width=1, stroke_fill='black')
                row = lookup[frame_index]
                for obs in row['observations']:
                    box = normalized_box(obs, meta)
                    highlight = bool(event and obs['track_id']
                                     == event['candidate']['targetTrackId'])
                    color = '#ff623b' if highlight else '#36d4ff'
                    marks.rectangle(box, outline=color, width=3 if highlight else 1)
                    tag = (obs['track_id'].rsplit('-', 1)[-1] if obs['track_id'] else '?')
                    text = f'ID{tag} {obs["pose"]["boxConfidence"]:.2f}'
                    marks.text((box[0], max(0, box[1])), text, font=font, fill=color,
                               stroke_width=1, stroke_fill='black')
                    for point in obs['pose']['keypoints']:
                        if point['confidence'] >= .5:
                            x, y = point['x'] * w, point['y'] * h
                            marks.ellipse((x-2, y-2, x+2, y+2), fill=color)
                x, y = (panel % 2) * w, (panel // 2) * (h + 55)
                sheet.paste(tile, (x, y + 55))
                label = f'{cid} frame {frame_index} / {frame_index / meta["fps"]:.3f}s'
                draw.text((x + 6, y + 4), label, font=font, fill='white')
                detail = (f'{event["candidate"]["candidateKind"]} emission f{event["frame_index"]}'
                          if event else 'GT green / model cyan / output track orange')
                if event and event.get('association_mode') == 'last_observed_not_current':
                    detail = (f'Last seen f{event["evidence_frame_index"]}; '
                              f'request f{event["frame_index"]}; current pose missing')
                draw.text((x + 6, y + 28), detail, font=font, fill='white')
        finally:
            cap.release()
        sheet.save(output / f'{cid}-review.jpg', quality=90)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audit', type=Path)
    parser.add_argument('--render-dataset', type=Path)
    args = parser.parse_args()
    frozen = verify_freeze(args.frozen)
    require(frozen.get('whole_dataset_target_and_timing_metrics_ready', True),
            'whole-dataset target/timing annotations unavailable; use video-level candidate counts')
    criteria = frozen['match']
    completed = json.loads((args.run / 'completed.json').read_text())
    for key, name in (('frames_sha256', 'frames.jsonl'), ('run_sha256', 'run.json')):
        require(sha(args.run / name) == completed[key], f'changed replay file: {name}')
    run = json.loads((args.run / 'run.json').read_text())
    require(run['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
    require(run['media_sha256'] == sha(args.frozen / 'media.json'), 'wrong media')
    media = json.loads((args.frozen / 'media.json').read_text())
    bundle = json.loads((args.frozen / 'evaluation_labels.json').read_text())
    rows = [json.loads(line) for line in (args.run / 'frames.jsonl').read_text().splitlines()]
    validate_rows(rows, media, run)
    cases = bundle['annotations']['cases']
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    evaluation_version = ('v2' if bundle['classifications'].get('schema_version') ==
                          'malbut.synthetic-video-human-review.v2' else 'v1')
    metas = {m['case_id']: m for m in media['cases']}
    all_events = events(rows)
    audit = (validate_audit(json.loads(args.audit.read_text()), all_events,
                            completed['frames_sha256']) if args.audit else {})
    anchor_records, case_reports = [], []
    for case in cases:
        cid = case['case_id']
        case_rows = [r for r in rows if r['case_id'] == cid]
        anchor_records.extend(anchors(case, case_rows, metas[cid], criteria))
        case_reports.append(case_score(case, labels[cid],
                                       [e for e in all_events if e['case_id'] == cid],
                                       audit, metas[cid]['fps']))
    if evaluation_version == 'v2':
        for case in case_reports:
            if case['classification'] == 'suspected_fall':
                case['evaluation_role'] = 'scored_verification_candidates'
    summary = aggregate(case_reports, anchor_records, rows, evaluation_version)
    summary['spatial_unknown_explicitly_excluded'] = sum(
        len(p.get('spatial_unknown_frames', [])) for c in cases for p in c['persons'])
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_json(args.output / 'report.json', dict(
        schema_version=2,
        scope='development synthetic pilot, not clinical accuracy or robot E2E',
        freeze_sha256=sha(args.frozen / 'freeze.json'), run_sha256=sha(args.run / 'run.json'),
        frames_sha256=completed['frames_sha256'], scorer_sha256=sha(Path(__file__)),
        audit_sha256=sha(args.audit) if args.audit else None,
        timing_note=run.get('timing_note', 'pipeline_ms measured during this replay'),
        summary=summary, cases=case_reports, anchors=anchor_records,
    ))
    write_json(args.output / 'audit_template.json', dict(
        frames_sha256=completed['frames_sha256'], method='manual_RGB_output_association_not_blind',
        events=[dict(key=e['key'], association='unknown',
                     note=f'{e["case_id"]} frame {e["frame_index"]}: not yet reviewed')
                for e in all_events],
    ))
    if args.render_dataset:
        for meta in media['cases']:
            path = (args.render_dataset / meta['source_path']).resolve()
            require(path.is_relative_to(args.render_dataset.resolve()), 'media path escape')
            require(sha(path) == meta['sha256'], 'review media changed')
        render(args.render_dataset, args.output, cases, metas, rows, all_events)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
