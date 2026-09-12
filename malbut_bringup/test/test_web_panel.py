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
    image_jpeg, PanelData, PanelServer, RosBridge, validate_command,
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
    for path in ('/api/maps', '/api/map', '/api/map/image/1'):
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
    """Geometry, versioned PNG and saved-map listing never dispatch a command."""
    server, submit = http_server
    headers = {'Authorization': 'Bearer test-secret'}
    server.catalog = Mock()
    server.catalog.list_maps.return_value = [{'id': 'home.yaml', 'name': 'home'}]
    status, content = _request(server, 'GET', '/api/maps', **headers)
    assert status == 200 and json.loads(content)['maps'][0]['id'] == 'home.yaml'
    status, content = _request(server, 'GET', '/api/map', **headers)
    assert status == 200 and not json.loads(content)['available']
    assert _request(server, 'GET', '/api/map/image/1', **headers)[0] == 409
    server.data.map_cache = Mock()
    server.data.map_cache.png.return_value = (7, b'PNG bytes')
    assert _request(server, 'GET', '/api/map/image/7', **headers) == (200, b'PNG bytes')
    server.data.map_cache.png.assert_called_once_with(7)
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
    bridge.action_status = {}
    bridge.cancel_clients = {}
    bridge.cancel_request = SimpleNamespace
    bridge.tf_buffer = Mock()
    bridge.topics = {'map_topic': '/map'}
    bridge.guard = Mock()
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


def test_runtime_start_reuses_ready_hardware_and_rejects_other_bringup():
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
    bridge.node.get_node_names_and_namespaces.return_value = []
    bridge.node.count_publishers.side_effect = lambda name: int(name == '/scan_raw')
    with pytest.raises(ValueError, match='partly running'):
        bridge._start_runtime({'mode': 'mapping'})


def test_runtime_stop_waits_for_nav2_terminal_status():
    """Cancel acknowledgement alone must not shut down a moving controller."""
    bridge, _ = _bridge()
    bridge.runtime = Mock()
    bridge.runtime.snapshot.return_value = {'state': 'RUNNING', 'mode': 'mapping'}
    uuid = bytes(range(16))
    goal = SimpleNamespace(goal_id=SimpleNamespace(uuid=uuid))
    from malbut_bringup.web_panel import RUNTIME_ACTIONS
    for name in RUNTIME_ACTIONS['mapping']:
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
