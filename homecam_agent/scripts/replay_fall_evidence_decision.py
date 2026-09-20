#!/usr/bin/env python3
"""Fixed-input Gemma comparison: original / structured observations / decision policy.

58 frozen gated inputs + 84 standalone clips. Equal payloads share one actual
inference. No GT/past answers in prompts, paid access, automatic retry or overwrite.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time

from diagnose_fall84_json_fences import diagnose
import fall_evidence_decision as decision
from fall_evaluation_v2 import score
from replay_fall84_cloud_pair import MODEL, cloud_identity
from replay_fall_baseline import sha
from replay_fall_rechecks import terminal_predictions, verify_files
from replay_pose_retention import verify_overlay
from replay_vlm_frames import api, digest, save
from review_fall_annotations import require
from run_ollama_fall_suite import extract_prefix
import run_free_cloud_fall_suite as cloud


def read(path):
    return json.loads(path.read_text())


def conditions(args):
    verify_files(args.gated_baseline)
    verify_files(args.full_baseline)
    records = read(args.gated_baseline/'results.json')
    require(len(records) == 58 and len({r['case_id'] for r in records}) == 52,
            'requires frozen 58 calls / 52 requested clips')
    value = []
    for n, r in enumerate(records, 1):
        root = Path(r['source'])
        require(read(root/f'{r["case_id"]}.result.json') == r['strict'],
                'cached first result differs from frozen replay record')
        require(sha(root/f'{r["case_id"]}.response.json') == r['strict']['response_sha256'],
                'cached response differs from frozen replay record')
        value.append(dict(
            condition_id=f'gated-{n:03d}', mode='gated', case_id=r['case_id'],
            incident_id=r['incident_id'], sequence=r['call']['sequence'],
            old_root=r['source'], last=r['call']['available_through_frame'],
            old_input_sha256=sha(Path(r['source'])/f'{r["case_id"]}.input.json'),
            old_response_sha256=sha(Path(r['source'])/f'{r["case_id"]}.response.json')))
    for file in sorted(args.full_baseline.glob('*.input.json')):
        cid = file.name.removesuffix('.input.json')
        inp = read(file)
        value.append(dict(
            condition_id='full-'+cid, mode='full', case_id=cid, incident_id=cid, sequence=1,
            old_root=str(args.full_baseline), last=inp['available_through_frame'],
            old_input_sha256=sha(file),
            old_response_sha256=sha(args.full_baseline/f'{cid}.response.json')))
    require(len(value) == 142 and len({c['condition_id'] for c in value}) == 142,
            'incomplete/duplicate conditions')
    return value


def build(args, condition, metas, prior):
    cid = condition['case_id']
    old_file = Path(condition['old_root'])/f'{cid}.input.json'
    require(sha(old_file) == condition['old_input_sha256'], 'old input changed')
    require(sha(Path(condition['old_root'])/f'{cid}.response.json') ==
            condition['old_response_sha256'], 'old response changed')
    old = read(old_file)
    images, frames = extract_prefix(args.dataset, metas[cid], condition['last'])
    duration = (condition['last']+1)/metas[cid]['fps']
    require(old['frames'] == frames, 'image selection/JPEG/clock changed')
    before = cloud.cloud_payload(MODEL, images, frames, duration,
                                 prior['options'], prior['thinking'], 'v2')
    require(digest(before) == old['request_sha256'], 'cannot reproduce baseline payload')
    after = decision.payload(MODEL, images, frames, duration, prior['options'], prior['thinking'])
    require({k: v for k, v in before.items() if k != 'messages'} ==
            {k: v for k, v in after.items() if k != 'messages'}, 'model/options changed')
    return after, frames, duration


def score_conditions(items, results, labels):
    """Never combine full-clip answers with gated answers, nor choose the best label."""
    output = {}
    for mode in ('gated', 'full'):
        output[mode] = {}
        selected = [c for c in items if c['mode'] == mode]
        for formatting in ('strict', 'outer_fence_only'):
            output[mode][formatting] = {}
            for variant in ('baseline', 'model', 'policy'):
                rows = []
                for c in selected:
                    source = Path(c['old_root'])
                    cid = c['case_id']
                    if variant == 'baseline':
                        raw = read(source/f'{cid}.response.json')
                        from fall_evaluation_v2 import assess_response
                        assessed = (assess_response(raw) if formatting == 'strict' else
                                    diagnose(raw, c['duration_s'])['assessment'])
                        status = 'responded'
                        seconds = read(source/f'{cid}.result.json')['request_s']
                    else:
                        result = results[c['input_id']]
                        status = result['status']
                        assessed = result[formatting][variant]
                        seconds = result['request_s']
                    v = dict(assessed, case_id=cid, status=status, request_s=seconds)
                    rows.append(dict(case_id=cid, incident_id=c['incident_id'], strict=v))
                if mode == 'gated':
                    resolved = terminal_predictions(rows, labels, False)
                else:
                    resolved = [r['strict'] for r in rows]
                output[mode][formatting][variant] = score(resolved, labels, mode)
    return output


def run(args):
    import cv2
    require(not args.output.exists(), 'output exists; no overwrite or silent retry')
    verify_overlay(args.spatial_final, args.dataset)
    items = conditions(args)
    metas = {m['case_id']: m for m in read(args.spatial_final/'media.json')['cases']}
    require(len(metas) == 84, 'requires frozen 84 cases')
    prior = read(args.full_baseline/'run.json')['contract']
    gated_contract = read(args.gated_baseline/'run.json')['contract']
    require(prior['model'] == gated_contract['model'], 'baseline models differ')
    require(prior['options'] == gated_contract['options'] and
            prior['thinking'] == gated_contract['thinking'], 'baseline settings differ')
    require(prior['cv2_version'] == cv2.__version__, 'JPEG version differs')
    unique = {}
    for c in items:
        payload, frames, duration = build(args, c, metas, prior)
        input_id = digest(payload)
        c.update(input_id=input_id, frames=frames, duration_s=duration)
        unique.setdefault(input_id, c)
    sources = [Path(__file__), Path(decision.__file__)]
    sources += [Path(__file__).with_name(n) for n in (
        'fall_evaluation_v2.py', 'replay_vlm_frames.py', 'run_ollama_fall_suite.py',
        'run_free_cloud_fall_suite.py', 'diagnose_fall84_json_fences.py',
        'replay_fall_rechecks.py', 'replay_fall84_cloud_pair.py', 'replay_fall_baseline.py',
        'replay_pose_retention.py', 'review_fall_annotations.py')]
    sources += [Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1' /
                'FALL_EVIDENCE_DECISION_PROTOCOL.md']
    hashes = {str(p): sha(p) for p in sources}
    args.output.mkdir(mode=0o700)
    (args.output/'code-snapshot').mkdir()
    for p in sources:
        shutil.copy2(p, args.output/'code-snapshot'/p.name)
    save(args.output/'plan.json', dict(
        conditions=items, unique_api_inputs=len(unique), execute=args.execute,
        prompt_sha256=decision.PROMPT_SHA256, schema=decision.SCHEMA,
        system_prompt=decision.SYSTEM, source_sha256=hashes,
        model=prior['model'], options=prior['options'], thinking=prior['thinking'],
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
        gated_marker_sha256=sha(args.gated_baseline/'completed.json'),
        full_marker_sha256=sha(args.full_baseline/'completed.json'),
        comparison='fixed RGB inputs, cached original responses vs real new prompt calls',
        clock='API duration only; fixed historical frame cutoffs; NOT live pipeline latency'))
    print('PREPARED', len(items), 'conditions;', len(unique), 'unique inputs; actual calls=0',
          flush=True)
    if not args.execute:
        return
    active, completed = None, 0
    try:
        free = cloud.free_account(args)
        require(cloud_identity(args.endpoint)[0] == prior['model'], 'model metadata changed')
        require(api(args.endpoint, '/api/version')['version'] == prior['ollama_version'],
                'Ollama version changed')
        save(args.output/'run.json', dict(
            created_utc=datetime.now(timezone.utc).isoformat(),
            free_access=free, retries=0, plan_sha256=sha(args.output/'plan.json')))
        results = {}
        for input_id, c in unique.items():
            active = input_id
            payload, frames, duration = build(args, c, metas, prior)
            require(digest(payload) == input_id, 'request changed after preflight')
            checked = cloud.free_account(args)
            folder = args.output/'calls'/input_id
            folder.mkdir(parents=True, mode=0o700)
            save(folder/'input.json', dict(
                request_sha256=input_id, prompt_sha256=decision.PROMPT_SHA256, frames=frames,
                media_sha256=metas[c['case_id']]['sha256'], duration_s=duration,
                user_prompt=payload['messages'][1]['content'], free_account_check=checked,
                labels_sent=False, previous_answers_sent=False, yolo_metadata_sent=False))
            start = time.perf_counter()
            try:
                raw = api(args.endpoint, '/api/chat', payload, timeout=prior['timeout_s'])
            except (OSError, ValueError) as exc:
                save(folder/'failed.json', dict(error_type=type(exc).__name__,
                                                http_status=getattr(exc, 'code', None),
                                                elapsed_s=time.perf_counter()-start))
                # Stop on ANY transport/protocol failure, not just quota; no silent retries.
                raise
            elapsed = time.perf_counter()-start
            save(folder/'response.json', raw)
            result = dict(status='responded', request_s=elapsed,
                          response_sha256=sha(folder/'response.json'),
                          finish_reason=raw.get('done_reason'))
            for name, remove in [('strict', False), ('outer_fence_only', True)]:
                parsed = decision.parse(raw, len(frames), remove_outer_fence=remove)
                result[name] = dict(
                    assessment=parsed, model=decision.project(parsed),
                    policy=decision.project(parsed, apply_policy=True))
            save(folder/'result.json', result)
            results[input_id] = result
            completed += 1
            # Progress is structural only; never tune the prompt using partial labels.
            print('PROGRESS', completed, '/', len(unique), c['condition_id'],
                  'valid=', result['outer_fence_only']['model']['valid'],
                  'seconds=', round(elapsed, 3), flush=True)
        labels = {c['case_id']: c for c in
                  read(args.spatial_final/'evaluation_labels.json')['classifications']['cases']}
        summary = score_conditions(items, results, labels)
        save(args.output/'summary.json', summary)
        require(cloud_identity(args.endpoint)[0] == prior['model'], 'Cloud metadata changed')
        for p, expected in hashes.items():
            require(sha(Path(p)) == expected, 'source changed during run')
        verify_overlay(args.spatial_final, args.dataset)
        save(args.output/'completed.json', dict(
            actual_calls=completed, conditions=len(items), videos=len(metas),
            files={str(p.relative_to(args.output)): sha(p) for p in args.output.rglob('*')
                   if p.is_file()}))
        print('ALL_COMPLETED', completed, 'real calls;', len(items), 'conditions', flush=True)
    except BaseException as exc:
        save(args.output/'stopped.json', dict(
            error_type=type(exc).__name__, http_status=getattr(exc, 'code', None),
            active_input=active, completed_calls=completed, total_inputs=len(unique)))
        raise


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'spatial-final', 'gated-baseline', 'full-baseline', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    parser.add_argument('--no-paid-balance-confirmed', action='store_true')
    parser.add_argument('--execute', action='store_true')
    run(parser.parse_args())
