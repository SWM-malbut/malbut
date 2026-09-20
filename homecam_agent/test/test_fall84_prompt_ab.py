"""Paired experiment input integrity and scoring, no network/model calls."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import replay_fall84_prompt_ab as ab
from replay_vlm_frames import request_payload, digest


def fixture_payload(monkeypatch):
    images = ['fake-jpeg-1', 'fake-jpeg-2']
    frames = [dict(frame_index=0, timestamp_s=0., jpeg_sha256='a'),
              dict(frame_index=4, timestamp_s=1., jpeg_sha256='b')]
    contract = dict(model=dict(name='gemma4:12b'), options=dict(seed=0, temperature=0),
                    thinking='disabled')
    payload = request_payload('gemma4:12b', images, frames, 1.25, contract['options'],
                              schema_in_prompt=True, evaluation_version='v2')
    payload.update(think=False, keep_alive='10m')
    original = dict(frames=frames, available_through_frame=4, media_sha256='c',
                    duration_s=1.25, contract_sha256=digest(contract), request_sha256=digest(payload))
    monkeypatch.setattr(ab.suite, 'extract_prefix', lambda *args: (images, frames))
    return payload, original, contract


def test_only_system_message_changes_and_no_gt_is_accepted(monkeypatch):
    before, original, contract = fixture_payload(monkeypatch)
    after = ab.rebuild(Path('.'), dict(sha256='c'), original, contract)
    expected = copy.deepcopy(before)
    expected['messages'][0]['content'] = ab.PROMPT
    assert after == expected
    assert after['messages'][1] == before['messages'][1]
    assert after['format'] == before['format']
    assert ab.PROMPT.startswith(ab.SYSTEM_PROMPT)
    assert 'SYN' not in ab.PROMPT and 'V034' not in ab.PROMPT


@pytest.mark.parametrize('field', ['frames', 'media_sha256', 'contract_sha256', 'request_sha256'])
def test_corrupt_baseline_cannot_be_silently_replayed(monkeypatch, field):
    _, original, contract = fixture_payload(monkeypatch)
    original[field] = [] if field == 'frames' else 'corrupted'
    with pytest.raises(ValueError):
        ab.rebuild(Path('.'), dict(sha256='c'), original, contract)


def row(cid, label=None, status='responded'):
    return dict(case_id=cid, status=status, valid=status == 'responded',
                prediction=(dict(outcome='classified', label=label, explanation_ko='test')
                            if status == 'responded' else None))


def test_comparison_does_not_turn_skips_into_normal_or_drop_failures():
    labels = {'A':dict(label='suspected_fall'), 'B':dict(label='observed_fall'),
              'C':dict(label='normal_activity')}
    before = [row('A','normal_activity'), row('B',status='not_triggered'),
              row('C','normal_activity')]
    after = [row('A','suspected_fall'), row('B',status='not_triggered'),
             row('C',status='request_failed')]
    comparison = ab.comparison(before, after, labels, 'gated')
    assert comparison['after']['classification']['denominator'] == 2
    assert comparison['after']['checking']['unresolved']['numerator'] == 1
    assert comparison['after']['checking']['caught']['numerator'] == 1
    assert {r['case_id'] for r in comparison['transitions']} == {'A','C'}


def test_missing_result_cannot_receive_full_accuracy():
    with pytest.raises(ValueError):
        ab.comparison([row('A','normal_activity')], [],
                      {'A':dict(label='normal_activity')}, 'full')


def fake_run(tmp_path, monkeypatch):
    frozen, baseline = tmp_path/'frozen', tmp_path/'A'
    frozen.mkdir()
    (frozen/'freeze.json').write_text('{}')
    metas = [dict(case_id=f'SYN{i:03d}', sha256='a'*64) for i in range(1,85)]
    (frozen/'media.json').write_text(json.dumps(dict(cases=metas)))
    (frozen/'evaluation_labels.json').write_text(json.dumps(dict(classifications=dict(
        cases=[dict(case_id=m['case_id'],label='normal_activity') for m in metas]))))
    model = dict(name='gemma4:12b',digest='test')
    contract = dict(model=model,evaluation_version='v2',timeout_s=1,
                    freeze_sha256=ab.sha(frozen/'freeze.json'))
    for mode in ('full','gated'):
        out=baseline/mode
        out.mkdir(parents=True)
        (out/'run.json').write_text(json.dumps(dict(contract=contract)))
        for i,m in enumerate(metas):
            cid=m['case_id']
            called=mode=='full' or i<39
            (out/f'{cid}.result.json').write_text(json.dumps(row(
                cid,'normal_activity',status='responded' if called else 'not_triggered')))
            if called:
                (out/f'{cid}.input.json').write_text(json.dumps(dict(
                    available_through_frame=4,request_sha256='original',frames=[],
                    user_prompt='RGB only',duration_s=1.)))
    monkeypatch.setattr(ab,'verify_freeze',lambda *a:None)
    monkeypatch.setattr(ab.suite,'verify_completed',lambda *a:None)
    monkeypatch.setattr(ab.suite,'local_model',lambda *a:model)
    monkeypatch.setattr(ab,'rebuild',lambda *a:dict(messages=[dict(role='system',content=ab.PROMPT)]))
    return SimpleNamespace(frozen=frozen,baseline=baseline,output=tmp_path/'B',
                           execute=True,endpoint='http://127.0.0.1:12345')


def response():
    return dict(done=True,done_reason='stop',message=dict(content=json.dumps(
        dict(outcome='classified',label='normal_activity',explanation_ko='test'))))


def test_full_execution_preserves_gate_and_all_results(tmp_path,monkeypatch):
    args=fake_run(tmp_path,monkeypatch)
    calls=[]
    def invoke(*a,**kw):
        calls.append(a)
        return response()
    monkeypatch.setattr(ab,'api',invoke)
    ab.run(args)
    assert len(calls)==123
    assert (args.output/'completed.json').exists()
    for mode in ('full','gated'):
        assert len(list((args.output/mode).glob('*.result.json')))==84
    skipped=json.loads((args.output/'gated/SYN084.result.json').read_text())
    assert skipped['status']=='not_triggered' and skipped['prediction'] is None
    with pytest.raises(ValueError,match='output exists'):
        ab.run(args)
    assert len(calls)==123


def test_systemic_failure_saves_prior_calls_and_stops_without_retry(tmp_path,monkeypatch):
    args=fake_run(tmp_path,monkeypatch)
    calls=[]
    def invoke(*a,**kw):
        calls.append(a)
        if len(calls)==2:
            raise HTTPError('http://127.0.0.1',400,'bad request',{},None)
        return response()
    monkeypatch.setattr(ab,'api',invoke)
    with pytest.raises(RuntimeError,match='systemic API failure'):
        ab.run(args)
    assert len(calls)==2
    assert (args.output/'full/SYN001.response.json').exists()
    failed=json.loads((args.output/'full/SYN002.result.json').read_text())
    assert failed['status']=='request_failed'
    assert (args.output/'stopped.json').exists()
    assert not (args.output/'completed.json').exists()
    assert not (args.output/'full/comparison.json').exists()
