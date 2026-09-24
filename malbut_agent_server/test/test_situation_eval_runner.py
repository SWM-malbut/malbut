"""Evaluation aggregation and privacy, without live model or credential access."""

from dataclasses import replace
import json

import pytest

from malbut_agent_server.situation_dialogue import (
    SituationDialogue, SituationInterpretation,
)
from malbut_agent_server import situation_eval_runner as runner


class ScriptedProvider:
    def __init__(self, *values):
        self.values = iter(values)

    def evaluate(self, _context):
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def factory(*values):
    return lambda: SituationDialogue(ScriptedProvider(*values))


def question():
    return SituationInterpretation('unknown', None, '실제로 넘어지셨나요?')


def test_packaged_suite_distinguishes_mock_state_flow_from_live_semantic_coverage():
    cases = runner.load_cases()
    assert len(cases) == 20
    engine_factory = runner.build_situation_factory(runner.Settings.from_env({}))
    report = runner.run_evaluation(cases, engine_factory)
    assert report['evaluation'] == 'mock_state_flow_only'
    assert report['counts'] == {'passed': 13, 'failed': 0, 'error': 0, 'skipped_live_only': 7}
    fields = {'id', 'status', 'situation_assessment', 'help_needed', 'question_count'}
    assert all(set(row) == fields for row in report['cases'])


def test_mismatched_result_and_operational_error_are_separate_from_silence(capsys):
    case = runner.load_cases()[0]
    bad = runner.run_evaluation([case], factory(
        question(), SituationInterpretation('unknown', True, '')))
    assert bad['counts']['failed'] == 1
    assert bad['cases'][0]['situation_assessment'] == 'unknown'
    failure = runner.run_evaluation([case], factory(
        question(), RuntimeError('sk-private-do-not-print 原文 답변')))
    row = failure['cases'][0]
    assert row['status'] == 'error'
    assert row['situation_assessment'] is None and row['help_needed'] is None
    assert failure['counts']['error'] == 1
    assert 'sk-private' not in json.dumps(failure)
    assert '原文' not in json.dumps(failure, ensure_ascii=False)
    assert capsys.readouterr() == ('', '')


def test_explicit_null_is_silence_but_exhausted_fixture_is_not():
    silence = runner.load_cases()[5]
    report = runner.run_evaluation([silence], factory(question()))
    assert report['counts']['passed'] == 1
    actual_answer = replace(silence, answers=('모호한 대답',))
    report = runner.run_evaluation([actual_answer], factory(question(), question()))
    assert report['counts']['failed'] == 1
    assert report['cases'][0]['help_needed'] is None
    assert report['cases'][0]['question_count'] == 2


def test_question_count_and_premature_termination_fail_even_with_matching_final_fields():
    case = replace(runner.load_cases()[0], answers=('대답 하나', '남은 대답'), question_count=2)
    report = runner.run_evaluation([case], factory(
        question(), SituationInterpretation('resolved', False, '')))
    assert report['cases'][0]['situation_assessment'] == 'resolved'
    assert report['counts']['failed'] == 1


def test_live_only_case_is_skipped_without_constructing_a_provider():
    def forbidden():
        pytest.fail('live-only mock case must not instantiate a provider')
    report = runner.run_evaluation([runner.load_cases()[13]], forbidden)
    assert report['counts']['skipped_live_only'] == 1


def test_live_mode_aggregation_can_be_tested_with_a_scripted_provider():
    case = runner.load_cases()[13]
    report = runner.run_evaluation([case], factory(
        question(), SituationInterpretation('resolved', False, '')), provider='openai')
    assert report['evaluation'] == 'openai_semantic_cases'
    assert report['counts']['passed'] == 1


def test_default_cli_ignores_live_environment_and_does_not_read_env_file(monkeypatch, capsys):
    monkeypatch.setenv('MALBUT_AGENT_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key-that-must-not-be-loaded')
    monkeypatch.setattr(runner, 'load_env_file', lambda *_a, **_k: pytest.fail('key file read'))
    real_factory = runner.build_situation_factory

    def checked(settings):
        assert settings.provider == 'mock' and settings.openai_api_key == ''
        return real_factory(settings)

    monkeypatch.setattr(runner, 'build_situation_factory', checked)
    assert runner.main(['--env-file', '/unused/private.env']) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report['counts']['skipped_live_only'] == 7
    assert 'fake-key' not in captured.out and not captured.err


def test_cli_filter_and_private_summary_output(tmp_path, capsys):
    destination = tmp_path / 'report.json'
    assert runner.main(['--case-id', 'resting-clear', '--output', str(destination)]) == 0
    report = json.loads(destination.read_text())
    assert len(report['cases']) == 1
    assert report['cases'][0]['id'] == 'resting-clear'
    assert '그냥 누워' not in destination.read_text()
    assert capsys.readouterr() == ('', '')


def test_cli_configuration_failure_does_not_log_exception_details(monkeypatch, capsys):
    def broken(*_args, **_kwargs):
        raise RuntimeError('secret-provider-message')
    monkeypatch.setattr(runner, 'load_cases', broken)
    assert runner.main([]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert 'secret-provider-message' not in captured.err


@pytest.mark.parametrize('change', [
    {'id': 'not a valid ID'}, {'answers': []}, {'answers': ['']},
    {'providers': ['mock']}, {'providers': ['openai', 'openai']},
    {'expected': {'situation_assessment': 'unknown', 'help_needed': 1, 'question_count': 1}},
])
def test_invalid_fixtures_are_rejected(change):
    value = dict(id='case', situation_type='fall', summary='요약', answers=[None],
                 providers=['mock', 'openai'], expected=dict(
                     situation_assessment='unknown', help_needed=True, question_count=1))
    with pytest.raises(ValueError):
        runner.SituationEvaluationCase.parse(value | change)


def test_duplicate_case_ids_are_rejected(tmp_path):
    case = runner.load_cases()[0]
    payload = dict(id=case.id, situation_type=case.situation_type, summary=case.summary,
                   answers=list(case.answers), providers=list(case.providers), expected=dict(
                       situation_assessment='resolved', help_needed=False, question_count=1))
    path = tmp_path / 'cases.json'
    path.write_text(json.dumps([payload, payload]))
    with pytest.raises(ValueError):
        runner.load_cases(path)
