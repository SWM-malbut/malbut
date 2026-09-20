#!/usr/bin/env python3
"""Free-only, VLM-only replay of the new runtime input/prompt on frozen fall84.

Uses the authenticated Mac Ollama through a loopback SSH tunnel. No YOLO gate,
Agent answers, audio, depth, label hints, production push or robot interaction.
Results are checkpointed per call, and an interrupted in-flight call is never
silently repeated. Full-clip request time is NOT fall detection latency.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

import fall_evaluation_v2 as scoring
from replay_fall_baseline import REPO, sha, verify_freeze
from replay_vlm_frames import api, digest, endpoint_url, save
from run_free_cloud_fall_suite import free_account

sys.path.insert(0, str(REPO / 'malbut_agent_server'))
from malbut_agent_server.adapters.outbound import ollama_cloud_fall as adapter  # noqa: E402
from malbut_agent_server.domain.fall_monitoring import (  # noqa: E402
    CloudFallRequest, FrameWindow, RgbFrame, VideoAssessment,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError  # noqa: E402


MODEL = 'gemma4:31b-cloud'
MODEL_API = 'gemma4:31b'
FRAME_COUNT = 12
WINDOW_S = 10.0
TIMEOUT_S = 20.0


def require(condition, message):
    if not condition:
        raise ValueError(message)


def letterbox(frame):
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    scale = min(640 / width, 400 / height)
    resized = cv2.resize(frame, (round(width * scale), round(height * scale)),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    canvas = np.zeros((400, 640, 3), dtype=np.uint8)
    y, x = (400 - resized.shape[0]) // 2, (640 - resized.shape[1]) // 2
    canvas[y:y+resized.shape[0], x:x+resized.shape[1]] = resized
    return canvas


def extract_request(dataset, meta, frame_count=FRAME_COUNT):
    """Only neutral media metadata, never annotations/YOLO/timing GT, is accepted."""
    import cv2

    require(type(frame_count) is int and frame_count in (6, 12), 'frames must be 6 or 12')
    path = (dataset / meta['source_path']).resolve()
    require(path.is_relative_to(dataset.resolve()), 'media escapes dataset')
    require(sha(path) == meta['sha256'], 'source changed')
    total, fps = meta['frames'], meta['fps']
    require(type(total) is int and total > 1 and math.isfinite(fps) and fps > 0,
            'invalid video metadata')
    end = (total - 1) / fps
    start = max(0, end - WINDOW_S)
    available = [i for i in range(total) if i / fps >= start]
    count = min(frame_count, len(available))
    require(count >= 2, 'insufficient frames')
    indices = [available[round(i * (len(available) - 1) / (count - 1))] for i in range(count)]
    cap, frames, evidence = cv2.VideoCapture(str(path)), [], []
    try:
        require(cap.isOpened() and abs(cap.get(cv2.CAP_PROP_FPS) - fps) < .001,
                'video could not be opened or fps changed')
        require(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == total, 'frame count changed')
        for index in range(total):
            ok, raw = cap.read()
            require(ok and raw.shape[:2] == (meta['height'], meta['width']), 'decode mismatch')
            if index not in indices:
                continue
            ok, encoded = cv2.imencode('.jpg', letterbox(raw), [cv2.IMWRITE_JPEG_QUALITY, 90])
            require(ok, 'JPEG failed')
            jpeg = bytes(encoded)
            # Synthetic device monotonic time. No padding/repeated video frames.
            frames.append(RgbFrame(100 + index / fps, jpeg))
            evidence.append(dict(frame_index=index, source_time_s=index/fps,
                                 jpeg_sha256=hashlib.sha256(jpeg).hexdigest()))
    finally:
        cap.release()
    require(len(frames) == count, 'missing samples')
    request = CloudFallRequest(
        'not-sent', 'crosscheck', 'not-sent', 'not-sent', None, None, 0,
        FrameWindow(tuple(frames), 100+end-WINDOW_S, 100+end, end < WINDOW_S), None)
    body = json.loads(adapter.build_payload(request, model=MODEL_API))
    body['model'] = MODEL  # Same cloud model through the authenticated daemon.
    return body, dict(frames=evidence, source_dimensions=[meta['width'], meta['height']],
                      input_dimensions=[640, 400], available_span_s=end-start,
                      requested_window_s=WINDOW_S, history_incomplete=end < WINDOW_S)


def assess(body):
    try:
        reply = adapter.parse_reply(body)
    except CloudFallProviderError as error:
        return dict(valid=False, prediction=None, schema_errors=[error.code], semantic_errors=[])
    unobservable = reply.assessment is VideoAssessment.UNOBSERVABLE
    prediction = dict(outcome='unobservable' if unobservable else 'classified',
                      label=None if unobservable else reply.assessment.value,
                      explanation_ko=reply.explanation)
    errors = scoring.validate_prediction(prediction)
    if errors:
        return dict(valid=False, prediction=None, schema_errors=errors, semantic_errors=[])
    return dict(valid=True, prediction=prediction, schema_errors=[], semantic_errors=[])


async def invoke(endpoint, payload):
    import aiohttp

    # No redirects, environment proxies, retry or local model selection.
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S, connect=5)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                                     auto_decompress=False) as session:
        async with session.post(endpoint_url(endpoint) + '/api/chat',
                                data=json.dumps(payload, ensure_ascii=False).encode(),
                                headers={'Content-Type': 'application/json',
                                         'Accept-Encoding': 'identity'},
                                allow_redirects=False) as response:
            if response.status != 200:
                return response.status, None
            chunks, count = [], 0
            async for chunk in response.content.iter_chunked(4096):
                count += len(chunk)
                require(count <= adapter.MAX_RESPONSE_BYTES, 'oversized response')
                chunks.append(chunk)
            return 200, b''.join(chunks)


def verify_baseline(root, dataset, media):
    """Verify preserved 12-frame results and reconstruct every original request."""
    completed = json.loads((root / 'completed.json').read_text())
    old_run = json.loads((root / 'run.json').read_text())
    spec = old_run['contract']
    require(completed['cases'] == 84 and spec['frames'] == 12, 'wrong baseline')
    require(digest(spec) == old_run['contract_sha256'] == completed['contract_sha256'],
            'baseline contract changed')
    for name, expected in completed['files'].items():
        path = (root / name).resolve()
        require(path.parent == root.resolve() and sha(path) == expected,
                'baseline artifact changed')
    for source, expected in spec['sources'].items():
        require(sha(root / 'code-snapshot' / Path(source).name) == expected,
                'baseline code snapshot changed')
        if Path(source).resolve() != Path(__file__).resolve():
            require(sha(Path(source)) == expected, 'non-sampling baseline code changed')
    for meta in media:
        cid = meta['case_id']
        original = json.loads((root / f'{cid}.input.json').read_text())
        payload, _ = extract_request(dataset, meta, 12)
        require(digest(payload) == original['request_sha256'], 'baseline request changed')
        result = json.loads((root / f'{cid}.result.json').read_text())
        require(result['case_id'] == cid and result['contract_sha256'] == digest(spec),
                'baseline case changed')
        if result['status'] == 'responded':
            body = (root / f'{cid}.response.json').read_bytes()
            require(hashlib.sha256(body).hexdigest() == result['response_sha256'],
                    'baseline response changed')
            require(all(result[k] == v for k, v in assess(body).items()),
                    'baseline parsing changed')
    return spec


def contract(args, model_details):
    source_paths = [Path(__file__), Path(adapter.__file__),
                    REPO / 'malbut_agent_server/malbut_agent_server/domain/fall_monitoring.py',
                    REPO / 'malbut_agent_server/malbut_agent_server/ports/cloud_fall.py',
                    Path(scoring.__file__), Path(__file__).with_name('replay_vlm_frames.py'),
                    Path(__file__).with_name('run_free_cloud_fall_suite.py')]
    import cv2
    import PIL
    return dict(version='runtime-cloud-frames-v2', mode='full', cases=84,
                model=MODEL, direct_api_model=MODEL_API,
                cloud_metadata_sha256=digest(model_details),
                freeze_sha256=sha(args.frozen / 'freeze.json'),
                label_sha256=sha(args.frozen / 'evaluation_labels.json'),
                criteria_version=scoring.CRITERIA_VERSION,
                frames=args.frames, window_s=WINDOW_S, timeout_s=TIMEOUT_S,
                baseline=(dict(path=str(args.baseline.resolve()),
                               completed_sha256=sha(args.baseline / 'completed.json'))
                          if args.baseline else None),
                image_policy='640x400_letterbox_keep_aspect_no_crop_jpeg90_runtime_scrub',
                short_clips='actual_available_frames_only; history_incomplete=true',
                purpose='crosscheck_scene_level_not_target_specific_incident',
                options={'temperature': 0, 'num_predict': 512}, thinking=False,
                retries=0, audio=False, yolo_gate=False, sensors=False, agent_answers=False,
                timing='offline request wall time including SSH relay; NOT detection latency',
                paid_calls_allowed=False, free_plan_check='before_every_call',
                prior_billing_confirmation='user: no paid credits/automatic top-up',
                runtime={'cv2': cv2.__version__, 'Pillow': PIL.__version__},
                sources={str(path): sha(path) for path in source_paths})


async def run(args):
    endpoint_url(args.endpoint)
    verify_freeze(args.frozen)
    media = json.loads((args.frozen / 'media.json').read_text())['cases']
    labels = {r['case_id']: r for r in json.loads(
        (args.frozen / 'evaluation_labels.json').read_text())['classifications']['cases']}
    require(len(media) == 84 and {r['case_id'] for r in media} == set(labels), 'wrong coverage')
    require({label: sum(r['label'] == label for r in labels.values()) for label in scoring.LABELS}
            == {'observed_fall': 25, 'suspected_fall': 25, 'normal_activity': 34}, 'labels changed')
    require(args.frames != 6 or args.baseline is not None, 'six-frame comparison needs baseline')
    baseline_spec = verify_baseline(args.baseline, args.frozen, media) if args.baseline else None
    # Validate every input locally before account/auth checks or paid inference.
    prepared = {}
    for meta in media:
        payload, sampling = extract_request(args.frozen, meta, args.frames)
        prepared[meta['case_id']] = (digest(payload), sampling)
    if not args.execute:
        print(f'VALID cases=84 frames={args.frames} timeout=20; no provider calls', flush=True)
        return
    free_account(args)
    details = api(args.endpoint, '/api/show', {'model': MODEL}, timeout=10)
    require('vision' in details.get('capabilities', []), 'Cloud model lacks vision')
    spec = contract(args, details)
    if baseline_spec is not None:
        excluded = {'frames', 'version', 'sources', 'baseline'}
        require({k: v for k, v in spec.items() if k not in excluded}
                == {k: v for k, v in baseline_spec.items() if k not in excluded},
                'comparison changed more than frame sampling')
    stamp = digest(spec)
    if args.resume:
        prior = json.loads((args.output / 'run.json').read_text())
        require(prior['contract'] == spec, 'resume contract changed')
    else:
        args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
        save(args.output / 'run.json', dict(created_utc=datetime.now(timezone.utc).isoformat(),
                                           contract=spec, contract_sha256=stamp))
        snapshot = args.output / 'code-snapshot'
        snapshot.mkdir(mode=0o700)
        for source in spec['sources']:
            shutil.copy2(source, snapshot / Path(source).name)
    print(f'PREFLIGHT_OK full=84 frames={args.frames} cloud_free_only timeout=20', flush=True)
    rows = []
    for meta in media:
        cid = meta['case_id']
        result_path, input_path = args.output / f'{cid}.result.json', args.output / f'{cid}.input.json'
        if result_path.exists():
            require(args.resume, 'existing case; use explicit resume')
            row = json.loads(result_path.read_text())
            require(row['contract_sha256'] == stamp, 'result contract changed')
            previous_input = json.loads(input_path.read_text())
            require(previous_input['request_sha256'] == prepared[cid][0], 'stored input changed')
            raw_path = args.output / f'{cid}.response.json'
            if row['status'] == 'responded':
                require(sha(raw_path) == row['response_sha256'], 'stored response changed')
                require(all(row[k] == v for k, v in assess(raw_path.read_bytes()).items()),
                        'stored assessment changed')
            rows.append(row)
            continue
        require(not input_path.exists(), 'interrupted in-flight request; inspect before any retry')
        free_account(args)  # Result may contain account PII; helper retains only plan.
        started = time.perf_counter()
        payload, sampling = extract_request(args.frozen, meta, args.frames)
        require(digest(payload) == prepared[cid][0], 'input changed since preflight')
        save(input_path, dict(case_id=cid, contract_sha256=stamp, request_sha256=digest(payload),
                              media_sha256=meta['sha256'], **sampling))
        row = dict(case_id=cid, contract_sha256=stamp, valid=False, prediction=None)
        call_started = time.perf_counter()
        status = None
        try:
            status, body = await invoke(args.endpoint, payload)
            if status != 200:
                row.update(status='request_failed', error_type=f'http_{status}')
            else:
                # Store raw envelope byte-for-byte before parsing/classification.
                raw_path = args.output / f'{cid}.response.json'
                fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                row.update(status='responded', response_sha256=sha(raw_path), **assess(body))
        except asyncio.TimeoutError:
            row.update(status='timeout', error_type='TimeoutError')
        except Exception as error:
            row.update(status='request_failed', error_type=type(error).__name__)
        row.update(request_s=time.perf_counter()-call_started,
                   prepare_and_request_s=time.perf_counter()-started)
        save(result_path, row)
        rows.append(row)
        print('RESULT', len(rows), '/84', cid, row['status'],
              (row.get('prediction') or {}).get('label'), round(row['request_s'], 3), flush=True)
        if status in {401, 402, 403, 429}:
            print('STOP_AUTH_PAYMENT_QUOTA completed=' + str(len(rows)), flush=True)
            return
    require(all(sha(Path(path)) == value for path, value in spec['sources'].items()),
            'source changed while running')
    verify_freeze(args.frozen)
    summary = scoring.score(rows, labels, 'full')
    save(args.output / 'summary.json', summary)
    save(args.output / 'completed.json', dict(cases=84, contract_sha256=stamp,
        files={p.name: sha(p) for p in args.output.iterdir() if p.is_file()}))
    print('COMPLETED', json.dumps(dict(classification=summary['classification'],
        checking=summary['checking'], confusion=summary['confusion'], latency_s=summary['latency_s']),
        ensure_ascii=False), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--frames', type=int, choices=(6, 12), default=12)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--no-paid-balance-confirmed', action='store_true')
    parser.add_argument('--resume', action='store_true')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
