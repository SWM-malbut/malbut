"""Test the LAN panel without starting ROS, robot nodes, or motion."""

from concurrent.futures import Future
import http.client
import json
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup.web_panel import (
    image_jpeg, live_zone_map, mission_arguments, PanelData, PanelServer, RosBridge,
    save_zones, validate_command, zone_view,
)


def _command(capability='autoslam', arguments=None):
    return {'command': 'start', 'capability': capability,
            'arguments': arguments or {'map_name': 'home2'}}


@pytest.mark.parametrize('payload', [
    {'command': 'launch', 'capability': 'autoslam', 'arguments': {}},
    _command('shell', {'command': 'anything'}),
    _command(arguments={'map_name': '../home'}),
    _command(arguments={'map_name': 'home.yaml'}),
    _command('patrol', {'thoroughness': True}),
    _command('follow_person', {'target_mode': 0, 'target_person_id': '',
                               'desired_distance_m': float('nan')}),
    _command('follow_person', {'target_mode': 1, 'target_person_id': '',
                               'desired_distance_m': 1.0}),
    {'command': 'bringup_start', 'mode': 'navigation', 'map': '../home.yaml'},
    {'command': 'bringup_start', 'mode': 'navigation', 'map': '/tmp/home.yaml'},
    {'command': 'bringup_start', 'mode': 'shell'},
    {'command': 'bringup_stop', 'pid': 1},
    {'command': 'teleop', 'linear_x': 0.1, 'linear_y': 0.0, 'angular_z': 0.0, 'hold_s': 5.0},
    {'command': 'teleop', 'linear_x': 0.1, 'linear_y': 0.0, 'angular_z': 0.0, 'hold_s': 0.0},
    _command('relocalize', {'method': 1}),
    _command('relocalize', {'method': 0, 'x': 1.0, 'y': 0.0, 'yaw': 0.0}),
    _command('relocalize', {'method': 3}),
    {'command': 'debug_start', 'capability': '../shell', 'arguments': {}},
    {'command': 'debug_start', 'capability': 'patrol', 'arguments': ['thoroughness']},
    {'command': 'debug_start', 'capability': 'patrol', 'arguments': {'note': 'x' * 9000}},
])
def test_invalid_commands_cannot_reach_ros(payload):
    """Reject arbitrary ROS commands, paths and invalid typed inputs."""
    with pytest.raises(ValueError):
        validate_command(payload)


def test_valid_commands_are_not_launched_by_validation():
    """Pure validation changes no runtime state."""
    payload = _command()
    assert validate_command(payload) == payload
    assert validate_command({'command': 'cancel'}) == {'command': 'cancel'}
    assert validate_command(_command('patrol', {'thoroughness': 2}))
    assert validate_command({'command': 'bringup_start', 'mode': 'mapping'})
    assert validate_command({'command': 'bringup_start', 'mode': 'navigation',
                             'map': 'home.yaml'})
    assert validate_command({'command': 'bringup_stop'})


def test_follow_distance_minimum_matches_the_web_input():
    """The web request and displayed input both allow 0.2 m, not less."""
    payload = _command('follow_person', {
        'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 0.2,
    })
    assert validate_command(payload) == payload
    for distance in (0.19, 0.0, -1.0, float('nan'), float('inf')):
        payload['arguments']['desired_distance_m'] = distance
        with pytest.raises(ValueError, match='at least 0.2 m'):
            validate_command(payload)
    page = Path(__file__).parents[1] / 'malbut_bringup/web_panel.html'
    assert 'id="distance" type="number" min="0.2"' in page.read_text()


def test_history_and_pending_requests_are_bounded():
    """Never grow history indefinitely or forget outstanding requests."""
    data = PanelData()
    for _ in range(100):
        request_id = data.register(_command())
        data.update(request_id, state='SUCCEEDED')
    assert len(data.requests) <= 32
    for _ in range(16):
        data.register(_command())
    with pytest.raises(ValueError, match='outstanding'):
        data.register(_command())
    assert len(data.requests) <= 32


def test_cloud_map_palette_is_explicit_and_lan_default_is_unchanged(monkeypatch):
    """Only callers opting into the cloud map change the cache's display mode."""
    cache = Mock()
    monkeypatch.setattr('malbut_bringup.web_panel.MapCache', cache)
    PanelData()
    cache.assert_called_with(palette='costmap')
    PanelData(map_palette='map')
    cache.assert_called_with(palette='map')


def test_frame_encoded_only_when_requested_and_once_per_frame(monkeypatch):
    """Camera callbacks do no JPEG work, and HTTP readers share one encoding."""
    data = PanelData()
    encoder = Mock(return_value=b'jpeg')
    monkeypatch.setattr('malbut_bringup.web_panel.image_jpeg', encoder)
    data.receive_frame('raw', object())
    encoder.assert_not_called()
    assert data.jpeg('raw') == b'jpeg'
    assert data.jpeg('raw') == b'jpeg'
    encoder.assert_called_once()
    data.receive_frame('raw', object())
    data.jpeg('raw')
    assert encoder.call_count == 2


def test_stale_images_are_not_reported_as_live():
    """A stopped camera produces an explicit error instead of frozen live video."""
    data = PanelData()
    data.frames['raw'] = (0, object())
    with pytest.raises(ValueError, match='recent'):
        data.jpeg('raw')


def test_jpeg_accepts_rgb_row_padding():
    """Respect sensor_msgs/Image.step rather than assuming packed camera rows."""
    image = SimpleNamespace(encoding='rgb8', width=1, height=2, step=4,
                            data=bytes([255, 0, 0, 99, 0, 255, 0, 99]))
    assert image_jpeg(image).startswith(b'\xff\xd8')
    image.step = 2
    with pytest.raises(ValueError, match='stride'):
        image_jpeg(image)


@pytest.fixture
def http_server():
    """Run only the local HTTP server with a fake command dispatcher."""
    submit = Mock(return_value='request-id')
    server = PanelServer(('127.0.0.1', 0), PanelData(), submit, 'test-secret')
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield server, submit
    server.shutdown()
    thread.join()
    server.server_close()


def _request(server, method, path, payload=None, **headers):
    client = http.client.HTTPConnection(*server.server_address, timeout=2)
    client.request(method, path, body=json.dumps(payload) if payload else None,
                   headers=headers)
    response = client.getresponse()
    result = (response.status, response.read())
    client.close()
    return result


def test_page_load_is_read_only_and_does_not_expose_token(http_server):
    """Anonymous page loads expose no image, state or credential, and send no Goal."""
    server, submit = http_server
    status, page = _request(server, 'GET', '/')
    assert status == 200
    assert b'test-secret' not in page
    assert _request(server, 'GET', '/api/status')[0] == 401
    assert _request(server, 'GET', '/api/image/raw')[0] == 401
    assert _request(server, 'GET', '/api/command')[0] == 401
    for path in ('/api/maps', '/api/map', '/api/map/image'):
        assert _request(server, 'GET', path)[0] == 401
    submit.assert_not_called()


def test_commands_require_authentication_and_same_origin(http_server):
    """Cross-site pages and unauthenticated clients cannot move the robot."""
    server, submit = http_server
    headers = {'Content-Type': 'application/json'}
    assert _request(server, 'POST', '/api/command', _command(), **headers)[0] == 401
    headers['Authorization'] = 'Bearer test-secret'
    headers['Origin'] = 'http://other-host.invalid'
    assert _request(server, 'POST', '/api/command', _command(), **headers)[0] == 403
    submit.assert_not_called()
    headers['Origin'] = f'http://127.0.0.1:{server.server_port}'
    assert _request(server, 'POST', '/api/command', _command(), **headers)[0] == 202
    submit.assert_called_once_with(_command())


def test_safe_get_and_bad_post_never_dispatch(http_server):
    """Even authenticated callers must use a valid POST for mutation."""
    server, submit = http_server
    headers = {'Authorization': 'Bearer test-secret', 'Content-Type': 'application/json'}
    assert _request(server, 'GET', '/api/command', **headers)[0] == 404
    assert _request(server, 'POST', '/api/command', {'command': 'anything'},
                    **headers)[0] == 400
    submit.assert_not_called()


def _future(result):
    future = Future()
    future.set_result(result)
    return future


def test_map_api_is_authenticated_and_does_not_launch(http_server):
    """A PNG carries matching geometry without requiring a still-current version."""
    server, submit = http_server
    headers = {'Authorization': 'Bearer test-secret'}
    server.catalog = Mock()
    server.catalog.list_maps.return_value = [{'id': 'home.yaml', 'name': 'home'}]
    status, content = _request(server, 'GET', '/api/maps', **headers)
    assert status == 200 and json.loads(content)['maps'][0]['id'] == 'home.yaml'
    status, content = _request(server, 'GET', '/api/map', **headers)
    assert status == 200 and not json.loads(content)['available']
    assert _request(server, 'GET', '/api/map/image', **headers)[0] == 503
    server.data.map_cache = Mock()
    metadata = {'version': 7, 'width': 3, 'height': 2, 'frame_id': 'map'}
    server.data.map_cache.png.return_value = (metadata, b'PNG bytes')
    client = http.client.HTTPConnection(*server.server_address, timeout=2)
    client.request('GET', '/api/map/image', headers=headers)
    response = client.getresponse()
    assert response.status == 200 and response.read() == b'PNG bytes'
    assert json.loads(response.getheader('X-Map-Metadata')) == metadata
    client.close()
    server.data.map_cache.png.assert_called_once_with()
    submit.assert_not_called()


def _bridge(manager_ready=False, autoslam_ready=True):
    bridge = object.__new__(RosBridge)
    bridge.data = PanelData()
    bridge.node = Mock()
    bridge.commands = queue.Queue(maxsize=64)
    bridge.handles = {}
    bridge.cancel_pending = set()
    bridge.runtime = None
    bridge.stopping_runtime = None
    bridge.runtime_message = ''
    bridge.startup_status = {}
    bridge.speech_ready = False
    bridge.action_status = {}
    bridge.cancel_clients = {}
    bridge.cancel_request = SimpleNamespace
    bridge.localization = {}
    bridge.load_map = Mock()
    bridge.load_map.service_is_ready.return_value = False
    bridge.load_map_request = SimpleNamespace
    bridge.start_mapping = Mock()
    bridge.start_mapping.service_is_ready.return_value = False
    bridge.start_mapping_request = SimpleNamespace
    bridge.tf_buffer = Mock()
    bridge.topics = {'map_topic': '/map'}
    bridge.guard = Mock()
    bridge.teleop_hold_s = 0.5
    bridge.to_dict = lambda message: vars(message)
    bridge.auto_goal = SimpleNamespace
    bridge.mission_goal = SimpleNamespace
    handle = Mock(accepted=True)
    handle.get_result_async.return_value = Future()
    handle.cancel_goal_async.return_value = _future(SimpleNamespace(goals_canceling=[1]))
    bridge.clients = {}
    for name, ready in [('manager', manager_ready), ('autoslam', autoslam_ready)]:
        client = Mock()
        client.server_is_ready.return_value = ready
        client.send_goal_async.return_value = _future(handle)
        bridge.clients[name] = client
    return bridge, handle


def test_autoslam_uses_manager_when_available():
    """Keep BASE arbitration through the manager whenever its server is ready."""
    bridge, _ = _bridge(manager_ready=True)
    request_id = bridge.submit(_command())
    bridge._drain()
    assert bridge.data.requests[request_id]['route'] == 'manager'
    goal = bridge.clients['manager'].send_goal_async.call_args.args[0]
    assert goal.capability_id == 'autoslam'
    bridge.clients['autoslam'].send_goal_async.assert_not_called()


def test_navigation_arguments_use_public_goal_without_arbitrary_behavior_tree():
    """Translate finite map coordinates, leaving Nav2's default behavior intact."""
    goal = mission_arguments('navigate_to_pose', {'x': 1, 'y': -2, 'yaw': 0})
    assert goal['pose']['header'] == {'frame_id': 'map'}
    assert goal['pose']['pose']['position'] == {'x': 1.0, 'y': -2.0, 'z': 0.0}
    assert goal['pose']['pose']['orientation']['w'] == 1.0
    assert goal['behavior_tree'] == ''


def test_navigation_requires_fresh_pose_then_uses_manager():
    """No direct Nav2 bypass exists when map/pose or the manager is unavailable."""
    bridge, _ = _bridge(manager_ready=True)
    command = _command('navigate_to_pose', {'x': 1, 'y': -2, 'yaw': 0})
    request_id = bridge.submit(command)
    bridge._drain()
    assert bridge.data.requests[request_id]['state'] == 'ERROR'
    bridge.clients['manager'].send_goal_async.assert_not_called()
    bridge.data.map_snapshot = Mock(return_value={'active': True, 'frame_id': 'map'})
    bridge._robot_pose = Mock(return_value={'x': 0, 'y': 0, 'yaw': 0})
    request_id = bridge.submit(command)
    bridge._drain()
    assert bridge.data.requests[request_id]['state'] == 'RUNNING'
    goal = bridge.clients['manager'].send_goal_async.call_args.args[0]
    assert goal.capability_id == 'navigate_to_pose'
    assert json.loads(goal.arguments_yaml)['pose']['header']['frame_id'] == 'map'
    bridge.clients['autoslam'].send_goal_async.assert_not_called()


def test_runtime_start_reuses_ready_hardware_and_rejects_other_bringup(monkeypatch):
    """Never launch another driver set over existing scan/odometry publishers."""
    bridge, _ = _bridge()
    bridge.runtime = Mock()
    bridge.node.get_node_names_and_namespaces.return_value = [('controller', '/')]
    bridge.node.count_publishers.side_effect = lambda name: int(name != '/map')
    bridge._start_runtime({'mode': 'mapping'})
    bridge.runtime.start.assert_called_once_with('mapping', map_id=None, start_hardware=False)
    bridge.node.get_node_names_and_namespaces.return_value += [('controller_server', '/')]
    with pytest.raises(ValueError, match='Stop existing'):
        bridge._start_runtime({'mode': 'mapping'})
    bridge.runtime.start.assert_called_once()
    monkeypatch.setenv('HOMECAM_BACKEND_URL', 'https://robot.example.com')
    bridge.node.get_node_names_and_namespaces.return_value = [('homecam_media_agent', '/')]
    with pytest.raises(ValueError, match='homecam_media_agent'):
        bridge._start_runtime({'mode': 'mapping'})
    bridge.runtime.start.assert_called_once()
    bridge.node.get_node_names_and_namespaces.return_value = []
    bridge.node.count_publishers.side_effect = lambda name: int(name == '/scan_raw')
    with pytest.raises(ValueError, match='partly running'):
        bridge._start_runtime({'mode': 'mapping'})


@pytest.mark.parametrize('name', [
    'malbut_stt', 'malbut_tts', 'malbut_agent_communication',
])
def test_runtime_start_rejects_existing_speech_nodes(name):
    """Another speech process must not share the microphone or satisfy new peers."""
    bridge, _ = _bridge()
    bridge.runtime = Mock()
    bridge.node.get_node_names_and_namespaces.return_value = [(name, '/')]
    bridge.node.count_publishers.return_value = 0
    with pytest.raises(ValueError, match=name):
        bridge._start_runtime({'mode': 'mapping'})
    bridge.runtime.start.assert_not_called()


def test_runtime_stop_waits_for_nav2_terminal_status():
    """Cancel acknowledgement alone must not shut down a moving controller."""
    bridge, _ = _bridge()
    bridge.runtime = Mock()
    bridge.runtime.snapshot.return_value = {'state': 'RUNNING', 'mode': 'mapping'}
    uuid = bytes(range(16))
    goal = SimpleNamespace(goal_id=SimpleNamespace(uuid=uuid))
    from malbut_bringup.web_panel import RUNTIME_ACTIONS
    for name in RUNTIME_ACTIONS:
        client = Mock()
        client.service_is_ready.return_value = name == '/follow_path'
        client.call_async.return_value = _future(
            SimpleNamespace(return_code=0, goals_canceling=[goal]))
        bridge.cancel_clients[name] = client
    bridge.action_status = {'/follow_path': {uuid: 3}}
    bridge._stop_runtime()
    bridge._finish_runtime_stop()
    bridge.runtime.stop.assert_not_called()
    with pytest.raises(ValueError, match='stopping'):
        bridge._start('unused', _command())
    bridge.action_status['/follow_path'][uuid] = 5
    bridge._finish_runtime_stop()
    bridge.runtime.stop.assert_called_once()
    assert bridge.stopping_runtime is None


def test_runtime_stop_rejection_preserves_processes():
    """Keep Bringup running when its active mission refuses cancellation."""
    bridge, _ = _bridge()
    bridge.runtime = Mock()
    import time
    bridge.stopping_runtime = {
        'since': time.monotonic(), 'names': ('/patrol',),
        'futures': {'/patrol': _future(SimpleNamespace(return_code=1))},
    }
    bridge._finish_runtime_stop()
    bridge.runtime.stop.assert_not_called()
    assert 'rejected' in bridge.runtime_message


def test_missing_servers_fail_without_waiting():
    """An unavailable ROS server must not block the web request or executor."""
    bridge, _ = _bridge(autoslam_ready=False)
    request_id = bridge.submit(_command())
    bridge._drain()
    assert bridge.data.requests[request_id]['state'] == 'ERROR'
    bridge.clients['autoslam'].send_goal_async.assert_not_called()


def test_manager_does_not_make_missing_autoslam_executable():
    """A ready manager cannot stand in for a missing application server."""
    bridge, _ = _bridge(manager_ready=True, autoslam_ready=False)
    request_id = bridge.submit(_command())
    bridge._drain()
    assert bridge.data.requests[request_id]['state'] == 'ERROR'
    bridge.clients['manager'].send_goal_async.assert_not_called()


def test_readiness_reason_is_exposed_without_changing_manager(monkeypatch):
    """Show exact preparation blockers while retaining the launch process state."""
    bridge, _ = _bridge(autoslam_ready=False)
    bridge.runtime = Mock()
    bridge.runtime.snapshot.return_value = {
        'state': 'RUNNING', 'mode': 'navigation', 'message': 'process alive'}
    monkeypatch.setattr(bridge, '_robot_pose', lambda: None)
    bridge.node.count_publishers.return_value = 0
    bridge._bringup_status(SimpleNamespace(data=json.dumps({
        'state': 'WAITING', 'missing': ['TF:map->base_footprint (set initial pose)'],
    })))
    bridge._refresh()
    runtime = bridge.data.snapshot()['runtime']
    assert runtime['state'] == 'RUNNING' and not runtime['ready']
    assert runtime['waiting'] == ['TF:map->base_footprint (set initial pose)']
    assert runtime['message'] == '필수 입력 준비 대기'
    bridge._bringup_status(SimpleNamespace(data='invalid JSON'))
    assert bridge.startup_status['state'] == 'WAITING'


@pytest.mark.parametrize('mode', ['mapping', 'navigation'])
def test_action_server_is_not_ready_until_speech_capture_starts(monkeypatch, mode):
    """The UI must wait through preflight/model loading after Manager appears."""
    bridge, _ = _bridge(manager_ready=True)
    bridge.runtime = Mock()
    bridge.runtime.snapshot.return_value = {
        'state': 'RUNNING', 'mode': mode, 'message': 'process alive'}
    bridge.data.system = {'system_state': 1}
    monkeypatch.setattr(bridge, '_robot_pose', lambda: None)
    bridge.node.count_publishers.return_value = 1
    bridge._refresh()
    status = bridge.data.snapshot()['runtime']
    assert not status['ready']
    assert status['waiting'] == ['speech: microphone startup']
    # DDS data delivery can precede the graph cache's writer discovery.
    bridge.node.count_publishers.return_value = 0
    bridge._speech_status(SimpleNamespace(data='ready'))
    bridge._refresh()
    assert not bridge.data.snapshot()['runtime']['ready']
    bridge.node.count_publishers.return_value = 1
    bridge._refresh()
    assert bridge.data.snapshot()['runtime']['ready']
    bridge.node.count_publishers.return_value = 0
    bridge._refresh()
    assert not bridge.data.snapshot()['runtime']['ready']


@pytest.mark.parametrize('state', ['STOPPED', 'STOPPING', 'ERROR', 'STARTING'])
def test_stale_speech_and_action_readiness_cannot_mark_inactive_runtime_ready(monkeypatch, state):
    """An old retained status cannot make a stopped or failed launch look ready."""
    bridge, _ = _bridge(manager_ready=True)
    bridge.runtime = Mock()
    bridge.runtime.snapshot.return_value = {
        'state': state, 'mode': 'navigation', 'message': 'process state'}
    bridge.speech_ready = True
    monkeypatch.setattr(bridge, '_robot_pose', lambda: None)
    bridge.node.count_publishers.return_value = 1
    bridge._refresh()
    assert not bridge.data.snapshot()['runtime']['ready']
    bridge._speech_status(SimpleNamespace(data='ready'))
    if state != 'STARTING':
        assert not bridge.speech_ready


@pytest.mark.parametrize('state', ['ERROR', 'STOPPED'])
def test_dead_bringup_does_not_keep_showing_its_managers_last_state(monkeypatch, state):
    """Retained localization, system and zone topics vanish with the owned Bringup."""
    bridge, _ = _bridge(manager_ready=False)
    bridge.runtime = Mock()
    bridge.runtime.snapshot.return_value = {'state': state, 'mode': None, 'map': None,
                                            'message': 'Bringup exited (1): x; stop'}
    bridge.localization = {'mode': 'MAPPING', 'map': None, 'message': 'mapping'}
    bridge.data.system = {'system_state': 1, 'control_mode': 0}
    bridge.data.zones = {'state': 'CLEARED'}
    monkeypatch.setattr(bridge, '_robot_pose', lambda: None)
    bridge.node.count_publishers.return_value = 0
    bridge._refresh()
    runtime = bridge.data.snapshot()['runtime']
    assert runtime['localization'] == {} and runtime['mode'] is None
    assert runtime['message'] == 'Bringup exited (1): x; stop'
    assert bridge.data.snapshot()['system'] is None and bridge.data.snapshot()['zones'] is None


def test_booting_manager_is_not_ready_and_live_localization_sets_mode(monkeypatch):
    """Missions open after READY; the shown mode follows the manager, not the request."""
    bridge, _ = _bridge(manager_ready=True)
    bridge.runtime = Mock()
    bridge.runtime.snapshot.return_value = {
        'state': 'RUNNING', 'mode': 'mapping', 'map': None, 'message': 'process alive'}
    monkeypatch.setattr(bridge, '_robot_pose', lambda: None)
    bridge.node.count_publishers.return_value = 1
    bridge.speech_ready = True
    bridge.data.system = {'system_state': 0}
    bridge._refresh()
    assert not bridge.data.snapshot()['runtime']['ready']
    bridge.data.system = {'system_state': 1}
    bridge._localization(SimpleNamespace(data=json.dumps({
        'mode': 'LOCALIZATION', 'map': '/maps/home.yaml', 'message': 'loaded'})))
    bridge._refresh()
    runtime = bridge.data.snapshot()['runtime']
    assert runtime['ready']
    assert runtime['mode'] == 'navigation' and runtime['map'] == 'home.yaml'


@pytest.mark.parametrize('mode,service', [('mapping', 'start_mapping'),
                                          ('navigation', 'load_map')])
def test_running_bringup_switches_localization_instead_of_relaunching(mode, service):
    """Map selection in a running Bringup never starts a second robot stack."""
    bridge, _ = _bridge()
    bridge.runtime = Mock()
    bridge.catalog = Mock()
    bridge.catalog.resolve.return_value = Path('/maps/home.yaml')
    for client in (bridge.load_map, bridge.start_mapping):
        client.service_is_ready.return_value = True
    response = (SimpleNamespace(success=True, message='mapping') if mode == 'mapping'
                else SimpleNamespace(result=0))
    getattr(bridge, service).call_async.return_value = _future(response)
    payload = {'command': 'bringup_start', 'mode': mode}
    if mode == 'navigation':
        payload['map'] = 'home.yaml'
    bridge.submit(payload)
    bridge._drain()
    bridge.runtime.start.assert_not_called()
    request = getattr(bridge, service).call_async.call_args.args[0]
    if mode == 'navigation':
        assert request.map_url == '/maps/home.yaml'
    assert 'failed' not in bridge.runtime_message
    failed = SimpleNamespace(success=False, message='cancel missions that use the base')
    if mode == 'navigation':
        failed = SimpleNamespace(result=255)
    getattr(bridge, service).call_async.return_value = _future(failed)
    bridge.submit(payload)
    bridge._drain()
    assert 'failed' in bridge.runtime_message


def test_new_bringup_clears_previous_speech_readiness():
    """Every requested launch must announce its own microphone readiness."""
    bridge, _ = _bridge()
    bridge.runtime = Mock()
    bridge.speech_ready = True
    bridge.node.get_node_names_and_namespaces.return_value = []
    bridge.node.count_publishers.return_value = 0
    bridge._start_runtime({'mode': 'mapping'})
    assert not bridge.speech_ready


def test_direct_autoslam_blocks_other_panel_starts():
    """Do not bypass resource arbitration while a direct mapping Goal is active."""
    bridge, _ = _bridge()
    first = bridge.submit(_command())
    bridge._drain()
    assert bridge.data.requests[first]['state'] == 'RUNNING'
    bridge.clients['manager'].server_is_ready.return_value = True
    second = bridge.submit(_command('patrol', {'thoroughness': 1}))
    bridge._drain()
    assert bridge.data.requests[second]['state'] == 'ERROR'
    bridge.clients['manager'].send_goal_async.assert_not_called()


def test_cancel_only_owned_handle_and_wait_for_terminal_result():
    """Cancel acceptance is not reported as actual robot-stop completion."""
    bridge, handle = _bridge()
    request_id = bridge.submit(_command())
    bridge._drain()
    bridge.submit({'command': 'cancel'})
    bridge._drain()
    handle.cancel_goal_async.assert_called_once()
    assert bridge.data.requests[request_id]['state'] == 'CANCELING'
    handle.get_result_async.return_value.set_result(
        SimpleNamespace(status=5, result=SimpleNamespace(success=False)))
    assert bridge.data.requests[request_id]['state'] == 'CANCELED'
    assert not bridge.handles


def test_cancel_before_goal_acceptance_is_delivered_later():
    """A pending send must not escape cancellation when the server replies late."""
    bridge, handle = _bridge()
    acceptance = Future()
    bridge.clients['autoslam'].send_goal_async.return_value = acceptance
    request_id = bridge.submit(_command())
    bridge._drain()
    bridge.cancel_owned()
    assert bridge.data.requests[request_id]['state'] == 'CANCELING'
    acceptance.set_result(handle)
    handle.cancel_goal_async.assert_called_once()


def test_result_transport_failure_does_not_forget_running_goal():
    """Losing the result response is not evidence that the robot has stopped."""
    bridge, handle = _bridge()
    request_id = bridge.submit(_command())
    bridge._drain()
    handle.get_result_async.return_value.set_exception(RuntimeError('Result unavailable'))
    assert bridge.data.requests[request_id]['state'] == 'UNCONFIRMED'
    assert request_id in bridge.handles
    bridge.cancel_owned()
    handle.cancel_goal_async.assert_called_once()


def test_lost_acceptance_reply_does_not_release_direct_autoslam():
    """An unanswered acceptance is not a confirmed rejection or robot stop."""
    bridge, _ = _bridge()
    acceptance = Future()
    bridge.clients['autoslam'].send_goal_async.return_value = acceptance
    first = bridge.submit(_command())
    bridge._drain()
    acceptance.set_exception(RuntimeError('Acceptance response lost'))
    assert bridge.data.requests[first]['state'] == 'UNCONFIRMED'
    bridge.clients['manager'].server_is_ready.return_value = True
    second = bridge.submit(_command('patrol', {'thoroughness': 1}))
    bridge._drain()
    assert bridge.data.requests[second]['state'] == 'ERROR'
    bridge.clients['manager'].send_goal_async.assert_not_called()


def test_result_request_failure_keeps_accepted_handle_for_cancellation():
    """A local error after Goal acceptance must not hide an active mission."""
    bridge, handle = _bridge()
    handle.get_result_async.side_effect = RuntimeError('Cannot request result')
    request_id = bridge.submit(_command())
    bridge._drain()
    assert bridge.data.requests[request_id]['state'] == 'UNCONFIRMED'
    bridge.cancel_owned()
    handle.cancel_goal_async.assert_called_once()


def test_cancel_queued_before_start_does_not_move_robot():
    """Commands queued after a cancel press must not accidentally run through it."""
    bridge, _ = _bridge()
    bridge.submit({'command': 'cancel'})
    request_id = bridge.submit(_command())
    bridge._drain()
    assert bridge.data.requests[request_id]['state'] == 'CANCELED'
    bridge.clients['autoslam'].send_goal_async.assert_not_called()


def test_html_uses_token_headers_and_explicit_start_confirmation():
    """Keep credentials out of URLs and require a user gesture for motion."""
    page = (Path(__file__).parents[1] / 'malbut_bringup' / 'web_panel.html').read_text()
    assert "Authorization:'Bearer '+token" in page
    assert 'localStorage' not in page
    assert 'confirm(' in page
    assert 'setTimeout(pollVideo,200)' in page
    assert 'cmd_vel' not in page


@pytest.mark.parametrize('payload', [
    {'command': 'teleop', 'linear_x': 0.25, 'linear_y': 0.0, 'angular_z': 0.0},
    {'command': 'teleop', 'linear_x': 0.0, 'linear_y': 0.0, 'angular_z': -0.6},
    {'command': 'teleop', 'linear_x': float('nan'), 'linear_y': 0.0, 'angular_z': 0.0},
    {'command': 'teleop', 'linear_x': True, 'linear_y': 0.0, 'angular_z': 0.0},
    {'command': 'teleop', 'linear_x': 0.1, 'linear_y': 0.0},
    {'command': 'teleop', 'linear_x': 0.1, 'linear_y': 0.0, 'angular_z': 0.0, 'topic': 'x'},
    _command('manual_drive', {'time_allowance': {'sec': 3600}}),
])
def test_manual_drive_rejects_unbounded_or_extra_input(payload):
    """Web teleop stays within the driver limits and the registered time limit."""
    with pytest.raises(ValueError):
        validate_command(payload)


def _teleop_bridge(monkeypatch, now):
    from malbut_bringup import web_panel
    monkeypatch.setattr(web_panel.time, 'monotonic', lambda: now[0])
    bridge, _ = _bridge(manager_ready=True)
    bridge.twist = lambda: SimpleNamespace(
        linear=SimpleNamespace(x=0.0, y=0.0), angular=SimpleNamespace(z=0.0))
    bridge.teleop_publisher = Mock()
    bridge.teleop_received = None
    return bridge


def _published(bridge):
    return [(call.args[0].linear.x, call.args[0].linear.y, call.args[0].angular.z)
            for call in bridge.teleop_publisher.publish.call_args_list]


def test_held_teleop_stops_once_when_the_page_stops_repeating(monkeypatch):
    """A closed page or lost Wi-Fi must not leave AssistedTeleop moving."""
    now = [100.0]
    bridge = _teleop_bridge(monkeypatch, now)
    assert bridge.submit({'command': 'teleop', 'linear_x': 0.15,
                          'linear_y': 0.0, 'angular_z': 0.0}) is None
    bridge._drain()
    assert not bridge.data.requests
    now[0] += 0.4
    bridge._teleop_watchdog()
    assert _published(bridge) == [(0.15, 0.0, 0.0)]
    now[0] += 0.2
    bridge._teleop_watchdog()
    bridge._teleop_watchdog()
    assert _published(bridge) == [(0.15, 0.0, 0.0), (0.0, 0.0, 0.0)]


def test_released_teleop_publishes_zero_without_a_later_stop(monkeypatch):
    """Joystick input keeps working after the web page releases its button."""
    now = [100.0]
    bridge = _teleop_bridge(monkeypatch, now)
    for move in ((0.0, 0.15, 0.0), (0.0, 0.0, 0.0)):
        bridge.submit({'command': 'teleop', 'linear_x': move[0],
                       'linear_y': move[1], 'angular_z': move[2]})
    bridge._drain()
    now[0] += 5.0
    bridge._teleop_watchdog()
    bridge.stop_teleop()
    assert _published(bridge) == [(0.0, 0.15, 0.0), (0.0, 0.0, 0.0)]


def test_manual_drive_starts_through_the_manager_with_registered_defaults():
    """The panel sends no time limit; the capability manifest owns it."""
    bridge, _ = _bridge(manager_ready=True)
    request_id = bridge.submit({'command': 'start', 'capability': 'manual_drive',
                                'arguments': {}})
    bridge._drain()
    assert bridge.data.requests[request_id]['route'] == 'manager'
    goal = bridge.clients['manager'].send_goal_async.call_args.args[0]
    assert goal.capability_id == 'manual_drive'
    assert json.loads(goal.arguments_yaml) == {}


@pytest.mark.parametrize('payload', [
    {'command': 'teleop', 'linear_x': 0.15, 'linear_y': 0.0, 'angular_z': 0.0, 'hold_s': 1.0},
    _command('relocalize', {'method': 0}),
    _command('relocalize', {'method': 1, 'x': 1.0, 'y': -2, 'yaw': 0.5}),
    {'command': 'debug_start', 'capability': 'get_weather', 'arguments': {}},
])
def test_remote_tools_are_bounded_commands(payload):
    """Held driving, pose finding and debug missions pass as fixed command shapes."""
    assert validate_command(payload) == payload


def test_given_pose_becomes_a_map_frame_initial_pose_with_a_spread():
    """The operator's pose reaches /relocalize like RViz's 2D Pose Estimate."""
    arguments = mission_arguments('relocalize', {'method': 1, 'x': 1.0, 'y': 2.0, 'yaw': 0.0})
    pose = arguments['initial_pose']
    assert arguments['method'] == 1 and pose['header']['frame_id'] == 'map'
    assert pose['pose']['pose']['position'] == {'x': 1.0, 'y': 2.0, 'z': 0.0}
    covariance = pose['pose']['covariance']
    assert len(covariance) == 36 and covariance[0] == covariance[7] == 0.25
    assert covariance[35] > 0
    assert mission_arguments('relocalize', {'method': 2}) == {'method': 2}


def test_debug_mission_reaches_only_the_manager_with_raw_arguments():
    """The manager's manifest validation is the only way a debug mission runs."""
    bridge, _ = _bridge(manager_ready=True)
    request_id = bridge.submit({'command': 'debug_start', 'capability': 'relocalize',
                                'arguments': {'method': 2}})
    bridge._drain()
    goal = bridge.clients['manager'].send_goal_async.call_args.args[0]
    assert (goal.capability_id, json.loads(goal.arguments_yaml)) == ('relocalize', {'method': 2})
    assert bridge.data.requests[request_id]['capability'] == 'relocalize'
    bridge, _ = _bridge(manager_ready=False)
    request_id = bridge.submit({'command': 'debug_start', 'capability': 'autoslam',
                                'arguments': {'map_name': 'home2'}})
    bridge._drain()
    assert bridge.data.requests[request_id]['state'] == 'ERROR'
    bridge.clients['autoslam'].send_goal_async.assert_not_called()


def test_diagnostics_report_the_graph_and_state_without_local_logs():
    """Remote debugging sees nodes, publishers and Actions, never Bringup log files."""
    bridge, _ = _bridge()
    bridge.node.get_node_names_and_namespaces.return_value = [('system_manager', '/')]
    bridge.node.count_publishers.return_value = 1
    bridge.node.count_subscribers.return_value = 0
    bridge.cancel_clients = {'/relocalize': Mock(**{'service_is_ready.return_value': True})}
    bridge.data.runtime = {**bridge.data.runtime, 'log_path': '/secret/run.log',
                           'log_tail': 'private contents'}
    report = bridge.diagnostics()
    assert report['nodes'] == ['/system_manager']
    assert report['actions'] == {'/relocalize': True}
    assert report['topics']['/scan_raw'] == {'publishers': 1, 'subscribers': 0}
    assert '/secret' not in json.dumps(report) and 'private' not in json.dumps(report)


def test_remote_hold_rides_out_polling_gaps_then_stops_once(monkeypatch):
    """A cloud page asks for a longer hold; a lost stop still ends within it."""
    now = [100.0]
    bridge = _teleop_bridge(monkeypatch, now)
    bridge.submit({'command': 'teleop', 'linear_x': 0.15, 'linear_y': 0.0,
                   'angular_z': 0.0, 'hold_s': 1.0})
    bridge._drain()
    assert bridge.data.snapshot()['manual'] == {'state': 'MOVING', 'message': 'Driving'}
    now[0] += 0.7  # Beyond the LAN panel's 0.5 s, inside the requested hold.
    bridge._teleop_watchdog()
    assert _published(bridge) == [(0.15, 0.0, 0.0)]
    now[0] += 0.4
    bridge._teleop_watchdog()
    bridge._teleop_watchdog()
    assert _published(bridge) == [(0.15, 0.0, 0.0), (0.0, 0.0, 0.0)]
    assert bridge.data.snapshot()['manual']['state'] == 'IDLE'
    bridge.submit({'command': 'teleop', 'linear_x': 0.0, 'linear_y': 0.0, 'angular_z': 0.0})
    bridge._drain()
    assert bridge.data.snapshot()['manual'] == {'state': 'IDLE', 'message': 'Stopped'}
    assert bridge.teleop_hold_s == 0.5  # The next LAN command restores its own hold.


@pytest.fixture
def zone_map(tmp_path):
    """Write a saved map in the panel's map directory, in use by the manager."""
    import cv2
    import numpy as np

    from malbut_bringup.web_runtime import SavedMapCatalog

    cv2.imwrite(str(tmp_path / 'home.pgm'), np.full((40, 80), 254, dtype=np.uint8))
    path = tmp_path / 'home.yaml'
    path.write_text('image: home.pgm\nresolution: 0.05\norigin: [-1.0, -1.0, 0.0]\n'
                    'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
    runtime = {'localization': {'mode': 'LOCALIZATION', 'map': str(path)}}
    return SavedMapCatalog(tmp_path), runtime, path


def test_zones_are_edited_only_on_the_saved_map_in_use(zone_map, tmp_path):
    """Mapping, switching or a map outside the directory is not editable."""
    catalog, runtime, path = zone_map
    assert live_zone_map(runtime, catalog) == path.resolve()
    for localization in ({'mode': 'MAPPING', 'map': None},
                         {'mode': 'SWITCHING', 'map': str(path)}, {}):
        view = zone_view({'localization': localization}, catalog)
        assert not view['editable'] and view['zones'] == []
    other = tmp_path / 'elsewhere'
    other.mkdir()
    (other / 'home.yaml').write_text(path.read_text())
    (other / 'home.pgm').write_bytes((tmp_path / 'home.pgm').read_bytes())
    view = zone_view({'localization': {'mode': 'LOCALIZATION',
                                       'map': str(other / 'home.yaml')}}, catalog)
    assert not view['editable'] and 'outside' in view['message']


def test_saved_zones_round_trip_to_the_map_zone_file(zone_map):
    """The editor's rectangles become the map's Zone GeoJSON."""
    catalog, runtime, path = zone_map
    rectangle = [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]
    assert save_zones(runtime, catalog, {'map': 'home.yaml', 'zones': [
        {'behavior': 'restricted', 'points': rectangle},
        {'behavior': 'avoid', 'name': 'rug', 'points': rectangle}]}) == 2
    view = zone_view(runtime, catalog)
    assert view['editable'] and view['map'] == 'home.yaml'
    assert view['zones'] == [
        {'behavior': 'restricted', 'name': '', 'points': rectangle},
        {'behavior': 'avoid', 'name': 'rug', 'points': rectangle}]
    assert save_zones(runtime, catalog, {'map': 'home.yaml', 'zones': []}) == 0
    assert zone_view(runtime, catalog)['zones'] == []


@pytest.mark.parametrize('payload, message', [
    ({'map': 'other.yaml', 'zones': []}, 'changed'),
    ({'map': 'home.yaml', 'zones': [{'behavior': 'lava', 'points': [[0, 0], [1, 0], [1, 1]]}]},
     'behavior'),
    ({'map': 'home.yaml', 'zones': [{'behavior': 'avoid', 'points': [[0, 0], [1, 0]]}]},
     'corners'),
    ({'map': 'home.yaml', 'zones': [{'behavior': 'avoid', 'points': [[0, 0], [1, 0], [1, 1]],
                                     'shell': 'rm -rf'}]}, 'behavior'),
    ({'map': 'home.yaml'}, 'Expected map and zones'),
])
def test_invalid_zone_requests_change_nothing(zone_map, payload, message):
    """Only bounded polygons for the map in use are written."""
    catalog, runtime, path = zone_map
    with pytest.raises(ValueError, match=message):
        save_zones(runtime, catalog, payload)
    assert not path.with_suffix('.zones.geojson').exists()


def test_zone_api_is_authenticated_and_never_dispatches_a_command(http_server, zone_map):
    """Zone edits are file writes for zone_filter, never robot commands."""
    server, submit = http_server
    catalog, runtime, _ = zone_map
    server.catalog = catalog
    server.data.runtime = runtime
    assert _request(server, 'GET', '/api/zones')[0] == 401
    headers = {'Authorization': 'Bearer test-secret', 'Content-Type': 'application/json'}
    status, content = _request(server, 'GET', '/api/zones', **headers)
    assert status == 200 and json.loads(content)['editable']
    rectangle = [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]
    body = {'map': 'home.yaml', 'zones': [{'behavior': 'restricted', 'points': rectangle}]}
    status, content = _request(server, 'POST', '/api/zones', body, **headers)
    assert status == 200 and json.loads(content)['saved'] == 1
    status, _ = _request(server, 'POST', '/api/zones', {'map': 'home.yaml'}, **headers)
    assert status == 400
    submit.assert_not_called()
