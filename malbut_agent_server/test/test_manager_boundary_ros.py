"""Resident Agent must use Manager even while downstream services are available."""

from pathlib import Path
from threading import Thread
import time
from types import SimpleNamespace

import pytest

rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')
from malbut_interfaces.action import DeviceOperation, GetWeather  # noqa: E402
from rclpy.action import ActionClient, ActionServer  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402

from malbut_agent_server.manager_client import ManagerClient  # noqa: E402
from malbut_agent_server.robot_device_client import RobotDeviceClient  # noqa: E402
from malbut_agent_server.weather_action import create_weather_action_node  # noqa: E402
from malbut_agent_server.weather_location_store import WeatherLocationStore  # noqa: E402
from malbut_agent_server.weather_query import ManagerWeatherQuery  # noqa: E402
from malbut_system_manager.system_manager_node import SystemManagerNode  # noqa: E402
from test_weather import state_at  # noqa: E402


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Manager boundary did not settle')
        time.sleep(0.01)


@pytest.fixture(params=[True, False], ids=['resident-manager', 'manager-absent'])
def boundary(request, tmp_path):
    rclpy.init(args=['--ros-args', '-p', 'system_manager:resident_runtime:=true'])
    store = WeatherLocationStore(':memory:')
    calls = {'weather': 0, 'location': 0, 'device': []}

    def fetch():
        calls['weather'] += 1
        return state_at(time.time())

    def resolve(location):
        calls['location'] += 1
        return [{'location': location, 'latitude': 36.36,
                 'longitude': 127.36, 'timezone': 'Asia/Seoul'}]

    weather = create_weather_action_node(
        client=SimpleNamespace(fetch=fetch), location_store=store,
        location_resolver=resolve,
    )
    node = Node('manager_boundary_client')

    def execute(handle):
        calls['device'].append(handle.request.operation)
        handle.succeed()
        return DeviceOperation.Result(success=True, code='completed',
                                      result_json='{"runtime":{"state":"STOPPED"}}',
                                      message='observed')

    device_server = ActionServer(node, DeviceOperation, '/malbut/device/operate', execute)
    manager = None
    if request.param:
        directory = tmp_path / 'manifests'
        directory.mkdir()
        source = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
        for capability in ('get_weather', 'set_weather_location', 'device_operation'):
            name = capability + '.yaml'
            (directory / name).write_bytes((source / name).read_bytes())
        # No robot runtime or hardware is started in this graph.
        manager = SystemManagerNode(manifest_directory=str(directory))
    client = ManagerClient(node, on_event=lambda event: query.handle(event))
    query = ManagerWeatherQuery(client, timeout_s=2.0)
    device = RobotDeviceClient(node)
    node.create_timer(0.01, query.drain)
    executor = MultiThreadedExecutor(num_threads=4)
    for item in (node, weather, manager):
        if item is not None:
            executor.add_node(item)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        probes = [ActionClient(node, DeviceOperation, '/malbut/device/operate'),
                  ActionClient(node, GetWeather, '/malbut/weather/get')]
        try:
            wait_until(lambda: all(probe.server_is_ready() for probe in probes))
        finally:
            for probe in probes:
                probe.destroy()
        if manager is not None:
            wait_until(lambda: client._client.server_is_ready() and device.action.server_is_ready())
        yield SimpleNamespace(query=query, device=device, calls=calls, store=store,
                              manager=manager)
    finally:
        query.close()
        weather.close()
        executor.shutdown(timeout_sec=5)
        thread.join(5)
        client.close()
        device.close()
        device_server.destroy()
        for item in (node, weather, manager):
            if item is not None:
                item.destroy_node()
        store.close()
        rclpy.shutdown()


def test_weather_and_location_have_no_direct_fallback(boundary):
    if boundary.manager is None:
        with pytest.raises(RuntimeError):
            boundary.query.execute('weather')
        with pytest.raises(RuntimeError):
            boundary.query.set_location('location', '대전 유성구')
        assert boundary.calls['weather'] == boundary.calls['location'] == 0
        assert boundary.store.get() is None
    else:
        assert boundary.query.execute('weather')['status'] == 'fresh'
        assert boundary.query.set_location('location', '대전 유성구') == {
            'status': 'location_set', 'location': '대전 유성구',
        }
        assert boundary.calls['weather'] == boundary.calls['location'] == 1
        assert boundary.store.get()['location'] == '대전 유성구'


def test_device_has_no_direct_fallback(boundary):
    outcomes = []
    boundary.device.send('status', 'status', {}, outcomes.append)
    wait_until(lambda: bool(outcomes))
    if boundary.manager is None:
        assert outcomes[0]['code'] == 'unavailable'
        assert boundary.calls['device'] == []
    else:
        assert outcomes[0]['success']
        assert outcomes[0]['result']['runtime']['state'] == 'STOPPED'
        assert boundary.calls['device'] == ['status']


@pytest.mark.parametrize('boundary', [True], indirect=True)
def test_manager_stop_rejects_old_preparation_before_backend_dispatch(boundary):
    runtime_id = boundary.manager._movement_runtime_id
    original_epoch = boundary.manager._movement_epoch
    stopped, outcomes = [], []
    boundary.device.stop('stop-before-preparation', stopped.append)
    wait_until(lambda: bool(stopped))
    assert stopped[0]['success']
    boundary.device.send('late-preparation', 'runtime_start', {
        'mode': 'mapping', 'movement_runtime_id': runtime_id, 'movement_epoch': original_epoch,
    }, outcomes.append)
    wait_until(lambda: bool(outcomes))
    assert outcomes[0]['code'] == 'movement_epoch_changed'
    assert outcomes[0]['not_dispatched'] is True
    assert boundary.calls['device'] == []
