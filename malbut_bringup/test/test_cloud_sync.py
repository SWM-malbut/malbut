"""Validate cloud contracts without ROS, internet access, or robot execution."""

import base64
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from malbut_bringup.cloud_sync import (
    capability_manifests, CloudClient, CloudError, CloudSync, map_payload, NoRedirect,
    panel_command, read_device_token, state_payload, validate_backend_url,
)
from malbut_bringup.web_panel import PanelData
from malbut_bringup.web_runtime import SavedMapCatalog


TOKEN = 'hc1.11111111-1111-4111-8111-111111111111.' + 'a' * 64
COMMAND_ID = '22222222-2222-4222-8222-222222222222'
OTHER_ID = '33333333-3333-4333-8333-333333333333'


@pytest.mark.parametrize('url', [
    'https://robot.example.com', 'https://robot.example.com/',
    'http://localhost:3000', 'http://127.0.0.1:3000', 'http://[::1]:3000',
])
def test_backend_url_accepts_tls_and_exact_loopback(url):
    """Match existing media-agent development and production conventions."""
    assert validate_backend_url(url) == url.rstrip('/')


@pytest.mark.parametrize('url', [
    '', 'http://192.168.1.2', 'http://localhost.evil', 'file:///tmp/socket',
    'https://user:secret@example.com', 'https://example.com?token=secret',
    'https://example.com#fragment', 'https://example.com/api',
    'https://example.com:70000', 'https://example.com\n', 'https://example.com\\host',
])
def test_backend_url_rejects_credentials_redirect_origins_and_lan_plaintext(url):
    """No device token may be configured for unsafe transports."""
    with pytest.raises(ValueError):
        validate_backend_url(url)


def test_token_requires_private_regular_file(tmp_path):
    """Reject readable-by-others credentials and symbolic link substitution."""
    filename = tmp_path / 'device.token'
    filename.write_text(TOKEN)
    filename.chmod(0o600)
    assert read_device_token(str(filename)) == TOKEN
    filename.chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        read_device_token(str(filename))
    filename.chmod(0o600)
    link = tmp_path / 'link.token'
    link.symlink_to(filename)
    with pytest.raises(ValueError, match='protected'):
        read_device_token(str(link))
    filename.write_text('not-a-device-credential')
    with pytest.raises(ValueError, match='format'):
        read_device_token(str(filename))


def test_redirects_are_not_followed_and_http_error_is_sanitized():
    """Never forward bearer authorization or report secret-bearing URLs."""
    assert NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil') is None
    client = CloudClient('https://robot.example.com', TOKEN)
    client.opener = Mock()
    client.opener.open.side_effect = HTTPError(
        'https://robot.example.com/' + TOKEN, 401, TOKEN, {}, None)
    with pytest.raises(CloudError, match='HTTP 401') as result:
        client.request('/api/device/v1/robot/state', 'POST', {})
    assert TOKEN not in str(result.value)


def test_client_uses_bounded_json_and_bearer_auth():
    """All requests use authenticated fixed paths with a finite timeout."""
    client = CloudClient('https://robot.example.com', TOKEN)
    response = Mock()
    response.read.return_value = b'{"commands": []}'

    @contextmanager
    def opened(request, timeout):
        assert request.get_header('Authorization') == 'Bearer ' + TOKEN
        assert timeout == 8.0
        yield response

    client.opener.open = opened
    assert client.request('/api/device/v1/robot/commands') == {'commands': []}
    response.read.assert_called_once_with(256 * 1024 + 1)
    response.read.return_value = b'x' * (256 * 1024 + 1)
    with pytest.raises(CloudError, match='size limit'):
        client.request('/api/device/v1/robot/commands')
    with pytest.raises(ValueError, match='API path'):
        client.request('//evil.example.com')


@pytest.mark.parametrize('operation,payload,expected', [
    ('runtime_start', {'mode': 'mapping'}, {'command': 'bringup_start', 'mode': 'mapping'}),
    ('runtime_start', {'mode': 'navigation', 'map': 'home.yaml'},
     {'command': 'bringup_start', 'mode': 'navigation', 'map': 'home.yaml'}),
    ('runtime_stop', {}, {'command': 'bringup_stop'}),
    ('mission_cancel', {}, {'command': 'cancel'}),
    ('mission_start', {'capability': 'patrol', 'arguments': {'thoroughness': 1}},
     {'command': 'start', 'capability': 'patrol', 'arguments': {'thoroughness': 1}}),
    ('mission_start', {'capability': 'navigate_to_pose',
                       'arguments': {'x': 1, 'y': -2, 'yaw': 0}},
     {'command': 'start', 'capability': 'navigate_to_pose',
      'arguments': {'x': 1, 'y': -2, 'yaw': 0}}),
    ('mission_start', {'capability': 'relocalize',
                       'arguments': {'method': 1, 'x': 1, 'y': 2, 'yaw': 0}},
     {'command': 'start', 'capability': 'relocalize',
      'arguments': {'method': 1, 'x': 1, 'y': 2, 'yaw': 0}}),
    ('manual_move', {'direction': 'left'}, {'command': 'nudge', 'direction': 'left'}),
    ('debug_mission_start', {'capability': 'get_weather', 'arguments': {}},
     {'command': 'debug_start', 'capability': 'get_weather', 'arguments': {}}),
])
def test_real_robot_commands_reuse_existing_validation(operation, payload, expected):
    """No Gazebo HTTP API, arbitrary launch command, or manager modification."""
    assert panel_command(operation, payload) == expected


@pytest.mark.parametrize('operation,payload', [
    ('navigation_start', {'previewToken': 'simulator-token'}),
    ('drive_mode_start', {'mode': 'roaming'}),
    ('runtime_stop', {'pid': 1}),
    ('runtime_start', {'command': 'cancel', 'mode': 'mapping'}),
    ('runtime_start', {'mode': 'navigation', 'map': '../home.yaml'}),
    ('mission_start', {'capability': 'shell', 'arguments': {}}),
    ('mission_start', {'capability': 'navigate_to_pose',
                       'arguments': {'x': True, 'y': 0, 'yaw': 0}}),
    ('mission_start', {'capability': 'navigate_to_pose',
                       'arguments': {'x': 1, 'y': 0, 'yaw': float('inf')}}),
    ('mission_start', {'capability': 'navigate_to_pose',
                       'arguments': {'x': 1, 'y': 0, 'yaw': 0, 'behavior_tree': '/tmp/tree'}}),
    ('mission_start', {'capability': 'relocalize', 'arguments': {'method': 1}}),
    ('manual_move', {'direction': 'forward', 'duration_s': 60}),
    ('manual_move', {'linear_x': 1.0}),
    ('debug_mission_start', {'capability': 'patrol'}),
])
def test_unsupported_or_unsafe_commands_fail_closed(operation, payload):
    """Do not translate unsupported simulator controls to physical commands."""
    with pytest.raises(ValueError):
        panel_command(operation, payload)


def _sync():
    bridge = SimpleNamespace(data=PanelData(), catalog=Mock(),
                             submit=Mock(return_value='request-id'), node=Mock())
    bridge.catalog.list_maps.return_value = []
    client = Mock()
    client.request.return_value = {'commands': []}
    return CloudSync(bridge, client), bridge, client


def test_receipts_report_queue_acceptance_and_retries_never_resubmit_goals():
    """A lost completion reply cannot execute the same movement twice."""
    sync, bridge, client = _sync()
    command = {'id': COMMAND_ID, 'operation': 'mission_start',
               'payload': {'capability': 'patrol', 'arguments': {'thoroughness': 1}}}
    sync.dispatch(command)
    receipt = sync.pending[COMMAND_ID]
    assert receipt == {'ok': True, 'result': {
        'accepted': True, 'requestId': 'request-id', 'status': 'queued',
        'robotInterface': 'malbut_manager_v1'}}
    client.request.side_effect = CloudError('temporary failure')
    with pytest.raises(CloudError):
        sync._complete_pending()
    sync.dispatch(command)
    bridge.submit.assert_called_once()
    client.request.side_effect = None
    sync._complete_pending()
    assert not sync.pending
    sync.dispatch(command)
    sync._complete_pending()
    bridge.submit.assert_called_once()


def _saved_map(directory, name):
    (directory / f'{name}.pgm').write_bytes(b'P5\n2 2\n255\n\xff\xff\x00\x80')
    (directory / f'{name}.yaml').write_text(
        f'image: {name}.pgm\nresolution: 0.05\norigin: [0, 0, 0]\nnegate: 0\n'
        'occupied_thresh: 0.65\nfree_thresh: 0.25\n')


def test_map_deletion_answers_directly_and_keeps_the_map_in_use(tmp_path):
    """Deleting a map is a file request; the saved map being driven on stays."""
    sync, bridge, _client = _sync()
    bridge.catalog = SavedMapCatalog(tmp_path)
    for name in ('home', 'office'):
        _saved_map(tmp_path, name)
    bridge.data.runtime = {**bridge.data.runtime, 'localization': {
        'mode': 'LOCALIZATION', 'map': str(tmp_path / 'home.yaml')}}
    sync.dispatch({'id': COMMAND_ID, 'operation': 'map_delete',
                   'payload': {'map': 'home.yaml'}})
    assert not sync.pending[COMMAND_ID]['ok']
    assert 'in use' in sync.pending[COMMAND_ID]['result']['error']
    sync.dispatch({'id': OTHER_ID, 'operation': 'map_delete',
                   'payload': {'map': 'office.yaml'}})
    assert sync.pending[OTHER_ID] == {'ok': True, 'result': {
        'deleted': 'office.yaml', 'files': ['office.yaml', 'office.pgm']}}
    assert (tmp_path / 'home.yaml').exists() and not (tmp_path / 'office.yaml').exists()
    bridge.submit.assert_not_called()


def test_zone_edits_are_saved_on_the_robot_and_uploaded_with_the_map(monkeypatch):
    """The robot writes the Zone file; the next map upload carries it back."""
    sync, bridge, _client = _sync()
    saved = Mock(return_value=2)
    monkeypatch.setattr('malbut_bringup.cloud_sync.save_zones', saved)
    sync.last_map_at = 50.0
    payload = {'map': 'home.yaml', 'zones': []}
    sync.dispatch({'id': COMMAND_ID, 'operation': 'zones_save', 'payload': payload})
    assert sync.pending[COMMAND_ID] == {'ok': True, 'result': {'saved': 2, 'map': 'home.yaml'}}
    assert saved.call_args.args[1:] == (bridge.catalog, payload)
    assert sync.last_map_at == 0.0
    bridge.submit.assert_not_called()


def test_diagnostics_and_ping_stay_within_the_device_body_limit():
    """Graph queries run on the ROS executor; oversized results become an error."""
    sync, bridge, _client = _sync()
    bridge.call = Mock(return_value={'nodes': ['/system_manager']})
    bridge.diagnostics = Mock()
    sync.capabilities = [{'id': 'patrol'}]
    sync.dispatch({'id': COMMAND_ID, 'operation': 'robot_diagnostics', 'payload': {}})
    result = sync.pending[COMMAND_ID]
    assert result['ok'] and result['result']['nodes'] == ['/system_manager']
    assert result['result']['capabilities'] == [{'id': 'patrol'}]
    bridge.call.assert_called_once_with(bridge.diagnostics)
    bridge.call.return_value = {'nodes': ['x' * 1000] * 100}
    sync.dispatch({'id': OTHER_ID, 'operation': 'robot_diagnostics', 'payload': {}})
    assert sync.pending[OTHER_ID] == {
        'ok': False, 'result': {'error': 'Result exceeds the cloud size limit'}}
    ping = '44444444-4444-4444-8444-444444444444'
    sync.dispatch({'id': ping, 'operation': 'robot_ping', 'payload': {}})
    assert sync.pending[ping]['result']['pong'] is True
    bridge.submit.assert_not_called()


def test_capability_list_carries_manifest_inputs_for_the_debug_runner():
    """The debug runner starts from each registered capability's defaults."""
    directory = Path(__file__).parents[2] / 'malbut_interfaces/capabilities'
    capabilities = {item['id']: item for item in capability_manifests(directory)}
    assert capabilities['relocalize']['fields']['method'] == {'type': 'uint8', 'default': 0}
    assert capabilities['patrol']['resources'] == ['BASE']


def test_read_only_tick_sends_status_but_no_goal():
    """Starting a bridge and viewing the app never starts Bringup or movement."""
    sync, bridge, client = _sync()
    sync.tick()
    bridge.submit.assert_not_called()
    assert client.request.call_args_list[0].args[:2] == (
        '/api/device/v1/robot/state', 'POST')


def test_state_contract_omits_local_log_files_and_map_paths():
    """Expose runtime progress, own requests and map IDs, not local file contents."""
    snapshot = PanelData().snapshot()
    snapshot['zones'] = {'state': 'APPLIED', 'map': '/secret/home.yaml', 'zones': 2}
    snapshot['runtime'].update(state='RUNNING', mode='navigation', ready=True,
                               log_path='/secret/runtime.log', log_tail='private contents',
                               localization={'mode': 'LOCALIZATION',
                                             'map': '/secret/home.yaml',
                                             'message': 'saved pose confirmed'})
    pose = {'x': 1, 'y': 2, 'yaw': 0}
    payload = state_payload(snapshot, {'active': True, 'pose': pose, 'version': 7},
                            [{'id': 'home.yaml', 'name': 'home', 'path': '/secret/home.yaml'}])
    assert payload['nav2']['robot_interface'] == 'malbut_manager_v1'
    assert payload['nav2']['runtime_mode'] == 'navigation'
    assert payload['pose'] == pose
    assert payload['mapRevision'] == 7
    assert payload['target']['runtime']['localization'] == {
        'mode': 'LOCALIZATION', 'map': 'home.yaml', 'message': 'saved pose confirmed'}
    assert payload['target']['zones'] == {'state': 'APPLIED', 'map': 'home.yaml', 'zones': 2}
    assert '/secret' not in json.dumps(payload)
    assert 'private contents' not in json.dumps(payload)
    assert state_payload(snapshot, {'active': False, 'pose': pose}, [])['pose'] is None


def test_state_and_feedback_fit_server_body_limit():
    """Large ROS feedback or map catalogs cannot prevent command polling."""
    snapshot = PanelData().snapshot()
    snapshot['requests'] = [{'id': str(number), 'state': 'RUNNING',
                             'feedback': {'large': 'x' * 100_000}}
                            for number in range(32)]
    maps = [{'id': 'a' * 255 + '.yaml', 'name': 'b' * 255} for _ in range(1000)]
    result = state_payload(snapshot, {'active': False}, maps)
    assert len(json.dumps(result).encode()) < 64 * 1024
    assert len(result['target']['requests']) <= 16
    assert result['target']['requests'][0]['feedback'] == {'truncated': True}


def test_map_contract_uses_matching_geometry_and_stable_content_revision():
    """Repeated identical map messages do not reupload; mapping remains a draft."""
    metadata = {'version': 1, 'width': 2, 'height': 3, 'resolution': 0.05,
                'origin': {'x': -1, 'y': -2, 'yaw': 0.5}, 'frame_id': 'map'}
    runtime = {'mode': 'navigation', 'map': 'home.yaml'}
    first = map_payload(metadata, b'png bytes', runtime)
    second = map_payload({**metadata, 'version': 2}, b'png bytes', runtime)
    assert first == second
    assert first['finalized']
    assert first['geometry'] == {
        'width': 2, 'height': 3, 'resolution': 0.05,
        'originX': -1, 'originY': -2, 'originYaw': 0.5,
    }
    assert base64.b64decode(first['previewBase64']) == b'png bytes'
    zones = {'format': 'malbut-semantic-zones-v1', 'map': 'home.yaml',
             'editable': True, 'zones': [], 'message': ''}
    edited = map_payload(metadata, b'png bytes', runtime, zones)
    assert edited['semanticZones'] == zones and edited['revision'] != first['revision']
    draft = map_payload(metadata, b'png bytes', {'mode': 'mapping'})
    assert not draft['finalized'] and draft['revision'].startswith('live-')
    with pytest.raises(ValueError, match='geometry'):
        map_payload({**metadata, 'width': 8193}, b'png bytes', runtime)


def test_invalid_map_does_not_prevent_command_polling():
    """A preview limitation must not disable remote mission cancellation."""
    sync, bridge, client = _sync()
    bridge.data.map_snapshot = Mock(return_value={
        'available': True, 'active': True, 'version': 1})
    bridge.data.map_cache.png = Mock(side_effect=ValueError('Bad PNG'))
    sync.tick()
    assert any(call.args == ('/api/device/v1/robot/commands',)
               for call in client.request.call_args_list)


def test_stopping_bridge_does_not_claim_or_dispatch_more_commands():
    """Shutdown closes the remote command intake before local cancellation."""
    sync, bridge, client = _sync()
    sync.stop_event.set()
    sync.tick()
    client.request.assert_not_called()
    bridge.submit.assert_not_called()
