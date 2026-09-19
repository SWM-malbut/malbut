#!/usr/bin/env python3
"""Free-only Gemma Cloud on the same original-A 40 gated + 84 full inputs."""
import argparse
import copy
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import replay_fall84_model as pair
import run_free_cloud_fall_suite as cloud
from replay_fall84_facts import origins, read
from replay_fall84_prompt_ab import comparison
from replay_fall_baseline import sha, verify_freeze
from replay_vlm_frames import api, digest, save
from review_fall_annotations import require

MODEL = 'gemma4:31b-cloud'
PROTOCOL = Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1/QWEN35_CLOUD_PAIR_PROTOCOL.md'


def rebuild(dataset, meta, original, contract, thinking):
    payload = pair.rebuild(dataset, meta, original, contract, MODEL, thinking)
    # Ollama Cloud does not support server-side JSON Schema enforcement.
    # The complete original schema remains in the unchanged user prompt.
    del payload['format']
    del payload['keep_alive']  # local model residency has no cloud equivalent
    return payload


def cloud_identity(endpoint):
    details = api(endpoint, '/api/show', {'model': MODEL}, timeout=20)
    require('vision' in details.get('capabilities', []), 'cloud model lacks vision')
    return dict(name=MODEL, details=details.get('details'), show_sha256=digest(details)), details


def run(args):
    require(args.execute, 'explicit --execute required')
    require(not args.output.exists(), 'output exists; no overwrite or silent retry')
    pair.suite.endpoint_url(args.endpoint)
    free_access = cloud.free_account(args)
    verify_freeze(args.frozen)
    plans = origins(args)
    metas = {m['case_id']: m for m in read(args.frozen / 'media.json')['cases']}
    require(len(metas) == 84, 'requires 84 frozen cases')
    model, details = cloud_identity(args.endpoint)
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
                expected[(mode, cid)] = digest(rebuild(args.frozen, metas[cid], original,
                                                       contracts[root], thinking))
    require(len(expected) == 124, 'incorrect planned calls')
    files = [Path(__file__), PROTOCOL, Path(pair.__file__), Path(cloud.__file__),
             Path(pair.suite.__file__)] + [Path(__file__).with_name(n) for n in (
                 'replay_vlm_frames.py', 'fall_evaluation_v2.py', 'replay_fall84_facts.py',
                 'replay_fall84_prompt_ab.py', 'replay_fall_baseline.py')]
    sources = {str(p): sha(p) for p in files}
    args.output.mkdir(mode=0o700)
    snapshot = args.output / 'code-snapshot'
    snapshot.mkdir(mode=0o700)
    for p in files:
        shutil.copy2(p, snapshot / p.name)
    contract = dict(version='fall84-cloud-pair-v1', created_utc=datetime.now(timezone.utc).isoformat(),
        model=model, thinking=thinking, ollama_version=version, source_sha256=sources,
        freeze_sha256=sha(args.frozen / 'freeze.json'), retries=0, free_access=free_access,
        baseline_contracts={str(p): digest(c) for p, c in contracts.items()},
        baseline_markers={str(p): sha(p / 'completed.json') for p in contracts},
        changed_fields=['model', 'omit_unsupported_format', 'omit_local_keep_alive'] +
                       (['omit_unsupported_think_false'] if thinking == 'unsupported' else []),
        scope='original A prompt/schema text/RGB; 40 gated + 84 full; no tuning or new candidates',
        cloud_revision='serving weights cannot be independently pinned',
        billing='previous user confirmation: no purchased credits or automatic top-up; Free plan checked per call')
    save(args.output / 'run.json', contract)
    print('PREFLIGHT_OK', MODEL, '124 verified A inputs; FREE_ONLY', flush=True)
    actual_calls = 0
    try:
        for mode, rows in plans.items():
            out = args.output / mode
            out.mkdir(mode=0o700)
            c = copy.deepcopy(contracts[args.baseline / mode])
            c.update(model=model, thinking=thinking, capabilities=details.get('capabilities'),
                source_sha256=sources, replay_protocol='fall84-cloud-pair-v1',
                reference_sha256=(contracts[args.extra]['reference_sha256']
                                  if mode == 'gated' else c['reference_sha256']),
                timing='offline identical RGB replay; Cloud/SSH request time, not fall latency',
                model_host='Ollama Cloud via authenticated Mac Ollama; not local/Jetson',
                schema_enforcement='prompt only; no format parameter; strict client validation',
                options_enforcement='requested; Cloud seed/context enforcement not verifiable',
                pair_contract_sha256=digest(contract), free_access=free_access)
            c.pop('wire_schema', None)
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
                    cloud.invoke(SimpleNamespace(dataset=args.frozen, endpoint=args.endpoint,
                        no_paid_balance_confirmed=args.no_paid_balance_confirmed), out, c, metas[cid],
                        original['available_through_frame'],
                        candidate=original.get('candidate') if mode == 'gated' else None)
                    actual_calls += 1
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
        require(cloud_identity(args.endpoint)[0] == model, 'cloud metadata changed during calls')
        for path, expected_sha in sources.items():
            require(sha(Path(path)) == expected_sha, 'source changed during calls')
        for root in contracts:
            pair.suite.verify_completed(root)
        verify_freeze(args.frozen)
        save(args.output / 'completed.json', dict(actual_calls=actual_calls,
            run_sha256=sha(args.output / 'run.json'),
            mode_marker_sha256={m: sha(args.output / m / 'completed.json') for m in plans}))
        print('ALL_COMPLETED calls=124', flush=True)
    except BaseException as error:
        # Count prepared requests separately: authentication may stop before any chat is sent.
        save(args.output / 'stopped.json', dict(error=type(error).__name__, message=str(error)[:500],
             completed_calls=actual_calls, inputs_written=len(list(args.output.glob('*/*.input.json'))),
             results_written=len(list(args.output.glob('*/*.result.json')))))
        raise


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen', 'baseline', 'extra', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--endpoint', required=True)
    p.add_argument('--no-paid-balance-confirmed', action='store_true')
    p.add_argument('--execute', action='store_true')
    run(p.parse_args())
