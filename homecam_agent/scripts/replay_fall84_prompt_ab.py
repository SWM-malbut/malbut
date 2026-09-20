#!/usr/bin/env python3
"""Local, paired system-prompt experiment on immutable RGB requests.

No fresh Pose, new gate, labels in prompts, cloud, retries, or production changes.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import run_ollama_fall_suite as suite
from replay_fall_baseline import sha, verify_freeze
from replay_vlm_frames import api, digest, request_payload, save, assess_response
from review_fall_annotations import require
from fall_evaluation_v2 import SYSTEM_PROMPT, score, report_lines


# A single frozen amendment; do not tune it after observing experiment results.
AMENDMENT = (
    '\n추가 판단 원칙: 정상 행동은 관찰된 정상 근거로 판단하세요. '
    '스스로 몸을 지지하며 눕거나 앉는 과정, 분명한 수면·휴식 맥락, '
    '일관된 운동·작업 동작 등 보이는 근거를 사용하세요. '
    '넘어지는 과정이 보이지 않음, 움직임이 적음, 동작이 느림, 다른 사람이 곁에 있음은 '
    '각각 그 자체로 정상이라는 근거가 아닙니다. '
    '바닥에 쓰러진 듯한 사람이 있고 사고인지 휴식인지 구분할 근거가 부족하면 '
    'suspected_fall로 남기세요. 반대로 사람이 단순히 누워 있거나 넘어지는 과정을 '
    '못 봤다는 이유만으로 의심으로 분류하지 마세요. 명확한 정상 행동은 유지하세요. '
    '다른 사람이 살피거나 돕는 모습은 이미 안전하다는 뜻이 아닙니다. '
    '정상으로 답할 때 explanation_ko에 실제 관찰된 정상 근거를 적고, '
    '의도·의식·부상 여부를 추측해 근거를 채우지 마세요.'
)
PROMPT = SYSTEM_PROMPT + AMENDMENT
PROTOCOL = Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1/FALL84_GAP_PROMPT_AB.md'


def rebuild(dataset, meta, original, contract):
    """Verify the exact A request before replacing only the system text."""
    images, frames = suite.extract_prefix(dataset, meta, original['available_through_frame'])
    require(frames == original['frames'], 'original JPEGs/timestamps differ')
    require(meta['sha256'] == original['media_sha256'], 'original video differs')
    require(digest(contract) == original['contract_sha256'], 'original contract differs')
    payload = request_payload(contract['model']['name'], images, frames,
                              original['duration_s'], contract['options'],
                              schema_in_prompt=True, evaluation_version='v2')
    if 'wire_schema' in contract:
        payload['format'] = contract['wire_schema']
    if contract['thinking'] == 'disabled':
        payload['think'] = False
    payload['keep_alive'] = '10m'
    require(digest(payload) == original['request_sha256'], 'original request reconstruction differs')
    payload['messages'][0]['content'] = PROMPT
    return payload


def comparison(baseline, changed, labels, mode):
    a, b = score(baseline, labels, mode), score(changed, labels, mode)
    ar, br = ({r['case_id']: r for r in s['rows']} for s in (a, b))
    transitions = [dict(case_id=k, label=ar[k]['label'],
                        before=ar[k]['assessment'], after=br[k]['assessment'],
                        before_status=ar[k]['outcome'], after_status=br[k]['outcome'])
                   for k in sorted(ar) if (ar[k]['assessment'], ar[k]['outcome']) !=
                   (br[k]['assessment'], br[k]['outcome'])]
    return dict(before=a, after=b, transitions=transitions)


def run(args):
    verify_freeze(args.frozen)
    require(not args.output.exists(), 'output exists; no silent retries or overwrite')
    require(args.execute, 'use --execute for local model calls')
    suite.endpoint_url(args.endpoint)
    sources = {str(p): sha(p) for p in (
        Path(__file__), PROTOCOL, Path(suite.__file__),
        Path(__file__).with_name('fall_evaluation_v2.py'),
        Path(__file__).with_name('replay_vlm_frames.py'))}
    meta = {m['case_id']: m for m in json.loads((args.frozen/'media.json').read_text())['cases']}
    # Ground truth is loaded only for reporting, never accepted by rebuild().
    labels = {r['case_id']: r for r in json.loads((args.frozen/'evaluation_labels.json').read_text())
              ['classifications']['cases']}
    require(len(meta) == len(labels) == 84 and set(meta) == set(labels), 'requires complete frozen 84')
    contracts, originals, plans = {}, {}, {}
    for mode in ('full', 'gated'):
        origin = args.baseline/mode
        suite.verify_completed(origin)
        c = json.loads((origin/'run.json').read_text())['contract']
        require(c['evaluation_version'] == 'v2' and c['model']['name'] == 'gemma4:12b', 'wrong baseline')
        require(c['freeze_sha256'] == sha(args.frozen/'freeze.json'), 'wrong freeze')
        contracts[mode] = c
        originals[mode] = [json.loads(p.read_text()) for p in sorted(origin.glob('SYN*.result.json'))]
        require({r['case_id'] for r in originals[mode]} == set(meta), 'incomplete A coverage')
        require(all(r['status'] in {'responded', 'not_triggered'} for r in originals[mode]),
                'requires A completed responses, not silent retry of A failures')
        require(mode != 'full' or all(r['status'] == 'responded' for r in originals[mode]),
                'full may not skip a video')
        plans[mode] = {r['case_id']: r for r in originals[mode]}
    require(sum(r['status'] == 'responded' for r in originals['gated']) == 39,
            'requires the fixed A gate with exactly 39 requests')
    model = suite.local_model(args.endpoint, 'gemma4:12b')
    require(all(model == c['model'] for c in contracts.values()), 'weights/model changed')
    args.output.mkdir(mode=0o700)
    contract = dict(version='fall84-prompt-ab-v1', system_prompt=PROMPT,
                    source_sha256=sources, freeze_sha256=sha(args.frozen/'freeze.json'),
                    baseline_contracts={k: digest(v) for k, v in contracts.items()},
                    model=model, only_changed_field='messages[0].content',
                    timing_scope='saved RGB request replay, NOT online fall latency',
                    created_utc=datetime.now(timezone.utc).isoformat(), retries=0)
    save(args.output/'run.json', contract)
    contract_hash = digest(contract)
    try:
        for mode in ('full', 'gated'):
            out = args.output/mode
            out.mkdir(mode=0o700)
            results = []
            for cid in sorted(meta):
                prior = plans[mode][cid]
                if prior['status'] == 'not_triggered':
                    result = dict(case_id=cid, status='not_triggered', valid=False, prediction=None)
                else:
                    original = json.loads((args.baseline/mode/f'{cid}.input.json').read_text())
                    payload = rebuild(args.frozen, meta[cid], original, contracts[mode])
                    save(out/f'{cid}.input.json', dict(
                        case_id=cid, original_request_sha256=original['request_sha256'],
                        request_sha256=digest(payload), frames=original['frames'],
                        available_through_frame=original['available_through_frame'],
                        user_prompt=original['user_prompt'], media_sha256=meta[cid]['sha256'],
                        candidate_sent_to_model=False, contract_sha256=contract_hash))
                    start = time.monotonic()
                    try:
                        raw = api(args.endpoint, '/api/chat', payload, timeout=contracts[mode]['timeout_s'])
                    except (OSError, ValueError) as error:
                        result = dict(case_id=cid, status='request_failed', valid=False,
                                      prediction=None, error_type=type(error).__name__,
                                      http_status=getattr(error, 'code', None))
                    else:
                        save(out/f'{cid}.response.json', raw)
                        result = dict(case_id=cid, status='responded', **assess_response(
                            raw, original['duration_s'], evaluation_version='v2'))
                        result['response_sha256'] = sha(out/f'{cid}.response.json')
                    result['request_s'] = time.monotonic()-start
                    print(mode, cid, result['status'], result['valid'], flush=True)
                result['contract_sha256'] = contract_hash
                save(out/f'{cid}.result.json', result)
                results.append(result)
                if result.get('http_status') in {400, 401, 403, 404}:
                    raise RuntimeError('systemic API failure; evidence preserved, no retries')
            summary = comparison(originals[mode], results, labels, mode)
            save(out/'comparison.json', summary)
            lines = ['# Gemma system prompt A/B', '',
                     '같은 RGB 입력, 모델, 출력·채점 기준. system 문구만 변경.',
                     'A의 후보 발생 여부를 그대로 사용. 새 YOLO 개선안과 합친 결과가 아님.', '',
                     *report_lines([('gemma4:12b A', mode, summary['before']),
                                    ('gemma4:12b B', mode, summary['after'])])]
            with (out/'comparison.md').open('x') as f:
                f.write('\n'.join(lines))
            save(out/'completed.json', dict(files={p.name: sha(p) for p in out.iterdir()}, cases=84))
            print('MODE_COMPLETED', mode, flush=True)
        require(suite.local_model(args.endpoint, 'gemma4:12b') == model, 'model changed during run')
        for p, expected in sources.items():
            require(sha(Path(p)) == expected, 'experiment source changed during run')
        verify_freeze(args.frozen)
        save(args.output/'completed.json', dict(modes=['full', 'gated'], actual_calls=123,
                                                run_sha256=sha(args.output/'run.json')))
        print('ALL_COMPLETED', flush=True)
    except BaseException as e:
        save(args.output/'stopped.json', dict(error=type(e).__name__, message=str(e)[:500]))
        raise


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'baseline', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--endpoint', required=True)
    p.add_argument('--execute', action='store_true')
    run(p.parse_args())


if __name__ == '__main__':
    main()
