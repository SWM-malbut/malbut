"""Evaluation labels must not influence the detector or escape its call budget."""

import json
from pathlib import Path
import runpy
import sys

import pytest

from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider


def test_audit_keeps_answers_out_of_input_and_bounds_paid_calls(tmp_path, monkeypatch):
    runner = Path(__file__).parents[1] / 'tools' / 'conversation_constraint_audit.py'
    fixture, output = tmp_path / 'cases.json', tmp_path / 'result.json'
    case = {'id': 'heldout', 'context': {'current_user_utterance': '초대문을 써줘'},
            'candidate': '내일 만나자.',
            'evaluation_only': {'secret_label': 'EVALUATION_ONLY_DO_NOT_SEND'}}
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        assert json.loads(payload['input']) == {
            'context': case['context'], 'candidate': case['candidate'],
        }
        assert 'EVALUATION_ONLY_DO_NOT_SEND' not in json.dumps(payload)
        return {'status': 'completed', 'output': [{
            'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': json.dumps({
                'checks': [{'condition': 'test quote integrity', 'treatment': 'include',
                            'status': 'met', 'source_quote': '없는 원문',
                            'candidate_quote': '조작한 인용문'}],
                'unsupported_claims': [],
            })}],
        }]}

    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key-never-save')
    monkeypatch.setattr(OpenAIResponsesProvider, '_urllib_transport', staticmethod(transport))
    monkeypatch.setattr(sys, 'argv', [str(runner), '--live', '--cases', str(fixture),
                                     '--output', str(output)])
    fixture.write_text(json.dumps([case] * 2))
    runpy.run_path(str(runner), run_name='__main__')
    assert len(calls) == 2
    recorded = output.read_text()
    assert 'EVALUATION_ONLY_DO_NOT_SEND' in recorded
    assert 'test-only-key-never-save' not in recorded
    assert 'Authorization' not in recorded
    assert all(item['quotation_issues'] == {'source': [0], 'candidate': [0]}
               for item in json.loads(recorded)['cases'])
    fixture.write_text(json.dumps([case] * 9))
    with pytest.raises(SystemExit) as rejected:
        runpy.run_path(str(runner), run_name='__main__')
    assert rejected.value.code == 2
    assert len(calls) == 2
