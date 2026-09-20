#!/usr/bin/env python3
"""Local Ollama VLM comparison with separate full-clip and fresh Pose-gated calls.

Cloud inference is deliberately unavailable here until free billing is verified.
Model pulls require --pull. No production/ROS changes, paid calls or automatic retries.
"""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from replay_fall_baseline import REPO, SOURCE, sha, verify_freeze
from replay_vlm_frames import (
    api, assess_response, canonical, digest, endpoint_url, frame_indices, local_model,
    request_payload, save, SYSTEM_PROMPT, USER_TEMPLATE, PREDICTION_JSON_SCHEMA,
    prompt_spec, check_evaluation_labels, SCHEMA_PROMPT_PREFIX,
)
from review_fall_annotations import require


# One default quantization per published size, not hundreds of aliases of identical weights.
# The catalog records oversized, text-only and unavailable entries, rather than hiding them.
FAMILIES = {
    'qwen2.5vl': ['3b', '7b', '32b', '72b'],
    'qwen3-vl': ['2b', '4b', '8b', '30b', '32b', '235b'],
    'qwen3.5': ['0.8b', '2b', '4b', '9b', '27b', '35b', '122b'],
    'qwen3.6': ['27b', '35b'], 'qwen3.8': ['27b'],
    'qwen3.8-flash-next': ['latest'],
    'gemma3': ['4b', '12b', '27b'], 'gemma3n': ['e2b', 'e4b'],
    'gemma4': ['e2b', 'e4b', '12b', '26b', '31b'],
    'minicpm-v': ['8b'], 'minicpm-v4.5': ['8b'], 'minicpm-v4.6': ['1b'],
    'granite3.2-vision': ['2b'], 'moondream': ['1.8b'],
    'llava': ['7b', '13b', '34b'], 'llava-llama3': ['8b'],
    'bakllava': ['7b'], 'llava-phi3': ['3.8b'], 'llama3.2-vision': ['11b', '90b'],
    'mistral-small3.1': ['24b'], 'mistral-small3.2': ['24b'],
    'ministral-3': ['3b', '8b', '14b'],
    'medgemma': ['4b', '27b'], 'medgemma1.5': ['4b'],
    'muse-glimmer': ['30b'], 'nemotron3': ['33b'], 'ornith-1.5': ['9b', '35b', '397b'],
    'mistral-medium-3.5': ['128b'], 'glm-ocr': ['latest'],
}
CLOUD_FAMILIES = ['glm-5.3-flash', 'minimax-m3', 'kimi-k2.6', 'kimi-k2.7-code',
                  'kimi-k3', 'mistral-large-3', 'qwen3.5', 'gemma4']
SCRIPTS = Path(__file__).resolve().parent
PROTOCOL = SCRIPTS.parent / 'evaluations/synthetic_fall_v1/OLLAMA_SUITE_PROTOCOL.md'


def registry_model(name):
    family, tag = name.split(':')
    url = f'https://registry.ollama.ai/v2/library/{family}/manifests/{tag}'
    record = dict(name=name, source=url)
    try:
        with urlopen(url, timeout=20) as response:
            raw = response.read(1024 * 1024)
        manifest = json.loads(raw)
        record.update(manifest=manifest, manifest_sha256=hashlib.sha256(raw).hexdigest(),
                      size_bytes=sum(layer.get('size', 0) for layer in manifest['layers']))
        if record['size_bytes'] <= 0:
            record['status'] = 'not_local_weights'
        elif record['size_bytes'] > 40 * 1024**3:
            record['status'] = 'weights_exceed_40GiB_memory_budget'
        else:
            record['status'] = 'planned'
    except (OSError, ValueError, KeyError) as error:
        record.update(status='registry_unavailable', error=type(error).__name__,
                      http_status=getattr(error, 'code', None))
    return record


def inventory(args):
    names = [f'{family}:{tag}' for family, tags in FAMILIES.items() for tag in tags]
    with ThreadPoolExecutor(max_workers=5) as pool:
        models = list(pool.map(registry_model, names))
    seen = {}
    for model in models:
        key = model.get('manifest_sha256')
        if key in seen:
            model.update(status='identical_manifest_alias', alias_of=seen[key])
        elif key:
            seen[key] = model['name']
    installed = api(args.endpoint, '/api/tags').get('models', [])
    order = {m['name']: i for i, m in enumerate(installed)}
    models.sort(key=lambda m: (m['name'] not in order, m.get('size_bytes', math.inf)))
    value = dict(created_utc=datetime.now(timezone.utc).isoformat(), models=models,
                 installed_before=installed,
                 cloud=[dict(family=f, status='awaiting_free_account_and_billing_verification')
                        for f in CLOUD_FAMILIES],
                 scope='official vision families, one default quant per size; not every community '
                       'fine-tune or quantization alias; no paid cloud inference',
                 maximum_weights_bytes=40 * 1024**3)
    save(args.output, value)
    print(json.dumps(dict(statuses=dict(Counter(m['status'] for m in models)),
                          planned_GB=sum(m.get('size_bytes', 0) for m in models
                                         if m['status'] == 'planned') / 1e9), indent=2))


def pull(endpoint, name):
    # Ollama pulls weights from its own registry; no prompts or dataset leave the machine.
    request = Request(endpoint_url(endpoint) + '/api/pull',
                      data=canonical(dict(model=name, stream=True)),
                      headers={'Content-Type': 'application/json'})
    started, last = time.monotonic(), 0
    with urlopen(request, timeout=60) as response:
        success = False
        for line in response:
            require(time.monotonic() - started < 8 * 3600, 'model pull exceeded eight hours')
            require(len(line) < 65536, 'oversized pull response')
            message = json.loads(line)
            if message.get('error'):
                raise ValueError('Ollama pull failed: ' + message['error'][:200])
            if time.monotonic() - last >= 30 or message.get('status') == 'success':
                print('PULL', name, message.get('status'), message.get('completed', 0),
                      '/', message.get('total', 0), flush=True)
                last = time.monotonic()
            if message.get('status') == 'success':
                success = True
        require(success, 'pull ended without success')


def prefix_indices(last_index, count=12):
    require(type(last_index) is int and last_index >= 0, 'invalid available frame')
    return [0] if last_index == 0 else frame_indices(last_index + 1, count)


def extract_prefix(dataset, meta, last_index):
    import cv2
    path = (dataset / meta['source_path']).resolve()
    require(path.is_relative_to(dataset.resolve()), 'media path outside dataset')
    require(sha(path) == meta['sha256'], 'source media changed')
    require(0 <= last_index < meta['frames'], 'future or invalid frame')
    selected = prefix_indices(last_index)
    images, frames = [], []
    cap = cv2.VideoCapture(str(path))
    try:
        require(cap.isOpened(), 'video unavailable')
        require(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == meta['frames'], 'frame count changed')
        require(abs(cap.get(cv2.CAP_PROP_FPS) - meta['fps']) < .001, 'fps changed')
        for index in range(last_index + 1):
            ok, frame = cap.read()
            require(ok, 'video decode failed')
            if index not in selected:
                continue
            require(frame.shape[:2] == (meta['height'], meta['width']), 'resolution changed')
            ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            require(ok, 'JPEG encode failed')
            data = jpeg.tobytes()
            images.append(base64.b64encode(data).decode('ascii'))
            frames.append(dict(frame_index=index, timestamp_s=index / meta['fps'],
                               jpeg_sha256=hashlib.sha256(data).hexdigest()))
    finally:
        cap.release()
    return images, frames


def normalize_ids(value):
    """Only random per-run ID prefixes; never alter numerical evidence or predictions."""
    if isinstance(value, str):
        return re.sub(r'\b(?:[0-9a-f]{12}|[0-9a-f]{32})-(?=(?:(?:frame|candidate)-)?\d+\b)',
                      '', value)
    if isinstance(value, list):
        return [normalize_ids(v) for v in value]
    if isinstance(value, dict):
        return {normalize_ids(k): normalize_ids(v) for k, v in value.items()}
    return value


def call_case(args, output, contract, meta, last_index, candidate=None, pose_ms=None):
    cid = meta['case_id']
    destination = output / f'{cid}.result.json'
    require(not (output / f'{cid}.input.json').exists(), 'no silent repeat of interrupted call')
    begin = time.perf_counter()
    images, frames = extract_prefix(args.dataset, meta, last_index)
    duration = (last_index + 1) / meta['fps']
    payload = request_payload(contract['model']['name'], images, frames, duration,
                              contract['options'], schema_in_prompt=True,
                              evaluation_version=contract.get('evaluation_version', 'v1'))
    if 'wire_schema' in contract:
        # Explicit, hash-bound transport compatibility; prompt/client validation
        # retain the complete semantic schema, including explanation length.
        payload['format'] = contract['wire_schema']
    if contract['thinking'] == 'disabled':
        payload['think'] = False
    # Do not silently unload/reload between cases; report the first case's cold-load separately.
    payload['keep_alive'] = '10m'
    save(output / f'{cid}.input.json', dict(
        case_id=cid, media_sha256=meta['sha256'], frames=frames, duration_s=duration,
        available_through_frame=last_index, request_sha256=digest(payload),
        contract_sha256=digest(contract), user_prompt=payload['messages'][1]['content'],
        candidate=candidate, candidate_sent_to_model=False,
        pose_compute_ms_until_request=pose_ms))
    start = time.perf_counter()
    request_started = time.monotonic()
    try:
        raw = api(args.endpoint, '/api/chat', payload, timeout=contract.get('timeout_s', 300))
    except (OSError, ValueError) as error:
        record = dict(case_id=cid, status='request_failed', valid=False,
                      error_type=type(error).__name__, http_status=getattr(error, 'code', None),
                      contract_sha256=digest(contract), request_s=time.perf_counter() - start,
                      request_started_monotonic_s=request_started,
                      request_finished_monotonic_s=time.monotonic())
        if isinstance(error, HTTPError):
            # Ollama error text is retained only in the private result, never the request body.
            record['server_error'] = error.read(4096).decode('utf-8', errors='replace')
        save(destination, record)
        if contract.get('evaluation_version') == 'v2' and not isinstance(error, HTTPError):
            return  # A failed case stays in the denominator; never retry it silently.
        raise RuntimeError(f'{cid}: request failed, evidence saved; no retry') from None
    seconds = time.perf_counter() - start
    response_received = time.monotonic()
    save(output / f'{cid}.response.json', raw)
    result = (assess_response(raw, round(duration, 3), evaluation_version='v2')
              if contract.get('evaluation_version') == 'v2'
              else assess_response(raw, round(duration, 3)))
    result.update(case_id=cid, status='responded', contract_sha256=digest(contract),
                  response_sha256=sha(output / f'{cid}.response.json'), request_s=seconds,
                  wall_s=time.perf_counter() - begin,
                  trigger_s=last_index / meta['fps'] if candidate else None,
                  telemetry={k: raw.get(k) for k in (
                      'total_duration', 'load_duration', 'prompt_eval_duration',
                      'eval_duration', 'prompt_eval_count', 'eval_count')},
                  thinking_returned=bool(raw.get('message', {}).get('thinking')),
                  request_started_monotonic_s=request_started,
                  response_received_monotonic_s=response_received,
                  validation_finished_monotonic_s=time.monotonic())
    save(destination, result)
    print('RESULT', contract['model']['name'], contract['mode'], cid,
          f'valid={result["valid"]} seconds={seconds:.2f}', flush=True)


def mode_contract(args, model, details, mode):
    import cv2
    version = getattr(args, 'evaluation_version', 'v1')
    check_evaluation_labels(args.frozen, version)
    timeout = getattr(args, 'timeout', None)
    if version == 'v2':
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 300,
                'v2 requires explicit --timeout (0..300 seconds) before inference')
    dependencies = [Path(__file__), SCRIPTS / 'stream_pose_candidates.py',
                    SCRIPTS / 'replay_vlm_frames.py', SCRIPTS / 'experimental_pose_gap.py',
                    SCRIPTS / 'experimental_leg_change.py',
                    SCRIPTS / 'experimental_request_dedup.py', PROTOCOL,
                    REPO / 'malbut_agent_server/malbut_agent_server/vlm_eval_schema.py']
    pose_files = ('pose.py', 'pose_tracker.py', 'fall_candidate.py', 'config.py')
    dependencies += [SOURCE / n for n in pose_files]
    if version == 'v2':
        from fall_evaluation_v2 import CRITERIA, CRITERIA_AMENDMENT, CRITERIA_VERSION
        dependencies += [SCRIPTS / 'fall_evaluation_v2.py', CRITERIA, CRITERIA_AMENDMENT]
    contract = dict(
        model=model, mode=mode, ollama_version=api(args.endpoint, '/api/version')['version'],
        capabilities=details.get('capabilities'),
        thinking='disabled' if 'thinking' in details.get('capabilities', []) else 'unsupported',
        model_host='MacBook Pro M5 Pro 64GiB, Ollama via SSH loopback; not Jetson',
        media_sha256=sha(args.frozen / 'media.json'),
        freeze_sha256=sha(args.frozen / 'freeze.json'),
        reference_sha256=sha(args.reference / 'completed.json'),
        source_sha256={str(p): sha(p) for p in dependencies},
        pose_model_sha256=sha(args.pose_model), pose_python=str(args.pose_python),
        options=dict(temperature=0, seed=0, num_predict=1200, num_ctx=16384),
        frame_count=12, jpeg_quality=90, cv2_version=cv2.__version__, resize='none',
        prompt_sha256=digest(dict(system=SYSTEM_PROMPT, user=USER_TEMPLATE,
                                  schema=PREDICTION_JSON_SCHEMA, schema_in_prompt=True)),
        timeout_s=300, retries=0, context='RGB_only_no_labels_no_yolo_summary_no_depth_no_audio',
        causal='only frames through emission; no post-roll or future frames' if mode == 'gated'
               else 'whole clip known in advance',
        timing='offline source timestamps and fresh CPU Pose time; VLM wall time includes SSH; '
               'not continuous real-time ROS capture or co-located hardware timing')
    if version == 'v2':
        contract.update(evaluation_version=version, criteria_sha256=sha(CRITERIA),
                        criteria_version=CRITERIA_VERSION,
                        criteria_amendment_sha256=sha(CRITERIA_AMENDMENT),
                        timeout_s=timeout, multiple_candidates='stop_before_extra_call',
                        prompt_sha256=digest(dict(**prompt_spec(version), schema_in_prompt=True,
                                                  schema_prefix=SCHEMA_PROMPT_PREFIX)))
    return contract


def execute_mode(args, root, model, details, mode):
    out = root / mode
    contract = mode_contract(args, model, details, mode)
    media = json.loads((args.frozen / 'media.json').read_text())['cases']
    metas = {m['case_id']: m for m in media}
    if out.exists():
        old = json.loads((out / 'run.json').read_text())
        require(old['contract'] == contract, 'source/config changed; start a distinct run')
        if (out / 'completed.json').exists():
            verify_completed(out)
            return
        require(not list(out.glob('*.input.json')) and not (out / 'pose-frames.jsonl').exists(),
                'partial run requires inspection; do not silently repeat calls')
    else:
        out.mkdir(mode=0o700)
        save(out / 'run.json', dict(contract=contract, contract_sha256=digest(contract)))
    if mode == 'full':
        for meta in media:
            call_case(args, out, contract, meta, meta['frames'] - 1)
    else:
        reference = [json.loads(line) for line in
                     (args.reference / 'frames.jsonl').read_text().splitlines()]
        command = [str(args.pose_python), str(SCRIPTS / 'stream_pose_candidates.py'),
                   '--dataset', str(args.dataset), '--frozen', str(args.frozen),
                   '--reference', str(args.reference), '--model', str(args.pose_model)]
        with (out / 'pose-stderr.txt').open('x') as errors, \
                (out / 'pose-frames.jsonl').open('x', buffering=1) as stream:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
            seen, called, counts = set(), set(), Counter()
            n = 0
            try:
                for line in process.stdout:
                    row = json.loads(line)
                    require(n < len(reference), 'unexpected extra Pose frame')
                    prior = reference[n]
                    require((row['case_id'], row['frame_index']) ==
                            (prior['case_id'], prior['frame_index']), 'Pose frame mismatch')
                    # Fresh inference must reproduce the frozen baseline before invoking VLM.
                    require(normalize_ids(row['observations']) ==
                            normalize_ids(prior['observations']), 'fresh Pose observations changed')
                    candidates = row['fall_analysis']['candidates']
                    require(normalize_ids(candidates) ==
                            normalize_ids(prior['fall_analysis']['candidates']),
                            'fresh candidate differs from frozen baseline')
                    stream.write(json.dumps(row, allow_nan=False) + '\n')
                    stream.flush()
                    cid = row['case_id']
                    seen.add(cid)
                    counts[cid] += row['pipeline_ms']
                    n += 1
                    if candidates:
                        require(len(candidates) == 1 and cid not in called,
                                'multiple requests: clip protocol needs incident scoring')
                        called.add(cid)
                        call_case(args, out, contract, metas[cid], row['frame_index'],
                                  candidate=candidates[0], pose_ms=counts[cid])
                require(process.wait() == 0 and n == len(reference), 'Pose worker failed')
                require(seen == set(metas), 'incomplete Pose coverage')
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
            for cid in sorted(seen - called):
                save(out / f'{cid}.result.json', dict(
                    case_id=cid, status='not_triggered', valid=False, prediction=None,
                    contract_sha256=digest(contract), pose_compute_ms=counts[cid]))
    require(local_model(args.endpoint, model['name']) == model, 'model changed mid-run')
    for path, expected in contract['source_sha256'].items():
        require(sha(Path(path)) == expected, 'source changed during model run')
    require(len(list(out.glob('SYN*.result.json'))) == len(media), 'case missing')
    files = {p.name: sha(p) for p in out.iterdir() if p.is_file()}
    save(out / 'completed.json', dict(files=files, cases=len(media)))


def verify_completed(out):
    marker = json.loads((out / 'completed.json').read_text())
    for name, expected in marker['files'].items():
        require(Path(name).name == name and sha(out / name) == expected, 'run evidence changed')


def metrics(rows, labels, evaluation_version='v1', mode=None):
    require(evaluation_version in ('v1', 'v2'), 'unknown evaluation version')
    if evaluation_version == 'v2':
        from fall_evaluation_v2 import score
        return score(rows, labels, mode)
    truth = dict(observed_fall='confirmed_fall', found_down='found_down',
                 normal_activity='normal_activity')
    resolved = []
    for row in rows:
        label = labels[row['case_id']]['label']
        answer = ((row.get('prediction') or {}).get('fall') or {}).get('assessment')
        expected = truth.get(label)
        skipped = row['status'] == 'not_triggered'
        correct = bool(expected and row.get('valid') and answer == expected)
        resolved.append(dict(row, label=label, assessment=answer,
                             raw_correct=bool(expected and answer == expected),
                             correct=correct, eligible=expected is not None,
                             pipeline_correct=correct or (skipped and label == 'normal_activity')))
    scored = [r for r in resolved if r['eligible']]
    invoked = [r for r in scored if r['status'] != 'not_triggered']
    seconds = [r['request_s'] for r in rows if r.get('request_s') is not None]
    return dict(
        rows=resolved, cases=len(rows), label_counts=dict(Counter(r['label'] for r in resolved)),
        eligible=len(scored), invoked_eligible=len(invoked),
        invocations=sum(r['status'] != 'not_triggered' for r in rows),
        valid=sum(bool(r.get('valid')) for r in rows),
        valid_correct=sum(r['correct'] for r in scored),
        raw_correct=sum(r['raw_correct'] for r in scored),
        invoked_correct=sum(r['correct'] for r in invoked),
        pipeline_correct=sum(r['pipeline_correct'] for r in scored),
        latency_s=dict(count=len(seconds), median=statistics.median(seconds) if seconds else None,
                       p95=sorted(seconds)[math.ceil(.95 * len(seconds))-1] if seconds else None),
        groups={label: dict(
            count=sum(r['label'] == label for r in resolved),
            correct=sum(r['label'] == label and r['correct'] for r in resolved),
            not_triggered=sum(r['label'] == label and r['status'] == 'not_triggered'
                              for r in resolved),
            accepted=dict(Counter(r['assessment'] for r in resolved
                                  if r['label'] == label and r.get('valid'))))
                for label in sorted({r['label'] for r in resolved})})


def report(args):
    verify_freeze(args.frozen)
    labels = {r['case_id']: r for r in json.loads(
        (args.frozen / 'evaluation_labels.json').read_text())['classifications']['cases']}
    results = []
    for directory in sorted(args.output.iterdir()):
        if not directory.is_dir():
            continue
        result = dict(model=directory.name, modes={})
        for mode in ('full', 'gated'):
            out = directory / mode
            if not (out / 'completed.json').exists():
                result['modes'][mode] = dict(status='incomplete_or_not_started',
                                             saved_cases=len(list(out.glob('SYN*.result.json'))),
                                             evaluation_version=(json.loads((out / 'run.json').read_text())
                                                 ['contract'].get('evaluation_version', 'v1')
                                                 if (out / 'run.json').exists()
                                                 else getattr(args, 'evaluation_version', 'v1')))
                continue
            verify_completed(out)
            contract = json.loads((out / 'run.json').read_text())['contract']
            require(contract['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
            result['model'] = contract['model']['name']
            rows = [json.loads(p.read_text()) for p in sorted(out.glob('SYN*.result.json'))]
            require({r['case_id'] for r in rows} == set(labels), 'missing or unknown case')
            result['modes'][mode] = dict(status='completed', **metrics(
                rows, labels, contract.get('evaluation_version', 'v1'), mode))
        results.append(result)
    destination = args.output / ('report-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    scoring = dict(results=results, scoring_sha256=sha(Path(__file__)))
    save(destination.with_suffix('.json'), scoring)
    lines = ['# Ollama 낙상 영상 비교', '',
             f'평가 목록 {len(labels)}개. 아래 v1 표는 당시의 명확한 유형만 채점한 과거 방식이다.',
             '호출·출력 오류도 분모에 포함한다. 미호출은 VLM의 정상 판정이 아니다.', '',
             '| 모델 | 방식 | 실제 호출 | 유효한 정답 | 호출된 영상 정답 | 전체 경로 정답 | 시간 중앙값 |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for result in results:
        for mode, data in result['modes'].items():
            if data.get('evaluation_version') == 'v2':
                continue
            if data['status'] != 'completed':
                lines.append(f'| {result["model"]} | {mode} | 미완료 '
                             f'({data["saved_cases"]}건 저장) | - | - | - | - |')
                continue
            seconds = data['latency_s']['median']
            seconds = f'{seconds:.2f}s' if seconds is not None else '-'
            lines.append(f'| {result["model"]} | {mode} | {data["invocations"]} | '
                         f'{data["valid_correct"]}/{data["eligible"]} | '
                         f'{data["invoked_correct"]}/{data["invoked_eligible"]} | '
                         f'{data["pipeline_correct"]}/{data["eligible"]} | {seconds} |')
    lines += ['', 'full: 전체 영상의 균등 RGB 12장. gated: 새 YOLO-Pose 후보 발생 시점까지의 RGB.',
              'gated의 전체 경로 정답은 정상 영상의 미호출을 정상으로 처리한 시스템 지표다.',
              'gated에서는 후보 없는 낙상을 분모에서 제외하지 않는다.',
              '두 입력의 관측 길이가 다르므로 차이를 모델 성능 향상으로 단정하지 않는다.',
              '단순 오프라인 재생이며 실물 동시 실행·실시간 전체 지연 검증은 아니다.', '']
    v2_measurements = [(r['model'], mode, data) for r in results for mode, data in r['modes'].items()
                       if data.get('evaluation_version') == 'v2' and data['status'] == 'completed']
    if any(data.get('evaluation_version') == 'v2' for r in results for data in r['modes'].values()):
        from fall_evaluation_v2 import report_lines
        if not any(data.get('status') == 'completed' and data.get('evaluation_version', 'v1') == 'v1'
                   for r in results for data in r['modes'].values()):
            lines = ['# Ollama 낙상 영상 비교', '', f'평가 목록: {len(labels)}개. 과거 결과는 변환하지 않았다.', '']
        lines += report_lines(v2_measurements)
        for r in results:
            for mode, data in r['modes'].items():
                if data['status'] != 'completed':
                    lines.append(f'- {r["model"]} {mode}: 미완료 ({data["saved_cases"]}건 저장), 전체 점수 없음')
    with destination.with_suffix('.md').open('x') as stream:
        stream.write('\n'.join(lines))
    print('REPORT', destination.with_suffix('.md'), flush=True)


def execute(args):
    verify_freeze(args.frozen)
    version = getattr(args, 'evaluation_version', 'v1')
    check_evaluation_labels(args.frozen, version)
    if version == 'v2':
        timeout = getattr(args, 'timeout', None)
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 300,
                'v2 requires explicit --timeout (0..300 seconds)')
    catalog = json.loads(args.catalog.read_text())
    args.output.mkdir(mode=0o700, exist_ok=True)
    selection = [m for m in catalog['models'] if m['status'] == 'planned'
                 and (not args.models or m['name'] in args.models)]
    require(bool(selection), 'no planned models selected')
    for model in selection:
        name = model['name']
        root = args.output / name.replace(':', '--')
        root.mkdir(mode=0o700, exist_ok=True)
        if (root / 'failed.json').exists():
            print('SKIP_PREVIOUS_FAILURE', name, flush=True)
            continue
        try:
            installed = {m['name'] for m in api(args.endpoint, '/api/tags')['models']}
            if name not in installed:
                require(args.pull, 'weights missing; --pull required')
                require(args.host_ssh, 'model host required to check free disk before download')
                disk = subprocess.check_output(
                    ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                     args.host_ssh, 'df -Pk /System/Volumes/Data'], text=True, timeout=15)
                available = int(disk.splitlines()[-1].split()[3]) * 1024
                require(available - model['size_bytes'] >= 100 * 1024**3,
                        'download would leave less than 100GiB free; no existing model deleted')
                pull(args.endpoint, name)
            meta = local_model(args.endpoint, name)
            require(meta['digest'] == model['manifest_sha256'], 'model changed after registry pin')
            details = api(args.endpoint, '/api/show', dict(model=name))
            info = details.get('model_info', {})
            context = [v for k, v in info.items() if k.endswith('.context_length')]
            require(not context or max(context) >= 16384, 'model context below comparison protocol')
            # Each mode is a different set of actual calls. No retrospective subset scoring.
            for mode in ('gated', 'full'):
                execute_mode(args, root, meta, details, mode)
                report(args)
        except (OSError, ValueError, RuntimeError) as error:
            if not (root / 'failed.json').exists():
                save(root / 'failed.json', dict(model=name, error_type=type(error).__name__,
                                                reason=str(error)[:600]))
            print('MODEL_STOPPED', name, type(error).__name__, str(error)[:160], flush=True)
        finally:
            # This suite owns the inference calls, not the Ollama service or unrelated models.
            try:
                api(args.endpoint, '/api/generate', dict(model=name, keep_alive=0), timeout=30)
            except (OSError, ValueError):
                pass
    report(args)
    print('QUEUE_FINISHED (check per-model failures and exclusions)', flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inventory', 'run', 'report'))
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--frozen', type=Path)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--pose-model', type=Path)
    parser.add_argument('--pose-python', type=Path)
    parser.add_argument('--models', nargs='+')
    parser.add_argument('--host-ssh', help='SSH model host, used only for free-disk checks')
    parser.add_argument('--pull', action='store_true')
    parser.add_argument('--evaluation-version', choices=('v1', 'v2'), default='v2')
    parser.add_argument('--timeout', type=float, help='explicit per-call timeout for v2; no retries')
    args = parser.parse_args()
    {'inventory': inventory, 'run': execute, 'report': report}[args.command](args)


if __name__ == '__main__':
    main()
