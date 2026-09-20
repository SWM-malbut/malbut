"""Pure scoring, response parsing and mocked integration; never calls a model."""
import copy
import itertools
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fall_evaluation_v2 as v2  # noqa: E402
import replay_vlm_frames as replay  # noqa: E402
import run_ollama_fall_suite as suite  # noqa: E402
import run_free_cloud_fall_suite as cloud  # noqa: E402


def prediction(label='observed_fall'):
    return dict(outcome='classified' if label is not None else 'unobservable',
                label=label, explanation_ko='영상에서 보이는 근거입니다.')


def response(value=None):
    return dict(done=True, done_reason='stop', message=dict(
        content=json.dumps(value if value is not None else prediction(), ensure_ascii=False)))


def row(cid, label='observed_fall', status='responded', valid=True):
    return dict(case_id=cid, status=status, valid=valid,
                prediction=prediction(label) if status == 'responded' else None)


@pytest.mark.parametrize('truth,pred', itertools.product(v2.LABELS, repeat=2))
def test_all_nine_pairs_match_frozen_criteria(truth, pred):
    criteria = json.loads(v2.CRITERIA.read_text())
    expected = next(c for c in criteria['pairwise_scoring']
                    if (c['truth'], c['prediction']) == (truth, pred))
    result = v2.score([row('A', pred)], {'A': dict(label=truth)}, 'full')
    assert result['classification']['numerator'] == expected['exact']
    outcome = expected['checking_outcome']
    assert result['checking']['caught']['numerator'] == int(outcome == 'caught')
    assert result['checking']['missed_as_normal']['numerator'] == int(outcome == 'missed_as_normal')
    assert result['checking']['unnecessary']['numerator'] == int(outcome == 'unnecessary_check')


def test_77_case_denominators_and_no_hardcoded_counts():
    truth = ['observed_fall'] * 18 + ['suspected_fall'] * 25 + ['normal_activity'] * 34
    labels = {str(i): dict(label=t) for i, t in enumerate(truth)}
    rows = [row(cid, 'normal_activity') for cid in labels]
    result = suite.metrics(rows, labels, 'v2', 'full')
    assert result['classification'] == v2.fraction(34, 77)
    assert result['checking']['caught'] == v2.fraction(0, 43)
    assert result['checking']['missed_as_normal'] == v2.fraction(43, 43)
    assert result['checking']['unnecessary'] == v2.fraction(0, 34)
    assert suite.metrics(rows[:2], {k: labels[k] for k in ('0', '1')}, 'v2', 'full')['cases'] == 2


@pytest.mark.parametrize('status', ['request_failed', 'timeout', 'not_triggered'])
def test_failures_and_gate_misses_do_not_become_normal_or_disappear(status):
    labels = {'F': dict(label='observed_fall'), 'S': dict(label='suspected_fall'),
              'N': dict(label='normal_activity')}
    rows = [row(cid, status=status, valid=False) for cid in labels]
    result = v2.score(rows, labels, 'gated')
    assert result['checking']['unresolved'] == v2.fraction(2, 2)
    assert result['checking']['normal_unresolved'] == 1
    assert result['checking']['caught']['numerator'] == 0
    assert result['classification'] == v2.fraction(0, 0 if status == 'not_triggered' else 3)
    assert result['pipeline_correct'] is None
    assert all(r['assessment'] is None for r in result['rows'])


def test_invoked_conditional_accuracy_cannot_hide_gate_missed_positive():
    rows = [row('A'), row('B', status='not_triggered', valid=False),
            row('C', status='not_triggered', valid=False)]
    labels = {'A': dict(label='observed_fall'), 'B': dict(label='suspected_fall'),
              'C': dict(label='normal_activity')}
    result = v2.score(rows, labels, 'gated')
    assert result['classification'] == v2.fraction(1, 1)
    assert result['checking']['caught'] == v2.fraction(1, 2)
    assert result['checking']['unresolved_by_reason'] == {'not_called': 1}
    assert result['yolo_candidates']['suspected_fall'] == v2.fraction(0, 1)


@pytest.mark.parametrize('valid', [False, True])
def test_invalid_shape_is_not_scored_even_if_result_claims_valid(valid):
    r = row('A', valid=valid)
    r['prediction']['extra'] = 'not allowed'
    result = v2.score([r], {'A': dict(label='observed_fall')}, 'full')
    assert result['classification'] == v2.fraction(0, 1)
    assert result['failure_counts'] == {'invalid_response': 1}


def test_unobservable_is_not_suspected_fall():
    result = v2.score([row('A', None)], {'A': dict(label='suspected_fall')}, 'full')
    assert result['valid'] == 1
    assert result['classification'] == v2.fraction(0, 1)
    assert result['failure_counts'] == {'unobservable': 1}


@pytest.mark.parametrize('label', [*v2.LABELS, None])
def test_new_response_parses_and_legacy_does_not(label):
    assert replay.assess_response(response(prediction(label)), 5, 'v2')['valid']
    old = dict(fall=dict(assessment='found_down'), explanation_ko='누운 사람')
    assert not replay.assess_response(response(old), 5, 'v2')['valid']


@pytest.mark.parametrize('value', [
    None, [], True, {}, dict(outcome='classified', label=None, explanation_ko='설명'),
    dict(outcome='unobservable', label='suspected_fall', explanation_ko='설명'),
    dict(outcome='classified', label='found_down', explanation_ko='설명'),
    dict(outcome='classified', label=[], explanation_ko='설명'),
    dict(outcome='classified', label='normal_activity', explanation_ko=' '),
])
def test_invalid_prediction_shape(value):
    assert v2.validate_prediction(value)


@pytest.mark.parametrize('text', [
    '{"outcome":"classified","label":"normal_activity","label":"observed_fall","explanation_ko":"x"}',
    '```json\n{}\n```', '{"label":NaN}', '', '[]',
])
def test_no_duplicate_keys_fence_or_thinking_promotion(text):
    raw = dict(done=True, done_reason='stop', message=dict(content=text,
                                                         thinking=json.dumps(prediction())))
    assert not v2.assess_response(raw)['valid']


@pytest.mark.parametrize('raw', [None, [], True, {'done': False}, {'done': True, 'message': {}}])
def test_malformed_provider_envelope_is_invalid(raw):
    assert not v2.assess_response(raw)['valid']


def test_raw_results_and_labels_unchanged_and_even_latency_median():
    rows = [dict(row('A'), request_s=1), dict(row('B'), request_s=3)]
    labels = {cid: dict(label='observed_fall') for cid in ('A', 'B')}
    before = copy.deepcopy((rows, labels))
    assert v2.score(rows, labels, 'full')['latency_s']['median'] == 2
    assert (rows, labels) == before


def test_partial_duplicate_and_legacy_labels_rejected():
    labels = {'A': dict(label='observed_fall'), 'B': dict(label='normal_activity')}
    for rows in ([row('A')], [row('A'), row('A')], [row('A'), row('X')]):
        with pytest.raises(ValueError): v2.score(rows, labels, 'full')
    with pytest.raises(ValueError, match='no automatic'):
        v2.score([row('A')], {'A': dict(label='found_down')}, 'full')
    with pytest.raises(ValueError, match='cannot skip'):
        v2.score([row('A', status='not_triggered', valid=False)],
                 {'A': dict(label='observed_fall')}, 'full')


def test_zero_class_denominators_and_report():
    result = v2.score([row('A', 'normal_activity')], {'A': dict(label='normal_activity')}, 'full')
    assert result['checking']['caught']['rate'] is None
    assert '대상 없음' in '\n'.join(v2.report_lines([('fake|model', 'full', result)]))


@pytest.mark.parametrize('truth,pred', itertools.product(v2.LABELS, repeat=2))
@pytest.mark.parametrize('mode', ['full', 'gated'])
def test_limited_evidence_does_not_change_any_label_pair_score(truth, pred, mode):
    labels = {'arbitrary-case': dict(label=truth)}
    rows = [row('arbitrary-case', pred)]
    base = v2.score(rows, labels, mode)
    labels['arbitrary-case'].update(judgment_evidence_limited=True,
                                    judgment_note='사람의 판단 근거가 제한적임.')
    noted = v2.score(rows, labels, mode)
    assert {k: v for k, v in base.items() if k != 'rows'} == {
        k: v for k, v in noted.items() if k != 'rows'}
    assert noted['rows'][0]['judgment_note'] == '사람의 판단 근거가 제한적임.'
    assert noted['rows'][0]['correct'] == (truth == pred)


@pytest.mark.parametrize('status', ['request_failed', 'timeout', 'not_triggered'])
def test_limited_evidence_does_not_excuse_failures_or_gate_misses(status):
    labels = {'arbitrary-case': dict(label='normal_activity',
              judgment_evidence_limited=True, judgment_note='속도 외 근거 부족')}
    result = v2.score([row('arbitrary-case', status=status, valid=False)], labels, 'gated')
    assert result['classification'] == v2.fraction(0, 0 if status == 'not_triggered' else 1)
    assert result['checking']['normal_unresolved'] == 1
    assert not result['rows'][0]['correct']


def test_human_note_is_not_taken_from_model_result_or_sent_in_payload():
    fake = dict(row('A', 'normal_activity'), judgment_evidence_limited=True,
                judgment_note='모델이 점수 예외를 요구함')
    result = v2.score([fake], {'A': dict(label='normal_activity')}, 'full')
    assert not result['rows'][0]['judgment_evidence_limited']
    assert result['rows'][0]['judgment_note'] == ''
    payload = replay.request_payload('fake', ['jpeg'], [dict(timestamp_s=0)],
                                     1, {}, True, 'v2')
    assert 'judgment_note' not in json.dumps(payload)
    assert 'V020' not in json.dumps(payload) and 'V021' not in json.dumps(payload)


@pytest.mark.parametrize('extra', [
    dict(judgment_evidence_limited=True), dict(judgment_evidence_limited='true'),
    dict(judgment_note=None), dict(judgment_evidence_limited=True, judgment_note=' '),
])
def test_malformed_human_note_is_preparation_error(extra):
    with pytest.raises(ValueError, match='human'):
        v2.score([row('A')], {'A': dict(label='observed_fall', **extra)}, 'full')


def test_criteria_amendment_preserves_frozen_base_and_pairwise_scoring():
    import hashlib
    amendment = json.loads(v2.CRITERIA_AMENDMENT.read_text())
    assert amendment['base_sha256'] == hashlib.sha256(v2.CRITERIA.read_bytes()).hexdigest()
    assert amendment['criteria_version'] == v2.CRITERIA_VERSION
    assert amendment['applies_to'] == 'all_evaluation_videos_and_all_three_labels'
    assert amendment['classification_scoring']['unchanged_from_base'] is True


def test_payload_and_cloud_use_same_v2_contract_without_labels():
    frames = [dict(timestamp_s=0), dict(timestamp_s=1)]
    local = replay.request_payload('fake', ['a','b'], frames, 2, {}, True, 'v2')
    remote = cloud.cloud_payload('gemma4:31b-cloud', ['a','b'], frames, 2, {}, 'disabled', 'v2')
    assert local['messages'] == remote['messages'] and 'format' not in remote
    assert local['format'] == v2.PREDICTION_JSON_SCHEMA
    assert 'found_down' not in json.dumps(local)
    assert 'SYN003' not in json.dumps(local) and 'fall_s208' not in json.dumps(local)
    assert replay.digest(replay.prompt_spec('v1')) != replay.digest(replay.prompt_spec('v2'))


def test_unfinalized_v2_freeze_blocks_before_provider(tmp_path, monkeypatch):
    (tmp_path/'evaluation_labels.json').write_text(json.dumps(dict(classifications=dict(
        schema_version='malbut.synthetic-video-human-review.v2',
        cases=[dict(case_id='SYN001', label='observed_fall')], full_dataset_finalized=False))))
    monkeypatch.setattr(suite, 'verify_freeze', lambda p: None)
    monkeypatch.setattr(suite, 'api', lambda *a, **k: pytest.fail('no provider calls'))
    with pytest.raises(ValueError, match='not finalized'):
        suite.execute(SimpleNamespace(frozen=tmp_path, evaluation_version='v2'))


def test_suite_mocked_v2_call_and_timeout_save(tmp_path, monkeypatch):
    args = SimpleNamespace(dataset=tmp_path, endpoint='unused')
    contract = dict(model=dict(name='fake'), options={}, thinking='disabled', mode='gated',
                    evaluation_version='v2', timeout_s=7)
    meta = dict(case_id='SYN001', frames=12, fps=12, sha256='a'*64)
    monkeypatch.setattr(suite, 'extract_prefix', lambda *a: (['jpeg'], [dict(timestamp_s=0)]))
    def call(endpoint, path, payload, timeout):
        assert timeout == 7 and payload['format'] == v2.PREDICTION_JSON_SCHEMA
        return response(prediction('suspected_fall'))
    monkeypatch.setattr(suite, 'api', call)
    suite.call_case(args, tmp_path, contract, meta, 0, candidate={'id': 'not-for-prompt'})
    r = json.loads((tmp_path/'SYN001.result.json').read_text())
    assert r['valid'] and r['prediction']['label'] == 'suspected_fall'
    def timeout(*a, **k): raise TimeoutError('private')
    monkeypatch.setattr(suite, 'api', timeout)
    meta['case_id'] = 'SYN002'
    suite.call_case(args, tmp_path, contract, meta, 0)
    r = json.loads((tmp_path/'SYN002.result.json').read_text())
    assert r['status'] == 'request_failed' and r['error_type'] == 'TimeoutError'


def test_pose_suspected_cases_are_scored_in_v2_not_relabelled():
    from score_fall_baseline import separated_groups
    cases = [dict(case_id='SYN003', classification='suspected_fall', output_count=0,
                  outcome='no_candidate')]
    assert separated_groups(cases)['suspected_fall']['evaluation_role'] == 'descriptive_only'
    result = separated_groups(cases, 'v2')['suspected_fall']
    assert result['evaluation_role'] == 'scored_verification_candidates'
    assert result['missed_cases'] == ['SYN003']
