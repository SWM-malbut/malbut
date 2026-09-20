#!/usr/bin/env python3
"""Offline, local-only Ollama comparison using uniformly sampled RGB frames.

No labels, YOLO output, detector crops, audio, depth, robot or cloud calls.
This is not native video inference and does not implement a missing-event trigger.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from replay_fall_baseline import REPO, sha, verify_freeze
from review_fall_annotations import require

sys.path.insert(0, str(REPO / 'malbut_agent_server'))
from malbut_agent_server.vlm_eval_prompt import (  # noqa: E402
    PREDICTION_JSON_SCHEMA, PROMPT_SHA256, SYSTEM_PROMPT,
)
from malbut_agent_server.vlm_eval_schema import validate_prediction  # noqa: E402


USER_TEMPLATE = (
    '동일한 짧은 RGB 영상에서 시간순으로 고르게 뽑은 {count}장의 이미지입니다.\n'
    '클립 길이: {duration:.3f}초. 이미지 순서별 시각(초): {timestamps}.\n'
    '이미지 사이의 장면은 제공되지 않았습니다. 보이지 않는 동작은 추측하지 마세요.\n'
    '낮은 위치의 이동형 카메라입니다. 사람의 자세 변화와 카메라 움직임을 구분하세요.\n'
    'YOLO 결과, 실제 depth, 음성, 대상자 답변은 제공되지 않았습니다.\n'
    '첫 이미지부터 마지막 이미지까지 함께 보고 지정된 JSON 객체 하나로 응답하세요.'
)
PROTOCOL = REPO / 'homecam_agent/evaluations/synthetic_fall_v1/VLM_FRAMES_PROTOCOL.md'
SCHEMA_PROMPT_PREFIX = '\n출력 형식(JSON Schema):\n'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save(path, value):
    """Exclusive, private and immediately durable per-case output."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canonical(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def frame_indices(total, count=12):
    require(type(total) is int and total > 1, 'invalid source frame count')
    require(type(count) is int and 2 <= count <= 32, 'frame count must be 2..32')
    count = min(total, count)
    return [round(i * (total - 1) / (count - 1)) for i in range(count)]


def endpoint_url(value):
    parsed = urlsplit(value)
    require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', '::1'),
            'only literal loopback HTTP is allowed; use an SSH tunnel')
    require(not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and parsed.path in ('', '/'), 'invalid endpoint')
    require(parsed.port is not None and 1 <= parsed.port <= 65535, 'explicit port required')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirects are forbidden')


def api(endpoint, path, payload=None, timeout=10):
    url = endpoint_url(endpoint) + path
    body = None if payload is None else canonical(payload)
    request = Request(url, data=body, headers={'Content-Type': 'application/json'})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    require(len(data) <= 2 * 1024 * 1024, 'response too large')
    value = json.loads(data)
    require(isinstance(value, dict), 'response must be an object')
    return value


def local_model(endpoint, name):
    require('cloud' not in name.lower(), 'cloud models are forbidden')
    models = api(endpoint, '/api/tags').get('models', [])
    matching = [m for m in models if m.get('name') == name]
    require(len(matching) == 1, 'model must already be installed; no automatic download')
    model = matching[0]
    require(model.get('size', 0) > 0 and model.get('details', {}).get('format') == 'gguf',
            'expected installed local GGUF weights')
    require(re.fullmatch('[0-9a-f]{64}', model.get('digest', '')), 'missing model digest')
    details = api(endpoint, '/api/show', {'model': name})
    require('vision' in details.get('capabilities', []), 'model must support vision')
    require(not details.get('remote_model') and not details.get('remote_host'),
            'remote-backed model forbidden')
    return {'name': name, 'digest': model['digest'], 'details': model['details'],
            'show_sha256': digest(details)}


def extract_frames(dataset, meta, count):
    import cv2

    path = (dataset / meta['source_path']).resolve()
    require(path.is_relative_to(dataset.resolve()), 'media path escapes dataset')
    require(sha(path) == meta['sha256'], 'source media changed')
    indices = frame_indices(meta['frames'], count)
    fps = meta['fps']
    require(math.isfinite(fps) and fps > 0, 'invalid fps')
    images, frames = [], []
    cap = cv2.VideoCapture(str(path))
    try:
        require(cap.isOpened(), 'video could not be opened')
        require(abs(cap.get(cv2.CAP_PROP_FPS) - fps) < .001, 'fps mismatch')
        require(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == meta['frames'], 'frame count mismatch')
        for index in range(meta['frames']):
            ok, frame = cap.read()
            require(ok, f'could not decode frame {index}')
            if index not in indices:
                continue
            require(frame.shape[:2] == (meta['height'], meta['width']), 'resolution mismatch')
            ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            require(ok, 'JPEG encode failed')
            data = jpeg.tobytes()
            images.append(base64.b64encode(data).decode('ascii'))
            frames.append({'frame_index': index, 'timestamp_s': index / fps,
                           'jpeg_sha256': hashlib.sha256(data).hexdigest()})
    finally:
        cap.release()
    require(len(frames) == len(indices), 'not all frames were extracted')
    return images, frames


def prompt_spec(evaluation_version='v1'):
    require(evaluation_version in ('v1', 'v2'), 'unknown evaluation version')
    if evaluation_version == 'v2':
        import fall_evaluation_v2 as v2
        return dict(system=v2.SYSTEM_PROMPT, user=USER_TEMPLATE, schema=v2.PREDICTION_JSON_SCHEMA)
    return dict(system=SYSTEM_PROMPT, user=USER_TEMPLATE, schema=PREDICTION_JSON_SCHEMA)


def check_evaluation_labels(frozen, evaluation_version):
    """Fail before any API call if v2 is pointed at historical four-class labels."""
    require(evaluation_version in ('v1', 'v2'), 'unknown evaluation version')
    if evaluation_version == 'v2':
        from fall_evaluation_v2 import LABELS, judgment_metadata
        bundle = json.loads((frozen / 'evaluation_labels.json').read_text())
        cases = bundle['classifications']['cases']
        require(bool(cases) and all(c['label'] in LABELS for c in cases),
                'v2 requires reviewed three-class labels; prepare a new freeze')
        require(bundle['classifications'].get('schema_version') ==
                'malbut.synthetic-video-human-review.v2', 'v2 label version required')
        require(bundle['classifications'].get('full_dataset_finalized') is True,
                'v2 label review/freeze is not finalized; do not invoke a model yet')
        media = json.loads((frozen / 'media.json').read_text())['cases']
        ids = [c['case_id'] for c in cases]
        media_ids = [c['case_id'] for c in media]
        require(len(ids) == len(set(ids)) and len(media_ids) == len(set(media_ids))
                and set(ids) == set(media_ids), 'v2 label/media case mismatch')
        for case in cases:
            judgment_metadata(case)


def request_payload(model, images, frames, duration, options, schema_in_prompt=False,
                    evaluation_version='v1'):
    """Intentionally cannot accept case IDs, filenames, labels or sensor summaries."""
    text = USER_TEMPLATE.format(count=len(frames), duration=duration,
                                timestamps=', '.join(f'{f["timestamp_s"]:.3f}' for f in frames))
    spec = prompt_spec(evaluation_version)
    if schema_in_prompt:
        text += SCHEMA_PROMPT_PREFIX + canonical(spec['schema']).decode('utf-8')
    return dict(model=model, stream=False, format=spec['schema'], options=options,
                messages=[{'role': 'system', 'content': spec['system']},
                          {'role': 'user', 'content': text, 'images': images}])


def assess_response(response, duration, evaluation_version='v1'):
    require(evaluation_version in ('v1', 'v2'), 'unknown evaluation version')
    if evaluation_version == 'v2':
        from fall_evaluation_v2 import assess_response as assess_v2
        return assess_v2(response, duration)
    errors, semantic, prediction = [], [], None
    if response.get('done') is not True or response.get('done_reason') != 'stop':
        errors.append('incomplete_response')
    message = response.get('message')
    text = message.get('content') if isinstance(message, dict) else None
    try:
        value = json.loads(text) if isinstance(text, str) else None
        if not isinstance(value, dict):
            errors.append('prediction_not_object')
        else:
            parsed, schema_errors, semantic = validate_prediction(value, duration)
            errors.extend(schema_errors)
            if parsed is not None:
                prediction = value
    except (ValueError, TypeError):
        errors.append('invalid_json')
    return dict(prediction=prediction, schema_errors=errors, semantic_errors=semantic,
                valid=not errors and not semantic and prediction is not None)


def run(args):
    import cv2

    verify_freeze(args.frozen)
    version = getattr(args, 'evaluation_version', 'v1')
    check_evaluation_labels(args.frozen, version)
    media = json.loads((args.frozen / 'media.json').read_text())['cases']
    require(len({m['case_id'] for m in media}) == len(media), 'duplicate case IDs')
    require(all(re.fullmatch(r'SYN[0-9]{3}', m['case_id']) for m in media), 'invalid case ID')
    if version == 'v1' and args.timeout is None:
        args.timeout = 180  # Explicitly selected historical protocol only.
    require(args.limit > 0 and type(args.timeout) in (int, float)
            and math.isfinite(args.timeout) and 0 < args.timeout <= 300,
            'invalid run bound; v2 requires explicit --timeout')
    model = local_model(args.endpoint, args.model)
    contract = dict(
        model=model, ollama_version=api(args.endpoint, '/api/version').get('version'),
        model_host=args.host_description, media_sha256=sha(args.frozen / 'media.json'),
        freeze_sha256=sha(args.frozen / 'freeze.json'), protocol_sha256=sha(PROTOCOL),
        script_sha256=sha(Path(__file__)),
        validator_sha256=sha(REPO / 'malbut_agent_server/malbut_agent_server/vlm_eval_schema.py'),
        shared_prompt_sha256=PROMPT_SHA256, user_template=USER_TEMPLATE,
        prompt_sha256=digest(dict(
            system=SYSTEM_PROMPT, user=USER_TEMPLATE, schema=PREDICTION_JSON_SCHEMA,
            schema_in_prompt=args.schema_in_prompt,
            schema_prefix=SCHEMA_PROMPT_PREFIX if args.schema_in_prompt else None)),
        schema_in_prompt=args.schema_in_prompt,
        frame_count=args.frames, jpeg_quality=90, resize='none', cv2_version=cv2.__version__,
        options=dict(temperature=0, seed=0, num_predict=1200, num_ctx=16384),
        timeout_s=args.timeout, retries=0, inference='ordered_rgb_images_not_native_video',
        context='RGB_only_no_labels_no_yolo_no_depth_no_audio_no_answers',
        case_order=[m['case_id'] for m in media],
    )
    if version == 'v2':
        import fall_evaluation_v2 as v2
        contract.update(evaluation_version=version, criteria_sha256=sha(v2.CRITERIA),
                        criteria_version=v2.CRITERIA_VERSION,
                        criteria_amendment_sha256=sha(v2.CRITERIA_AMENDMENT),
                        shared_prompt_sha256=None,
                        validator_sha256=sha(Path(v2.__file__)),
                        prompt_sha256=digest(dict(
                            **prompt_spec(version), schema_in_prompt=args.schema_in_prompt,
                            schema_prefix=SCHEMA_PROMPT_PREFIX if args.schema_in_prompt else None)))
    if args.resume:
        existing = json.loads((args.output / 'run.json').read_text())
        require(existing['contract'] == contract, 'resume contract changed')
    else:
        args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
        save(args.output / 'run.json', dict(
            contract=contract, contract_sha256=digest(contract),
            created_utc=datetime.now(timezone.utc).isoformat()))
    cv2.setNumThreads(1)
    attempted = 0
    for meta in media:
        case_id = meta['case_id']
        result_path = args.output / f'{case_id}.result.json'
        if result_path.exists():
            previous = json.loads(result_path.read_text())
            require(previous['contract_sha256'] == digest(contract), 'result contract mismatch')
            continue
        require(not (args.output / f'{case_id}.input.json').exists(),
                f'{case_id}: interrupted attempt; inspect saved evidence, do not retry silently')
        begin = time.perf_counter()
        images, frames = extract_frames(args.dataset, meta, args.frames)
        duration = meta['frames'] / meta['fps']
        payload = request_payload(args.model, images, frames, duration, contract['options'],
                                  schema_in_prompt=args.schema_in_prompt, evaluation_version=version)
        save(args.output / f'{case_id}.input.json', dict(
            case_id=case_id, media_sha256=meta['sha256'], frames=frames, duration_s=duration,
            contract_sha256=digest(contract), request_sha256=digest(payload),
            user_prompt=payload['messages'][1]['content'],
        ))
        start = time.perf_counter()
        try:
            response = api(args.endpoint, '/api/chat', payload, timeout=args.timeout)
        except (OSError, ValueError) as error:
            save(result_path, dict(case_id=case_id, contract_sha256=digest(contract),
                                   status='request_failed', error_type=type(error).__name__,
                                   wall_s=time.perf_counter() - begin, valid=False))
            message = f'{case_id}: request failed, saved and stopped without retry'
            raise RuntimeError(message) from None
        latency = time.perf_counter() - start
        # Save raw response BEFORE validation. Invalid output is data, not a lost run.
        raw_path = args.output / f'{case_id}.response.json'
        save(raw_path, response)
        result = (assess_response(response, duration, evaluation_version=version)
                  if version == 'v2' else assess_response(response, duration))
        result.update(case_id=case_id, contract_sha256=digest(contract), status='responded',
                      response_sha256=sha(raw_path), request_s=latency,
                      wall_s=time.perf_counter() - begin,
                      timing_includes_network=True,
                      telemetry={k: response.get(k) for k in (
                          'total_duration', 'load_duration', 'prompt_eval_duration',
                          'eval_duration', 'prompt_eval_count', 'eval_count')})
        save(result_path, result)
        attempted += 1
        print(f'{case_id} response_saved valid={result["valid"]} seconds={latency:.2f}', flush=True)
        if attempted >= args.limit:
            break
    require(local_model(args.endpoint, args.model) == model, 'model changed during run')
    results = sorted(args.output.glob('SYN*.result.json'))
    if len(results) == len(media) and not (args.output / 'completed.json').exists():
        save(args.output / 'completed.json', dict(
            run_sha256=sha(args.output / 'run.json'), cases=len(results),
            files={p.name: sha(p) for p in sorted(args.output.glob('SYN*.json'))}))
    print(f'SAVED {len(results)}/{len(media)} output={args.output}', flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    parser.add_argument('--model', required=True)
    parser.add_argument('--host-description', required=True)
    parser.add_argument('--frames', type=int, default=12)
    parser.add_argument('--timeout', type=float, help='required for v2; historical v1 default is 180s')
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--evaluation-version', choices=('v1', 'v2'), default='v2',
                        help='new runs use v2; v1 is historical protocol only')
    parser.add_argument('--schema-in-prompt', action='store_true',
                        help='separate contract probe: also describe the output schema in text')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
