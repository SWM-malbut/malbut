"""Opt-in, sequential HTTPS runner. No retries, fallback, uploads or keys in dry-run."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
import re
import time

from .inputs import (assess, code_hashes, digest, environment_versions, private_dir, require, save,
                     system_prompt)
from .native_boxes import PROFILE as NATIVE_PROFILE
from .metrics import estimate_cost, money, summarize, validate_rate
from .providers import MODELS, endpoint, error_details, headers, normalize, payload

TIMEOUT_S = 20.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
STOP_OUTCOMES = {'auth_error', 'quota_error', 'rate_or_quota_error'}


def diagnostic_headers(response_headers, secret):
    result = {}
    for name, value in response_headers.items():
        name = name.lower()
        if name not in ('retry-after', 'x-request-id', 'request-id', 'x-ratelimit-limit',
                        'x-ratelimit-remaining', 'x-ratelimit-reset',
                        'x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests',
                        'x-ratelimit-reset-requests', 'x-ratelimit-limit-tokens',
                        'x-ratelimit-remaining-tokens', 'x-ratelimit-reset-tokens'):
            continue
        if (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:, +/=-]{1,128}', value)
                and secret not in value and not value.startswith(('sk-', 'Bearer'))):
            result[name] = value
    return result


def request_deadline(timeout_s=None):
    value = TIMEOUT_S if timeout_s is None else timeout_s
    require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= 60,
            'invalid response deadline')
    return float(value)


async def https_post(url, body, request_headers, diagnostics=None, timeout_s=None):
    import aiohttp  # Optional dependency. Dry-run/fixtures do not need it.

    progress = diagnostics if diagnostics is not None else {}
    started = time.monotonic()
    credential = next((v.removeprefix('Bearer ') for k, v in request_headers.items()
                       if k.lower() in ('authorization', 'x-api-key', 'x-goog-api-key')), '')
    progress.update(stage='waiting_headers', response_bytes=0)
    timeout = aiohttp.ClientTimeout(total=request_deadline(timeout_s), connect=5)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                                     auto_decompress=False) as session:
        async with session.post(url, data=body, headers=request_headers,
                                allow_redirects=False) as response:
            progress.update(stage='reading_body', http_status=response.status,
                            headers_received_s=time.monotonic()-started,
                            response_headers=diagnostic_headers(getattr(response, 'headers', {}), credential))
            chunks, total = [], 0
            async for chunk in response.content.iter_chunked(16384):
                total += len(chunk)
                progress.setdefault('first_body_chunk_s', time.monotonic()-started)
                progress['response_bytes'] = total
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError('response_too_large')
                chunks.append(chunk)
            progress['stage'] = 'completed'
            # Only retain documented, numeric Kimi cache billing headers.
            writes = {}
            for ttl in ('5m', '1h'):
                value = getattr(response, 'headers', {}).get('Msh-Usage-Cache-Write-Tokens-' + ttl)
                if isinstance(value, str) and value.isascii() and value.isdigit():
                    writes[ttl] = int(value)
            if writes:
                return response.status, b''.join(chunks), writes
            return response.status, b''.join(chunks)


async def invoke(model, common, key, rate, workspace=None, transport=https_post, profile='baseline',
                 timeout_s=None, response_profile='runtime_crosscheck_with_findings'):
    native_system = system_prompt(NATIVE_PROFILE)
    if response_profile == NATIVE_PROFILE or common.get('system') == native_system:
        require(response_profile == NATIVE_PROFILE and common.get('system') == native_system,
                'native request/response profile mismatch')
    deadline = request_deadline(timeout_s)
    body = json.dumps(payload(model, common, profile), ensure_ascii=False, allow_nan=False).encode()
    require(len(body) <= MAX_INPUT_BYTES, 'request too large')
    started = time.monotonic()
    response_elapsed = None
    progress = {}
    result = dict(outcome='request_failed', reason='transport_error', label=None,
                  usage=None, usage_raw=None, model_returned=None, manual_review=False,
                  http_status=None, response_sha256=None, request_sha256=hashlib.sha256(body).hexdigest())
    try:
        args = (endpoint(model, workspace), body, headers(model, key))
        call = (transport(*args, diagnostics=progress, timeout_s=deadline)
                if transport is https_post else transport(*args))
        response = await asyncio.wait_for(call, timeout=deadline)
        response_elapsed = time.monotonic()-started
        status, raw = response[:2]
        writes = response[2] if len(response) == 3 else None
        result.update(normalize(model, status, raw, writes), http_status=status,
                      provider_error=error_details(raw, key),
                      cache_write_headers=writes,
                      response_sha256=hashlib.sha256(raw).hexdigest())
        if result['outcome'] == 'response':
            result.update(assess(result['text'], len(common['images']), response_profile))
            result['reason'] = 'response_checked' if result['outcome'] != 'invalid_response' else 'invalid_content'
    except asyncio.TimeoutError:
        result.update(outcome='timeout', reason='response_deadline')
    except Exception:
        # Never log exception strings: transport/SDK errors may contain headers,
        # request bodies or credentials. No retry is performed here.
        result.update(outcome='request_failed', reason='transport_error')
    billed = result.get('provider_billed_usd')
    if result['http_status'] is None and 'http_status' in progress:
        result['http_status'] = progress['http_status']
    result['transport_diagnostics'] = progress
    result.update(elapsed_s=response_elapsed if response_elapsed is not None else time.monotonic()-started,
                  cost_estimate_usd=billed if billed is not None else estimate_cost(result.get('usage'), rate),
                  cost_basis='provider_reported' if billed is not None else 'token_rate_estimate')
    result.update(request_timeout_s=deadline,
                  response_received=response_elapsed is not None,
                  response_within_20s=response_elapsed is not None and response_elapsed <= 20,
                  usable_reply_within_20s=result['outcome'] == 'classified'
                      and response_elapsed is not None and response_elapsed <= 20)
    return result


def make_plan(manifest, inputs, model_ids, workspace=None, profile='baseline', request_timeout_s=20):
    require(type(request_timeout_s) in (int, float) and request_timeout_s in (20, 60),
            'select a 20s benchmark or separate 60s diagnostic')
    require(model_ids and len(model_ids) == len(set(model_ids))
            and set(model_ids) <= set(MODELS), 'unknown or duplicate models')
    models = {}
    for mid in model_ids:
        model = MODELS[mid]
        example = payload(model, inputs[manifest['case_ids'][0]], profile)
        options = {k: v for k, v in example.items() if k not in (
            'input', 'messages', 'system', 'systemInstruction', 'contents')}
        url = endpoint(model, workspace)  # Fixed HTTPS hosts, no user URL/proxy.
        models[mid] = dict(**asdict(model), resolved_endpoint=url, options=options,
                           safety_settings='provider_default_unmodified', api_live_verified=False)
        for common in inputs.values():
            require(len(json.dumps(payload(model, common, profile), ensure_ascii=False).encode())
                    <= MAX_INPUT_BYTES, 'request too large')
    # Rotate model order across cases to avoid always measuring one provider last.
    schedule = []
    for i, cid in enumerate(manifest['case_ids']):
        order = model_ids[i % len(model_ids):] + model_ids[:i % len(model_ids)]
        schedule.extend(dict(case_id=cid, model=mid) for mid in order)
    plan = dict(version='paid-vlm-run-v1', scope=manifest['scope'], mode='standalone',
                manifest_sha256=digest(manifest), common_input_hashes={c: digest(v) for c, v in inputs.items()},
                condition=manifest['condition'], models=models, schedule=schedule,
                sources=code_hashes(), all_conditions_real_api_verified=False)
    if profile != 'baseline':
        plan['model_profile'] = profile
    if request_timeout_s != 20:
        plan['request_timeout_s'] = request_timeout_s
        plan['condition'] = dict(manifest['condition'], timeout_s=request_timeout_s,
                                 benchmark_deadline_s=20, separate_timeout_diagnostic=True)
    return plan


async def execute(plan, manifest, inputs, output, rates, *, approved_upload=False,
                  budget_usd=None, request_reserve_usd=None, max_calls=None,
                  workspace=None, credentials=None, transport=https_post,
                  continue_after_timeout=False):
    """Reserve is a user-chosen planning allowance, NOT a provider billing cap.

    Unknown cost stops further calls by default. Explicit timeout continuation
    retains each unknown request's reservation instead of declaring it free.
    This is NOT a guaranteed provider billing cap.
    """
    require(approved_upload, 'explicit upload approval required')
    require(type(continue_after_timeout) is bool, 'invalid timeout policy')
    budget, reserve = money(budget_usd), money(request_reserve_usd)
    require(budget > 0 and reserve > 0 and reserve <= budget, 'invalid budget/reservation')
    require(type(max_calls) is int and max_calls > 0, 'max_calls required')
    require(plan['manifest_sha256'] == digest(manifest), 'changed manifest')
    require(plan['sources'] == code_hashes(), 'code changed after planning')
    require(plan['common_input_hashes'] == {c: digest(v) for c, v in inputs.items()}, 'changed inputs')
    profile = plan.get('model_profile', 'baseline')
    deadline = plan.get('request_timeout_s', 20)
    require(plan == make_plan(manifest, inputs, list(plan['models']), workspace, profile, deadline),
            'changed plan')
    keys = {}
    for mid in plan['models']:
        validate_rate(rates.get(mid))  # null example rates cannot start live work.
        model = MODELS[mid]
        key = (credentials if credentials is not None else os.environ).get(model.key_env)
        headers(model, key)  # Fail before sending anything if any selected key is missing.
        keys[mid] = key
    if transport is https_post:
        import aiohttp  # noqa: F401 -- fail before creating an in-flight journal.
    private_dir(output)
    save(output / 'run.json', dict(plan=plan, rates=rates, created_utc=datetime.now(timezone.utc).isoformat(),
                                  budget_usd=str(budget), per_request_reserve_usd=str(reserve),
                                  max_calls=max_calls, budget_is_provider_cap=False,
                                  continue_after_timeout=continue_after_timeout,
                                  environment=environment_versions()))
    rows, charged, held, unknown, stop = [], Decimal(0), Decimal(0), False, 'completed'
    for job in plan['schedule']:
        if len(rows) >= max_calls:
            stop = 'max_calls'
            break
        if charged + held + reserve > budget:
            stop = 'budget_reservation'
            break
        mid, cid = job['model'], job['case_id']
        number = len(rows) + 1
        # Durable pre-call record. If interrupted, this entry without a result
        # means delivery/billing unknown. Existing output dirs are never resumed.
        save(output / f'{number:05d}.started.json', dict(**job,
             reserved_usd=str(reserve), input_sha256=digest(inputs[cid]),
             started_utc=datetime.now(timezone.utc).isoformat()))
        row = await invoke(MODELS[mid], inputs[cid], keys[mid], rates[mid], workspace, transport, profile,
                           timeout_s=deadline,
                           response_profile=manifest['condition'].get('prompt',
                               'runtime_crosscheck_with_findings'))
        row.update(**job, sequence=number)
        save(output / f'{number:05d}.result.json', row)
        rows.append(row)
        if row['cost_estimate_usd'] is None:
            unknown = True
            held += reserve
            if continue_after_timeout and row['outcome'] == 'timeout':
                continue
            stop = row['outcome'] if row['outcome'] in STOP_OUTCOMES else 'unknown_cost'
            break
        cost = money(row['cost_estimate_usd'])
        charged += cost
        if row['outcome'] in STOP_OUTCOMES:
            stop = row['outcome']
            break
        if cost > reserve:
            stop = 'request_exceeded_reservation'
            break
    summaries = {mid: summarize([r for r in rows if r['model'] == mid], manifest['labels'])
                 for mid in plan['models']}
    report = dict(stop_reason=stop, completed=len(rows) == len(plan['schedule']),
                  actual_calls=len(rows), planned_calls=len(plan['schedule']), models=summaries,
                  known_cost_estimate_usd=str(charged), unknown_cost=unknown,
                  unresolved_reservation_usd=str(held),
                  continue_after_timeout=continue_after_timeout)
    save(output / 'summary.json', report)
    return report
