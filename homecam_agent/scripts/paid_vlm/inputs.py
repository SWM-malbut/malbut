"""Extract once, hash, and reuse exactly the same RGB bytes for every model."""

import base64
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'malbut_agent_server'))
from malbut_agent_server.adapters.outbound import ollama_cloud_fall as adapter  # noqa: E402
from malbut_agent_server.domain.fall_monitoring import (  # noqa: E402
    CloudFallRequest, FrameWindow, RgbFrame,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError  # noqa: E402

from .providers import strict_json
from .prompts import PROMPT_ADDITIONS, native_box_prompt
from .native_boxes import PROFILE as NATIVE_PROFILE, WIRE_FORMAT, NativeBoxError, normalize_native_reply

LABELS = ('observed_fall', 'suspected_fall', 'normal_activity')
VERSION = 'paid-vlm-standalone-v1'


def system_prompt(profile):
    require(profile in PROMPT_ADDITIONS, 'unknown prompt')
    if profile == NATIVE_PROFILE:
        return native_box_prompt(adapter.CROSSCHECK_SYSTEM_PROMPT)
    return adapter.CROSSCHECK_SYSTEM_PROMPT + PROMPT_ADDITIONS[profile]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    """Exclusive publication, private files, flush before starting the next call."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def private_dir(path):
    path = path.resolve()
    require(not path.is_relative_to(REPO), 'keep evaluation inputs/results outside Git')
    path.mkdir(mode=0o700, parents=False, exist_ok=False)


def code_hashes():
    files = list(Path(__file__).parent.glob('*.py')) + [Path(adapter.__file__),
        REPO / 'malbut_agent_server/malbut_agent_server/domain/fall_monitoring.py',
        REPO / 'homecam_agent/scripts/evaluate_paid_vlm.py']
    return {str(p.relative_to(REPO)): sha(p) for p in sorted(files)}


def environment_versions():
    result = {'python': sys.version.split()[0]}
    for name in ('Pillow', 'opencv-python', 'opencv-python-headless', 'numpy', 'aiohttp'):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def extract(dataset, meta):
    """Accept only neutral media metadata. No labels, reviewed boxes or onset hints."""
    import cv2
    import numpy as np

    path = (dataset / meta['source_path']).resolve()
    require(path.is_relative_to(dataset.resolve()) and sha(path) == meta['sha256'], 'changed media')
    total, fps = meta['frames'], meta['fps']
    require(type(total) is int and total >= 2 and math.isfinite(fps) and fps > 0, 'invalid video')
    end = (total - 1) / fps
    start = max(0, end - 5.0)
    first = max(0, math.ceil(start * fps))
    while first / fps < start:
        first += 1
    available = total - first
    count = min(12, available)
    require(count >= 2, 'insufficient video frames')
    indices = [first + round(i * (available - 1) / (count - 1)) for i in range(count)]
    frames, cap = [], cv2.VideoCapture(str(path))
    try:
        require(cap.isOpened() and abs(cap.get(cv2.CAP_PROP_FPS) - fps) < .001
                and int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == total, 'video metadata changed')
        for i in range(total):
            ok, raw = cap.read()
            require(ok and raw.shape[:2] == (meta['height'], meta['width']), 'decode mismatch')
            if i not in indices:
                continue
            h, w = raw.shape[:2]
            scale = min(640 / w, 400 / h)
            resized = cv2.resize(raw, (round(w * scale), round(h * scale)),
                                 interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            canvas = np.zeros((400, 640, 3), dtype=np.uint8)
            y, x = (400 - resized.shape[0]) // 2, (640 - resized.shape[1]) // 2
            canvas[y:y+resized.shape[0], x:x+resized.shape[1]] = resized
            ok, jpeg = cv2.imencode('.jpg', canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
            require(ok, 'JPEG failed')
            frames.append(RgbFrame(100 + i/fps, bytes(jpeg)))
    finally:
        cap.release()
    request = CloudFallRequest('not-sent', 'crosscheck', 'not-sent', 'not-sent', None, None, 0,
                              FrameWindow(tuple(frames), 100+end-5, 100+end, end < 5), None)
    wire = strict_json(adapter.build_payload(request, model='gemma4:31b'))
    # Metadata scrubbing/re-encoding is done only here. Provider wrappers reuse
    # these exact base64 strings; do not scrub/resize a second time per provider.
    common = dict(system=wire['messages'][0]['content'], text=wire['messages'][1]['content'],
                  images=wire['messages'][1]['images'])
    evidence = dict(source_sha256=meta['sha256'], frame_indices=indices,
                    source_times_s=[i/fps for i in indices], window_s=5.0, max_frames=12,
                    dimensions=[640, 400], history_incomplete=end < 5,
                    jpeg_sha256=[hashlib.sha256(base64.b64decode(im)).hexdigest()
                                 for im in common['images']])
    return common, evidence


def prepare(frozen, output, case_ids=None):
    from prepare_fall_evaluation_v2 import verify
    verify(frozen)  # Includes final label/media hash bindings; never revise them.
    media = strict_json((frozen / 'media.json').read_bytes())['cases']
    labels = {r['case_id']: r['label'] for r in strict_json(
        (frozen / 'evaluation_labels.json').read_bytes())['classifications']['cases']}
    require(len(media) == len(labels) == 84, 'expected reviewed fall84 dataset')
    require([list(labels.values()).count(k) for k in LABELS] == [25, 25, 34], 'changed label counts')
    selected = list(case_ids) if case_ids else sorted(labels)
    require(selected and len(selected) == len(set(selected)) and set(selected) <= set(labels),
            'unknown or duplicate cases')
    require(all(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', k) for k in selected), 'invalid case ID')
    private_dir(output)
    metas, files = {m['case_id']: m for m in media}, {}
    for cid in selected:
        common, evidence = extract(frozen, metas[cid])
        name = cid + '.input.json'
        save(output / name, dict(common=common, evidence=evidence))
        files[name] = sha(output / name)
    manifest = dict(version=VERSION, scope='full84' if len(selected) == 84 else 'pilot',
                    mode='standalone', case_ids=selected, labels={c: labels[c] for c in selected},
                    files=files, freeze_sha256=sha(frozen / 'freeze.json'),
                    labels_sha256=sha(frozen / 'evaluation_labels.json'), sources=code_hashes(),
                    environment=environment_versions(),
                    condition=dict(window_s=5, max_frames=12, width=640, height=400,
                                   timeout_s=20, retries=0, audio=False, depth=False,
                                   prompt='runtime_crosscheck_with_findings',
                                   scoring='three_labels_strict_response_v1'))
    save(output / 'manifest.json', manifest)
    save(output / 'manifest_hash.json', {'sha256': digest(manifest)})
    return manifest


def load_bundle(directory):
    manifest = strict_json((directory / 'manifest.json').read_bytes())
    require(digest(manifest) == strict_json((directory / 'manifest_hash.json').read_bytes())['sha256'],
            'changed manifest')
    require(manifest['version'] == VERSION and manifest['mode'] == 'standalone', 'unsupported bundle')
    ids = manifest['case_ids']
    require(ids and len(ids) == len(set(ids)) and set(ids) == set(manifest['labels']), 'bad case list')
    require(set(manifest['files']) == {cid + '.input.json' for cid in ids}, 'bad input manifest')
    require(manifest['sources'] == code_hashes(), 'code changed: prepare a new experiment bundle')
    require(all(v in LABELS for v in manifest['labels'].values()), 'bad labels')
    require(all(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', cid) for cid in ids), 'bad case IDs')
    inputs = {}
    for cid in ids:
        path = directory / (cid + '.input.json')
        require(not path.is_symlink() and sha(path) == manifest['files'][path.name], 'changed input')
        record = strict_json(path.read_bytes())
        common, evidence = record['common'], record['evidence']
        require(set(common) == {'system', 'text', 'images'}, 'non-neutral request fields')
        profile = manifest.get('condition', {}).get('prompt', 'runtime_crosscheck_with_findings')
        require(common['system'] == system_prompt(profile), 'changed prompt')
        require(2 <= len(common['images']) <= 12 and evidence['window_s'] == 5.0, 'wrong sampling')
        require([hashlib.sha256(base64.b64decode(im, validate=True)).hexdigest()
                 for im in common['images']] == evidence['jpeg_sha256'], 'changed JPEGs')
        inputs[cid] = common
    return manifest, inputs


def derive_bundle(source, output, prompt='runtime_crosscheck_with_findings', case_ids=None):
    """Create a new experiment from immutable media, retaining old source hashes.

    This explicitly allows runner changes; it does not bless changed media or labels.
    Original bundles and results are never overwritten or resumed.
    """
    new_system = system_prompt(prompt)  # Reject unknown profiles before creating output.
    original = strict_json((source / 'manifest.json').read_bytes())
    require(digest(original) == strict_json((source / 'manifest_hash.json').read_bytes())['sha256'],
            'changed parent manifest')
    require(original['version'] == VERSION and original['mode'] == 'standalone', 'unsupported parent')
    selected = list(case_ids) if case_ids is not None else original['case_ids']
    require(selected and len(selected) == len(set(selected))
            and set(selected) <= set(original['case_ids']), 'invalid derived cases')
    require(all(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', c) for c in original['case_ids']), 'invalid case ID')
    records = {}
    for cid in original['case_ids']:
        name = cid + '.input.json'
        path = source / name
        require(not path.is_symlink() and sha(path) == original['files'][name], 'changed parent input')
        record = strict_json(path.read_bytes())
        common, evidence = record['common'], record['evidence']
        require(set(common) == {'system', 'text', 'images'}, 'non-neutral request fields')
        require(common['system'] == system_prompt(original['condition']['prompt']), 'changed parent prompt')
        require([hashlib.sha256(base64.b64decode(im, validate=True)).hexdigest()
                 for im in common['images']] == evidence['jpeg_sha256'], 'changed JPEGs')
        if cid in selected:
            records[cid] = dict(common=dict(common, system=new_system), evidence=evidence)
    private_dir(output)
    files = {}
    for cid in selected:
        name = cid + '.input.json'
        save(output / name, records[cid])
        files[name] = sha(output / name)
    manifest = dict(original, scope='full84' if len(selected) == 84 else 'pilot',
                    case_ids=selected, labels={c:original['labels'][c] for c in selected},
                    files=files, sources=code_hashes(), environment=environment_versions(),
                    condition=dict(original['condition'], prompt=prompt),
                    parent_manifest_sha256=digest(original), parent_sources=original['sources'])
    save(output / 'manifest.json', manifest)
    save(output / 'manifest_hash.json', {'sha256': digest(manifest)})
    return load_bundle(output)


def response_details(text, image_count):
    """Read the declared label without repairing values or relaxing scoring.

    Diagnostic codes identify obvious coordinate errors. The adapter remains
    authoritative for the full contract; this is not a second response validator.
    """
    details = dict(reported_assessment=None, response_issue_codes=[], localization_counts=None)
    content = text.strip()
    fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', content, re.DOTALL)
    if fence:
        content = fence.group(1)
    try:
        obj = strict_json(content)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        details['response_issue_codes'] = ['invalid_json']
        return details
    if not isinstance(obj, dict):
        details['response_issue_codes'] = ['not_json_object']
        return details
    value = obj.get('assessment')
    if isinstance(value, str) and value in (*LABELS, 'unobservable'):
        details['reported_assessment'] = value
    else:
        details['response_issue_codes'].append('invalid_assessment')
    findings = obj.get('findings')
    if isinstance(findings, list):
        # Descriptive counts, NOT validation or measured person-location accuracy.
        # Keep None for malformed containers; do not call missing fields "empty".
        if all(isinstance(f, dict) and isinstance(f.get('regions'), list) for f in findings):
            details['localization_counts'] = dict(
                findings=len(findings),
                findings_with_empty_regions=sum(not f['regions'] for f in findings),
                findings_with_regions=sum(bool(f['regions']) for f in findings),
                regions=sum(len(f['regions']) for f in findings))
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get('regions'), list):
                continue
            for region in finding['regions']:
                if not isinstance(region, dict):
                    continue
                index, box = region.get('frame_index'), region.get('box')
                if type(index) is not int or not 0 <= index < image_count:
                    details['response_issue_codes'].append('invalid_frame_index')
                if not isinstance(box, list) or len(box) != 4:
                    details['response_issue_codes'].append('invalid_box_shape')
                elif not all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box):
                    details['response_issue_codes'].append('invalid_box_range')
                elif not (box[0] < box[2] and box[1] < box[3]):
                    details['response_issue_codes'].append('invalid_box_extent')
    details['response_issue_codes'] = sorted(set(details['response_issue_codes']))
    return details


def assess(text, image_count, response_profile='runtime_crosscheck_with_findings'):
    """Same classification contract for all models; no prose/JSON repair."""
    require(response_profile in PROMPT_ADDITIONS, 'unknown response profile')
    if response_profile == NATIVE_PROFILE:
        try:
            normalized = normalize_native_reply(text)
        except NativeBoxError as exc:
            details = response_details(text, image_count)
            # Legacy details expects "box", not "box_2d". Do not misreport
            # its missing-key shape warning as the native failure.
            details['response_issue_codes'] = sorted(set(
                code for code in details['response_issue_codes']
                if code not in {'invalid_box_shape', 'invalid_box_range', 'invalid_box_extent'}
            ) | {str(exc)})
            return dict(outcome='invalid_response', label=None, explanation=None, manual_review=True,
                        response_wire_format=WIRE_FORMAT, normalized_response=None, **details)
        checked = assess(normalized, image_count)
        return dict(checked, response_wire_format=WIRE_FORMAT,
                    normalized_response=strict_json(normalized))
    details = response_details(text, image_count)
    try:
        content = text.strip()
        fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', content, re.DOTALL)
        if fence:
            content = fence.group(1)
        obj = strict_json(content)
        require(isinstance(obj, dict) and set(obj) == {'assessment', 'explanation', 'findings'},
                'wrong response fields')
        # Only the frame count is needed for checking finding indices.
        request = CloudFallRequest('test', 'crosscheck', 'test', 'test', None, None, 0,
                                  FrameWindow(tuple(RgbFrame(float(i), b'\xff\xd8\xff\xd9')
                                                    for i in range(image_count)), 0, 12, False), None)
        envelope = json.dumps(dict(done=True, done_reason='stop', message={
            'role': 'assistant', 'content': content})).encode()
        reply = adapter.parse_reply(envelope, request)
        require(not reply.localization_failed, 'invalid or contradictory findings')
        label = reply.assessment.value
        return dict(outcome='unobservable' if label == 'unobservable' else 'classified',
                    label=None if label == 'unobservable' else label,
                    explanation=reply.explanation, manual_review=label == 'unobservable', **details)
    except (ValueError, TypeError, UnicodeError, CloudFallProviderError, RecursionError):
        # A free-text refusal has no trustworthy machine flag: retain for human
        # review, but do not inflate the automatic refusal count.
        if not details['response_issue_codes']:
            details['response_issue_codes'] = ['response_contract_error']
        return dict(outcome='invalid_response', label=None, explanation=None, manual_review=True, **details)
