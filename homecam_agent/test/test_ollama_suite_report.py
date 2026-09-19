"""Detailed report validity and readable CSV safety; no live model calls."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from summarize_ollama_suite import (  # noqa: E402
    csv_cell, distribution, inspect_final_json, supplementary_counts, wilson,
)


def test_small_sample_interval_and_empty_latency():
    assert wilson(0, 0) is None
    low, high = wilson(10, 14)
    assert .45 < low < .46 and .88 < high < .89
    assert distribution([])['median'] is None
    assert distribution([1, 2, 3])['p95'] == 3


@pytest.mark.parametrize('text', [
    '=HYPERLINK("x")', '+formula', '-formula', '@SUM(1)', '\t=bad', '  =bad',
])
def test_csv_formula_prefix_is_neutralized_but_raw_json_unchanged(text):
    assert csv_cell(text) == "'" + text


def test_numbers_and_korean_description_are_preserved():
    assert csv_cell(-1.5) == -1.5
    assert csv_cell('사람이 바닥에 누워 있다.') == '사람이 바닥에 누워 있다.'


def test_single_final_fence_is_audited_without_changing_raw_response():
    prediction = dict(subjects=dict(person=1, pet=0, other=0), events=[],
                      fall=dict(assessment='normal_activity', confidence=.1, recovery='unknown'),
                      posture_end='standing', risk='none', risk_confidence=.9,
                      camera_motion_observed='none', explanation_ko='서 있는 사람입니다.',
                      evidence_ko=['서 있는 자세'], uncertainty_flags=[])
    raw = dict(done=True, done_reason='stop', message=dict(
        content='```json\n' + json.dumps(prediction) + '\n```'))
    previous = copy.deepcopy(raw)
    audit = inspect_final_json(raw, 5.25)
    assert audit['wrapper'] == 'single_markdown_json_fence'
    assert audit['assessment'] == 'normal_activity' and audit['semantic_valid']
    assert raw == previous


@pytest.mark.parametrize('text', [
    '설명\n```json\n{}\n```', '```json\n{}\n```\n```json\n{}\n```',
    '{"fall":{},"fall":{"assessment":"confirmed_fall"}}', '[{}]', '',
])
def test_does_not_guess_json_from_prose_multiple_answers_or_duplicate_keys(text):
    raw = dict(done=True, done_reason='stop', message=dict(content=text))
    audit = inspect_final_json(raw, 5.25)
    assert not audit['semantic_valid'] and audit['assessment'] is None


def test_thinking_is_not_promoted_to_final_answer():
    raw = dict(done=True, done_reason='stop', message=dict(
        content='', thinking='{"fall":{"assessment":"confirmed_fall"}}'))
    assert inspect_final_json(raw, 5.25)['assessment'] is None


def test_supplement_keeps_invalid_and_failed_calls_in_denominator():
    rows = [dict(case_id='SYN001', status='responded', final_json_audit=dict(
        assessment='confirmed_fall', semantic_valid=False)),
        dict(case_id='SYN002', status='request_failed'),
        dict(case_id='SYN003', status='not_triggered')]
    labels = {r['case_id']: dict(label='observed_fall') for r in rows}
    summary = supplementary_counts(rows, labels)
    assert summary['eligible'] == 3 and summary['invoked_eligible'] == 2
    assert summary['invoked_label_only_correct'] == 1
    assert summary['invoked_semantic_correct'] == 0
