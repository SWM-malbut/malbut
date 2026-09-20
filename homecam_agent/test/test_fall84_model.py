"""Model-only paired requests; no network calls in tests."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replay_fall84_model as pair
from replay_vlm_frames import digest, request_payload, save


def fixture_payload(monkeypatch):
    images = ['frame0', 'frame4']
    frames = [dict(frame_index=i, timestamp_s=i / 4, jpeg_sha256=str(i)) for i in (0, 4)]
    contract = dict(model=dict(name='gemma4:12b'), options=dict(temperature=0, seed=0),
                    thinking='disabled', wire_schema={'preserve': 'schema'})
    payload = request_payload('gemma4:12b', images, frames, 1.25, contract['options'],
                              schema_in_prompt=True, evaluation_version='v2')
    payload.update(format=contract['wire_schema'], think=False, keep_alive='10m')
    original = dict(frames=frames, available_through_frame=4, duration_s=1.25,
                    media_sha256='video', contract_sha256=digest(contract), request_sha256=digest(payload))
    monkeypatch.setattr(pair.suite, 'extract_prefix', lambda *a: (images, frames))
    return payload, original, contract


@pytest.mark.parametrize('thinking', ['disabled', 'unsupported'])
def test_only_model_and_unsupported_think_change(monkeypatch, thinking):
    a, original, contract = fixture_payload(monkeypatch)
    b = pair.rebuild(Path('.'), dict(sha256='video'), original, contract, 'qwen2.5vl:7b', thinking)
    expected = copy.deepcopy(a)
    expected['model'] = 'qwen2.5vl:7b'
    if thinking == 'unsupported':
        expected.pop('think')
    assert b == expected
    assert b['messages'] == a['messages']
    assert b['format'] == a['format']


@pytest.mark.parametrize('key', ['frames', 'media_sha256', 'contract_sha256', 'request_sha256'])
def test_corrupt_source_refused(monkeypatch, key):
    _, original, contract = fixture_payload(monkeypatch)
    original[key] = [] if key == 'frames' else 'broken'
    with pytest.raises(ValueError):
        pair.rebuild(Path('.'), dict(sha256='video'), original, contract, 'qwen2.5vl:7b', 'unsupported')


def test_cannot_enable_thinking_during_comparison(monkeypatch):
    _, original, contract = fixture_payload(monkeypatch)
    with pytest.raises(ValueError, match='thinking must not be enabled'):
        pair.rebuild(Path('.'), dict(sha256='video'), original, contract, 'other', 'enabled')


def fake_run(tmp_path, monkeypatch):
    frozen, base, extra = tmp_path / 'frozen', tmp_path / 'baseline', tmp_path / 'extra'
    frozen.mkdir()
    (frozen / 'freeze.json').write_text('{}')
    metas = [dict(case_id=f'SYN{i:03d}', sha256='a') for i in range(1, 85)]
    (frozen / 'media.json').write_text(json.dumps(dict(cases=metas)))
    (frozen / 'evaluation_labels.json').write_text(json.dumps(dict(classifications=dict(
        cases=[dict(case_id=m['case_id'], label='normal_activity') for m in metas]))))
    contract = dict(model=dict(name='gemma4:12b'), evaluation_version='v2', thinking='disabled',
                    cv2_version=cv2.__version__, ollama_version='test', reference_sha256='gate',
                    freeze_sha256=pair.sha(frozen / 'freeze.json'))
    for root in (extra, base / 'gated', base / 'full'):
        root.mkdir(parents=True)
        c = dict(contract, mode='full' if root.name == 'full' else 'gated')
        (root / 'run.json').write_text(json.dumps(dict(contract=c)))
        (root / 'completed.json').write_text('{}')
    plans = {}
    prediction = dict(outcome='classified', label='normal_activity', explanation_ko='독서')
    for mode in ('gated', 'full'):
        rows = {}
        for i, meta in enumerate(metas):
            cid = meta['case_id']
            called = mode == 'full' or i < 39 or cid == 'SYN074'
            root = extra if mode == 'gated' and cid == 'SYN074' else base / mode
            rows[cid] = (root, dict(case_id=cid, status='responded' if called else 'not_triggered',
                         valid=called, prediction=prediction if called else None))
            if called:
                (root / f'{cid}.input.json').write_text(json.dumps(dict(frames=[], duration_s=1.25,
                    available_through_frame=4, request_sha256='original', candidate={'present': True})))
        plans[mode] = rows
    monkeypatch.setattr(pair, 'origins', lambda args: plans)
    monkeypatch.setattr(pair, 'verify_freeze', lambda *a: None)
    monkeypatch.setattr(pair.suite, 'verify_completed', lambda *a: None)
    monkeypatch.setattr(pair.suite, 'local_model', lambda *a: dict(name='qwen2.5vl:7b', digest='test'))
    monkeypatch.setattr(pair, 'api', lambda endpoint, path, *a: (
        dict(version='test') if path == '/api/version' else dict(capabilities=['vision'])))
    monkeypatch.setattr(pair, 'rebuild', lambda *a: dict(model='qwen2.5vl:7b'))
    return SimpleNamespace(frozen=frozen, baseline=base, extra=extra, output=tmp_path / 'out',
                           execute=True, model='qwen2.5vl:7b', endpoint='http://127.0.0.1:12345')


def save_fake_call(out, contract, meta, valid=True):
    cid = meta['case_id']
    save(out / f'{cid}.input.json', dict(frames=[], request_sha256=digest(dict(model='qwen2.5vl:7b'))))
    save(out / f'{cid}.result.json', dict(case_id=cid, status='responded', valid=valid,
        contract_sha256=digest(contract), prediction=dict(outcome='classified', label='normal_activity',
                                                        explanation_ko='독서') if valid else None))


def test_124_calls_and_no_silent_repeats(tmp_path, monkeypatch):
    args = fake_run(tmp_path, monkeypatch)
    calls = []
    def invoke(a, out, contract, meta, last, **kw):
        calls.append((out.name, meta['case_id']))
        save_fake_call(out, contract, meta)
    monkeypatch.setattr(pair.suite, 'call_case', invoke)
    pair.run(args)
    assert len(calls) == 124
    assert len([x for x in calls if x[0] == 'gated']) == 40
    assert ('gated', 'SYN074') in calls
    skipped = pair.read(args.output / 'gated/SYN084.result.json')
    assert skipped['status'] == 'not_triggered' and skipped['prediction'] is None
    assert pair.read(args.output / 'completed.json')['actual_calls'] == 124
    with pytest.raises(ValueError, match='output exists'):
        pair.run(args)
    assert len(calls) == 124


def test_stop_preserves_results_no_retry(tmp_path, monkeypatch):
    args = fake_run(tmp_path, monkeypatch)
    calls = []
    def invoke(a, out, contract, meta, last, **kw):
        calls.append(meta['case_id'])
        save_fake_call(out, contract, meta, valid=len(calls) != 2)
        if len(calls) == 2:
            raise RuntimeError('request failed; no retry')
    monkeypatch.setattr(pair.suite, 'call_case', invoke)
    with pytest.raises(RuntimeError, match='request failed'):
        pair.run(args)
    assert len(calls) == 2
    assert (args.output / 'gated/SYN001.result.json').exists()
    assert (args.output / 'gated/SYN002.result.json').exists()
    assert (args.output / 'stopped.json').exists()
    assert not (args.output / 'completed.json').exists()


def test_preflight_failure_makes_no_calls(tmp_path, monkeypatch):
    args = fake_run(tmp_path, monkeypatch)
    def fail(*a):
        raise ValueError('original A request changed')
    monkeypatch.setattr(pair, 'rebuild', fail)
    monkeypatch.setattr(pair.suite, 'call_case', lambda *a: pytest.fail('must not call'))
    with pytest.raises(ValueError, match='original A request changed'):
        pair.run(args)
    assert not args.output.exists()
