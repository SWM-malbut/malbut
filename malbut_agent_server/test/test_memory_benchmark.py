"""Verify latency evidence and real memory boundaries without API calls."""

import json
import sqlite3
from urllib.error import HTTPError

import pytest

from malbut_agent_server import memory_benchmark as bench
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.providers.base import ProviderError


USER = 'benchmark-user'


def recorder(mutate=None):
    """Return actual provider JSON through a controlled transport."""
    def transport(_url, _headers, payload, _timeout):
        response = bench.fixture_response(payload)
        if mutate:
            mutate(response)
        return response
    return bench.CallRecorder(transport)


def change_output(response, mutate):
    """Edit the untrusted JSON, not the server's interpretation."""
    part = response['output'][0]['content'][0]
    value = json.loads(part['text'])
    mutate(value)
    part['text'] = json.dumps(value, ensure_ascii=False)


def seed_pet(memory, _conversations):
    """Create a real fact to exercise deduplication and deletion versions."""
    case = bench.CASES[1]
    source = dict(bench.consent_source(), text=case['text'], request_id='seed')
    fact = dict(case['expected'], evidence=case['text'])
    memory.upsert_fact(USER, fact, source)


@pytest.mark.parametrize('mode,count', [('A', 1), ('B', 1), ('C', 2)])
def test_call_order_and_exact_early_reply(tmp_path, mode, count):
    """Timestamp JSON before deferred work and only after synchronous work."""
    calls = recorder()
    events = []

    def on_reply(row, memory, _conversations):
        events.append('reply')
        assert row['reply_ready_ms'] is not None
        assert json.loads(row['reply_json'])['decision']['type'] == 'message'
        assert len(memory.list_for_user(USER)) == (1 if mode == 'A' else 0)
        assert len(calls.calls) == 1

    def before_commit(_memory, _conversations):
        events.append('commit')
        assert len(calls.calls) == count

    row = bench.run_trial(tmp_path / 'trial.db', bench.CASES[1], mode,
                          calls, 'fixture-key', on_reply=on_reply,
                          before_commit=before_commit)
    assert row['valid'], row
    assert row['call_count'] == count
    assert events == (['commit', 'reply'] if mode == 'A'
                      else ['reply', 'commit'])
    assert (row['reply_ready_ms'] >= row['memory_done_ms']) == (mode == 'A')
    phases = [item['phase'] for item in calls.calls]
    assert phases == (['answer', 'extraction'] if mode == 'C'
                      else ['combined'])
    for call in calls.calls:
        assert call['payload']['model'] == bench.MODEL
        assert call['payload']['max_output_tokens'] == 500
        assert call['payload']['reasoning']['effort'] == 'none'


@pytest.mark.parametrize('mode', list('ABC'))
def test_no_consent_and_duplicate_fact(tmp_path, mode):
    """Use the production consent and duplicate-slot checks."""
    disabled = bench.run_trial(tmp_path / 'disabled.db', bench.CASES[1], mode,
                               recorder(), 'fixture-key', enabled=False)
    assert disabled['valid'] and disabled['facts'] == []
    duplicate = bench.run_trial(
        tmp_path / 'duplicate.db', bench.CASES[1], mode,
        recorder(), 'fixture-key', setup=seed_pet,
    )
    assert duplicate['valid'] and len(duplicate['facts']) == 1


@pytest.mark.parametrize('mode', ['B', 'C'])
@pytest.mark.parametrize('event', ['disable', 'delete', 'reset'])
def test_late_work_cannot_survive_invalidation(tmp_path, mode, event):
    """A separate connection invalidates the original inference snapshot."""
    path = tmp_path / 'race.db'

    def invalidate(_memory, conversations):
        if event == 'reset':
            conversations.reset(USER, 'benchmark-conversation')
            return
        other = SQLiteMemoryStore(str(path))
        try:
            if event == 'disable':
                other.set_personalization(USER, False, bench.consent_source())
            else:
                ids = [record.id for record in other.list_for_user(USER)]
                assert ids
                other.remove_facts(USER, ids)
        finally:
            other.close()

    row = bench.run_trial(
        path, bench.CASES[1], mode, recorder(), 'fixture-key',
        setup=seed_pet if event == 'delete' else None,
        before_commit=invalidate,
    )
    assert not row['valid']
    assert row['error'] in {'ValidationError', 'ConversationChangedError'}
    assert row['reply_json'] is not None and row['memory_done_ms'] is None
    assert row['facts'] == []


@pytest.mark.parametrize('mode', list('ABC'))
def test_invalid_evidence_and_frozen_reply(tmp_path, mode):
    """Reject fabricated facts and retain the reply actually timed."""
    def corrupt(response):
        def change(value):
            proposal = value.get('memory_proposal')
            if proposal:
                proposal['facts'][0]['value'] = '원문에 없는 이름'
        change_output(response, change)

    row = bench.run_trial(tmp_path / 'bad.db', bench.CASES[1], mode,
                          recorder(corrupt), 'fixture-key')
    assert not row['valid'] and row['facts'] == []
    before = json.loads(row['reply_json'])['decision']
    after = json.loads(row['postcommit_reply_json'])['decision']
    assert after['type'] == 'clarification'
    if mode != 'A':
        assert before['message'] == '이야기해 줘서 고마워요.'
        assert 'postcommit_decision_changed' in row['quality_issues']


@pytest.mark.parametrize('mode', list('ABC'))
def test_false_saved_claim_is_not_an_eligible_fast_reply(tmp_path, mode):
    """Inspect the early JSON instead of only the postcommit decision."""
    def lie(response):
        def change(value):
            if 'message' in value:
                value['message'] = '기억 저장했어요.'
        change_output(response, change)

    row = bench.run_trial(tmp_path / 'claim.db', bench.CASES[1], mode,
                          recorder(lie), 'fixture-key')
    assert 'premature_memory_claim' in row['quality_issues']
    assert '저장했' not in json.loads(row['reply_json'])['decision']['message']
    assert bench.summarize([row])[mode]['valid'] == 0


@pytest.mark.parametrize('mode', ['B', 'C'])
def test_failed_write_is_not_saved_success(tmp_path, monkeypatch, mode):
    """SQLite failure leaves neither the proposed fact nor completed turn."""
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError('synthetic write failure')
    monkeypatch.setattr(SQLiteMemoryStore, 'upsert_fact', fail)
    row = bench.run_trial(tmp_path / 'failure.db', bench.CASES[1], mode,
                          recorder(), 'fixture-key')
    assert row['error'] == 'OperationalError' and not row['valid']
    assert row['facts'] == [] and row['memory_done_ms'] is None
    assert '저장' not in json.loads(row['reply_json'])['decision']['message']


def test_truncated_output_missing_usage_and_call_cap(tmp_path):
    """Failures have no winning latency and missing token usage is unknown."""
    def truncate(response):
        response['status'] = 'incomplete'
        response['output'][0]['content'][0]['text'] = '{"message":'
    row = bench.run_trial(tmp_path / 'cut.db', bench.CASES[1], 'B',
                          recorder(truncate), 'fixture-key')
    assert not row['valid'] and row['reply_ready_ms'] is None
    assert bench.summarize([row])['B']['reply_ready_ms'] is None
    assert row['cost_usd'] is None
    calls = bench.CallRecorder(lambda *_: {'status': 'completed'})
    for _ in range(bench.CALL_LIMIT):
        calls('url', {'Authorization': 'fixture-secret'}, {}, 30)
    with pytest.raises(ProviderError, match='call limit'):
        calls('url', {}, {}, 30)
    assert len(calls.calls) == bench.CALL_LIMIT == 44
    assert 'fixture-secret' not in json.dumps(calls.calls)
    assert bench.usage_cost({'input_tokens': 4000, 'output_tokens': 500}) == (
        pytest.approx(0.0014))


def test_schedule_is_bounded_balanced_and_uses_fresh_databases(tmp_path):
    """Ten trials per mode have identical 4/3/3 cases, plus three warmups."""
    directory = tmp_path / 'fixed'
    assert bench.main(['--output-dir', str(directory)]) == 0
    result = json.loads((directory / 'results.json').read_text())
    assert result['calls_used'] == 44 and len(result['rows']) == 33
    assert result['trials_per_mode'] == bench.TRIALS_PER_MODE == 10
    assert result['comparison_complete']
    assert len(list(directory.glob('trial-*.sqlite3'))) == 33
    for case in bench.CASES:
        for mode in 'ABC':
            rows = [r for r in result['rows'] if not r['warmup']
                    and r['case'] == case['id'] and r['mode'] == mode]
            assert len(rows) == (4 if case['id'] == 'pet' else 3)
            assert all(r['valid'] for r in rows)
    assert all(result['summary'][mode]['trials'] == 10 for mode in 'ABC')
    with pytest.raises(ValueError, match='fresh database'):
        bench.run_trial(directory / 'trial-00.sqlite3', bench.CASES[0], 'A',
                        recorder(), 'fixture-key')


def test_live_auth_error_stops_without_fallback(tmp_path, monkeypatch):
    """Exercise the live branch with a failing transport, never a network."""
    def denied(*_args):
        raise ProviderError('denied') from HTTPError('url', 401, '', {}, None)
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    monkeypatch.setattr(bench.OpenAIResponsesProvider, '_urllib_transport',
                        staticmethod(denied))
    directory = tmp_path / 'denied'
    assert bench.main(['--output-dir', str(directory), '--live']) == 1
    result = json.loads((directory / 'results.json').read_text())
    assert result['stop_reason'] == 'api_access_or_configuration_error'
    assert result['calls_used'] == 1 and len(result['rows']) == 1
    assert result['rows'][0]['error'] == 'ProviderError'
