"""Owner-registered Cloud key sync. No real HTTP, Cloud call or key in logs."""

import asyncio
import io
import os
import json
import stat
import sys
import types
import urllib.error

import pytest

from malbut_agent_server.adapters.outbound.homecam_fall_events import FallUploadError
from malbut_agent_server.adapters.outbound.homecam_fall_key import HomecamFallKeyClient
from malbut_agent_server.adapters.outbound.ollama_cloud_fall import OllamaCloudFallProvider
from malbut_agent_server.application.fall_key_sync import FallCloudKeySync
from malbut_agent_server.fall_runtime import FallKeySyncSettings, FallNodeSettings
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError

EXAMPLE = {
    'device_id': 'robot-a', 'journal_path': '/var/lib/malbut-falls/events.sqlite',
    'cloud_key_file': '/var/lib/malbut-falls/ollama-cloud.key', 'model': 'gemma4:31b',
    'image_topic': '/depth_cam/rgb0/image_raw', 'retention_s': 10, 'buffer_bytes': 16777216,
    'buffer_frames': 64, 'input_fps': 5, 'max_source_age_s': 1, 'control_lease_s': 5,
    'tracking': None, 'policy': {
        'retry_interval_s': 3, 'max_person_observation_age_s': 2, 'clip_window_s': 5,
        'max_frame_age_s': 2, 'max_calls_per_minute': 5, 'max_incidents': 10, 'max_images': 12},
}
SYNC = {'base_url': 'https://homecam.example.com', 'allow_hosts': ['homecam.example.com'],
        'token_file': '/etc/malbut-homecam.token'}


def test_key_sync_is_optional_and_strict():
    assert FallNodeSettings.parse(json.dumps(EXAMPLE)).key_sync is None
    parsed = FallNodeSettings.parse(json.dumps({**EXAMPLE, 'key_sync': SYNC})).key_sync
    assert parsed == FallKeySyncSettings('https://homecam.example.com', ('homecam.example.com',),
                                         parsed.token_file, 60.0)
    for change in ({'base_url': 'http://homecam.example.com'},
                   {'base_url': 'https://evil.example.com'}, {'token_file': 'relative.token'},
                   {'interval_s': 5}, {'interval_s': True}, {'allow_hosts': []}, {'extra': 1}):
        with pytest.raises(ValueError):
            FallNodeSettings.parse(json.dumps({**EXAMPLE, 'key_sync': {**SYNC, **change}}))


def test_provider_swaps_keys_without_restart():
    provider = OllamaCloudFallProvider(model='gemma4:31b', api_key=None)
    assert provider._blocked == 'cloud_auth_required'
    provider.replace_key('new-key-1234')
    assert provider._blocked is None and provider._api_key == 'new-key-1234'
    provider._blocked = 'cloud_quota_exhausted'
    provider.replace_key('another-key')
    assert provider._blocked is None
    provider.replace_key(None)
    assert provider._blocked == 'cloud_auth_required'
    with pytest.raises(ValueError):
        provider.replace_key('bad key')


def test_old_key_failure_does_not_block_a_replaced_key(monkeypatch):
    provider = OllamaCloudFallProvider(model='gemma4:31b', api_key='old-key-1234')

    class Response:
        status = 401

        async def __aenter__(self):
            provider.replace_key('new-key-5678')  # replaced while the old call is in flight
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, headers, **kwargs):
            assert headers['Authorization'] == 'Bearer old-key-1234'
            return Response()

    # A stand-in transport: aiohttp itself is optional in this test environment.
    fake = types.SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **kw: None,
                                 DummyCookieJar=lambda: None, ClientError=OSError)
    monkeypatch.setitem(sys.modules, 'aiohttp', fake)
    with pytest.raises(CloudFallProviderError):
        asyncio.run(provider._post(b'{}'))
    assert provider._blocked is None


class Client:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def sync(self, known, model):
        self.calls.append((known, model))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(tmp_path, *replies):
    applied = []
    client = Client(*replies)
    sync = FallCloudKeySync(client=client, key_file=tmp_path / 'cloud.key', model='gemma4:31b',
                            apply_key=applied.append)
    return sync, client, applied


def test_server_without_a_key_keeps_the_robot_file(tmp_path):
    (tmp_path / 'cloud.key').write_text('robot-own-key\n')
    sync, client, applied = make(tmp_path, (0, False, None))
    assert sync.apply(sync.fetch()) == 'unchanged'
    assert (tmp_path / 'cloud.key').read_text() == 'robot-own-key\n'
    assert applied == [] and client.calls == [(0, 'gemma4:31b')]


def test_new_key_is_written_0600_and_applied_then_survives_restart(tmp_path):
    sync, client, applied = make(tmp_path, (3, True, 'server-key-1234'), (3, False, None))
    assert sync.apply(sync.fetch()) == 'replaced'
    key = tmp_path / 'cloud.key'
    assert key.read_text() == 'server-key-1234\n'
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert applied == ['server-key-1234']
    assert sync.apply(sync.fetch()) == 'unchanged'
    assert client.calls == [(0, 'gemma4:31b'), (3, 'gemma4:31b')]
    restarted, client2, _ = make(tmp_path, (3, False, None))
    restarted.apply(restarted.fetch())
    assert client2.calls == [(3, 'gemma4:31b')]
    assert not list(tmp_path.glob('.*.tmp'))


def test_server_delete_removes_the_key_but_a_failure_does_not(tmp_path):
    (tmp_path / 'cloud.key').write_text('old\n')
    sync, _, applied = make(tmp_path, FallUploadError('upload_failed'),
                            FallUploadError('http_503'), (4, True, None))
    assert sync.apply(sync.fetch()) == 'kept'
    assert sync.apply(sync.fetch()) == 'kept'
    assert (tmp_path / 'cloud.key').exists()
    assert sync.failures == 2 and sync.last_error == 'http_503'
    assert sync.apply(sync.fetch()) == 'deleted'
    assert not (tmp_path / 'cloud.key').exists()
    assert applied == [None]


def test_unwritable_directory_keeps_the_current_key(tmp_path):
    folder = tmp_path / 'ro'
    folder.mkdir()
    sync, _, applied = make(folder, (2, True, 'server-key-1234'))
    folder.chmod(0o500)
    try:
        assert sync.apply(sync.fetch()) == 'write_failed'
    finally:
        folder.chmod(0o700)
    assert applied == [] and sync.key_version == 0 and sync.last_error == 'key_write_failed'


def client(responses):
    instance = HomecamFallKeyClient(
        base_url='https://homecam.example.com', device_id='robot-a',
        allowed_hosts={'homecam.example.com'}, device_token='secret-token')
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        body = io.BytesIO(json.dumps(item).encode())
        body.status = 200
        return body

    instance._opener.open = open_request
    return instance, requests


def test_key_client_posts_version_and_model_and_validates_the_reply():
    instance, requests = client([{'keyVersion': 2, 'changed': True, 'apiKey': 'server-key-1234'}])
    assert instance.sync(1, 'gemma4:31b') == (2, True, 'server-key-1234')
    request = requests[0]
    assert request.full_url == 'https://homecam.example.com/api/device/v1/fall-cloud-key'
    assert request.get_header('Authorization') == 'Bearer secret-token'
    assert request.get_header('X-malbut-device-id') == 'robot-a'
    assert json.loads(request.data) == {'knownVersion': 1, 'model': 'gemma4:31b'}
    for bad in ({'keyVersion': 2, 'changed': False, 'apiKey': 'server-key-1234'},
                {'keyVersion': -1, 'changed': False, 'apiKey': None},
                {'keyVersion': 2, 'changed': True, 'apiKey': 'bad key'},
                {'keyVersion': 2, 'changed': True, 'apiKey': None, 'extra': 1}):
        instance, _ = client([bad])
        with pytest.raises(FallUploadError):
            instance.sync(1, 'gemma4:31b')
    instance, _ = client([urllib.error.HTTPError('secret-url', 401, 'secret-token', {}, None)])
    with pytest.raises(FallUploadError) as caught:
        instance.sync(1, 'gemma4:31b')
    assert caught.value.code == 'http_401' and 'secret' not in str(caught.value)


def test_crash_leftovers_do_not_block_and_a_lost_key_file_is_refetched(tmp_path):
    sync, _, _ = make(tmp_path, (2, True, 'server-key-1234'))
    # A temp file from a crashed earlier run with any PID-like name.
    (tmp_path / f'.cloud.key.{os.getpid()}.tmp').write_text('stale')
    assert sync.apply(sync.fetch()) == 'replaced'
    assert not list(tmp_path.glob('.cloud.key.*.tmp'))[1:]
    (tmp_path / 'cloud.key').unlink()  # version file says 2, key is gone
    restarted, client, applied = make(tmp_path, (2, True, 'server-key-1234'))
    assert restarted.key_version == 0
    assert restarted.apply(restarted.fetch()) == 'replaced'
    assert client.calls == [(0, 'gemma4:31b')] and applied == ['server-key-1234']


def test_sync_errors_are_logged_once_per_code_without_secrets(tmp_path):
    import logging
    from malbut_agent_server.application import fall_key_sync
    messages = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    fall_key_sync.LOG.addHandler(handler)
    previous = fall_key_sync.LOG.level
    fall_key_sync.LOG.setLevel(logging.INFO)
    try:
        sync, _, _ = make(tmp_path, FallUploadError('http_401'), FallUploadError('http_401'),
                          FallUploadError('http_503'), (0, False, None))
        for _ in range(4):
            sync.fetch()
    finally:
        fall_key_sync.LOG.removeHandler(handler)
        fall_key_sync.LOG.setLevel(previous)
    assert messages == ['fall cloud key sync failed: http_401',
                        'fall cloud key sync failed: http_503', 'fall cloud key sync recovered']


def test_preflight_requires_a_private_key_directory(tmp_path):
    from malbut_agent_server.fall_preflight import key_directory_reason
    folder = tmp_path / 'keys'
    folder.mkdir(mode=0o700)
    assert key_directory_reason(folder / 'cloud.key') == 'ok'
    folder.chmod(0o770)
    assert key_directory_reason(folder / 'cloud.key') == 'writable_by_others'
    assert key_directory_reason(tmp_path / 'missing' / 'cloud.key') == 'not_accessible'
