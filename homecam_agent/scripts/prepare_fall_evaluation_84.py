#!/usr/bin/env python3
"""Freeze approved 84-video development inputs; never perform model inference.

The 77-video predecessor and raw review snapshots remain immutable. Approval
sidecars, not edits to historical draft files, authorize the effective annotations.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile

import prepare_fall_evaluation_v2 as previous

require, sha, save, unique = previous.require, previous.sha, previous.save, previous.unique
REVISION = 'fall84-v2-r1-20260917'
COUNTS = dict(observed_fall=25, suspected_fall=25, normal_activity=34)
PREVIOUS_SHA = '334fadcf1fc576b81db21d5b9fd6e1dc8ab1625c76eae57162b2ec10adabab4f'
ACCEPTED = ('V040', 'V043', 'V044', 'V045', 'V048', 'V049', 'V050')
EXCLUDED = ('V041', 'V042', 'V046', 'V047', 'V051')
MAPPING = {v: f'SYN{n:03d}' for n, v in enumerate(ACCEPTED, 78)}
TIMED = {'V001', 'V002', 'V009', 'V019', *ACCEPTED}
SAMPLES = [0, 24, 48, 72, 96, 120]
INPUT_NAMES = {
    'box36.json', 'box36_approval.json', 'box7.json', 'box7_approval.json',
    'new_index.json', 'new_labels.csv', 'new_labels_r1.csv', 'new_labels_r2.csv',
    'USER_REVIEW_R1.md', 'USER_REVIEW_R2.md', 'USER_REVIEW_R3.md',
    'timing_review.json', 'timing_review.csv', 'bundle_manifest.json',
    'timing_approval.json', 'timing_probe.json', 'timing_protocol.md',
    'v026_clarification.json',
}


def parsed(records, name):
    text = records[name]['text'].lstrip('\ufeff')
    return json.loads(text) if name.endswith('.json') else list(csv.DictReader(io.StringIO(text)))


def read_records(directory, names):
    records = {}
    for name in names:
        require(Path(name).name == name, 'unsafe record name')
        path = directory / name
        require(path.is_file() and not path.is_symlink(), 'missing/symlink record: ' + name)
        data = path.read_bytes()
        records[name] = dict(sha256=hashlib.sha256(data).hexdigest(), text=data.decode('utf-8'))
    return records


def validate_evidence(records):
    old, inputs = records['previous'], records['inputs']
    require(set(old) == previous.FILES | {'freeze.json'}, 'previous records incomplete')
    require(set(inputs) == INPUT_NAMES | {'input_manifest.json'}, 'input records incomplete')
    for group in (old, inputs):
        for name, record in group.items():
            require(hashlib.sha256(record['text'].encode('utf-8')).hexdigest() == record['sha256'],
                    'record hash mismatch: ' + name)
    require(old['freeze.json']['sha256'] == PREVIOUS_SHA, 'wrong predecessor')
    for name, expected in parsed(old, 'freeze.json')['files'].items():
        require(old[name]['sha256'] == expected, 'predecessor metadata changed')
    manifest = parsed(inputs, 'input_manifest.json')['files']
    require(set(manifest) == INPUT_NAMES, 'missing/extra input evidence')
    require(all(inputs[n]['sha256'] == h for n, h in manifest.items()), 'input snapshot changed')
    for group in ('36', '7'):
        approval = parsed(inputs, f'box{group}_approval.json')
        require(approval['status'] == 'user_approved', 'boxes not approved')
        require(approval['annotation_sha256'] == inputs[f'box{group}.json']['sha256'],
                'box approval hash mismatch')
    approval = parsed(inputs, 'box7_approval.json')
    require(approval['source_index_sha256'] == inputs['new_index.json']['sha256']
            and approval['approved_label_csv_sha256'] == inputs['new_labels.csv']['sha256'],
            'new labels/index not bound to approval')
    require(set(approval['scope']['review_video_ids']) == set(ACCEPTED)
            and set(approval['scope']['case_ids']) == {'GEN-' + v for v in ACCEPTED}
            and set(approval['excluded_video_ids']) == set(EXCLUDED), 'box approval scope changed')
    approval = parsed(inputs, 'timing_approval.json')
    require(approval['status'] == 'user_approved'
            and set(approval['scope']['review_video_ids']) == TIMED, 'timing approval scope changed')
    require(set(approval['approved_snapshot']) == {
        'timing_review.json', 'timing_review.csv', 'bundle_manifest.json'}, 'timing snapshot incomplete')
    require(all(inputs[n]['sha256'] == h for n, h in approval['approved_snapshot'].items()),
            'timing approval hash mismatch')


def interval(value, frames, name):
    if value is not None:
        require(isinstance(value, list) and len(value) == 2
                and all(type(v) is int for v in value)
                and 0 <= value[0] <= value[1] < frames, 'invalid interval: ' + name)


def validate_annotation(annotation, meta, sampled=False):
    persons = unique(annotation.get('persons', []), 'person_id')
    target = annotation.get('target_person_id')
    require(target is None or target in persons, 'unknown target identity')
    for person in persons.values():
        seen = set()
        for box in person.get('boxes', []):
            require(len(box) == 5 and all(type(n) is int for n in box), 'invalid box numbers')
            f, x1, y1, x2, y2 = box
            require(f not in seen and 0 <= f < meta['frames'], 'duplicate/outside box frame')
            require(0 <= x1 < x2 <= meta['width'] and 0 <= y1 < y2 <= meta['height'],
                    'box outside reviewed image')
            seen.add(f)
        if sampled:
            absent = person.get('not_visible_frames', []) + person.get('spatial_unknown_frames', [])
            require(len(absent) == len(set(absent)) and not seen.intersection(absent)
                    and seen | set(absent) == set(SAMPLES), 'sampled box coverage changed')
    for name in ('onset_frames', 'landing_frames', 'first_down_frames'):
        interval(annotation.get(name), meta['frames'], name)
    onset, landing = annotation.get('onset_frames'), annotation.get('landing_frames')
    if onset and landing:
        require(onset[0] <= landing[1], 'landing precedes onset')


def assemble(records):
    """Pure assembly, also rerun by verify to detect effective/source drift."""
    validate_evidence(records)
    old, inp = records['previous'], records['inputs']
    bundle = copy.deepcopy(parsed(old, 'evaluation_labels.json'))
    labels = unique(bundle['classifications']['cases'], 'case_id')
    annotations = unique(bundle['annotations']['cases'], 'case_id')
    media = unique(parsed(old, 'media.json')['cases'], 'case_id')
    ids77 = {f'SYN{n:03d}' for n in range(1, 78)}
    require(set(labels) == set(annotations) == set(media) == ids77, 'predecessor IDs changed')
    index = unique(parsed(inp, 'new_index.json')['videos'], 'video')
    rows = unique(parsed(inp, 'new_labels.csv'), 'video')
    require(set(index) == set(rows) == {v + '.mp4' for v in ACCEPTED + EXCLUDED},
            'new index/labels partition changed')
    excluded = []
    for v in EXCLUDED:
        row, source = rows[v + '.mp4'], index[v + '.mp4']
        require(not row['label'] and bool(row['quality_exclusion_reason']), 'exclusion changed')
        excluded.append(dict(row, video_sha256=source['sha256']))
    for v, cid in MAPPING.items():
        row, source = rows[v + '.mp4'], index[v + '.mp4']
        require(row['label'] == 'observed_fall' and row['reason']
                and not row['quality_exclusion_reason'] and source['status'] == 'ready',
                'unapproved accepted label')
        labels[cid] = dict(case_id=cid, review_case_id='GEN-' + v, video=v + '.mp4',
                           source_path='media/' + cid + '.mp4', source_sha256=source['sha256'],
                           original_source_path=source['path'],
                           source_group='observed-fall12-bf16-20260916_accepted7',
                           scene_group='observed-fall12:' + source['scene_id'],
                           label=row['label'], label_ko='낙상', reason=row['reason'],
                           expected_safety_assessment=True, label_review_revision='USER_REVIEW_R3.md')
        media[cid] = dict(sha256=source['sha256'], width=1280, height=720, fps=24., frames=123)
    clarification = parsed(inp, 'v026_clarification.json')
    c = labels['SYN066']
    require(clarification['case_id'] == c['case_id'] and clarification['review_video_id'] == 'V026'
            and clarification['label'] == c['label'] == 'normal_activity'
            and clarification['source_video_sha256'] == c['source_sha256'], 'V026 binding mismatch')
    c['reason_additions'] = [dict(text=clarification['additional_reason'],
                                 source_record='v026_clarification.json',
                                 source_sha256=inp['v026_clarification.json']['sha256'])]
    for group in ('36', '7'):
        boxes = parsed(inp, f'box{group}.json')['cases']
        by_video = unique(boxes, 'video')
        require(set(by_video) == ({c['video'][:-4] for c in labels.values()
                                  if c['source_group'] == 'additional36'} if group == '36'
                                 else set(ACCEPTED + EXCLUDED)), 'box source scope changed')
        selected = boxes if group == '36' else [by_video[v] for v in ACCEPTED]
        approval = parsed(inp, f'box{group}_approval.json')
        scope = approval['scope']
        require(scope['videos'] == len(selected)
                and scope['sampled_frames_per_video'] == SAMPLES
                and scope['sampled_frames_total'] == len(selected)*6
                and scope['boxes'] == sum(len(p['boxes']) for b in selected for p in b['persons'])
                and scope['within_clip_person_ids'] == sum(len(b['persons']) for b in selected),
                'approved box totals changed')
        for b in selected:
            cid = b['case_id'] if group == '36' else MAPPING[b['video']]
            require(cid in labels and b['source_sha256'] == media[cid]['sha256'], 'box/video hash mismatch')
            require(all(b[k] == media[cid][k] for k in ('width', 'height', 'fps'))
                    and b['frames'] == SAMPLES, 'box geometry mismatch')
            if group == '36':
                require(b['label'] == labels[cid]['label'], 'box label changed')
            else:
                require(b['case_id'] == 'GEN-' + b['video'] and len(b['persons']) == 1
                        and b['persons'][0]['person_id'] == 'P1', 'sole target assumption invalid')
            a = dict(case_id=cid, persons=copy.deepcopy(b['persons']),
                     target_person_id=b['target_person_id'], reviewed_frames=SAMPLES,
                     box_format='source_pixel_xyxy_visible_extent', interpolation='forbidden',
                     source_sha256=b['source_sha256'], spatial_annotations_available=True,
                     spatial_review_status='user_approved',
                     spatial_source_record=f'box{group}.json', spatial_approval_record=f'box{group}_approval.json',
                     temporal_annotations_available=False, onset_frames=None, landing_frames=None,
                     first_down_frames=None, temporal_review_status='not_annotated_not_event_absent',
                     notes=b.get('notes', ''))
            if group == '7':
                a['target_person_id'] = 'P1'
                a['persons'][0]['role'] = 'target'
                a['target_assignment_basis'] = 'sole_annotated_person_in_approved_fall_clip'
            annotations[cid] = a
    timing = unique(parsed(inp, 'timing_review.json')['cases'], 'video')
    require(set(timing) == TIMED, 'timing cases changed')
    by_video = {c['video'][:-4]: c for c in labels.values() if c.get('video')}
    for v, t in timing.items():
        label = by_video[v]
        cid, meta = label['case_id'], media[label['case_id']]
        require(t['case_id'] == ('GEN-' + v if v in MAPPING else cid)
                and t['sha256'] == meta['sha256'] and t['source_pts_verified'] is True
                and all(t[k] == meta[k] for k in ('width', 'height', 'fps'))
                and t['decoded_frames'] == meta['frames'], 'timing/video binding mismatch')
        for field in ('onset', 'landing'):
            interval(t[field + '_frames'], meta['frames'], field)
            require(t[field + '_frames'] is not None, 'approved interval missing')
            require(all(abs(s - f/meta['fps']) < .000002 for s, f in
                        zip(t[field + '_seconds'], t[field + '_frames'])), 'timing seconds mismatch')
            annotations[cid][field + '_frames'] = t[field + '_frames']
            annotations[cid][field + '_status'] = t[field + '_status']
        annotations[cid].update(temporal_annotations_available=True, temporal_review_status='user_approved',
                                timing_source_record='timing_review.json',
                                timing_approval_record='timing_approval.json', timing_note=t['note'])
    require(dict(Counter(c['label'] for c in labels.values())) == COUNTS, '84-label counts mismatch')
    require(len({c['source_sha256'] for c in labels.values()}) == 84, 'duplicate video bytes')
    require(not {c['source_sha256'] for c in labels.values()} & {e['video_sha256'] for e in excluded},
            'excluded video content included')
    for cid, annotation in annotations.items():
        validate_annotation(annotation, media[cid], sampled=int(cid[3:]) >= 42)
    falls = [annotations[cid] for cid, c in labels.items() if c['label'] == 'observed_fall']
    require(sum(a.get('onset_frames') is not None for a in falls) == 25
            and sum(a.get('landing_frames') is not None for a in falls) == 21, 'fall timing coverage changed')
    bundle['classifications'].update(counts=COUNTS, cases=[labels[k] for k in sorted(labels)])
    bundle['classifications']['exclusions']['observed_fall12'] = excluded
    bundle['annotations'].update(cases=[annotations[k] for k in sorted(annotations)],
                                 whole_dataset_target_and_timing_metrics_ready=False)
    bundle['review_use'] = 'Approved labels, sampled boxes and timing intervals merged; no new visual judgments.'
    bundle['dataset_revision'] = REVISION
    return bundle, media


PROTOCOL = '''# 84개 합성 영상 · 평가 입력 고정

낙상 25 / 낙상 의심 25 / 정상 행동 34. 기존 77개와 채택한 추가 7개를 합쳤다.
기존 SYN001–077 번호와 정답은 유지한다. V049는 최종 사용자 판단인 낙상으로,
V026은 정상 라벨을 유지하면서 추가 설명을 반영한다. 제외 영상 5개는 편입하지 않는다.

## 판단·채점

기존 fall-evaluation-v2-20260912-r1 기준을 그대로 사용한다. 3개 라벨 모두 채점하며,
낙상 의심을 분모에서 빼거나 추측으로 낙상/정상으로 바꾸지 않는다.
이유·정답·박스·생성 정보는 평가 전용이다. 모델 입력 목록은 media.json만 사용한다.
영상 바이트를 그대로 복사했으므로 음성 트랙이 남아 있을 수 있으나 입력은 RGB 전용이다.
실제 모델 호출 시 음성을 배제해야 하며 이 단계에서는 모델을 호출하지 않았다.

## 박스와 시간

원본 41개 박스는 그대로 유지한다. 추가 43개는 승인된 6개 프레임만 사용한다.
중간 프레임 보간이나 가려진 신체 추정은 하지 않는다. 박스가 없는 프레임을 사람 없음으로
해석하지 않는다. SYN075 첫 프레임은 원본의 not_visible_frames=[0]을 유지한다.
새 7개는 승인된 유일한 사람 P1을 대상으로 연결한다. 이는 단일 인물에 따른 연결이며
사용자가 별도 ID를 검증했다는 의미는 아니다. 원본 role은 source_records.json에 보존한다.

낙상 시작은 25개 모두 구간으로 표시되어 있다. 몸의 첫 바닥 접촉은 21개만 확인되며
가려진 기존 4개는 확인 불가(null)로 남긴다. 구간의 중간값을 단일 정답 시각으로 쓰지 않는다.
새로 승인된 11개 이외 기존 시간은 그대로 재사용한다. 모든 라벨의 시간 검토가 끝난 것은
아니므로 whole_dataset_target_and_timing_metrics_ready는 false를 유지한다.

감지 지연을 실제로 측정하려면 시간순 재생과 대상 인물 연결을 검증하고, 미탐을 별도 표시해야
한다. 전체 영상을 미리 본 VLM의 처리 시간은 온라인 감지 지연이 아니다. 이번에는 시간 정답만
준비했다. 모델·프레임 샘플링·타임아웃 등 실행 조건은 아직 정하지 않았다.

## 사용 범위

이미 사람이 검토하고 개선에 사용한 합성 개발 데이터다. 독립 시험 세트나 실물 성능 증명이
아니다. 해시가 같은 파일과 디코딩 결과 중복은 검사하지만 비슷한 장면의 독립성을 보장하지
않는다. 기존 묶음·원본 영상·검토 이력·기존 모델 평가 결과를 변경하지 않는다.
'''


def verify(directory):
    frozen = json.loads((directory / 'freeze.json').read_text())
    require(frozen['schema_version'] == previous.SCHEMA and frozen.get('dataset_revision') == REVISION
            and set(frozen['files']) == previous.FILES and frozen['cases'] == 84, 'invalid 84 freeze')
    for name, expected in frozen['files'].items():
        require(not (directory/name).is_symlink() and sha(directory/name) == expected, 'changed metadata: ' + name)
    records = json.loads((directory/'source_records.json').read_text())
    expected, expected_media = assemble(records)
    bundle = json.loads((directory/'evaluation_labels.json').read_text())
    require(bundle == expected, 'effective annotations differ from approved sources')
    require(frozen['criteria_version'] == previous.VERSION
            and frozen['whole_dataset_target_and_timing_metrics_ready'] is False
            and frozen['model_evaluation_run'] is False, 'invalid freeze capabilities')
    for name in ('scoring_criteria_v2.json', 'scoring_criteria_v2_r1.json'):
        require(sha(directory/name) == records['previous'][name]['sha256'], 'criteria changed')
    envelope = json.loads((directory/'media.json').read_text())
    require(set(envelope) == {'schema_version', 'cases'}, 'unexpected media manifest fields')
    media = unique(envelope['cases'], 'case_id')
    require(set(media) == set(expected_media), 'media case mismatch')
    require(not (directory/'media').is_symlink(), 'symlink media directory')
    require({p.name for p in (directory/'media').iterdir()} == {c + '.mp4' for c in media}, 'media extra/missing')
    allowed = set(parsed(records['previous'], 'media.json')['cases'][0])
    for cid, m in media.items():
        require(set(m) == allowed and m['source_path'] == 'media/' + cid + '.mp4', 'non-neutral media manifest')
        path = directory / m['source_path']
        require(not path.is_symlink() and path.is_file() and sha(path) == m['sha256']
                and path.stat().st_size == m['size_bytes'], 'changed media: ' + cid)
        require(all(m[k] == expected_media[cid][k] for k in ('sha256', 'width', 'height', 'fps', 'frames')),
                'review/media geometry mismatch')
    return frozen


def prepare(args):
    dataset, old, output = args.dataset.resolve(), args.previous.resolve(), args.output.absolute()
    require(1 <= args.workers <= 2, 'workers must be 1 or 2')
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    require(output.parent.resolve() == dataset/'evaluation_sets' and not output.parent.is_symlink(),
            'output must be directly inside dataset/evaluation_sets')
    require(old.is_relative_to(dataset) and not args.previous.is_symlink(), 'invalid predecessor path')
    previous.verify(old)
    records = dict(previous=read_records(old, previous.FILES | {'freeze.json'}),
                   inputs=read_records(args.inputs, INPUT_NAMES | {'input_manifest.json'}))
    bundle, expected_media = assemble(records)
    labels = unique(bundle['classifications']['cases'], 'case_id')
    protected = {old/n: r['sha256'] for n, r in records['previous'].items()}
    protected.update({args.inputs/n: r['sha256'] for n, r in records['inputs'].items()})
    sources = {}
    for cid, label in labels.items():
        source = previous.inside(old, label['source_path']) if int(cid[3:]) <= 77 else previous.inside(
            dataset, label['original_source_path'])
        require(sha(source) == label['source_sha256'], 'source changed: ' + cid)
        sources[cid] = source
        protected[source] = label['source_sha256']
    index = unique(parsed(records['inputs'], 'new_index.json')['videos'], 'video')
    for v in EXCLUDED:
        source = previous.inside(dataset, index[v + '.mp4']['path'])
        require(sha(source) == index[v + '.mp4']['sha256'], 'excluded source changed')
        protected[source] = sha(source)
    require(shutil.disk_usage(dataset).free > 2*sum(p.stat().st_size for p in sources.values()) + 100_000_000,
            'insufficient free space')
    stage = Path(tempfile.mkdtemp(prefix='.freeze84-', dir=output.parent))
    print('STAGING', stage, flush=True)
    (stage/'media').mkdir(mode=0o700)

    def copy_video(cid):
        source, relative = sources[cid], 'media/' + cid + '.mp4'
        destination = stage / relative
        with source.open('rb') as src, destination.open('xb') as dst:
            shutil.copyfileobj(src, dst, 4*1024*1024)
        destination.chmod(0o600)
        require(sha(destination) == protected[source] == sha(source), 'video copy changed')
        info = previous.inspect_video(destination, args.ffprobe, args.ffmpeg)
        require(all(info[k] == expected_media[cid][k] for k in ('width', 'height', 'fps', 'frames')),
                'decoded geometry differs from reviewed geometry')
        require(abs(info['first_pts_s']) < .000002, 'nonzero PTS requires timing review')
        result = dict(case_id=cid, source_path=relative, sha256=protected[source],
                      size_bytes=destination.stat().st_size, **info)
        print('CHECKED', cid, info['frames'], flush=True)
        return result

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        media = list(pool.map(copy_video, sorted(sources)))
    exact = previous.duplicate_groups(media, lambda m: m['sha256'])
    decoded = previous.duplicate_groups(media, lambda m: (
        m['width'], m['height'], m['frames'], m['decoded_rgb_sha256']))
    require(not exact and not decoded, 'duplicate accepted videos')
    annotations = bundle['annotations']['cases']
    validation = dict(total=84, counts=COUNTS, files_decoded=84, frames_decoded=sum(m['frames'] for m in media),
                      original_bytes_preserved=True, file_hash_duplicate_groups=exact,
                      decoded_rgb_duplicate_groups=decoded,
                      same_scene_groups=previous.duplicate_groups(list(labels.values()), lambda c: c['scene_group']),
                      near_duplicate_or_independent_scene_validation=False,
                      sampled_spatial_cases=84,
                      approved_added_box_cases=43, approved_added_boxes=281,
                      total_boxes=sum(len(p['boxes']) for a in annotations for p in a.get('persons', [])),
                      observed_fall_onset_intervals=25, observed_fall_landing_intervals=21,
                      observed_fall_landing_unobservable=4, approved_new_timing_cases=11,
                      model_evaluation_run=False, detection_latency_measured=False,
                      video_content_human_rereviewed_this_run=False,
                      audio_present=sum(m['audio_present'] for m in media), audio_sent_to_model=False,
                      ffmpeg_version=previous.command([args.ffmpeg, '-version']).splitlines()[0],
                      ffprobe_version=previous.command([args.ffprobe, '-version']).splitlines()[0])
    save(stage/'media.json', dict(schema_version='malbut.fall-evaluation-media.v2', cases=media))
    save(stage/'evaluation_labels.json', bundle)
    save(stage/'source_records.json', records)
    save(stage/'validation.json', validation)
    for name in ('scoring_criteria_v2.json', 'scoring_criteria_v2_r1.json'):
        with (stage/name).open('xb') as stream:
            stream.write((old/name).read_bytes())
    with (stage/'protocol.md').open('x', encoding='utf-8') as stream:
        stream.write(PROTOCOL)
    with (stage/'inventory.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        columns = ['case_id', 'review_case_id', 'label', 'reason', 'reason_supplement', 'agreed_reason',
                   'reason_additions', 'source_sha256', 'source_path', 'original_source_path']
        writer = csv.DictWriter(stream, columns, extrasaction='ignore')
        writer.writeheader()
        for cid in sorted(labels):
            row = dict(labels[cid])
            row['reason_additions'] = ' / '.join(a['text'] for a in row.get('reason_additions', []))
            for key, value in row.items():
                if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                    row[key] = "'" + value
            writer.writerow(row)
    for name in previous.FILES:
        (stage/name).chmod(0o600)
    save(stage/'freeze.json', dict(
        schema_version=previous.SCHEMA, dataset_revision=REVISION, criteria_version=previous.VERSION,
        created_utc=datetime.now(timezone.utc).isoformat(), files={n: sha(stage/n) for n in sorted(previous.FILES)},
        cases=84, previous_freeze_sha256=PREVIOUS_SHA, preparation_script_sha256=sha(Path(__file__)),
        shared_preparation_script_sha256=sha(Path(previous.__file__)),
        source_records_sha256=sha(stage/'source_records.json'), media_included=True,
        media_transform='byte_for_byte_copy_only', model_evaluation_run=False,
        whole_dataset_target_and_timing_metrics_ready=False,
        allowed_metrics=['video_level_candidate_presence', 'vlm_three_class', 'checking_outcomes'],
        timing_ground_truth_scope=dict(observed_fall_onset=25, observed_fall_landing=21, landing_unknown=4),
        match=parsed(records['previous'], 'freeze.json')['match']))
    verify(stage)
    require(all(sha(p) == h for p, h in protected.items()), 'source changed during preparation')
    require(not output.exists() and not output.is_symlink(), 'concurrent output publication')
    stage.rename(output)
    verify(output)
    print(json.dumps(dict(status='FROZEN_VERIFIED', path=str(output), **validation), ensure_ascii=False), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    if args.verify:
        verify(args.verify)
        print('VERIFIED', args.verify)
    else:
        require(all((args.dataset, args.previous, args.inputs, args.output)), 'missing preparation paths')
        prepare(args)


if __name__ == '__main__':
    main()
