"""Exercise benchmark failure handling, real gating and honest metric denominators."""

import copy
import json
import sqlite3

import pytest

from malbut_agent_server.memory_review_benchmark import (
    HELDOUT_DATASET, REFINED_ARMS, Recorder, audit_database, decisions_payload,
    load_cases, main, parse_decisions,
    review_candidates, run_trial, summarize, summarize_paired, usage_cost,
)
from malbut_agent_server.memory_contract import SOURCE_REVIEW_SCOPE_INSTRUCTIONS
from malbut_agent_server.personal_memory import fact_matches_source


def answer(name='fact_0', label='supported', supported=0.95):
    return {'type': 'choice', 'name': name, 'choice': label,
            'probabilities': [
                {'value': 'supported', 'probability': supported},
                {'value': 'contradicted', 'probability': 0.0},
                {'value': 'insufficient', 'probability': 1 - supported},
            ]}


def test_real_gate_skips_rules_empty_and_disallowed_sources():
    cases = {case['id']: case for case in load_cases()}
    for name in ('greeting', 'lexical_name', 'lexical_preference', 'no_consent',
                 'quoted_name', 'hypothetical_name', 'do_not_store', 'question',
                 'absent_value'):
        assert review_candidates(cases[name]) == []
    mixed = cases['batch_rule_plus_review']
    assert review_candidates(mixed) == [mixed['facts'][1]]
    assert len(review_candidates(cases['batch_mixed_names'])) == 2


def test_questions_are_batched_and_do_not_contain_gold_answers():
    case = next(case for case in load_cases() if case['id'] == 'batch_mixed_names')
    body = decisions_payload(case['source'], case['facts'])
    assert len(body['questions']) == 2
    assert json.loads(body['input']) == {
        'current_user_utterance': case['source'], 'candidate_facts': case['facts'],
    }
    assert 'expected' not in json.dumps(body)
    assert 'tools' not in body


def test_answer_order_is_matched_by_identity_and_refusal_cannot_approve():
    parsed = parse_decisions({'answers': [
        {'name': 'fact_1', 'type': 'refusal'}, answer(),
    ]}, 2)
    assert [row['label'] for row in parsed] == ['supported', 'refusal']
    assert parsed[1]['supported_probability'] is None


@pytest.mark.parametrize('answers', [
    [], [answer(), answer()], [answer('foreign')],
    [dict(answer(), type='predicate')], [dict(answer(), choice='invented')],
    [dict(answer(), probabilities=[])],
    [dict(answer(), probabilities=[{'value': 'supported', 'probability': 1}] * 3)],
    [answer(supported=float('nan'))], [answer(supported=1.1)],
])
def test_malformed_answers_fail_closed(answers):
    with pytest.raises((ValueError, TypeError)):
        parse_decisions({'answers': answers}, 1)


def test_failed_call_does_not_become_a_successful_rejection_or_leak_secrets():
    def transport(*args):
        raise RuntimeError('secret-do-not-serialize')

    case = next(case for case in load_cases() if case['id'] == 'name_casual')
    recorder = Recorder(transport=transport)
    row = run_trial(case, case['facts'], 'decisions', 'test-key', recorder, 5)
    summary = summarize([row])['decisions']
    assert row['predictions'] == []
    assert summary['failed_batches'] == 1
    assert summary['valid_batches'] == 0
    assert summary['counts'] == {}
    assert summary['unknown_cost_batches'] == 1
    assert 'secret-do-not-serialize' not in json.dumps(row)
    assert 'test-key' not in json.dumps(row)


def test_malformed_success_is_reported_as_error_with_usage_cost():
    recorder = Recorder(transport=lambda *args: {'answers': [], 'usage': {'input_tokens': 500}})
    case = next(case for case in load_cases() if case['id'] == 'name_casual')
    row = run_trial(case, case['facts'], 'decisions', 'test-key', recorder, 5)
    assert row['error'] == 'ValueError'
    assert row['validation_error'] == 'ValueError'
    assert row['cost_usd'] == pytest.approx(0.00005)


def test_usage_cost_keeps_unknown_and_charges_decision_cached_input():
    assert usage_cost(None, 'decisions') is None
    assert usage_cost({'input_tokens': -1}, 'decisions') is None
    usage = {'input_tokens': 1000, 'output_tokens': 100,
             'input_tokens_details': {'cached_tokens': 400, 'cache_write_tokens': 100}}
    assert usage_cost(usage, 'decisions') == pytest.approx(0.0001)
    assert usage_cost(usage, 'responses_current') == pytest.approx(0.000253)
    assert usage_cost({'input_tokens': 100}, 'responses_control') is None


def test_false_approval_and_threshold_metrics_have_correct_denominators():
    row = {
        'arm': 'decisions', 'error': None, 'validation_error': None, 'call_count': 1,
        'cost_usd': .001, 'elapsed_ms': 100,
        'expected': ['supported', 'contradicted', 'insufficient'],
        'predictions': [
            {'label': 'supported', 'supported_probability': .8},
            {'label': 'supported', 'supported_probability': .7},
            {'label': 'insufficient', 'supported_probability': .1},
        ],
    }
    summary = summarize([row])['decisions']
    assert summary['counts']['supported'] == 1
    assert summary['counts']['unsupported'] == 2
    assert summary['counts']['false_approval'] == 1
    assert summary['counts']['false_rejection'] == 0
    assert summary['threshold_counts'] == {'false_approval': 0, 'false_rejection': 1}
    assert summary['p50_ms'] == 100


def test_call_cap_prevents_an_extra_paid_request():
    calls = []

    def transport(*args):
        calls.append(1)
        return {}

    recorder = Recorder(transport=transport, max_calls=1)
    recorder('url', {}, {'model': 'fixture'}, 5)
    with pytest.raises(RuntimeError):
        recorder('url', {}, {'model': 'fixture'}, 5)
    assert len(calls) == 1


def test_audit_is_read_only_and_null_is_unknown(tmp_path):
    path = tmp_path / 'metrics.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE automatic_memory_jobs '
                     '(state TEXT, review_ms REAL, created_at REAL)')
        conn.executemany('INSERT INTO automatic_memory_jobs VALUES (?, ?, ?)', [
            ('saved', 12, 1), ('discarded', 0, 2), ('failed', None, 3), ('queued', None, 4),
        ])
    before = path.read_bytes()
    report = audit_database(path)
    assert path.read_bytes() == before
    assert report['jobs'] == 4
    assert report['review_unknown'] == 2
    assert report['observed_review_fraction_all_jobs'] == .25
    assert report['review_fraction_known_jobs'] == .5
    assert report['all_conversation_turns_call_rate'] is None
    missing = tmp_path / 'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        audit_database(missing)
    assert not missing.exists()


def test_dry_run_needs_no_key_or_network(monkeypatch, capsys):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    assert main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['live'] is False
    assert result['case_count'] == 38
    assert result['review_batches'] == 28
    assert result['review_candidates'] == 30
    assert result['planned_calls'] == 168


def test_responses_uses_original_reviewer_contract():
    case = next(case for case in load_cases() if case['id'] == 'name_casual')

    def transport(url, headers, payload, timeout):
        assert url.endswith('/responses')
        assert payload['model'] == 'gpt-5.6-luna'
        assert payload['reasoning']['effort'] == 'low'
        assert SOURCE_REVIEW_SCOPE_INSTRUCTIONS not in payload['instructions']
        value = {'type': 'message', 'message': '검토', 'reason': 'review', 'confidence': 1,
                 'memory_proposal': {'operation': 'remember',
                                     'facts': copy.deepcopy(case['facts']),
                                     'target_ids': [], 'query': '',
                                     'evidence': case['source']}}
        return {'status': 'completed', 'model': payload['model'],
                'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': json.dumps(value, ensure_ascii=False)},
                ]}], 'usage': {'input_tokens': 100, 'output_tokens': 100, 'total_tokens': 200}}

    row = run_trial(case, case['facts'], 'responses_current', 'test-key', Recorder(transport), 5)
    assert row['error'] is None
    assert row['validation_error'] is None
    assert row['predictions'][0]['label'] == 'supported'


def test_refined_review_isolates_each_candidate_and_retains_full_source():
    case = next(case for case in load_cases() if case['id'] == 'batch_preferences')
    payload = decisions_payload(case['source'], case['facts'], refined=True)
    assert json.loads(payload['input']) == {'current_user_utterance': case['source']}
    assert len(payload['questions']) == 2
    for question, fact in zip(payload['questions'], case['facts']):
        assert SOURCE_REVIEW_SCOPE_INSTRUCTIONS in question['instructions']
        assert json.loads(question['instructions'].split('데이터):\n')[1]) == fact
    assert 'expected' not in json.dumps(payload)


def test_refined_responses_receives_scope_policy():
    captured = []

    def transport(url, headers, payload, timeout):
        captured.append(payload)
        raise RuntimeError('fixture: stop before any network request')

    case = next(case for case in load_cases() if case['id'] == 'pet_wrong_owner')
    run_trial(case, case['facts'], 'responses_refined', 'test-key', Recorder(transport), 5)
    assert len(captured) == 1
    assert captured[0]['instructions'].endswith(SOURCE_REVIEW_SCOPE_INSTRUCTIONS)


@pytest.mark.parametrize('identifier', [
    'h_neighbor_label', 'h_neighbor_trimmed', 'h_former_pet', 'h_preference_whole_source',
])
def test_ownership_or_later_correction_cannot_bypass_review(identifier):
    case = next(case for case in load_cases(HELDOUT_DATASET) if case['id'] == identifier)
    assert not fact_matches_source(case['facts'][0], case['source'])
    assert review_candidates(case) == case['facts']


def test_clear_own_pet_still_skips_remote_review():
    source = '우리 둘째 강아지 이름은 보리야'
    fact = {'kind': 'pet', 'subject': '둘째 강아지', 'attribute': 'name',
            'value': '보리', 'evidence': source}
    assert fact_matches_source(fact, source)
    assert review_candidates({'id': 'own', 'source': source, 'facts': [fact]}) == []


def test_refined_suite_covers_old_and_new_cases_separately(capsys):
    assert main(['--suite', 'ownership-v2']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['arms'] == list(REFINED_ARMS)
    assert report['case_count'] == 58
    assert report['review_batches'] == 48
    assert report['review_candidates'] == 60
    assert report['planned_calls'] == 384
    assert len(report['holdout_case_ids']) == 20


def test_paired_summary_excludes_an_entire_group_on_one_endpoint_failure():
    rows = []
    for case_id in ('success', 'partial_failure'):
        for arm in ('decisions', 'decisions_refined'):
            rows.append({
                'case_id': case_id, 'repeat': 0, 'arm': arm, 'expected': ['supported'],
                'predictions': [{'label': 'supported', 'supported_probability': .99}],
                'error': 'TimeoutError' if case_id == 'partial_failure'
                and arm == 'decisions' else None,
                'validation_error': None, 'call_count': 1, 'cost_usd': .01, 'elapsed_ms': 1,
            })
    report = summarize_paired(rows, ('decisions', 'decisions_refined'))
    assert report['complete_groups'] == 1
    assert report['excluded_groups'] == 1
    for summary in report['summary'].values():
        assert summary['counts']['supported'] == 1
        assert summary['attempted_batches'] == 1
