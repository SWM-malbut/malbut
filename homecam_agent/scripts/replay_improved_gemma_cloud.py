#!/usr/bin/env python3
"""Replay all 53 frozen improved-Pose requests. Free-only, no retries or GT input."""
import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

from diagnose_fall84_json_fences import diagnose
from fall_evaluation_v2 import CRITERIA, CRITERIA_AMENDMENT, score, report_lines
from finalize_partial_pose_review import verify_arm
from replay_fall84_cloud_pair import cloud_identity, MODEL
from replay_fall_baseline import sha
from replay_pose_retention import verify_overlay
from replay_vlm_frames import (api, check_evaluation_labels, digest, prompt_spec,
                               save, SCHEMA_PROMPT_PREFIX)
from review_fall_annotations import require
import run_free_cloud_fall_suite as cloud
from run_ollama_fall_suite import extract_prefix, prefix_indices, verify_completed


def read(path):
    return json.loads(path.read_text())


def validate_calls(plan, cases, metas, dataset):
    """Validate against every actual request, including unresolved and duplicates."""
    expected = []
    for case in cases:
        cid = case['case_id']
        meta = metas[cid]
        for n, request in enumerate(case['requests'], 1):
            last = request['decision_frame_index']
            require(type(last) is int and 0 <= last < meta['frames'], 'invalid frame')
            require(last/meta['fps'] <= request['dispatch_time_s']+1e-9, 'future frame')
            expected.append(dict(
                call_id=f'{cid}-request-{n:02d}', case_id=cid,
                mode='gated_primary' if n == 1 else 'gated_additional_request',
                media_path=str((dataset/meta['source_path']).resolve()),
                media_sha256=meta['sha256'], available_through_frame=last,
                frame_indices=prefix_indices(last), dispatch_time_s=request['dispatch_time_s']))
    calls = [c for c in plan['calls'] if c['mode'] != 'full']
    require(calls == expected, 'call plan differs from actual requests')
    require(len({c['call_id'] for c in calls}) == len(calls), 'duplicate call ID')
    return calls


def prepare(args):
    import cv2
    verify_overlay(args.spatial_final, args.dataset)
    check_evaluation_labels(args.spatial_final, 'v2')
    cases = verify_arm(args.pose_run, 'partial_brief')
    plan = read(args.plan)
    require(plan['source_summary_sha256'] == sha(args.pose_run/'summary.json'), 'wrong run')
    require(plan['spatial_freeze_sha256'] == sha(args.spatial_final/'freeze.json'), 'wrong GT')
    metas = {m['case_id']: m for m in read(args.spatial_final/'media.json')['cases']}
    calls = validate_calls(plan, cases, metas, args.dataset)
    require(len(metas) == 84 and len(calls) == 53, 'requires frozen 84 cases / 53 requests')
    require(Counter(c['mode'] for c in calls) ==
            {'gated_primary': 52, 'gated_additional_request': 1}, 'wrong call counts')
    verify_completed(args.baseline)
    prior = read(args.baseline/'run.json')['contract']
    require(prior['model']['name'] == MODEL and prior['evaluation_version'] == 'v2',
            'wrong Gemma baseline')
    prompt_hash = digest(dict(**prompt_spec('v2'), schema_in_prompt=True,
                              schema_prefix=SCHEMA_PROMPT_PREFIX))
    require(prior['prompt_sha256'] == prompt_hash, 'prompt changed')
    require(prior['criteria_sha256'] == sha(CRITERIA) and
            prior['criteria_amendment_sha256'] == sha(CRITERIA_AMENDMENT), 'scoring changed')
    require(prior['cv2_version'] == cv2.__version__, 'JPEG environment differs')
    require(prior['frame_count'] == 12 and prior['resize'] == 'none' and
            prior['jpeg_quality'] == 90, 'preprocessing differs')
    for call in calls:
        meta = metas[call['case_id']]
        images, frames = extract_prefix(args.dataset, meta, call['available_through_frame'])
        require([f['frame_index'] for f in frames] == call['frame_indices'], 'sampling changed')
        duration = (call['available_through_frame']+1)/meta['fps']
        payload = cloud.cloud_payload(MODEL, images, frames, duration, prior['options'],
                                      prior['thinking'], 'v2')
        call.update(expected_request_sha256=digest(payload), frames=frames)
    return calls, metas, prior


def summarize(rows, labels):
    """No request is not a normal prediction; additional calls do not inflate clip accuracy."""
    primary = {r['case_id']: r for r in rows if r['call_mode'] == 'gated_primary'}
    require(len(primary) == sum(r['call_mode'] == 'gated_primary' for r in rows),
            'duplicate primary result')
    complete = [primary.get(cid, dict(
        case_id=cid, status='not_triggered', valid=False, prediction=None)) for cid in labels]
    return dict(primary=score(complete, labels, 'gated'),
                additional_requests=[r for r in rows if r['call_mode'] != 'gated_primary'],
                actual_requests=len(rows))


def report(args, calls):
    labels = {c['case_id']: c for c in
              read(args.spatial_final/'evaluation_labels.json')['classifications']['cases']}
    strict, cleaned = [], []
    for call in calls:
        out = args.output/'calls'/call['call_id']
        cid = call['case_id']
        row = read(out/f'{cid}.result.json')
        row.update(call_id=call['call_id'], call_mode=call['mode'])
        strict.append(row)
        auxiliary = copy.deepcopy(row)
        if row['status'] == 'responded':
            parsed = diagnose(read(out/f'{cid}.response.json'),
                              read(out/f'{cid}.input.json')['duration_s'])
            auxiliary.update(parsed['assessment'],
                             removed_outer_fence=parsed['removed_outer_fence'])
        cleaned.append(auxiliary)
    value = dict(strict=summarize(strict, labels),
                 outer_fence_only=summarize(cleaned, labels),
                 policy='Additional request retained separately. 32 skipped clips are NOT normal. '
                        'Three-class accuracy denominator is 52; safety coverage uses all 50 '
                        'positive clips, and false checks all 34 normal clips. '
                        '413 response is not proof of correct YOLO target/time association.')
    save(args.output/'summary.json', value)
    lines = ['# 개선 YOLO-Pose → Gemma Cloud', '',
             '52개 영상의 첫 요청 + 108의 추가 요청 1건. 원본 RGB만 전달.',
             '미호출 32개를 정상으로 간주하지 않는다. 413의 YOLO 근거 보류는 유지한다.',
             '전체 경로의 3분류 정확도는 정의하지 않고 전체 확인 대상 포착률을 별도로 표기한다.',
             '지연은 저장 영상 재생 호출의 왕복 시간이며 실물 낙상 감지 지연이 아니다.', '',
             *report_lines([(MODEL+' strict', 'gated', value['strict']['primary']),
                            (MODEL+' outer fence only', 'gated',
                             value['outer_fence_only']['primary'])])]
    (args.output/'summary.md').write_text('\n'.join(lines))


def run(args):
    require(not args.output.exists(), 'output exists; no overwrite or silent retries')
    calls, metas, prior = prepare(args)
    args.output.mkdir(mode=0o700)
    save(args.output/'prepared.json', dict(
        actual_calls=0, calls=calls, prompt_sha256=prior['prompt_sha256'],
        plan_sha256=sha(args.plan), spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
        pose_run_marker_sha256=sha(args.pose_run/'completed.json'),
        baseline_marker_sha256=sha(args.baseline/'completed.json')))
    print('PREPARED 53 requests / 52 videos; actual calls=0', flush=True)
    if not args.execute:
        return
    names = ('run_free_cloud_fall_suite.py', 'replay_vlm_frames.py',
             'run_ollama_fall_suite.py', 'fall_evaluation_v2.py',
             'diagnose_fall84_json_fences.py', 'replay_fall_baseline.py',
             'review_fall_annotations.py', 'replay_pose_retention.py',
             'finalize_partial_pose_review.py', 'replay_fall84_cloud_pair.py')
    sources = [Path(__file__), CRITERIA, CRITERIA_AMENDMENT]
    sources += [Path(__file__).with_name(n) for n in names]
    source_hashes = {str(p): sha(p) for p in sources}
    snapshot = args.output/'code-snapshot'
    snapshot.mkdir(mode=0o700)
    for p in sources:
        shutil.copy2(p, snapshot/p.name)
    completed, active = 0, None
    try:
        free = cloud.free_account(args)
        model, details = cloud_identity(args.endpoint)
        require(api(args.endpoint, '/api/version')['version'] == prior['ollama_version'],
                'Ollama version changed')
        thinking = 'disabled' if 'thinking' in details.get('capabilities', []) else 'unsupported'
        require(thinking == prior['thinking'], 'thinking capability changed')
        contract = dict(
            version='improved-pose-gemma-53-v1', created_utc=datetime.now(timezone.utc).isoformat(),
            model=model, mode='gated', evaluation_version='v2', options=prior['options'],
            thinking=thinking, timeout_s=prior['timeout_s'], retries=0, free_access=free,
            prompt_sha256=prior['prompt_sha256'], source_sha256=source_hashes,
            prepared_sha256=sha(args.output/'prepared.json'),
            baseline_model_metadata_equal=model == prior['model'],
            scope='53 real frozen requests; no new detection, filtering, relabeling or future RGB',
            cloud_revision='Cannot independently pin serving weights',
            postprocessing='strict plus separately reported exact outer Markdown fence removal',
            billing='previously confirmed no purchased credits/top-up; '
                    'Free checked before EACH call')
        save(args.output/'run.json', dict(contract=contract))
        for call in calls:
            active = call['call_id']
            out = args.output/'calls'/active
            out.mkdir(parents=True, mode=0o700)
            cid = call['case_id']
            # Only the immutable request timestamp is saved locally as candidate metadata.
            cloud.invoke(args, out, contract, metas[cid], call['available_through_frame'],
                         candidate=dict(dispatch_time_s=call['dispatch_time_s']))
            actual = read(out/f'{cid}.input.json')
            require(actual['request_sha256'] == call['expected_request_sha256'],
                    'actual request differs from preflight')
            require(actual['frames'] == call['frames'], 'JPEG or frame timestamp changed')
            completed += 1
            print(f'PROGRESS {completed}/53 {active}', flush=True)
        report(args, calls)
        require(cloud_identity(args.endpoint)[0] == model, 'Cloud metadata changed')
        for p, expected in source_hashes.items():
            require(sha(Path(p)) == expected, 'source changed during run')
        verify_overlay(args.spatial_final, args.dataset)
        verify_arm(args.pose_run, 'partial_brief')
        save(args.output/'completed.json', dict(
            actual_calls=completed, videos=52, additional_requests=1,
            summary_sha256=sha(args.output/'summary.json'),
            files={str(p.relative_to(args.output)): sha(p) for p in args.output.rglob('*')
                   if p.is_file()}))
        print('ALL_COMPLETED 53 actual calls', flush=True)
    except BaseException as error:
        save(args.output/'stopped.json', dict(
            error_type=type(error).__name__, active_call=active, completed_calls=completed,
            results_written=len(list(args.output.glob('calls/*/*.result.json'))),
            note='No retries. Inspect partial evidence before any continuation.'))
        raise


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'spatial-final', 'pose-run', 'plan', 'baseline', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    parser.add_argument('--no-paid-balance-confirmed', action='store_true')
    parser.add_argument('--execute', action='store_true')
    run(parser.parse_args())
