#!/usr/bin/env python3
"""Replace only the local model on verified original Gemma A RGB requests."""
import argparse
import copy
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import run_ollama_fall_suite as suite
from replay_fall84_facts import origins, read
from replay_fall84_prompt_ab import comparison
from replay_fall_baseline import sha, verify_freeze
from replay_vlm_frames import api, digest, request_payload, save
from review_fall_annotations import require

PROTOCOL = Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1/MODEL_PAIR_PROTOCOL.md'


def rebuild(dataset, meta, original, contract, model_name, thinking):
    images, frames = suite.extract_prefix(dataset, meta, original['available_through_frame'])
    require(frames == original['frames'], 'original JPEG/timestamp changed')
    require(meta['sha256'] == original['media_sha256'], 'original media changed')
    require(digest(contract) == original['contract_sha256'], 'original contract changed')
    payload = request_payload(contract['model']['name'], images, frames, original['duration_s'],
                              contract['options'], schema_in_prompt=True, evaluation_version='v2')
    if 'wire_schema' in contract:
        payload['format'] = contract['wire_schema']
    if contract['thinking'] == 'disabled':
        payload['think'] = False
    payload['keep_alive'] = '10m'
    require(digest(payload) == original['request_sha256'], 'original A request changed')
    payload['model'] = model_name
    require(thinking in ('disabled', 'unsupported'), 'thinking must not be enabled')
    if thinking == 'disabled':
        payload['think'] = False
    else:
        payload.pop('think', None)
    return payload


def run(args):
    require(args.execute, 'explicit --execute required')
    require(not args.output.exists(), 'output exists; no overwrite or silent retry')
    suite.endpoint_url(args.endpoint)
    verify_freeze(args.frozen)
    plans = origins(args)
    metas = {m['case_id']: m for m in read(args.frozen / 'media.json')['cases']}
    require(len(metas) == 84, 'requires 84 frozen cases')
    model = suite.local_model(args.endpoint, args.model)
    require(model['name'] != 'gemma4:12b', 'comparison requires another local model')
    details = api(args.endpoint, '/api/show', {'model': args.model})
    thinking = 'disabled' if 'thinking' in details.get('capabilities', []) else 'unsupported'
    version = api(args.endpoint, '/api/version')['version']
    contracts, expected = {}, {}
    for mode, rows in plans.items():
        require(set(rows) == set(metas), 'wrong baseline coverage')
        for cid, (root, prior) in rows.items():
            if root not in contracts:
                c = read(root / 'run.json')['contract']
                require(c['model']['name'] == 'gemma4:12b' and c['evaluation_version'] == 'v2',
                        'requires original Gemma A v2')
                require(c['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
                require(c['ollama_version'] == version, 'server version changed')
                import cv2
                require(c['cv2_version'] == cv2.__version__, 'JPEG environment changed')
                contracts[root] = c
            if prior['status'] == 'responded':
                original = read(root / f'{cid}.input.json')
                payload = rebuild(args.frozen, metas[cid], original, contracts[root], args.model, thinking)
                expected[(mode, cid)] = digest(payload)
    require(len(expected) == 124, 'incorrect planned calls')
    files = [Path(__file__), PROTOCOL, Path(suite.__file__),
             Path(__file__).with_name('replay_vlm_frames.py'),
             Path(__file__).with_name('fall_evaluation_v2.py'),
             Path(__file__).with_name('replay_fall84_facts.py'),
             Path(__file__).with_name('replay_fall84_prompt_ab.py')]
    sources = {str(p): sha(p) for p in files}
    args.output.mkdir(mode=0o700)
    snapshot = args.output / 'code-snapshot'
    snapshot.mkdir(mode=0o700)
    for p in files:
        shutil.copy2(p, snapshot / p.name)
    contract = dict(version='fall84-model-pair-v1', created_utc=datetime.now(timezone.utc).isoformat(),
        model=model, thinking=thinking, ollama_version=version, source_sha256=sources,
        freeze_sha256=sha(args.frozen / 'freeze.json'), retries=0,
        baseline_contracts={str(p): digest(c) for p, c in contracts.items()},
        baseline_markers={str(p): sha(p / 'completed.json') for p in contracts},
        changed_fields=['model'] + (['omit_unsupported_think_false'] if thinking == 'unsupported' else []),
        scope='original A prompt/schema/RGB; 40 gated + 84 full; no tuning, labels, sensors or cloud')
    save(args.output / 'run.json', contract)
    print('PREFLIGHT_OK', args.model, '124 verified A inputs; thinking=' + thinking, flush=True)
    actual_calls = 0
    try:
        for mode, rows in plans.items():
            out = args.output / mode
            out.mkdir(mode=0o700)
            c = copy.deepcopy(contracts[args.baseline / mode])
            c.update(model=model, thinking=thinking, capabilities=details.get('capabilities'),
                     source_sha256=sources, replay_protocol='fall84-model-pair-v1',
                     reference_sha256=(contracts[args.extra]['reference_sha256']
                                       if mode == 'gated' else c['reference_sha256']),
                     timing='offline identical RGB request replay; not real-time fall latency',
                     pair_contract_sha256=digest(contract),
                     baseline_contract_sha256=digest(contracts[args.baseline / mode]))
            save(out / 'run.json', dict(contract=c, contract_sha256=digest(c)))
            results = []
            for cid, (root, prior) in sorted(rows.items()):
                if prior['status'] == 'not_triggered':
                    save(out / f'{cid}.result.json', dict(case_id=cid, status='not_triggered',
                         valid=False, prediction=None, contract_sha256=digest(c)))
                else:
                    original = read(root / f'{cid}.input.json')
                    save(out / f'{cid}.pair.json', dict(baseline_input=str(root / f'{cid}.input.json'),
                        baseline_input_sha256=sha(root / f'{cid}.input.json'),
                        original_request_sha256=original['request_sha256'],
                        expected_request_sha256=expected[(mode, cid)]))
                    actual_calls += 1
                    suite.call_case(SimpleNamespace(dataset=args.frozen, endpoint=args.endpoint),
                        out, c, metas[cid], original['available_through_frame'],
                        candidate=original.get('candidate') if mode == 'gated' else None)
                    inp = read(out / f'{cid}.input.json')
                    require(inp['request_sha256'] == expected[(mode, cid)], 'actual request differs from preflight')
                    require(inp['frames'] == original['frames'], 'actual images/timestamps changed')
                    print('PROGRESS', actual_calls, '/124', flush=True)
                results.append(read(out / f'{cid}.result.json'))
            labels = {r['case_id']: r for r in read(args.frozen / 'evaluation_labels.json')['classifications']['cases']}
            save(out / 'comparison.json', comparison([r for _, r in rows.values()], results, labels, mode))
            save(out / 'completed.json', dict(cases=84, files={p.name: sha(p) for p in out.iterdir()}))
            print('MODE_COMPLETED', mode, flush=True)
        require(actual_calls == 124, 'missing calls')
        require(suite.local_model(args.endpoint, args.model) == model, 'model changed during calls')
        for path, expected_sha in sources.items():
            require(sha(Path(path)) == expected_sha, 'source changed during calls')
        for root in contracts:
            suite.verify_completed(root)
        verify_freeze(args.frozen)
        save(args.output / 'completed.json', dict(actual_calls=actual_calls,
            run_sha256=sha(args.output / 'run.json'),
            mode_marker_sha256={m: sha(args.output / m / 'completed.json') for m in plans}))
        print('ALL_COMPLETED calls=124', flush=True)
    except BaseException as error:
        save(args.output / 'stopped.json', dict(error=type(error).__name__, message=str(error)[:500],
                                             actual_calls=actual_calls))
        raise


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'baseline', 'extra', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--endpoint', required=True)
    p.add_argument('--execute', action='store_true')
    run(p.parse_args())
