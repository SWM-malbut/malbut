"""Authenticated story management over real loopback HTTP and temporary SQLite."""

from contextlib import contextmanager
import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.http_server import make_server
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.story_http import StoryHTTPBoundary
from malbut_agent_server.story_memory_service import StoryMemoryService
from test_story_memory_service import FakeExtractor


@pytest.fixture
def runtime(tmp_path):
    path = str(tmp_path / 'http.sqlite3')
    conversations = SQLiteConversationStore(path, semantic_context=True)
    memory = SQLiteMemoryStore(path)
    runtime = AgentOrchestrator(
        provider=MockProvider(), memory_store=memory,
        conversation_store=conversations, safety_policy=SafetyPolicy(),
    )
    extractor = FakeExtractor()
    service = StoryMemoryService(conversations, memory, extractor,
                                 debounce_seconds=0, max_delay_seconds=0)
    runtime.story_memory = service
    yield runtime
    if extractor.release is not None:
        extractor.release.set()
    service.close()
    runtime.close()


@contextmanager
def server_for(runtime, token='http-secret', **kwargs):
    server = make_server('127.0.0.1', 0, runtime, auth_token=token,
                         allowed_user_id='http-user', **kwargs)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield server, 'http://%s:%s/v1/stories/' % server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(base, action, body=None, token='http-secret', raw=None, headers=None):
    data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
    request_headers = {'Content-Type': 'application/json', **(headers or {})}
    if token:
        request_headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(base + action, data=data, headers=request_headers, method='POST')
    try:
        response = urllib.request.urlopen(req, timeout=7)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read()), response.headers


def enable_body(revision=0, **extra):
    return dict(consent=True, external_consent=True, expected_revision=revision, **extra)


def complete(runtime, text, identifier='first', user='http-user'):
    store = runtime.conversation_store
    store.create(user, 'room')
    turn = store.begin_turn(user, 'room', 'turn-' + identifier, 'request-' + identifier,
                            'fingerprint-' + identifier, text)
    store.complete_turn(turn.token, '알겠어요.', {
        'schema_version': 1, 'decision': {'type': 'message', 'message': '알겠어요.',
                                        'tool_name': None, 'arguments': {}},
    })
    return turn.token


def seed_story(runtime):
    runtime.story_memory.enable('http-user')
    token = complete(runtime, '전시 안내를 한 장으로 만들기로 했어.')
    runtime.story_memory.after_turn('http-user', token.request_id)
    assert runtime.story_memory.flush('http-user', timeout=3)
    return runtime.story_memory.list_stories('http-user')[0]['story_id']


def test_authentication_is_required_even_for_mock_and_identity_is_server_owned(runtime):
    with server_for(runtime, token='') as (_, base):
        status, result, _ = request(base, 'status')
        assert status == 503 and result['error']['code'] == 'story_auth_required'
    with server_for(runtime) as (_, base):
        for token in ('', 'wrong'):
            assert request(base, 'status', token=token)[0] == 401
        status, result, headers = request(base, 'status')
        assert status == 200 and not result['policy']['enabled']
        assert result['consent']['external_processing'] is True
        assert headers['Cache-Control'] == 'no-store'
        for action in ('status', 'list', 'enable', 'disable', 'history-preview', 'sync'):
            body = enable_body(user_id='secret-other-user') if action == 'enable' else {'user_id': 'secret-other-user'}
            status, error, _ = request(base, action, body)
            assert status == 400
            assert 'secret-other-user' not in json.dumps(error)


@pytest.mark.parametrize('body', [
    {}, {'consent': True}, enable_body(consent_value=True),
    {'consent': 1, 'external_consent': True, 'expected_revision': 0},
    {'consent': True, 'external_consent': False, 'expected_revision': 0},
    enable_body(True), enable_body(-1), enable_body('0'),
    enable_body(include_history='true'), enable_body(include_history=True),
    enable_body(preview_token='a' * 43), enable_body(history_scope={'turns': []}),
])
def test_enable_strict_schema_and_separate_consent(runtime, body):
    with server_for(runtime) as (_, base):
        status, error, _ = request(base, 'enable', body)
        assert status == 400
        assert error['error']['code'] == 'invalid_story_request'
        assert runtime.story_memory.policy('http-user')['enabled'] is False


def test_policy_cas_blocks_delayed_enable_after_disable(runtime):
    with server_for(runtime) as (_, base):
        assert request(base, 'enable', enable_body())[0] == 200
        status, disabled, _ = request(base, 'disable', {'expected_revision': 1})
        assert status == 200 and not disabled['policy']['enabled']
        status, error, _ = request(base, 'enable', enable_body(1))
        assert status == 409 and error['error']['code'] == 'story_changed'
        assert runtime.story_memory.policy('http-user')['revision'] == 2


def test_preview_is_exact_single_use_and_never_accepts_client_raw_scope(runtime):
    old = complete(runtime, '전시 안내를 만들고 싶어.', 'old')
    with server_for(runtime) as (_, base):
        status, preview, _ = request(base, 'history-preview')
        assert status == 200 and preview['scope']['turn_count'] == 1
        assert 'turns' not in preview['scope']
        assert old.request_id not in json.dumps(preview)
        later = complete(runtime, '설명서는 두 장으로 만들고 싶어.', 'later')
        body = enable_body(include_history=True, preview_token=preview['preview_token'])
        assert request(base, 'enable', body)[0] == 200
        assert request(base, 'enable', body)[0] == 409
        assert runtime.story_memory.flush('http-user', timeout=3)
        connection = runtime.conversation_store._connection
        grants = [row[0] for row in connection.execute('SELECT request_id FROM story_runtime_scope')]
        assert old.request_id in grants and later.request_id not in grants
        assert [story['title'] for story in runtime.story_memory.list_stories('http-user')] == ['전시']


def test_preview_expiry_policy_change_and_wrong_user_are_rejected(runtime):
    boundary = StoryHTTPBoundary(runtime, clock=lambda: 10)
    _, preview = boundary.handle('/v1/stories/history-preview', {}, 'http-user')
    token = preview['preview_token']
    from malbut_agent_server.story_http import StoryHTTPError
    with pytest.raises(StoryHTTPError) as error:
        boundary.handle('/v1/stories/enable', enable_body(include_history=True, preview_token=token), 'other-user')
    assert error.value.status == 409
    boundary._clock = lambda: 311
    with pytest.raises(StoryHTTPError) as error:
        boundary.handle('/v1/stories/enable', enable_body(include_history=True, preview_token=token), 'http-user')
    assert error.value.code == 'history_preview_expired'
    with server_for(runtime) as (_, base):
        _, preview, _ = request(base, 'history-preview')
        assert request(base, 'enable', enable_body())[0] == 200
        status, error, _ = request(base, 'enable', enable_body(
            1, include_history=True, preview_token=preview['preview_token']))
        assert status == 409 and error['error']['code'] == 'history_preview_expired'


def test_off_allows_management_evidence_and_deletion(runtime):
    story_id = seed_story(runtime)
    with server_for(runtime) as (_, base):
        assert request(base, 'disable', {'expected_revision': 1})[0] == 200
        status, result, _ = request(base, 'list')
        assert status == 200 and result['stories'][0]['story_id'] == story_id
        assert 'source_keys' not in json.dumps(result)
        status, evidence, _ = request(base, 'evidence', {'story_id': story_id, 'limit': 1})
        assert status == 200 and len(evidence['sources']) == 1
        assert evidence['sources'][0]['text'] == '전시 안내를 한 장으로 만들기로 했어.'
        status, deleted, _ = request(base, 'delete', {'story_id': story_id})
        assert status == 200 and deleted['deleted'] and not deleted['physical_erasure']
        assert request(base, 'delete', {'story_id': story_id})[0] == 404
        assert request(base, 'list')[1]['stories'] == []


def test_unsettled_delete_is_conflict_and_sync_is_bounded(runtime):
    story_id = seed_story(runtime)
    runtime.story_memory.extractor.release = threading.Event()
    token = complete(runtime, '전시 안내는 두 장으로 바꿀래.', 'change')
    runtime.story_memory.after_turn('http-user', token.request_id)
    with server_for(runtime) as (_, base):
        status, result, _ = request(base, 'sync')
        assert status == 202 and not result['settled']
        status, error, _ = request(base, 'delete', {'story_id': story_id})
        assert status == 409 and error['error']['code'] == 'story_not_settled'
        assert runtime.story_memory.list_stories('http-user')
        runtime.story_memory.extractor.release.set()
        assert request(base, 'sync', {'timeout_seconds': 3})[0] == 200


@pytest.mark.parametrize('action,body', [
    ('list', {'limit': True}), ('list', {'offset': -1}), ('list', {'limit': 51}),
    ('evidence', {'story_id': 'x' * 129}), ('delete', {'story_id': '\nsecret'}),
    ('sync', {'timeout_seconds': 6}), ('sync', {'timeout_seconds': True}),
    ('sync', {'retry_failed': 'yes'}), ('disable', {'expected_revision': None}),
])
def test_invalid_limits_identifiers_and_json_types(runtime, action, body):
    with server_for(runtime) as (_, base):
        assert request(base, action, body)[0] == 400


def test_failure_messages_and_error_fields_do_not_echo_secrets(runtime, monkeypatch):
    secret = 'secret-source-or-credential'
    with server_for(runtime) as (_, base):
        def explode(*args, **kwargs):
            raise RuntimeError(secret)
        monkeypatch.setattr(runtime.story_memory, 'list_stories', explode)
        status, error, _ = request(base, 'list')
        assert status == 500 and secret not in json.dumps(error)
        runtime.story_memory._errors['http-user'] = secret
        status, result, _ = request(base, 'status')
        assert status == 200 and result['policy']['error'] == 'processing_failed'
        assert secret not in json.dumps(result)
        assert request(base, 'status', raw=b'[]')[0] == 400
        assert request(base, 'status', raw=b'{')[0] == 400
        assert request(base, 'status', raw=b'{"x":NaN}')[0] == 400


def test_response_pages_are_bounded_and_preserve_whole_entries(runtime, monkeypatch):
    import malbut_agent_server.story_http as boundary_module
    monkeypatch.setattr(boundary_module, 'MAX_RESPONSE_BYTES', 6000)
    stories = [{'story_id': 's' + str(i), 'title': '전시', 'aliases': [], 'version': 1,
                'updated_at': 0, 'current': [{'text': 'x' * 1000, 'kind': 'context',
                                             'actor': 'user', 'status': 'stated'}]}
               for i in range(5)]
    monkeypatch.setattr(runtime.story_memory, 'list_stories', lambda user: stories)
    with server_for(runtime) as (_, base):
        status, first, _ = request(base, 'list', {'limit': 5})
        assert status == 200 and first['next_offset'] == 1
        assert len(json.dumps(first).encode()) < 6000
        status, second, _ = request(base, 'list', {'offset': first['next_offset']})
        assert status == 200 and second['stories'][0]['story_id'] == 's1'
        assert second['stories'][0]['current'][0]['text'] == 'x' * 1000


def test_shared_input_body_and_rate_limits_still_apply(runtime):
    with server_for(runtime, max_request_bytes=64) as (_, base):
        assert request(base, 'status', {'x': 'a' * 100})[0] == 413
    with server_for(runtime, requests_per_minute=1) as (_, base):
        assert request(base, 'status')[0] == 200
        assert request(base, 'status')[0] == 429


def test_duplicate_json_consent_and_unknown_field_names_are_not_reflected(runtime):
    with server_for(runtime) as (_, base):
        status, error, _ = request(base, 'enable', raw=(
            b'{"consent":false,"consent":true,"external_consent":true,"expected_revision":0}'
        ))
        assert status == 400 and error['error']['code'] == 'invalid_story_request'
        status, error, _ = request(base, 'status', {'private-field-secret': 'secret'})
        assert status == 400 and 'secret' not in json.dumps(error)


def test_history_snapshot_limit_and_cache_bound(runtime, monkeypatch):
    import malbut_agent_server.story_http as boundary_module
    boundary = StoryHTTPBoundary(runtime)
    for _ in range(boundary_module.MAX_PREVIEWS + 2):
        boundary.handle('/v1/stories/history-preview', {}, 'http-user')
    assert len(boundary._previews) == boundary_module.MAX_PREVIEWS
    complete(runtime, '전시에 다녀왔어.')
    monkeypatch.setattr(boundary_module, 'MAX_HISTORY_TURNS', 0)
    with server_for(runtime) as (_, base):
        status, error, _ = request(base, 'history-preview')
        assert status == 413 and error['error']['code'] == 'history_scope_too_large'


def test_concurrent_history_token_consumption_succeeds_only_once(runtime):
    complete(runtime, '전시에 다녀왔어.')
    with server_for(runtime) as (_, base):
        _, preview, _ = request(base, 'history-preview')
        body = enable_body(include_history=True, preview_token=preview['preview_token'])
        results = []
        threads = [threading.Thread(target=lambda: results.append(request(base, 'enable', body)[0]))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)
        assert sorted(results) == [200, 409]


def test_story_evidence_does_not_escape_after_concurrent_policy_change(runtime, monkeypatch):
    story_id = seed_story(runtime)
    original = runtime.story_memory.evidence
    def evidence_then_disable(user, identifier):
        result = original(user, identifier)
        runtime.story_memory.disable(user)
        return result
    monkeypatch.setattr(runtime.story_memory, 'evidence', evidence_then_disable)
    with server_for(runtime) as (_, base):
        status, error, _ = request(base, 'evidence', {'story_id': story_id})
        assert status == 409 and error['error']['code'] == 'story_changed'
        assert '전시' not in json.dumps(error, ensure_ascii=False)


def test_conversation_get_fences_concurrent_reset_and_delete_uses_runtime(runtime, monkeypatch):
    story_id = seed_story(runtime)
    original = runtime.conversation_store.snapshot
    def snapshot_then_reset(user, identifier, **kwargs):
        result = original(user, identifier, **kwargs)
        runtime.conversation_store.reset(user, identifier)
        return result
    monkeypatch.setattr(runtime.conversation_store, 'snapshot', snapshot_then_reset)
    with server_for(runtime) as (_, base):
        prefix = base.replace('/stories/', '/conversations/')
        status, error, _ = request(prefix, 'get', {'user_id': 'http-user', 'conversation_id': 'room'})
        assert status == 409 and error['error']['code'] == 'conversation_changed'
        assert '전시' not in json.dumps(error, ensure_ascii=False)
        monkeypatch.setattr(runtime.conversation_store, 'snapshot', original)
        status, deleted, _ = request(prefix, 'delete', {'user_id': 'http-user', 'conversation_id': 'room'})
        assert status == 200 and deleted['deleted']
        assert not runtime.story_memory.list_stories('http-user')
        assert runtime.conversation_store._connection.execute(
            'SELECT 1 FROM story_runtime_stories WHERE story_id=?', (story_id,),
        ).fetchone() is None


def test_conversation_get_fences_concurrent_story_delete(runtime, monkeypatch):
    story_id = seed_story(runtime)
    original = runtime.conversation_store.snapshot
    def snapshot_then_forget(user, identifier, **kwargs):
        result = original(user, identifier, **kwargs)
        assert runtime.story_memory.forget(user, story_id, timeout=0)['deleted']
        return result
    monkeypatch.setattr(runtime.conversation_store, 'snapshot', snapshot_then_forget)
    with server_for(runtime) as (_, base):
        prefix = base.replace('/stories/', '/conversations/')
        status, error, _ = request(prefix, 'get', {'user_id': 'http-user', 'conversation_id': 'room'})
        assert status == 409 and error['error']['code'] == 'story_changed'
        assert '전시' not in json.dumps(error, ensure_ascii=False)


def test_reset_and_close_continue_when_checkpoint_fails(runtime, monkeypatch):
    complete(runtime, '새 대화를 시작할게.')
    calls = []
    def failing_checkpoint(user, timeout):
        calls.append((user, timeout))
        raise RuntimeError('private-failure')
    monkeypatch.setattr(runtime, 'checkpoint_story_memory', failing_checkpoint)
    with server_for(runtime) as (_, base):
        prefix = base.replace('/stories/', '/conversations/')
        for action in ('reset', 'close'):
            assert request(prefix, action, {'user_id': 'http-user', 'conversation_id': 'room'})[0] == 200
    assert calls == [('http-user', 0.25), ('http-user', 0.25)]


@pytest.mark.parametrize('endpoint', ['evidence', 'conversation'])
def test_raw_reads_fence_concurrent_fact_invalidation(runtime, monkeypatch, endpoint):
    story_id = seed_story(runtime)
    if endpoint == 'evidence':
        owner, method = runtime.story_memory, 'evidence'
    else:
        owner, method = runtime.conversation_store, 'snapshot'
    original = getattr(owner, method)
    def read_then_invalidate(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime.memory_store.invalidate_answers('http-user')
        return result
    monkeypatch.setattr(owner, method, read_then_invalidate)
    with server_for(runtime) as (_, base):
        if endpoint == 'evidence':
            status, error, _ = request(base, 'evidence', {'story_id': story_id})
        else:
            status, error, _ = request(base.replace('/stories/', '/conversations/'), 'get',
                                        {'user_id': 'http-user', 'conversation_id': 'room'})
        assert status == 409
        assert error['error']['code'] in {'story_changed', 'memory_changed'}
        assert '전시' not in json.dumps(error, ensure_ascii=False)


def test_factory_http_turn_sync_new_session_and_reopened_cache_guard(tmp_path):
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator
    from test_story_orchestration import MemoryAwareProvider
    settings = Settings(provider='mock', database_path=str(tmp_path / 'factory-http.sqlite3'))
    provider = MemoryAwareProvider()
    with patch('malbut_agent_server.factory.build_provider', return_value=provider):
        runtime = build_orchestrator(settings, story_extractor=FakeExtractor())
    def turn(base, room, identifier, text):
        prefix = base.replace('/stories/', '/')
        assert request(prefix, 'conversations', {'user_id': 'http-user', 'conversation_id': room})[0] == 201
        body = {'user_id': 'http-user', 'conversation_id': room,
                'request_id': identifier, 'turn_id': identifier,
                'utterance': text, 'robot_state': {}, 'available_tools': []}
        status, result, _ = request(prefix, 'agent/respond', body)
        assert status == 200
        assert request(base, 'sync', {'timeout_seconds': 5})[0] == 200
        return body, result
    try:
        with server_for(runtime) as (_, base):
            assert request(base, 'enable', enable_body())[0] == 200
            turn(base, 'source', 'source-request', '전시의 작품이 좋아서 즐거웠어.')
            recalled_request, recalled_result = turn(
                base, 'recall', 'recall-request', '전시 이야기를 다시 들려줘.')
            assert recalled_result['decision']['message'] == '전시의 작품이 좋아서 즐거웠어.'
            assert provider.calls[-1]['story_memory_untrusted']['stories']
    finally:
        runtime.close()
    reopened_provider = MemoryAwareProvider()
    with patch('malbut_agent_server.factory.build_provider', return_value=reopened_provider):
        reopened = build_orchestrator(settings, story_extractor=FakeExtractor())
    try:
        with server_for(reopened) as (_, base):
            prefix = base.replace('/stories/', '/')
            status, cached, _ = request(prefix, 'agent/respond', recalled_request)
            assert status == 200
            assert cached['execution']['decision_id'] == recalled_result['execution']['decision_id']
            assert reopened_provider.calls == []
            policy = request(base, 'status')[1]['policy']
            assert request(base, 'disable', {'expected_revision': policy['revision']})[0] == 200
            status, error, _ = request(prefix, 'agent/respond', recalled_request)
            assert status == 409 and error['error']['code'] == 'memory_changed'
            assert reopened_provider.calls == []
            assert '즐거웠어' not in json.dumps(error, ensure_ascii=False)
    finally:
        reopened.close()
