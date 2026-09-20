#!/usr/bin/env python3
"""Paired local Gemma experiment: visible observations vs unknowns vs assessment."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time

import run_ollama_fall_suite as suite
from fall_evaluation_v2 import PREDICTION_JSON_SCHEMA, SYSTEM_PROMPT, validate_prediction
from replay_fall84_prompt_ab import comparison
from replay_fall_baseline import sha, verify_freeze
from replay_vlm_frames import (
    api, canonical, digest, request_payload, save, SCHEMA_PROMPT_PREFIX,
)
from review_fall_annotations import require

PROTOCOL = Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1/FACTS_SEPARATION_PROTOCOL.md'
PROMPT = SYSTEM_PROMPT + (
    '\n이번 출력은 observations와 assessment를 가진 JSON으로 정리하세요. '
    'observations.visible_facts_ko에는 실제로 보이는 사람의 자세·동작·주변 상황을, '
    'observations.unknowns_ko에는 영상만으로 알 수 없는 내용을 각각 한국어로 간단히 적으세요. '
    '쉬려고 했다, 의식이 있다, 기절했다 등 보이지 않는 의도·상태를 관찰 사실에 넣지 마세요. '
    '움직임이 없음·움직임이 느림·넘어지는 과정이 안 보임이라는 관찰과 쉬고 있다는 해석을 '
    '구분하세요. 명확한 일상 동작이나 휴식의 모습이 보인다면 그 근거도 적으세요. '
    '알 수 없는 내용이 있다는 이유만으로 모두 의심하지 마세요. '
    'assessment에는 위의 원래 판단 기준에 따른 outcome, label, explanation_ko를 넣으세요. '
    'explanation_ko는 보이는 근거와 불명확한 점을 고려한 짧은 설명으로 적으세요.'
)
SCHEMA = dict(type='object', additionalProperties=False,
              required=['observations', 'assessment'], properties={
                  'observations': dict(type='object', additionalProperties=False,
                      required=['visible_facts_ko', 'unknowns_ko'], properties={
                          k: dict(type='string', minLength=1, maxLength=1200)
                          for k in ('visible_facts_ko', 'unknowns_ko')}),
                  'assessment': copy.deepcopy(PREDICTION_JSON_SCHEMA),
              })


def wire_schema(value):
    if isinstance(value, dict):
        return {k: wire_schema(v) for k, v in value.items() if k not in ('minLength', 'maxLength')}
    if isinstance(value, list):
        return [wire_schema(v) for v in value]
    return value


WIRE_SCHEMA = wire_schema(SCHEMA)


def assess(raw):
    """Strict new fields + unchanged assessment; never rewrite model's label."""
    errors, value = [], None
    def unique(pairs):
        output = {}
        for key, item in pairs:
            require(key not in output, 'duplicate key')
            output[key] = item
        return output
    def reject_constant(_):
        raise ValueError('non-finite value')
    try:
        require(isinstance(raw, dict), 'response not object')
        require(raw.get('done') is True and raw.get('done_reason') == 'stop', 'incomplete response')
        message = raw.get('message')
        require(isinstance(message, dict) and isinstance(message.get('content'), str), 'missing content')
        value = json.loads(message['content'], object_pairs_hook=unique, parse_constant=reject_constant)
        require(isinstance(value, dict) and set(value) == {'observations', 'assessment'}, 'wrong fields')
        obs = value['observations']
        require(isinstance(obs, dict) and set(obs) == {'visible_facts_ko', 'unknowns_ko'}, 'wrong observations')
        require(all(isinstance(v, str) and v.strip() and len(v) <= 1200 for v in obs.values()),
                'invalid observation text')
        errors.extend(validate_prediction(value['assessment']))
    except (ValueError, TypeError, RecursionError) as error:
        errors.append(str(error)[:120])
    return dict(valid=not errors, schema_errors=errors, semantic_errors=[],
                prediction=value['assessment'] if not errors else None,
                structured_prediction=value)


def rebuild(dataset, meta, original, contract):
    images, frames = suite.extract_prefix(dataset, meta, original['available_through_frame'])
    require(frames == original['frames'], 'original JPEGs/timestamps differ')
    require(meta['sha256'] == original['media_sha256'], 'original video differs')
    require(digest(contract) == original['contract_sha256'], 'original contract differs')
    payload = request_payload(contract['model']['name'], images, frames, original['duration_s'],
                              contract['options'], schema_in_prompt=True, evaluation_version='v2')
    if 'wire_schema' in contract:
        payload['format'] = contract['wire_schema']
    if contract['thinking'] == 'disabled':
        payload['think'] = False
    payload['keep_alive'] = '10m'
    require(digest(payload) == original['request_sha256'], 'original A reconstruction differs')
    body, separator, _ = payload['messages'][1]['content'].partition(SCHEMA_PROMPT_PREFIX)
    require(bool(separator), 'missing schema instructions')
    payload['messages'][0]['content'] = PROMPT
    payload['messages'][1]['content'] = body + separator + canonical(SCHEMA).decode()
    payload['format'] = WIRE_SCHEMA
    return payload


def read(path):
    return json.loads(path.read_text())


def origins(args):
    """Original full 84 and gated 39 + one previously measured V034 request."""
    plans = {}
    suite.verify_completed(args.extra)
    require(read(args.extra / 'completed.json')['fresh_calls'] == 1, 'wrong extra run')
    for mode in ('gated', 'full'):
        root = args.baseline / mode
        suite.verify_completed(root)
        rows = {p.stem.split('.')[0]: (root, read(p)) for p in root.glob('SYN*.result.json')}
        require(len(rows) == 84, 'baseline cases missing')
        if mode == 'gated':
            require(rows['SYN074'][1]['status'] == 'not_triggered', 'extra was already called')
            rows['SYN074'] = (args.extra, read(args.extra / 'SYN074.result.json'))
        require(all(r['status'] in ('responded', 'not_triggered') for _, r in rows.values()), 'baseline failure')
        require(sum(r['status'] == 'responded' for _, r in rows.values()) == (40 if mode == 'gated' else 84),
                'wrong baseline call count')
        plans[mode] = rows
    return plans


def run(args):
    require(args.execute, 'use --execute for local calls')
    require(not args.output.exists(), 'output exists; no overwrite or silent retry')
    suite.endpoint_url(args.endpoint)
    verify_freeze(args.frozen)
    metas = {m['case_id']: m for m in read(args.frozen / 'media.json')['cases']}
    require(len(metas) == 84, 'requires frozen 84 cases')
    plans = origins(args)
    model = suite.local_model(args.endpoint, 'gemma4:12b')
    contracts = {}
    for rows in plans.values():
        require(set(rows) == set(metas), 'wrong baseline coverage')
        for root, _ in rows.values():
            if root not in contracts:
                c = read(root / 'run.json')['contract']
                require(c['model'] == model and c['evaluation_version'] == 'v2', 'wrong model or criteria')
                require(c['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
                require(c['ollama_version'] == api(args.endpoint, '/api/version')['version'], 'server changed')
                import cv2
                require(cv2.__version__ == c['cv2_version'], 'JPEG decoder changed')
                contracts[root] = c
    # Validate EVERY paired A request before the first inference request.
    for rows in plans.values():
        for cid, (root, row) in rows.items():
            if row['status'] == 'responded':
                rebuild(args.frozen, metas[cid], read(root / f'{cid}.input.json'), contracts[root])
    dependencies = [Path(__file__), PROTOCOL, Path(suite.__file__),
                    Path(__file__).with_name('fall_evaluation_v2.py'),
                    Path(__file__).with_name('replay_vlm_frames.py'),
                    Path(__file__).with_name('replay_fall84_prompt_ab.py')]
    sources = {str(p): sha(p) for p in dependencies}
    args.output.mkdir(mode=0o700)
    snapshot = args.output / 'code-snapshot'
    snapshot.mkdir(mode=0o700)
    for p in dependencies:
        shutil.copy2(p, snapshot / p.name)
    contract = dict(version='facts-separation-v1', created_utc=datetime.now(timezone.utc).isoformat(),
        system_prompt=PROMPT, schema=SCHEMA, wire_schema=WIRE_SCHEMA,
        source_sha256=sources, model=model, freeze_sha256=sha(args.frozen / 'freeze.json'),
        baseline_contracts={str(p): digest(c) for p, c in contracts.items()},
        baseline_markers={str(p): sha(p / 'completed.json') for p in contracts},
        scope='84 full + 40 gated fresh calls; fixed original A RGB; no labels/sensors/model pulls/cloud',
        retries=0, changed=['system', 'user_schema_instructions', 'format'],
        comparison='prompt and structured-output intervention together; not prompt-only',
        observations_are_model_claims_not_verified_facts=True)
    save(args.output / 'run.json', contract)
    print('PREFLIGHT_OK 124 identical A RGB requests; original labels; local Gemma', flush=True)
    actual_calls = 0
    try:
        for mode, rows in plans.items():
            out = args.output / mode
            out.mkdir(mode=0o700)
            results = []
            for cid, (root, previous) in sorted(rows.items()):
                if previous['status'] == 'not_triggered':
                    result = dict(case_id=cid, status='not_triggered', prediction=None, valid=False)
                else:
                    original = read(root / f'{cid}.input.json')
                    payload = rebuild(args.frozen, metas[cid], original, contracts[root])
                    save(out / f'{cid}.input.json', dict(
                        case_id=cid, baseline_input=str(root / f'{cid}.input.json'),
                        original_request_sha256=original['request_sha256'],
                        request_sha256=digest(payload), frames=original['frames'],
                        available_through_frame=original['available_through_frame'],
                        duration_s=original['duration_s'], media_sha256=metas[cid]['sha256'],
                        user_prompt=payload['messages'][1]['content'], candidate_sent_to_model=False,
                        contract_sha256=digest(contract)))
                    started = time.monotonic()
                    actual_calls += 1
                    try:
                        raw = api(args.endpoint, '/api/chat', payload, timeout=contracts[root]['timeout_s'])
                    except (OSError, ValueError) as error:
                        result = dict(case_id=cid, status='request_failed', valid=False, prediction=None,
                                      error_type=type(error).__name__, http_status=getattr(error, 'code', None))
                    else:
                        save(out / f'{cid}.response.json', raw)
                        result = dict(case_id=cid, status='responded', **assess(raw),
                            response_sha256=sha(out / f'{cid}.response.json'),
                            telemetry={k: raw.get(k) for k in ('total_duration', 'load_duration',
                                'prompt_eval_duration', 'eval_duration', 'prompt_eval_count', 'eval_count')})
                    result['request_s'] = time.monotonic() - started
                    print(mode, cid, result['status'], 'valid=' + str(result['valid']),
                          f"seconds={result['request_s']:.2f} calls={actual_calls}/124", flush=True)
                result['contract_sha256'] = digest(contract)
                save(out / f'{cid}.result.json', result)
                results.append(result)
                if result.get('http_status') in (400, 401, 403, 404):
                    raise RuntimeError('systemic API failure; results preserved; no retry')
            # Labels are loaded only for reporting after model requests.
            labels = {r['case_id']: r for r in read(args.frozen / 'evaluation_labels.json')['classifications']['cases']}
            summary = comparison([r for _, r in rows.values()], results, labels, mode)
            save(out / 'comparison.json', summary)
            save(out / 'completed.json', dict(cases=84, files={p.name: sha(p) for p in out.iterdir()}))
            print('MODE_COMPLETED', mode, flush=True)
        require(actual_calls == 124, 'missing calls')
        require(suite.local_model(args.endpoint, 'gemma4:12b') == model, 'model changed during run')
        for path, expected in sources.items():
            require(sha(Path(path)) == expected, 'source changed during run')
        for root in contracts:
            suite.verify_completed(root)
        verify_freeze(args.frozen)
        save(args.output / 'completed.json', dict(actual_calls=actual_calls,
            run_sha256=sha(args.output / 'run.json'), modes=['gated', 'full'],
            mode_marker_sha256={m: sha(args.output / m / 'completed.json') for m in plans}))
        print('ALL_COMPLETED calls=124', flush=True)
    except BaseException as error:
        save(args.output / 'stopped.json', dict(error=type(error).__name__, actual_calls=actual_calls,
                                             message=str(error)[:500]))
        raise


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'baseline', 'extra', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--execute', action='store_true')
    run(parser.parse_args())
