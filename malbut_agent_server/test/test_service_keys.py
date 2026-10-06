"""Offline checks for keys the owner sets on the web (SWM25-235). No network, no real keys."""

import io
import json
import os
import urllib.error
import urllib.request

import pytest

from malbut_agent_server.adapters.outbound.homecam_fall_events import FallUploadError
from malbut_agent_server.adapters.outbound.homecam_service_keys import HomecamServiceKeyClient
from malbut_agent_server.application.service_key_sync import ServiceKeySync
from malbut_agent_server.config import Settings
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.providers.reliable import (
    CircuitState, NormalizedProviderError, ProviderFailureCode, ReliableProvider,
)
from malbut_agent_server.ros_key_sync import KeyHealthBoard, settings_from_env
from malbut_agent_server.schemas import AgentDecision, AgentRequest, ProviderResult
from malbut_agent_server.service_keys import ManagedKey, read_version
from malbut_agent_server.tools import select_tool_specs

TEAM = 'team-test-key-0001'
OWNER = 'owner-test-key-0002'


def managed(tmp_path, team=TEAM):
    return ManagedKey('openai', environ={'OPENAI_API_KEY': team}, directory=tmp_path)


def web_sets(tmp_path, version, key=None):
    """Write the files key_sync writes."""
    if key is not None:
        (tmp_path / 'openai.key').write_text(key + '\n')
    (tmp_path / 'openai.key.version').write_text(
        json.dumps({'keyVersion': version, 'deleted': key is None}))


# --- ManagedKey: which key is used ---------------------------------------------------------

def test_without_web_files_the_team_key_is_used(tmp_path):
    assert managed(tmp_path).current() == TEAM
    assert managed(tmp_path, team='').current() == ''


def test_owner_key_replaces_team_key_without_restart(tmp_path):
    key = managed(tmp_path)
    first = key.generation
    web_sets(tmp_path, 1, OWNER)
    assert key.current() == OWNER
    assert key.generation == first + 1
    assert key.generation == first + 1  # unchanged files: same generation


def test_deleted_on_web_means_no_key_never_the_team_key(tmp_path):
    key = managed(tmp_path)
    web_sets(tmp_path, 1, OWNER)
    assert key.current() == OWNER
    (tmp_path / 'openai.key').unlink()
    web_sets(tmp_path, 2)
    assert key.current() == ''
    assert key.managed


def test_unusable_key_file_counts_as_no_key(tmp_path):
    web_sets(tmp_path, 1, 'short')
    assert managed(tmp_path).current() == ''


def test_health_listeners_hear_only_changes_and_never_the_key(tmp_path):
    key = managed(tmp_path)
    heard = []
    key.add_listener(lambda *event: heard.append(event))
    key.report('invalid', 'authentication_failed')
    key.report('invalid', 'authentication_failed')
    key.report('ok')
    key.report('invalid', 'Not A Code')
    assert heard == [('openai', 'invalid', 'authentication_failed'), ('openai', 'ok', None),
                     ('openai', 'invalid', None)]
    late = []
    key.add_listener(lambda *event: late.append(event))
    assert late == [('openai', 'invalid', None)]
    assert TEAM not in repr(key)
    with pytest.raises(ValueError):
        key.report('broken')


# --- ServiceKeySync: writing what the web says -----------------------------------------------

class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def sync(self, known, model, health):
        self.calls.append((known, model, health))
        if self.error is not None:
            raise self.error
        return self.reply


def reply(openai=(0, False, None), kma=(0, False, None)):
    return {'openai': openai, 'kma': kma}


def test_version_zero_leaves_the_team_key_in_use(tmp_path):
    sync = ServiceKeySync(client=FakeClient(reply()), directory=tmp_path, model='gpt-test')
    assert sync.apply(sync.fetch({})) == {'openai': 'unchanged', 'kma': 'unchanged'}
    assert not any(tmp_path.iterdir())
    assert managed(tmp_path).current() == TEAM


def test_new_key_is_written_private_then_deleted(tmp_path):
    folder = tmp_path / 'keys'
    sync = ServiceKeySync(client=FakeClient(reply(openai=(3, True, OWNER))), directory=folder)
    assert sync.apply(sync.fetch({}))['openai'] == 'replaced'
    assert (folder / 'openai.key').read_text().strip() == OWNER
    assert oct(os.stat(folder / 'openai.key').st_mode & 0o777) == '0o600'
    assert read_version(folder / 'openai.key.version') == (3, False)
    assert sync.known() == {'openai': 3, 'kma': 0}
    assert managed(folder).current() == OWNER

    sync._client = FakeClient(reply(openai=(4, True, None)))
    assert sync.apply(sync.fetch({}))['openai'] == 'deleted'
    assert not (folder / 'openai.key').exists()
    assert read_version(folder / 'openai.key.version') == (4, True)
    assert sync.known() == {'openai': 4, 'kma': 0}
    assert managed(folder).current() == ''


def test_lost_key_file_is_asked_for_again(tmp_path):
    web_sets(tmp_path, 3, OWNER)
    (tmp_path / 'openai.key').unlink()
    (tmp_path / 'openai.key.version').write_text(json.dumps({'keyVersion': 3, 'deleted': False}))
    assert ServiceKeySync(client=None, directory=tmp_path).known()['openai'] == 0


def test_failed_sync_keeps_the_last_key_and_sends_health(tmp_path):
    web_sets(tmp_path, 3, OWNER)
    client = FakeClient(error=FallUploadError('http_503'))
    sync = ServiceKeySync(client=client, directory=tmp_path, model='gpt-test')
    health = {'openai': ('invalid', 'authentication_failed')}
    assert sync.apply(sync.fetch(health)) == {'openai': 'kept', 'kma': 'kept'}
    assert client.calls == [({'openai': 3, 'kma': 0}, 'gpt-test', health)]
    assert sync.last_error == 'http_503'
    assert managed(tmp_path).current() == OWNER


# --- HomecamServiceKeyClient: the request and a strict reply ---------------------------------

class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def client_with(body=None, error=None):
    client = HomecamServiceKeyClient(base_url='https://malbut.example', device_id='robot-1',
                                     device_token='device-token', allowed_hosts={'malbut.example'})
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            if error is not None:
                raise error
            return Response(json.dumps(body).encode())

    client._opener = Opener()
    return client, sent


def test_client_posts_versions_model_and_health_only():
    good = {'openai': {'keyVersion': 2, 'changed': True, 'apiKey': OWNER},
            'kma': {'keyVersion': 0, 'changed': False, 'apiKey': None}}
    client, sent = client_with(good)
    result = client.sync({'openai': 1, 'kma': 0}, 'gpt-5.6-luna',
                         {'openai': ('quota', 'insufficient_quota'), 'fall': ('ok', None)})
    assert result == {'openai': (2, True, OWNER), 'kma': (0, False, None)}
    request = sent[0]
    assert request.full_url == 'https://malbut.example/api/device/v1/service-keys'
    assert request.get_header('X-malbut-device-id') == 'robot-1'
    assert json.loads(request.data) == {
        'known': {'openai': 1, 'kma': 0}, 'models': {'openai': 'gpt-5.6-luna'},
        'health': {'openai': {'state': 'quota', 'code': 'insufficient_quota'},
                   'fall': {'state': 'ok', 'code': None}}}


@pytest.mark.parametrize('body', [
    {'openai': {'keyVersion': 2, 'changed': True, 'apiKey': OWNER}},
    {'openai': {'keyVersion': 2, 'changed': False, 'apiKey': OWNER},
     'kma': {'keyVersion': 0, 'changed': False, 'apiKey': None}},
    {'openai': {'keyVersion': -1, 'changed': False, 'apiKey': None},
     'kma': {'keyVersion': 0, 'changed': False, 'apiKey': None}},
    {'openai': {'keyVersion': 2, 'changed': True, 'apiKey': 'has space key'},
     'kma': {'keyVersion': 0, 'changed': False, 'apiKey': None}},
])
def test_client_rejects_any_unexpected_reply(body):
    client, _ = client_with(body)
    with pytest.raises(FallUploadError) as raised:
        client.sync({}, None, {})
    assert raised.value.code == 'invalid_ack'


def test_client_http_errors_become_codes():
    error = urllib.error.HTTPError('https://malbut.example', 401, 'no', {}, io.BytesIO(b'secret'))
    client, _ = client_with(error=error)
    with pytest.raises(FallUploadError) as raised:
        client.sync({}, None, {})
    assert raised.value.code == 'http_401'


# --- key_sync: health board and settings ------------------------------------------------------

def test_health_board_keeps_latest_and_reads_fall_results_once():
    board = KeyHealthBoard()
    board.from_message('{"service":"openai","state":"invalid","code":"authentication_failed"}')
    board.from_message('{"service":"openai","state":"broken"}')
    board.from_message('not json')
    board.from_message('{"service":"fall","state":"ok"}')
    board.from_fall_status('failed', 'req-1', 'cloud_quota_exhausted')
    assert board.snapshot() == {'openai': ('invalid', 'authentication_failed'),
                                'fall': ('quota', 'cloud_quota_exhausted')}
    board.from_fall_status('completed', 'req-2', '')
    board.from_fall_status('failed', 'req-3', 'network_failed')
    assert board.snapshot()['fall'] == ('ok', None)
    board.from_fall_status('running', 'req-4', 'cloud_auth_required')
    board.from_fall_status('failed', 'req-5', 'cloud_auth_required')
    assert board.snapshot()['fall'] == ('invalid', 'cloud_auth_required')


def test_key_sync_needs_the_web_settings_and_defaults_to_the_homecam_device():
    assert settings_from_env({}) is None
    assert settings_from_env({'HOMECAM_BACKEND_URL': 'https://malbut.example'}) is None
    assert settings_from_env({'HOMECAM_BACKEND_URL': 'https://malbut.example',
                              'HOMECAM_DEVICE_TOKEN_FILE': '/token'}) == (
        'https://malbut.example', 'malbut.example', 'jetson-homecam', '/token')


# --- Dialogue: no key, wrong key, no credit ---------------------------------------------------

def _request():
    return AgentRequest.from_dict({
        'request_id': 'key-test', 'user_id': 'u', 'conversation_id': 'c', 'turn_id': 't',
        'utterance': '안녕', 'robot_state': {'battery_percent': 80, 'navigation_available': True,
                                           'localization_ok': True},
        'available_tools': ['navigate']})


def _ok_response(*args):
    return {'id': 'resp', 'status': 'completed', 'model': 'm',
            'output': [{'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': json.dumps({
                    'type': 'message', 'message': '안녕하세요', 'reason': 'r', 'confidence': 1.0},
                    ensure_ascii=False)}]}],
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}


def adapter(key, transport):
    return OpenAIResponsesProvider(api_key=key, model='m', transport=transport,
                                   token_counter=lambda payload: 1)


def complete(provider):
    return provider.complete(_request(), [], [], select_tool_specs(['navigate']))


def test_missing_key_fails_before_any_request(tmp_path):
    key = managed(tmp_path, team='')
    calls = []
    with pytest.raises(NormalizedProviderError) as raised:
        complete(adapter(key, lambda *args: calls.append(args)))
    assert raised.value.failure.code is ProviderFailureCode.MISSING_CREDENTIALS
    assert calls == [] and key.health == ('missing', 'missing_api_key')


def test_each_request_sends_the_current_key_and_reports_ok(tmp_path):
    key = managed(tmp_path)
    sent = []

    def transport(url, headers, payload, timeout):
        sent.append(headers['Authorization'])
        return _ok_response()

    provider = adapter(key, transport)
    complete(provider)
    web_sets(tmp_path, 1, OWNER)
    complete(provider)
    assert sent == ['Bearer ' + TEAM, 'Bearer ' + OWNER]
    assert key.health == ('ok', None)
    assert OWNER not in repr(provider)


def _http_error(status, body):
    return urllib.error.HTTPError('https://api.openai.com/v1/responses', status, 'x', {},
                                  io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize('status, body, health', [
    (401, {'error': {'code': 'invalid_api_key'}}, ('invalid', 'authentication_failed')),
    (429, {'error': {'code': 'insufficient_quota', 'type': 'insufficient_quota'}},
     ('quota', 'insufficient_quota')),
    (429, {'error': {'code': 'rate_limit_exceeded'}}, None),
])
def test_http_failures_are_classified_from_the_real_transport(
        tmp_path, monkeypatch, status, body, health):
    key = managed(tmp_path)
    provider = adapter(key, None)

    class Opener:
        def open(self, request, timeout):
            raise _http_error(status, body)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: Opener())
    with pytest.raises(Exception):
        complete(provider)
    assert key.health == health


class _Scripted(AgentProvider):
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def complete(self, request, memories, conversation_turns, tools, conversation_summary=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _result():
    return ProviderResult(decision=AgentDecision(type='message', message='응답', reason='r',
                                                 confidence=1.0),
                          provider='p', model='m', latency_ms=1.0)


@pytest.mark.parametrize('code', [ProviderFailureCode.QUOTA,
                                  ProviderFailureCode.MISSING_CREDENTIALS,
                                  ProviderFailureCode.AUTHENTICATION])
def test_key_failures_do_not_try_fallback_models_with_the_same_key(code):
    primary = _Scripted([NormalizedProviderError(code)])
    fallback = _Scripted([_result()])
    result = ReliableProvider([primary, fallback], sleep=lambda s: None).complete(
        _request(), [], [], select_tool_specs(['navigate']))
    assert result.decision.reason == 'provider_unavailable'
    assert (primary.calls, fallback.calls) == (1, 0)


def test_a_new_key_closes_the_circuit_the_old_key_opened():
    generation = [1]
    primary = _Scripted([NormalizedProviderError(ProviderFailureCode.AUTHENTICATION), _result()])
    provider = ReliableProvider([primary], failure_threshold=1, sleep=lambda s: None,
                                credential_generation=lambda: generation[0])
    tools = select_tool_specs(['navigate'])
    provider.complete(_request(), [], [], tools)
    assert provider.circuit_state(0) is CircuitState.OPEN
    provider.complete(_request(), [], [], tools)
    assert primary.calls == 1  # still open: the same wrong key is not retried
    generation[0] = 2
    assert provider.complete(_request(), [], [], tools).decision.message == '응답'
    assert primary.calls == 2


# --- Startup without a key ------------------------------------------------------------------

def test_dialogue_starts_without_key_but_the_http_server_still_needs_one():
    settings = Settings(provider='openai', openai_api_key='')
    settings.validate_for_dialogue(require_api_key=False)
    with pytest.raises(ValueError):
        settings.validate_for_dialogue()
