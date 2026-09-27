#!/usr/bin/env python3
"""Prepare/check paid VLM comparison offline. Actual uploads require --execute."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from paid_vlm.inputs import derive_bundle, load_bundle, prepare, require
from paid_vlm.prompts import PROMPT_ADDITIONS
from paid_vlm.providers import MODELS, strict_json
from paid_vlm.runner import execute, make_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='list configured candidates; no network')
    p = commands.add_parser('prepare', help='verify reviewed fall84 and extract last 5s/12 JPEGs offline')
    p.add_argument('--frozen', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--cases', nargs='+', help='preselect pilot case IDs; omitted means all84')
    p = commands.add_parser('derive', help='copy frozen JPEGs into a new prompt experiment offline')
    p.add_argument('--inputs', type=Path, required=True, help='existing immutable input bundle')
    p.add_argument('--output', type=Path, required=True, help='new directory outside Git')
    p.add_argument('--prompt', choices=sorted(PROMPT_ADDITIONS), required=True)
    p.add_argument('--cases', nargs='+', help='case IDs to retain; omitted means all parent cases')
    p = commands.add_parser('run', help='dry-run by default; checks inputs without reading keys')
    p.add_argument('--inputs', type=Path, required=True)
    p.add_argument('--models', nargs='+', choices=sorted(MODELS), required=True)
    p.add_argument('--model-profile', choices=('baseline', 'low_reasoning'), default='baseline')
    p.add_argument('--qwen-workspace', help='Singapore Model Studio workspace ID, not an API key')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--approve-upload', action='store_true')
    p.add_argument('--output', type=Path)
    p.add_argument('--rates', type=Path, help='reviewed USD token rates JSON')
    p.add_argument('--budget-usd')
    p.add_argument('--request-reserve-usd', help='planning allowance per call; not a billing cap')
    p.add_argument('--max-calls', type=int)
    p.add_argument('--continue-after-timeout', action='store_true',
                   help='continue to next video after timeout, retaining its cost reservation')
    p.add_argument('--request-timeout-s', type=int, choices=(20, 60), default=20,
                   help='60 is a separate diagnostic; production and benchmark remain 20 seconds')
    args = parser.parse_args()
    if args.command == 'list':
        print(json.dumps([dict(model=k, provider=v.provider, key_env=v.key_env,
                               live_api_verified=False) for k, v in MODELS.items()], indent=2))
        return
    if args.command == 'prepare':
        result = prepare(args.frozen.resolve(), args.output, args.cases)
        print(json.dumps(dict(status='PREPARED_OFFLINE', scope=result['scope'],
                              cases=len(result['case_ids']), uploads=0)))
        return
    if args.command == 'derive':
        manifest, _ = derive_bundle(args.inputs, args.output, args.prompt, args.cases)
        print(json.dumps(dict(status='DERIVED_OFFLINE', scope=manifest['scope'],
                              cases=len(manifest['case_ids']), prompt=manifest['condition']['prompt'],
                              uploads=0)))
        return
    manifest, inputs = load_bundle(args.inputs)
    plan = make_plan(manifest, inputs, args.models, args.qwen_workspace,
                     profile=args.model_profile,
                     request_timeout_s=args.request_timeout_s)
    if not args.execute:
        print(json.dumps(dict(status='DRY_RUN', uploads=0, scope=plan['scope'],
                              planned_calls=len(plan['schedule']), models=list(plan['models']),
                              model_profile=plan.get('model_profile', 'baseline'),
                              condition=plan['condition']), indent=2))
        return
    require(args.output is not None and args.rates is not None, 'output/rates required for execution')
    rates = strict_json(args.rates.read_bytes())
    report = asyncio.run(execute(plan, manifest, inputs, args.output, rates,
        approved_upload=args.approve_upload, budget_usd=args.budget_usd,
        request_reserve_usd=args.request_reserve_usd, max_calls=args.max_calls,
        workspace=args.qwen_workspace, continue_after_timeout=args.continue_after_timeout))
    # Do not print model prose, response bodies, images, exceptions or credentials.
    print(json.dumps(dict(status='COMPLETED' if report['completed'] else 'STOPPED',
                         calls=report['actual_calls'], reason=report['stop_reason'])))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('INTERRUPTED: keep started/result records; do not automatically retry.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        # A provider/library exception may carry a secret. Never dump traceback.
        print(f'FAILED ({type(error).__name__}): check inputs, rates and execution options; '
              'no automatic retry.', file=sys.stderr)
        sys.exit(1)
