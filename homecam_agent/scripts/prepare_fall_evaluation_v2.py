#!/usr/bin/env python3
"""Freeze the reviewed 77-video development set. No inference, relabeling or transcoding.

Copy original reviewed bytes to neutral filenames; decode-check using local FFmpeg.
Human reasons and generation provenance live only in evaluation_labels/source_records.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import csv
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


SCHEMA = 'malbut.fall-evaluation-freeze.v2'
VERSION = 'fall-evaluation-v2-20260912-r1'
COUNTS = {'observed_fall': 18, 'suspected_fall': 25, 'normal_activity': 34}
NAMES = dict(zip(COUNTS, ('낙상', '낙상 의심', '정상 행동')))
FILES = {'media.json', 'evaluation_labels.json', 'protocol.md', 'inventory.csv',
         'source_records.json', 'validation.json', 'scoring_criteria_v2.json', 'scoring_criteria_v2_r1.json'}
REVIEW = 'annotations/evaluation_criteria_v2_r1_20260912'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def inside(base, value):
    path = (base / value).resolve()
    require(path.is_relative_to(base.resolve()) and path.is_file(), 'missing/outside source: ' + str(value))
    return path


def unique(rows, key):
    result = {r[key]: r for r in rows}
    require(len(rows) == len(result), 'duplicate ' + key)
    return result


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    path.chmod(0o600)


def load_sources(dataset, legacy):
    records, protected, raw_data = {}, {}, {}

    def read(relative, root=dataset):
        path = inside(root, relative)
        data = path.read_bytes()
        key = relative if root == dataset else 'legacy_freeze/' + relative
        raw_data[key] = data
        records[key] = dict(sha256=hashlib.sha256(data).hexdigest(),
                            content=json.loads(data) if path.suffix == '.json' else data.decode('utf-8-sig'))
        protected[path] = records[key]['sha256']
        return records[key]['content']

    publication = read(REVIEW + '/publication.json')
    for name, expected in publication['files'].items():
        require(Path(name).name == name, 'unsafe published path')
        read(REVIEW + '/' + name)
        require(records[REVIEW + '/' + name]['sha256'] == expected, 'published review changed')
    audit = records[REVIEW + '/label_consistency_review.json']['content']
    require(audit['text_review_complete'] is True and not audit['needs_clarification']
            and not audit['criteria_review_needed'], 'human review is incomplete')
    require(audit['criteria_version'] == VERSION and audit['counts'] == COUNTS, 'review version/count mismatch')
    for relative, expected in audit['source_sha256'].items():
        read(relative)
        require(records[relative]['sha256'] == expected, 'original annotation changed')
    base = read('annotations/evaluation_criteria_v2_20260912/scoring_criteria_v2.json')
    amendment = records[REVIEW + '/scoring_criteria_v2_r1.json']['content']
    require(amendment['base_sha256'] == records[
        'annotations/evaluation_criteria_v2_20260912/scoring_criteria_v2.json']['sha256'], 'base criteria changed')
    index = read('generation_runs/gap-remaining-bf16-20260912/viewing_index.json')
    legacy_freeze = read('freeze.json', legacy)
    for name, expected in legacy_freeze['files'].items():
        require(name in {'media.json', 'evaluation_labels.json', 'protocol.md'}, 'legacy freeze path')
        read(name, legacy)
        require(records['legacy_freeze/' + name]['sha256'] == expected, 'legacy freeze changed')
    require(all('legacy_freeze/' + n in records for n in ('media.json', 'evaluation_labels.json')),
            'missing legacy metadata')
    sources = dict(
        audit=audit, base=base, amendment=amendment, index=index,
        base_bytes=raw_data['annotations/evaluation_criteria_v2_20260912/scoring_criteria_v2.json'],
        amendment_bytes=raw_data[REVIEW + '/scoring_criteria_v2_r1.json'],
        old=records['annotations/vlm_review_v2/labels.json']['content'],
        additional=list(csv.DictReader(io.StringIO(records[
            'annotations/gap_review_20260912/accepted_labels_v2_20260912.csv']['content']))),
        exclusions=records['annotations/gap_review_20260912/excluded_videos.json']['content'],
        legacy_media=records['legacy_freeze/media.json']['content'],
        legacy_labels=records['legacy_freeze/evaluation_labels.json']['content'])
    return sources, records, protected


def assemble(sources):
    old = unique(sources['old']['cases'], 'case_id')
    reviewed = unique(sources['audit']['cases'], 'case_id')
    additional = unique(sources['additional'], 'video')
    index = unique(sources['index']['videos'], 'video')
    excluded = unique(sources['exclusions']['videos'], 'video')
    require(len(old) == 41 and set(old) == {f'SYN{i:03d}' for i in range(1, 42)}, 'legacy case IDs changed')
    require(set(index) == {f'V{i:03d}.mp4' for i in range(1, 40)}, '39-video index changed')
    require(set(excluded) == {'V013.mp4', 'V036.mp4', 'V038.mp4'}, 'quality exclusions changed')
    require(set(additional).isdisjoint(excluded) and set(additional) | set(excluded) == set(index),
            'accepted/excluded videos do not partition index')
    annotations = unique(sources['legacy_labels']['annotations']['cases'], 'case_id')
    legacy_media = unique(sources['legacy_media']['cases'], 'case_id')
    require(set(annotations) == set(legacy_media) == set(old), 'legacy spatial coverage mismatch')
    cases, used = [], set()
    for cid, original in sorted(old.items()):
        review = reviewed[cid]
        require(review['label'] == original['label'] and review['reason'] == original['reason'],
                'legacy label/reason mismatch: ' + cid)
        require(legacy_media[cid]['sha256'] == original['source_sha256'], 'legacy media changed')
        scene = re.search(r'_s(\d+)_', Path(original['source_path']).name)
        cases.append(dict(case_id=cid, review_id=cid, review=copy.deepcopy(review),
                          original_path=original['source_path'], expected_sha256=original['source_sha256'],
                          source_group='legacy41', scene_group='legacy:' + scene[1] if scene else cid,
                          annotation=copy.deepcopy(annotations[cid]), legacy_meta=legacy_media[cid]))
        used.add(cid)
    for n, (video, original) in enumerate(sorted(additional.items()), 42):
        review_id = 'GAP-' + Path(video).stem
        review = reviewed[review_id]
        require(NAMES[review['label']] == original['label'] and review['reason'] == original['reason'],
                'additional label/reason mismatch: ' + video)
        cases.append(dict(case_id=f'SYN{n:03d}', review_id=review_id, review=copy.deepcopy(review),
                          original_path=index[video]['path'], expected_sha256=index[video]['sha256'],
                          source_group='additional36', scene_group='gap:' + index[video]['scene_id'],
                          annotation=None, legacy_meta=None))
        used.add(review_id)
    require(used == set(reviewed) and len(cases) == 77, 'missing/extra reviewed case')
    require(dict(Counter(c['review']['label'] for c in cases)) == COUNTS, 'class counts changed')
    require(len({c['expected_sha256'] for c in cases}) == 77, 'duplicate accepted video bytes')
    require(not {c['expected_sha256'] for c in cases} & {v['video_sha256'] for v in excluded.values()},
            'excluded content present under another name')
    return cases


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    require(result.returncode == 0 and not result.stderr.strip(), 'media validation failed: ' + result.stderr[:300])
    return result.stdout


def inspect_video(path, ffprobe, ffmpeg):
    metadata = json.loads(command([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(path)]))
    videos = [s for s in metadata['streams'] if s['codec_type'] == 'video']
    require(len(videos) == 1, 'expected one video stream')
    video = videos[0]
    fps = float(Fraction(video['avg_frame_rate']))
    require(math.isfinite(fps) and fps >= 5, 'unsupported source frame rate')
    decoded = json.loads(command([
        ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_frames', '-show_entries',
        'frame=best_effort_timestamp_time,width,height', '-of', 'json', str(path)]))['frames']
    require(len(decoded) > 1, 'empty/short video')
    require(all((f['width'], f['height']) == (video['width'], video['height']) for f in decoded),
            'frame dimensions change within video')
    pts = [float(f['best_effort_timestamp_time']) for f in decoded]
    require(all(math.isfinite(t) for t in pts), 'invalid frame timestamps')
    require(all(abs((b-a) - 1/fps) < max(.00002, .005/fps) for a, b in zip(pts, pts[1:])),
            'variable/gapped timing; do not silently use frame/fps')
    if video.get('nb_frames') not in (None, 'N/A'):
        require(int(video['nb_frames']) == len(decoded), 'declared/decoded frame mismatch')
    pixel_hash = command([ffmpeg, '-v', 'error', '-xerror', '-threads', '1', '-i', str(path),
                          '-map', '0:v:0', '-an', '-sn', '-dn', '-pix_fmt', 'rgb24',
                          '-fps_mode', 'passthrough', '-f', 'hash', '-hash', 'sha256', '-']).strip()
    require(re.fullmatch(r'SHA256=[0-9a-f]{64}', pixel_hash) is not None, 'missing decoded RGB hash')
    return dict(width=video['width'], height=video['height'], fps=fps,
                fps_rational=video['avg_frame_rate'], frames=len(decoded),
                duration_s=len(decoded)/fps, first_pts_s=pts[0], constant_frame_rate=True,
                codec=video['codec_name'], decoded_rgb_sha256=pixel_hash.split('=')[1],
                audio_present=any(s['codec_type'] == 'audio' for s in metadata['streams']))


def duplicate_groups(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row['case_id'])
    return [ids for ids in groups.values() if len(ids) > 1]


def copy_case(case, dataset, stage, ffprobe, ffmpeg):
    source = inside(dataset, case['original_path'])
    before = sha(source)
    require(before == case['expected_sha256'], 'reviewed media hash changed: ' + case['review_id'])
    relative = 'media/' + case['case_id'] + '.mp4'
    destination = stage / relative
    with source.open('rb') as src, destination.open('xb') as dst:
        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
    destination.chmod(0o600)
    require(sha(destination) == before == sha(source), 'copy/source changed')
    info = inspect_video(destination, ffprobe, ffmpeg)
    if case['legacy_meta']:
        require(all(info[k] == case['legacy_meta'][k] for k in ('width', 'height', 'fps', 'frames')),
                'legacy frame geometry changed')
    media = dict(case_id=case['case_id'], source_path=relative, sha256=before,
                 size_bytes=destination.stat().st_size, **info)
    label = dict(case['review'], case_id=case['case_id'], review_case_id=case['review_id'],
                 source_path=relative, source_sha256=before,
                 original_source_path=str(source.relative_to(dataset.resolve())),
                 source_group=case['source_group'], scene_group=case['scene_group'],
                 label_ko=NAMES[case['review']['label']], expected_safety_assessment=case['review']['label'] != 'normal_activity')
    # Raw historical annotations are also preserved in source_records; do not invent boxes/times.
    if case['annotation'] is not None:
        annotation = dict(case['annotation'], spatial_annotations_available=True,
                          temporal_annotations_available=True,
                          reuse_note='Existing reviewed source-pixel/frame annotations; no interpolation or relabeling.')
    else:
        annotation = dict(case_id=case['case_id'], spatial_annotations_available=False,
                          temporal_annotations_available=False, onset_frames=None,
                          annotation_status='not_annotated_not_person_absent')
    print('CHECKED', case['case_id'], case['review_id'], info['frames'], flush=True)
    return media, label, annotation, source


PROTOCOL = '''# 77개 개발 평가 자료 · v2 r1

낙상 18 / 낙상 의심 25 / 정상 행동 34. 생성 의도가 아니라 사용자 영상 판단을 정답으로 사용한다.
이전 41개와 추가 채택 36개다. 기존 라벨·판단 이유·9개 보충 설명·근거 한계 메모를 보존한다.
V013·V036·V038은 품질 제외이며, 기존 제외 이력도 evaluation_labels.json에 남긴다.
이미 검토하고 기준 조정에 사용한 개발용 합성 자료다. 독립 시험셋이나 실물 성능 검증이 아니다.

media/에는 검토한 원본 바이트를 SYN 번호로 복사했다. 재인코딩·리사이즈·프레임률 변환은 하지 않았다.
media.json에는 정답·원래 파일명·생성 프롬프트·판단 메모가 없다. 모델에는 RGB 프레임만 전달한다.
오디오가 있는 원본도 바이트 보존을 위해 유지하지만, 이번 RGB 평가 입력으로 사용하지 않는다.
추론 경로의 --dataset과 --frozen은 모두 이 묶음 디렉터리를 사용한다.

평가 정답은 evaluation_labels.json, 사람이 보는 전체 목록은 inventory.csv다.
V 번호와 새 SYN 번호는 inventory.csv / classifications.cases의 review_case_id로 연결한다.
원래 이유 reason과 추가 설명 reason_supplement, 합의한 보충 이유 agreed_reason은 서로 덮어쓰지 않는다.
근거 한계 메모는 보고서에서만 사용한다. 모든 라벨에 같은 채점·분모를 적용하며 부분 점수나 제외가 없다.

기존 41개의 수동 위치·시각 자료는 원본 영상 해시/크기/FPS/프레임 수 일치 확인 후 재사용한다.
추가 36개는 위치·시각 미라벨이다. 이를 사람 없음, 낙상 시각 0초, 대상 검출 성공으로 바꾸지 않는다.
77개 영상 단위 후보 발생률과 VLM 3분류 평가용이다. 전체 77개의 대상자 검출률·낙상 시작 지연은 준비되지 않았다.

파일 해시와 전체 디코딩 RGB 해시로 완전히 같은 영상의 중복을 검사한다.
같은 장면의 생성 변형은 별도 영상이지만 독립 상황으로 과장하지 않는다. 유사도/독립성 검증은 수행하지 않았다.
정답은 모델 실행 전에 고정했다. 이번 단계에서는 모델·프롬프트 입력 샘플링·응답 제한 시간을 선택하지 않는다.
새로운 모델 비교는 이 묶음과 실행 조건을 함께 고정한 뒤 별도로 시작한다. 모델은 실행하지 않았다.
'''


def prepare(args):
    dataset, output = args.dataset.resolve(), args.output.absolute()
    require(args.workers in (1, 2), 'workers must be 1 or 2')
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    require(output.parent.resolve().is_relative_to(dataset), 'output parent must be within dataset')
    require(not output.parent.is_symlink() and not (output.parent/'AGENTS.md').exists(), 'inspect output parent')
    sources, records, protected = load_sources(dataset, args.legacy_freeze.resolve())
    cases = assemble(sources)
    # Verify excluded source hashes; never copy them into inference media.
    indexed = unique(sources['index']['videos'], 'video')
    for excluded in sources['exclusions']['videos']:
        item = indexed[excluded['video']]
        source = inside(dataset, item['path'])
        require(sha(source) == excluded['video_sha256'] == item['sha256'], 'excluded source changed')
        protected[source] = excluded['video_sha256']
    require(shutil.disk_usage(dataset).free > 2*sum(inside(dataset, c['original_path']).stat().st_size
                                                 for c in cases) + 100_000_000, 'insufficient free space')
    output.parent.mkdir(mode=0o700, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.freeze77-', dir=output.parent))
    (stage/'media').mkdir(mode=0o700)
    outputs = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(copy_case, c, dataset, stage, args.ffprobe, args.ffmpeg) for c in cases]
        for future in as_completed(futures):
            outputs.append(future.result())
    outputs.sort(key=lambda r: r[0]['case_id'])
    media, labels, annotations = ([r[n] for r in outputs] for n in range(3))
    exact = duplicate_groups(media, lambda m: m['sha256'])
    decoded = duplicate_groups(media, lambda m: (m['width'], m['height'], m['frames'], m['decoded_rgb_sha256']))
    require(not exact and not decoded, 'exact duplicate accepted videos; review before freezing')
    for m, _, _, source in outputs:
        protected[source] = m['sha256']
    validation = dict(total=77, counts=COUNTS, files_decoded=77, original_bytes_preserved=True,
                      file_hash_duplicate_groups=exact, decoded_rgb_duplicate_groups=decoded,
                      same_scene_groups=duplicate_groups(labels, lambda c: c['scene_group']),
                      near_duplicate_or_independent_scene_validation=False,
                      media_formats=dict(Counter(f'{m["width"]}x{m["height"]}@{m["fps"]}' for m in media)),
                      audio_present=sum(m['audio_present'] for m in media), audio_sent_to_model=False,
                      ffmpeg_version=command([args.ffmpeg, '-version']).splitlines()[0],
                      ffprobe_version=command([args.ffprobe, '-version']).splitlines()[0],
                      spatial_annotated_cases=41, spatial_unannotated_cases=36,
                      model_evaluation_run=False, video_content_human_rereviewed_this_run=False)
    save(stage/'media.json', dict(schema_version='malbut.fall-evaluation-media.v2', cases=media))
    save(stage/'evaluation_labels.json', dict(
        classifications=dict(schema_version='malbut.synthetic-video-human-review.v2',
                             criteria_version=VERSION, full_dataset_finalized=True,
                             dataset_usage='development_only_previously_inspected_and_tuned',
                             counts=COUNTS, cases=labels, exclusions=dict(
                                 legacy=sources['old']['exclusions'], additional=sources['exclusions']['videos'])),
        annotations=dict(schema_version='malbut.fall-video-spatial-temporal.partial.v2',
                         cases=annotations, whole_dataset_target_and_timing_metrics_ready=False),
        review_use='User approved assembling the reviewed 77-video evaluation set; no new visual judgments.'))
    save(stage/'validation.json', validation)
    save(stage/'source_records.json', records)
    for name, key in [('scoring_criteria_v2.json', 'base_bytes'), ('scoring_criteria_v2_r1.json', 'amendment_bytes')]:
        with (stage/name).open('xb') as stream:
            stream.write(sources[key])
    with (stage/'protocol.md').open('x', encoding='utf-8') as stream:
        stream.write(PROTOCOL)
    columns = ['case_id', 'review_case_id', 'original_source_path', 'source_path', 'source_sha256',
               'source_group', 'scene_group', 'label', 'label_ko', 'reason', 'reason_supplement',
               'agreed_reason', 'judgment_evidence_limited', 'judgment_note',
               'width', 'height', 'fps', 'frames', 'duration_s', 'audio_present']
    with (stage/'inventory.csv').open('x', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, columns, extrasaction='ignore'); writer.writeheader()
        for label, meta in zip(labels, media):
            cells = dict(label, **{k: meta[k] for k in ('width', 'height', 'fps', 'frames', 'duration_s', 'audio_present')})
            for key, value in cells.items():
                if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                    cells[key] = "'" + value
            writer.writerow(cells)
    for name in FILES:
        (stage/name).chmod(0o600)
    save(stage/'freeze.json', dict(
        schema_version=SCHEMA, criteria_version=VERSION, created_utc=datetime.now(timezone.utc).isoformat(),
        files={n: sha(stage/n) for n in sorted(FILES)}, cases=77, model_evaluation_run=False,
        source_records_sha256=sha(stage/'source_records.json'),
        preparation_script_sha256=sha(Path(__file__)),
        media_included=True, media_transform='byte_for_byte_copy_only',
        allowed_metrics=['video_level_candidate_presence', 'vlm_three_class', 'checking_outcomes'],
        whole_dataset_target_and_timing_metrics_ready=False,
        match=dict(visible_coverage=0.5, prediction_coverage=0.25, margin=0.1)))
    verify(stage)
    require(all(sha(path) == expected for path, expected in protected.items()), 'source changed during preparation')
    require(not output.exists(), 'concurrent output publication')
    stage.rename(output)
    verify(output)
    print(json.dumps(dict(status='FROZEN_VERIFIED', path=str(output), **validation), ensure_ascii=False), flush=True)


def verify(directory):
    frozen = json.loads((directory/'freeze.json').read_text())
    if frozen.get('dataset_revision') == 'fall84-v2-r1-20260917':
        from prepare_fall_evaluation_84 import verify as verify84
        return verify84(directory)
    require(frozen['schema_version'] == SCHEMA and set(frozen['files']) == FILES, 'invalid freeze manifest')
    for name, expected in frozen['files'].items():
        require(not (directory/name).is_symlink() and sha(directory/name) == expected, 'changed metadata: ' + name)
    amendment = json.loads((directory/'scoring_criteria_v2_r1.json').read_text())
    require(amendment['base_file'] == 'scoring_criteria_v2.json'
            and amendment['base_sha256'] == sha(directory/'scoring_criteria_v2.json')
            and amendment['criteria_version'] == frozen['criteria_version'] == VERSION,
            'criteria base/revision binding mismatch')
    media = json.loads((directory/'media.json').read_text())['cases']
    bundle = json.loads((directory/'evaluation_labels.json').read_text())
    labels = unique(bundle['classifications']['cases'], 'case_id')
    require(len(media) == len(labels) == 77 and bundle['classifications']['full_dataset_finalized'] is True,
            'incomplete freeze')
    require(dict(Counter(c['label'] for c in labels.values())) == COUNTS, 'changed label counts')
    require(set(labels) == {m['case_id'] for m in media}, 'media/label case mismatch')
    require({p.name for p in (directory/'media').iterdir()} == {c + '.mp4' for c in labels}, 'media extras/missing')
    for m in media:
        require(m['source_path'] == 'media/' + m['case_id'] + '.mp4', 'non-neutral source path')
        path = inside(directory, m['source_path'])
        require(not (directory/m['source_path']).is_symlink() and sha(path) == m['sha256'], 'changed media bytes')
        require(labels[m['case_id']]['source_sha256'] == m['sha256'], 'label/media hash mismatch')
    return frozen


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--legacy-freeze', type=Path)
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    if args.verify:
        verify(args.verify); print('VERIFIED', args.verify)
    else:
        require(all((args.dataset, args.output, args.legacy_freeze)), 'dataset/output/legacy-freeze required')
        prepare(args)


if __name__ == '__main__':
    main()
