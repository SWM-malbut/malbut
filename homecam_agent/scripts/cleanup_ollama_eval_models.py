#!/usr/bin/env python3
"""User-authorized cleanup of newly downloaded, completed evaluation model tags.

Never removes source videos/results/pre-existing models, or an active runner's model.
Checks exact registry digest and immutable completion evidence before each deletion.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

from replay_vlm_frames import api, canonical, endpoint_url, save
from run_ollama_fall_suite import verify_completed
from review_fall_annotations import require


# Verified Mac inventory before this request. qwen3-vl:2b was downloaded in this task.
PROTECTED = {'qwen2.5vl:7b', 'qwen2.5:7b', 'bge-m3:latest', 'nomic-embed-text:latest'}


def eligible_model(directory, planned, installed, active):
    if not directory.is_dir():
        return None
    name = directory.name.replace('--', ':')
    if name in PROTECTED or 'cloud' in name or name not in planned:
        return None
    current = installed.get(name)
    if current is None or name in active:
        return None
    if current.get('digest') != planned[name].get('manifest_sha256'):
        return None
    complete = all((directory / mode / 'completed.json').exists() for mode in ('full', 'gated'))
    # Only after successful full runs; ambiguous/interrupted/download failures are retained.
    if not complete:
        return None
    for mode in ('full', 'gated'):
        out = directory / mode
        verify_completed(out)
        contract = json.loads((out / 'run.json').read_text())['contract']
        require(contract['model']['name'] == name, 'cleanup model name mismatch')
        require(contract['model']['digest'] == current['digest'], 'cleanup model digest mismatch')
    return dict(name=name, digest=current['digest'], model_bytes=current['size'],
                results=str(directory), reason='both actual evaluation modes completed')


def sweep(args):
    planned = {}
    for path in args.catalogs:
        for model in json.loads(path.read_text())['models']:
            planned[model['name']] = model
    installed = {m['name']: m for m in api(args.endpoint, '/api/tags')['models']}
    active = {m['name'] for m in api(args.endpoint, '/api/ps')['models']}
    for directory in args.runs.iterdir():
        target = eligible_model(directory, planned, installed, active)
        if target is None:
            continue
        # Recheck immediately before the destructive call, avoiding stale scans.
        now = {m['name']: m for m in api(args.endpoint, '/api/tags')['models']}
        loaded = {m['name'] for m in api(args.endpoint, '/api/ps')['models']}
        if eligible_model(directory, planned, now, loaded) is None:
            continue
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        audit = args.audit / (stamp + '-' + directory.name)
        save(audit.with_suffix('.requested.json'), dict(
            target, authorization='user: newly downloaded models may be deleted after evaluation',
            recoverable='ollama pull ' + target['name'],
            preexisting_models_preserved=sorted(PROTECTED)))
        request = Request(endpoint_url(args.endpoint) + '/api/delete',
                          data=canonical(dict(model=target['name'])), method='DELETE',
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=30) as response:
            require(response.status == 200, 'model deletion did not succeed')
        remaining = {m['name'] for m in api(args.endpoint, '/api/tags')['models']}
        require(target['name'] not in remaining, 'deleted tag still present')
        save(audit.with_suffix('.completed.json'), target)
        print('DELETED_NEW_EVAL_MODEL', target['name'], 'results retained; re-download possible',
              flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--catalogs', type=Path, nargs='+', required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--watch-seconds', type=int, default=0)
    parser.add_argument('--until-pids', type=int, nargs='+', default=[])
    args = parser.parse_args()
    require(0 <= args.watch_seconds <= 7 * 86400, 'cleanup watch bound invalid')
    args.audit.mkdir(mode=0o700, exist_ok=True)
    started = time.monotonic()
    while True:
        try:
            sweep(args)
        except (OSError, ValueError) as error:
            print('CLEANUP_PAUSED', type(error).__name__, 'no broad deletion attempted', flush=True)
        queues_running = any(Path(f'/proc/{pid}').exists() for pid in args.until_pids)
        if (time.monotonic() - started >= args.watch_seconds or
                args.until_pids and not queues_running):
            break
        time.sleep(30)


if __name__ == '__main__':
    main()
