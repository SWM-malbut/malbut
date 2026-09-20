#!/usr/bin/env python3
"""Explicitly authorized free-tier Ollama Cloud replay; never buy credits or change billing.

Requires confirmation of NO purchased credits/automatic top-up, plus a Free account check
before EACH inference. Stops the entire queue on auth/quota/payment errors. The service,
not this client, enforces free allowances. Local model comparisons run in another queue.
"""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError

from replay_fall_baseline import sha, verify_freeze
from replay_vlm_frames import api, assess_response, digest, request_payload, save, check_evaluation_labels
from review_fall_annotations import require
from run_ollama_fall_suite import (
    extract_prefix, normalize_ids, report, SCRIPTS, mode_contract, verify_completed,
)


MODELS = ['gemma4:31b-cloud', 'qwen3.5:397b-cloud', 'glm-5.3-flash:cloud',
          'minimax-m3:cloud', 'kimi-k2.6:cloud', 'kimi-k2.7-code:cloud',
          'kimi-k3:cloud', 'mistral-large-3:675b-cloud']


class FreeAccessStopped(RuntimeError):
    pass


def free_account(args):
    require(args.no_paid_balance_confirmed,
            'user must confirm no purchased credits and no automatic top-up')
    account = api(args.endpoint, '/api/me', {}, timeout=15)
    if account.get('plan') != 'free':
        raise FreeAccessStopped('Free account not verified; no inference sent')
    # Never persist names, e-mail, user IDs or account credentials.
    return dict(plan='free', no_paid_balance_user_confirmed=True)


def cloud_payload(name, images, frames, duration, options, thinking, evaluation_version='v1'):
    require(name in MODELS, 'cloud model not in explicit evaluation list')
    payload = request_payload(name, images, frames, duration, options, schema_in_prompt=True,
                              evaluation_version=evaluation_version)
    # Official Ollama Cloud documentation does not support structured outputs.
    # Keep the exact textual schema, but do not claim server-side schema enforcement.
    del payload['format']
    if thinking == 'disabled':
        payload['think'] = False
    return payload


def invoke(args, output, contract, meta, last, candidate=None, pose_ms=None):
    cid = meta['case_id']
    require(not (output / f'{cid}.input.json').exists(), 'interrupted attempt requires inspection')
    checked = free_account(args)
    images, frames = extract_prefix(args.dataset, meta, last)
    duration = (last + 1) / meta['fps']
    payload = cloud_payload(contract['model']['name'], images, frames, duration,
                            contract['options'], contract['thinking'],
                            contract.get('evaluation_version', 'v1'))
    save(output / f'{cid}.input.json', dict(
        case_id=cid, media_sha256=meta['sha256'], duration_s=duration, frames=frames,
        available_through_frame=last, request_sha256=digest(payload),
        contract_sha256=digest(contract), user_prompt=payload['messages'][1]['content'],
        candidate=candidate, candidate_sent_to_model=False,
        pose_compute_ms_until_request=pose_ms, free_account_check=checked))
    started = time.perf_counter()
    try:
        response = api(args.endpoint, '/api/chat', payload, timeout=contract.get('timeout_s', 300))
    except (OSError, ValueError) as error:
        record = dict(case_id=cid, status='request_failed', valid=False,
                      contract_sha256=digest(contract), request_s=time.perf_counter()-started,
                      error_type=type(error).__name__, http_status=getattr(error, 'code', None))
        if isinstance(error, HTTPError):
            record['server_error'] = error.read(4096).decode('utf-8', errors='replace')
        save(output / f'{cid}.result.json', record)
        if record['http_status'] in (401, 402, 403, 429):
            raise FreeAccessStopped('authentication/free access/quota stopped; no retry') from None
        if contract.get('evaluation_version') == 'v2' and not isinstance(error, HTTPError):
            return
        raise RuntimeError(f'{cid}: request failed; no retry') from None
    seconds = time.perf_counter() - started
    save(output / f'{cid}.response.json', response)
    result = (assess_response(response, round(duration, 3), evaluation_version='v2')
              if contract.get('evaluation_version') == 'v2'
              else assess_response(response, round(duration, 3)))
    result.update(case_id=cid, status='responded', contract_sha256=digest(contract),
                  response_sha256=sha(output / f'{cid}.response.json'), request_s=seconds,
                  trigger_s=last / meta['fps'] if candidate else None,
                  telemetry={k: response.get(k) for k in (
                      'total_duration', 'load_duration', 'prompt_eval_duration',
                      'eval_duration', 'prompt_eval_count', 'eval_count')},
                  thinking_returned=bool(response.get('message', {}).get('thinking')))
    save(output / f'{cid}.result.json', result)
    print('CLOUD_RESULT', contract['model']['name'], contract['mode'], cid,
          f'valid={result["valid"]} seconds={seconds:.2f}', flush=True)


def execute_mode(args, root, name, details, mode):
    output = root / mode
    meta = dict(name=name, details=details.get('details'), show_sha256=digest(details))
    contract = mode_contract(args, meta, details, mode)
    contract.update(
        model_host='Ollama Cloud via authenticated Mac Ollama; not local/Jetson',
        schema_enforcement='prompt_only; no format parameter; Cloud currently unsupported',
        free_access=free_account(args),
        cloud_runner_sha256=sha(Path(__file__)),
        options_enforcement='requested options; Cloud may not expose/effect local num_ctx or seed',
        cloud_models_not_locally_pinned='exact serving weights/revision unavailable')
    if output.exists():
        prior = json.loads((output / 'run.json').read_text())['contract']
        require(prior == contract, 'cloud run contract changed')
        require((output / 'completed.json').exists(), 'partial cloud run requires inspection')
        verify_completed(output)
        return
    output.mkdir(mode=0o700)
    save(output / 'run.json', dict(contract=contract, contract_sha256=digest(contract)))
    media = json.loads((args.frozen / 'media.json').read_text())['cases']
    metas = {m['case_id']: m for m in media}
    if mode == 'full':
        for m in media:
            invoke(args, output, contract, m, m['frames'] - 1)
    else:
        reference = [json.loads(line) for line in
                     (args.reference / 'frames.jsonl').read_text().splitlines()]
        command = [str(args.pose_python), str(SCRIPTS / 'stream_pose_candidates.py'),
                   '--dataset', str(args.dataset), '--frozen', str(args.frozen),
                   '--reference', str(args.reference), '--model', str(args.pose_model)]
        with (output / 'pose-stderr.txt').open('x') as errors, \
                (output / 'pose-frames.jsonl').open('x', buffering=1) as stream:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
            seen, called, compute = set(), set(), Counter()
            count = 0
            try:
                for line in process.stdout:
                    row = json.loads(line)
                    require(count < len(reference), 'unexpected Pose frame')
                    expected = reference[count]
                    for key in ('case_id', 'frame_index', 'observations'):
                        require(normalize_ids(row[key]) == normalize_ids(expected[key]),
                                'fresh Pose parity failed')
                    candidates = row['fall_analysis']['candidates']
                    require(normalize_ids(candidates) ==
                            normalize_ids(expected['fall_analysis']['candidates']),
                            'fresh candidate parity failed')
                    stream.write(json.dumps(row, allow_nan=False) + '\n')
                    stream.flush()
                    count += 1
                    cid = row['case_id']
                    seen.add(cid)
                    compute[cid] += row['pipeline_ms']
                    if candidates:
                        require(len(candidates) == 1 and cid not in called,
                                'multiple incidents need another scoring protocol')
                        called.add(cid)
                        invoke(args, output, contract, metas[cid], row['frame_index'],
                               candidate=candidates[0], pose_ms=compute[cid])
                require(process.wait() == 0 and count == len(reference), 'Pose worker failed')
                require(seen == set(metas), 'missing Pose cases')
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
        for cid in sorted(seen - called):
            save(output / f'{cid}.result.json', dict(
                case_id=cid, status='not_triggered', valid=False, prediction=None,
                contract_sha256=digest(contract), pose_compute_ms=compute[cid]))
    for path, expected in contract['source_sha256'].items():
        require(sha(Path(path)) == expected, 'source changed mid-run')
    require(sha(Path(__file__)) == contract['cloud_runner_sha256'], 'cloud runner changed')
    files = {p.name: sha(p) for p in output.iterdir() if p.is_file()}
    save(output / 'completed.json', dict(files=files, cases=len(media)))


def run(args):
    verify_freeze(args.frozen)
    check_evaluation_labels(args.frozen, getattr(args, 'evaluation_version', 'v1'))
    if getattr(args, 'evaluation_version', 'v1') == 'v2':
        timeout = getattr(args, 'timeout', None)
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 300,
                'v2 requires explicit --timeout (0..300 seconds)')
    free_account(args)
    args.output.mkdir(mode=0o700, exist_ok=True)
    for name in args.models or MODELS:
        require(name in MODELS, 'model outside cloud evaluation list')
        root = args.output / name.replace(':', '--')
        root.mkdir(mode=0o700, exist_ok=True)
        require(not (root / 'failed.json').exists(), 'previous failure requires inspection')
        try:
            details = api(args.endpoint, '/api/show', dict(model=name), timeout=20)
            require('vision' in details.get('capabilities', []), 'Cloud model lacks vision')
            for mode in ('gated', 'full'):
                execute_mode(args, root, name, details, mode)
                report(args)
        except (OSError, ValueError, RuntimeError) as error:
            save(root / 'failed.json', dict(error_type=type(error).__name__, reason=str(error)))
            print('CLOUD_STOPPED', name, type(error).__name__, str(error)[:160], flush=True)
            if isinstance(error, (FreeAccessStopped, HTTPError)):
                break
    report(args)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    for name in ('dataset', 'frozen', 'reference', 'pose-model', 'pose-python', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--models', nargs='+')
    parser.add_argument('--evaluation-version', choices=('v1', 'v2'), default='v2')
    parser.add_argument('--timeout', type=float, help='explicit per-call timeout for v2; no retries')
    parser.add_argument('--no-paid-balance-confirmed', action='store_true',
                        help='only after user confirms no purchased credits or auto top-up')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
