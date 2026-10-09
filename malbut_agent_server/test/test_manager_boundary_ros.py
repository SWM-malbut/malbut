"""The original Manager contract dispatches weather without resident workflow."""

from pathlib import Path
from threading import Thread
import time
from types import SimpleNamespace

import pytest

rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')
from malbut_interfaces.action import ExecuteMission, GetWeather  # noqa: E402
from malbut_interfaces.msg import SystemState  # noqa: E402
from rclpy.action import ActionClient  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402

from malbut_agent_server.manager_client import ManagerClient  # noqa: E402
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


@pytest.fixture(params=[True, False], ids=['original-manager', 'manager-absent'])
def boundary(request, tmp_path, monkeypatch):
    monkeypatch.setenv('ROS_DOMAIN_ID', '198')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    store = WeatherLocationStore(':memory:')
    calls = []
    weather = create_weather_action_node(
        client=SimpleNamespace(fetch=lambda: calls.append('weather') or state_at(time.time())),
        location_store=store,
        location_resolver=lambda location: [{'location': location, 'latitude': 36.36,
                                             'longitude': 127.36, 'timezone': 'Asia/Seoul'}],
    )
    node = Node('manager_boundary_client')
    manager = None
    if request.param:
        directory = tmp_path / 'manifests'
        directory.mkdir()
        source = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
        for capability in ('get_weather', 'set_weather_location'):
            name = capability + '.yaml'
            (directory / name).write_bytes((source / name).read_bytes())
        manager = SystemManagerNode(manifest_directory=str(directory))
    client = ManagerClient(node, on_event=lambda event: query.handle(event))
    query = ManagerWeatherQuery(client, timeout_s=2.0)
    node.create_timer(0.01, query.drain)
    probe = ActionClient(node, GetWeather, '/malbut/weather/get')
    executor = MultiThreadedExecutor(num_threads=4)
    for item in (node, weather, manager):
        if item is not None:
            executor.add_node(item)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        wait_until(probe.server_is_ready)
        if manager is not None:
            wait_until(client._client.server_is_ready)
        yield SimpleNamespace(query=query, calls=calls, manager=manager)
    finally:
        query.close()
        weather.close()
        executor.shutdown(timeout_sec=5)
        thread.join(5)
        probe.destroy()
        client.close()
        for item in (node, weather, manager):
            if item is not None:
                item.destroy_node()
        store.close()
        rclpy.shutdown()


def test_weather_does_not_bypass_missing_manager(boundary):
    if boundary.manager is None:
        with pytest.raises(RuntimeError):
            boundary.query.execute('weather')
        assert boundary.calls == []
    else:
        assert boundary.query.execute('weather')['status'] == 'fresh'
        assert boundary.calls == ['weather']


def test_public_manager_types_are_the_pre_integration_contract():
    assert set(ExecuteMission.Goal.get_fields_and_field_types()) == {
        'capability_id', 'arguments_yaml',
    }
    assert set(SystemState.get_fields_and_field_types()) == {
        'system_state', 'control_mode', 'active_foreground_missions',
        'active_background_missions', 'suspended_missions', 'pending_missions',
    }
