"""Real read-only SQLite HTTP inspection, including legacy logs and redaction."""

import hashlib
import json
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from malbut_resource_monitor.memory_view import MemoryView, configured_databases
from malbut_resource_monitor.viewer import LogServer


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'dialogue.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
          CREATE TABLE conversation_sessions(user_id,conversation_id,status,generation,updated_at);
          CREATE TABLE conversation_turns(user_id,request_id,conversation_id,session_instance_id,
            turn_id,generation,ordinal,status,user_content,assistant_content,created_at,completed_at,response_json);
          CREATE TABLE story_runtime_stories(user_id,story_id,title,current_json,version,updated_at,active,refs_json);
          CREATE TABLE story_runtime_policy(user_id,enabled,revision);
          CREATE TABLE memories(user_id,id,kind,content,created_at,expires_at);
          CREATE TABLE memory_trace_events(sequence INTEGER PRIMARY KEY,wall_time,event,user_id,request_id,
            conversation_id,turn_id,data_json);
        ''')
        for user, text in [('alice', '전시에서 마음이 편안했어 <script>'), ('bob', '다른 사용자 비밀')]:
            conn.execute('INSERT INTO conversation_sessions VALUES (?,?,?,?,?)', (user, 'room', 'active', 1, 2))
            conn.execute('INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (user, 'same-request', 'room', 'instance', 'turn', 1, 1, 'completed', text, '답변', 1, 2, '{}'))
        ref = {'conversation_id': 'room', 'session_instance_id': 'instance', 'generation': 1,
               'turn_id': 'turn', 'role': 'user', 'start': 0, 'end': 2,
               'sha256': hashlib.sha256('전시에서 마음이 편안했어 <script>'.encode()).hexdigest()}
        conn.execute('INSERT INTO story_runtime_stories VALUES (?,?,?,?,?,?,?,?)',
                     ('alice', 'story', '전시', '[{"text":"가상 이야기"}]', 1, 2, 1, json.dumps([ref])))
        conn.execute('INSERT INTO memory_trace_events VALUES (?,?,?,?,?,?,?,?)',
                     (1, 1, 'model_input', 'alice', 'same-request', 'room', 'turn', '{"stories":[]}'))
        conn.execute('INSERT INTO memory_trace_events VALUES (?,?,?,?,?,?,?,?)',
                     (2, 1, 'model_input', 'bob', 'same-request', 'room', 'turn', '{"private":"bob"}'))
        conn.execute('INSERT INTO memory_trace_events VALUES (?,?,?,?,?,?,?,?)',
                     (3, 3, 'provider_failed', 'alice', 'failed-request', 'room', 'failed-turn', '{"error_type":"ProviderError"}'))
    return path


def test_snapshot_and_turn_keep_users_isolated_and_preserve_failed_requests(database):
    view = MemoryView([database]); key = next(iter(view.paths))
    data = view.snapshot(key, 'alice')
    assert data['trace_available']
    assert [turn['request_id'] for turn in data['turns']] == ['failed-request', 'same-request']
    assert data['turns'][0]['status'] == 'trace_only'
    detail = view.turn(key, 'alice', 'same-request')
    assert len(detail['events']) == 1
    assert 'bob' not in json.dumps(detail)
    assert view.snapshot(key, 'alice', query="' OR 1=1 --")['turns'] == []
    assert len(view.snapshot(key, 'alice', query='편안')['turns']) == 1


def test_view_is_read_only_and_current_evidence_respects_redaction(database):
    view = MemoryView([database]); key = next(iter(view.paths))
    before = database.read_bytes()
    assert view.evidence(key, 'alice', 'story')['sources'][0]['text'] == '전시'
    with view.connection(key) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM conversation_turns')
    assert database.read_bytes() == before
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE conversation_turns SET user_content='' WHERE user_id='alice'")
    source = view.evidence(key, 'alice', 'story')['sources'][0]
    assert not source['available'] and source['text'] is None
    with pytest.raises(ValueError):
        view.evidence(key, 'bob', 'story')


def test_legacy_database_does_not_invent_trace_and_missing_file_is_not_created(database, tmp_path):
    with sqlite3.connect(database) as conn:
        conn.execute('DROP TABLE memory_trace_events')
    view = MemoryView([database]); key = next(iter(view.paths))
    assert not view.snapshot(key, 'alice')['trace_available']
    assert view.turn(key, 'alice', 'same-request')['events'] == []
    missing = tmp_path / 'missing.sqlite3'
    view = MemoryView([missing])
    with pytest.raises(ValueError):
        view.snapshot(next(iter(view.paths)))
    assert not missing.exists()


def test_http_memory_works_without_resource_sessions_and_rejects_arbitrary_paths(database, tmp_path):
    server = LogServer(('127.0.0.1', 0), tmp_path / 'no-resource-logs', memory_databases=[database])
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/api/memory/databases') as response:
            key = json.load(response)[0]['id']
        with urlopen(base + '/api/memory?' + urlencode({'database': key, 'user': 'alice'})) as response:
            assert json.load(response)['stories'][0]['story_id'] == 'story'
        with pytest.raises(HTTPError) as error:
            urlopen(base + '/api/memory?database=/etc/passwd')
        assert error.value.code == 400
        with urlopen(base + '/memory-viewer.js') as response:
            assert 'javascript' in response.headers['Content-Type']
    finally:
        server.shutdown(); server.server_close(); thread.join()


def add_failure(database, state='failed'):
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE story_runtime_jobs(user_id,request_id,job_id,state,attempts,error_code,created_at)')
        conn.execute('INSERT INTO story_runtime_jobs VALUES (?,?,?,?,?,?,?)',
                     ('alice', 'same-request', 'job', state, 3 if state == 'failed' else 1,
                      'actor_source_mismatch' if state == 'failed' else None, 2))
        conn.execute('INSERT INTO memory_trace_events VALUES (?,?,?,?,?,?,?,?)',
                     (4, 4, 'story_extraction_failed', 'alice', 'same-request', 'room', 'turn',
                      '{"attempt":3,"error_code":"actor_source_mismatch","retry_scheduled":false}'))


def test_activity_and_failure_history_survive_successful_retry(database):
    add_failure(database)
    view = MemoryView([database]); key = next(iter(view.paths))
    data = view.snapshot(key, 'alice')
    assert len(data['attention']) == 1
    activity = next(t['activity'] for t in data['turns'] if t['request_id'] == 'same-request')
    assert activity['jobs'][0]['error_code'] == 'actor_source_mismatch'
    assert activity['failure_attempts'] == 1 and activity['saved'] == 0
    assert activity['model_input']['stories'] == 0
    assert not view.snapshot(key, 'bob')['attention']
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE story_runtime_jobs SET state='done',attempts=1,error_code=NULL")
    recovered = view.snapshot(key, 'alice')['attention'][0]['activity']
    assert recovered['jobs'][0]['state'] == 'done' and recovered['failure_attempts'] == 1


def test_legacy_failed_job_visible_without_inventing_trace(database):
    add_failure(database)
    with sqlite3.connect(database) as conn:
        conn.execute('DROP TABLE memory_trace_events')
    view = MemoryView([database]); data = view.snapshot(next(iter(view.paths)), 'alice')
    assert len(data['attention']) == 1
    activity = data['attention'][0]['activity']
    assert activity['model_input'] is None and activity['saved'] == 0
    assert not activity['trace_observed'] and activity['failure_attempts'] == 0


def test_persistent_database_allowlist_and_explicit_override(tmp_path, monkeypatch):
    config = tmp_path / 'viewer.json'
    monkeypatch.setattr('malbut_resource_monitor.memory_view.DEFAULT_MEMORY_CONFIG', str(config))
    assert configured_databases() is None
    config.write_text(json.dumps(['/live.sqlite3', '/experiment.sqlite3']))
    assert configured_databases() == ['/live.sqlite3', '/experiment.sqlite3']
    config.write_text('{broken')
    assert configured_databases(['/override.sqlite3']) == ['/override.sqlite3']
    with pytest.raises(ValueError, match='Cannot read'):
        configured_databases()
    config.write_text('[42]')
    with pytest.raises(ValueError, match='JSON array'):
        configured_databases()
