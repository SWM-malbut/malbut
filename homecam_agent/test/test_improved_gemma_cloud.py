"""Frozen 53-request replay: no filtering, future frames, GT input or duplicate credit."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from replay_improved_gemma_cloud import summarize, validate_calls  # noqa: E402
from run_ollama_fall_suite import prefix_indices  # noqa: E402


def source(tmp_path):
    metas = {'C': dict(source_path='media/C.mp4', frames=61, fps=12, sha256='rgb')}
    requests = [dict(decision_frame_index=f, dispatch_time_s=f/12) for f in (10, 53)]
    cases = [dict(case_id='C', requests=requests)]
    calls = [dict(call_id=f'C-request-{n:02d}', case_id='C',
                  mode='gated_primary' if n == 1 else 'gated_additional_request',
                  media_path=str(tmp_path/'media/C.mp4'), media_sha256='rgb',
                  available_through_frame=r['decision_frame_index'],
                  frame_indices=prefix_indices(r['decision_frame_index']),
                  dispatch_time_s=r['dispatch_time_s']) for n, r in enumerate(requests, 1)]
    return dict(calls=calls), cases, metas


def test_actual_duplicate_kept_without_deduplication(tmp_path):
    plan, cases, metas = source(tmp_path)
    before = copy.deepcopy(plan)
    assert len(validate_calls(plan, cases, metas, tmp_path)) == 2
    assert plan == before


@pytest.mark.parametrize('change', ['omit', 'future', 'extra_label', 'wrong_rgb', 'wrong_time'])
def test_modified_call_list_rejected(tmp_path, change):
    plan, cases, metas = source(tmp_path)
    if change == 'omit':
        plan['calls'].pop()
    elif change == 'future':
        plan['calls'][0]['frame_indices'].append(60)
    elif change == 'extra_label':
        plan['calls'][0]['label'] = 'observed_fall'
    elif change == 'wrong_rgb':
        plan['calls'][0]['media_sha256'] = 'changed'
    else:
        plan['calls'][0]['dispatch_time_s'] = 0
    with pytest.raises(ValueError, match='actual requests'):
        validate_calls(plan, cases, metas, tmp_path)


def test_future_frame_even_in_original_request_rejected(tmp_path):
    plan, cases, metas = source(tmp_path)
    cases[0]['requests'][0]['dispatch_time_s'] = 0
    with pytest.raises(ValueError, match='future frame'):
        validate_calls(plan, cases, metas, tmp_path)


def prediction(cid, label, mode='gated_primary'):
    return dict(case_id=cid, status='responded', valid=True, call_mode=mode,
                prediction=dict(outcome='classified', label=label, explanation_ko='보이는 근거'))


def test_no_call_is_not_normal_and_extra_call_not_extra_accuracy_credit():
    labels = {'A': dict(label='observed_fall'), 'B': dict(label='suspected_fall'),
              'C': dict(label='normal_activity')}
    rows = [prediction('A', 'observed_fall'),
            prediction('A', 'normal_activity', 'gated_additional_request')]
    result = summarize(rows, labels)
    assert result['actual_requests'] == 2
    assert result['primary']['classification']['denominator'] == 1
    assert result['primary']['classification']['numerator'] == 1
    assert result['primary']['cases'] == 3
    assert result['primary']['checking']['caught']['denominator'] == 2
    assert result['primary']['failure_counts']['not_called'] == 2
    assert result['primary']['whole_path_three_class_accuracy'] is None
    assert len(result['additional_requests']) == 1


def test_failed_primary_not_replaced_with_successful_extra():
    labels = {'A': dict(label='observed_fall')}
    result = summarize([
        dict(case_id='A', status='request_failed', valid=False, call_mode='gated_primary'),
        prediction('A', 'observed_fall', 'gated_additional_request')], labels)
    assert result['primary']['classification']['numerator'] == 0
    assert result['primary']['classification']['denominator'] == 1


def test_duplicate_primary_rejected():
    row = prediction('A', 'observed_fall')
    with pytest.raises(ValueError, match='duplicate primary'):
        summarize([row, row], {'A': dict(label='observed_fall')})
