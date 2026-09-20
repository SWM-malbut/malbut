"""Facts-output experiment: strict parsing, paired RGB integrity and durable calls."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import cv2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import replay_fall84_facts as facts
from replay_vlm_frames import request_payload, digest, SCHEMA_PROMPT_PREFIX


def answer(label='normal_activity'):
    return dict(observations=dict(visible_facts_ko='바닥에 앉아 책을 읽는다.',
                                  unknowns_ko='이전 상황은 알 수 없다.'),
                assessment=dict(outcome='classified', label=label, explanation_ko='독서 중이다.'))


def response(value=None):
    return dict(done=True, done_reason='stop', message=dict(content=json.dumps(
        answer() if value is None else value, ensure_ascii=False)))


@pytest.mark.parametrize('label', ['normal_activity', 'suspected_fall', 'observed_fall'])
def test_does_not_reclassify_model(label):
    value = answer(label)
    result = facts.assess(response(value))
    assert result['valid']
    assert result['prediction'] == value['assessment']
    assert result['structured_prediction'] == value


def test_unobservable_remains_unobservable():
    value = answer()
    value['assessment'].update(outcome='unobservable', label=None)
    assert facts.assess(response(value))['prediction']['label'] is None


@pytest.mark.parametrize('mutate', [
    lambda x: x.update(extra=True),
    lambda x: x.pop('observations'),
    lambda x: x.update(observations=[]),
    lambda x: x['observations'].update(extra='bad'),
    lambda x: x['observations'].update(unknowns_ko=''),
    lambda x: x['observations'].update(unknowns_ko=' '),
    lambda x: x['observations'].update(unknowns_ko=3),
    lambda x: x['observations'].update(visible_facts_ko='x' * 1201),
    lambda x: x['assessment'].update(label='found_down'),
    lambda x: x['assessment'].update(outcome='unobservable'),
    lambda x: x['assessment'].update(explanation_ko='x' * 4001),
])
def test_invalid_structured_output_not_counted_as_valid_assessment(mutate):
    value = answer()
    mutate(value)
    result = facts.assess(response(value))
    assert not result['valid'] and result['prediction'] is None


@pytest.mark.parametrize('raw', [None, [], {}, {'done': False},
    {'done': True, 'done_reason': 'stop', 'message': []},
    {'done': True, 'done_reason': 'stop', 'message': {'content': '{"x":1,"x":2}'}},
    {'done': True, 'done_reason': 'stop', 'message': {'content': '{"x":NaN}'}},
    {'done': True, 'done_reason': 'length', 'message': {'content': '{}'}}])
def test_bad_envelope_duplicate_nonfinite_and_truncation_rejected(raw):
    assert not facts.assess(raw)['valid']


def fixture_payload(monkeypatch):
    frames = [dict(frame_index=i, timestamp_s=i / 4, jpeg_sha256=str(i)) for i in (0, 4)]
    images = ['image0', 'image4']
    contract = dict(model=dict(name='gemma4:12b'), options=dict(seed=0, temperature=0),
                    thinking='disabled', wire_schema={'same': 'baseline'})
    payload = request_payload('gemma4:12b', images, frames, 1.25, contract['options'],
                              schema_in_prompt=True, evaluation_version='v2')
    payload.update(format=contract['wire_schema'], think=False, keep_alive='10m')
    original = dict(frames=frames, available_through_frame=4, media_sha256='media',
                    contract_sha256=digest(contract), request_sha256=digest(payload), duration_s=1.25)
    monkeypatch.setattr(facts.suite, 'extract_prefix', lambda *a: (images, frames))
    return payload, original, contract


def test_rgb_times_options_preserved_only_output_contract_changes(monkeypatch):
    a, original, contract = fixture_payload(monkeypatch)
    b = facts.rebuild(Path('.'), dict(sha256='media'), original, contract)
    assert b['messages'][1]['images'] == a['messages'][1]['images']
    assert b['messages'][1]['content'].split(SCHEMA_PROMPT_PREFIX)[0] == a['messages'][1]['content'].split(SCHEMA_PROMPT_PREFIX)[0]
    for key in ('model', 'options', 'think', 'stream', 'keep_alive'):
        assert b[key] == a[key]
    assert b['format'] == facts.WIRE_SCHEMA
    assert facts.PROMPT.startswith(facts.SYSTEM_PROMPT)
    assert 'SYN074' not in facts.PROMPT and 'V034' not in facts.PROMPT
    assert 'maxLength' not in json.dumps(facts.WIRE_SCHEMA)
    assert facts.SCHEMA['properties']['observations']['properties']['visible_facts_ko']['maxLength'] == 1200


@pytest.mark.parametrize('key', ['frames', 'media_sha256', 'contract_sha256', 'request_sha256'])
def test_corrupt_a_refused(monkeypatch, key):
    _, original, contract = fixture_payload(monkeypatch)
    original[key] = [] if key == 'frames' else 'bad'
    with pytest.raises(ValueError):
        facts.rebuild(Path('.'), dict(sha256='media'), original, contract)


def fake_run(tmp_path, monkeypatch):
    frozen, baseline = tmp_path / 'frozen', tmp_path / 'baseline'
    frozen.mkdir()
    (frozen / 'freeze.json').write_text('{}')
    metas = [dict(case_id=f'SYN{i:03d}', sha256='a') for i in range(1, 85)]
    (frozen / 'media.json').write_text(json.dumps(dict(cases=metas)))
    (frozen / 'evaluation_labels.json').write_text(json.dumps(dict(classifications=dict(
        cases=[dict(case_id=m['case_id'], label='normal_activity') for m in metas]))))
    model = dict(name='gemma4:12b', digest='test')
    contract = dict(model=model, evaluation_version='v2', timeout_s=1,
                    cv2_version=cv2.__version__, ollama_version='test',
                    freeze_sha256=facts.sha(frozen / 'freeze.json'))
    plans = {}
    for mode in ('gated', 'full'):
        root = baseline / mode
        root.mkdir(parents=True)
        (root / 'run.json').write_text(json.dumps(dict(contract=contract)))
        (root / 'completed.json').write_text('{}')
        rows = {}
        for i, meta in enumerate(metas):
            cid = meta['case_id']
            called = mode == 'full' or i < 40
            rows[cid] = (root, dict(case_id=cid, status='responded' if called else 'not_triggered',
                                    prediction=answer()['assessment'] if called else None, valid=called))
            if called:
                (root / f'{cid}.input.json').write_text(json.dumps(dict(
                    available_through_frame=4, request_sha256='a', frames=[], duration_s=1.25)))
        plans[mode] = rows
    monkeypatch.setattr(facts, 'origins', lambda a: plans)
    monkeypatch.setattr(facts, 'verify_freeze', lambda a: None)
    monkeypatch.setattr(facts.suite, 'verify_completed', lambda a: None)
    monkeypatch.setattr(facts.suite, 'local_model', lambda *a: model)
    monkeypatch.setattr(facts, 'rebuild', lambda *a: dict(messages=[{}, dict(content='RGB')]))
    return SimpleNamespace(execute=True, frozen=frozen, baseline=baseline,
                           extra=tmp_path / 'extra', output=tmp_path / 'out',
                           endpoint='http://127.0.0.1:12345')


def test_all_calls_saved_without_relabeling_skips_or_retries(tmp_path, monkeypatch):
    args = fake_run(tmp_path, monkeypatch)
    calls = []
    def api(endpoint, path, *a, **kw):
        if path == '/api/version':
            return dict(version='test')
        calls.append(path)
        return response()
    monkeypatch.setattr(facts, 'api', api)
    facts.run(args)
    assert len(calls) == 124
    assert json.loads((args.output / 'completed.json').read_text())['actual_calls'] == 124
    assert len(list((args.output / 'gated').glob('*.result.json'))) == 84
    skipped = json.loads((args.output / 'gated/SYN084.result.json').read_text())
    assert skipped['status'] == 'not_triggered' and skipped['prediction'] is None
    with pytest.raises(ValueError, match='output exists'):
        facts.run(args)
    assert len(calls) == 124


def test_systemic_error_stops_and_preserves_prior_response(tmp_path, monkeypatch):
    args = fake_run(tmp_path, monkeypatch)
    calls = []
    def api(endpoint, path, *a, **kw):
        if path == '/api/version':
            return dict(version='test')
        calls.append(path)
        if len(calls) == 2:
            raise HTTPError('http://127.0.0.1', 400, 'bad request', {}, None)
        return response()
    monkeypatch.setattr(facts, 'api', api)
    with pytest.raises(RuntimeError, match='systemic API failure'):
        facts.run(args)
    assert len(calls) == 2
    assert (args.output / 'gated/SYN001.response.json').exists()
    assert (args.output / 'gated/SYN002.result.json').exists()
    assert (args.output / 'stopped.json').exists()
    assert not (args.output / 'completed.json').exists()


def test_timeout_and_invalid_output_remain_in_denominator(tmp_path, monkeypatch):
    args = fake_run(tmp_path, monkeypatch)
    calls = []
    def api(endpoint, path, *a, **kw):
        if path == '/api/version':
            return dict(version='test')
        calls.append(path)
        if len(calls) == 1:
            raise TimeoutError('test timeout')
        if len(calls) == 2:
            bad = answer()
            bad['observations']['unknowns_ko'] = ''
            return response(bad)
        return response()
    monkeypatch.setattr(facts, 'api', api)
    facts.run(args)
    summary = json.loads((args.output / 'gated/comparison.json').read_text())['after']
    assert len(calls) == 124
    assert summary['classification']['denominator'] == 40
    assert summary['classification']['numerator'] == 38
    assert summary['failure_counts']['timeout'] == 1
    assert summary['failure_counts']['invalid_response'] == 1
    assert not (args.output / 'gated/SYN001.response.json').exists()
    assert (args.output / 'gated/SYN002.response.json').exists()


def test_preflight_failure_sends_no_inference(tmp_path, monkeypatch):
    args = fake_run(tmp_path, monkeypatch)
    calls = []
    def api(endpoint, path, *a, **kw):
        calls.append(path)
        return dict(version='test')
    def broken(*a):
        raise ValueError('original A reconstruction differs')
    monkeypatch.setattr(facts, 'api', api)
    monkeypatch.setattr(facts, 'rebuild', broken)
    with pytest.raises(ValueError, match='reconstruction differs'):
        facts.run(args)
    assert '/api/chat' not in calls
    assert not args.output.exists()
