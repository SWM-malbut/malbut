"""Verify weather/location Actions on a real ROS graph with no Manager node."""

from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest


rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402

from malbut_agent_server.resident_weather_query import ResidentWeatherQuery  # noqa: E402
from malbut_agent_server.weather_action import create_weather_action_node  # noqa: E402
from malbut_agent_server.weather_location_store import WeatherLocationStore  # noqa: E402
from test_weather import state_at  # noqa: E402


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Resident weather Action did not settle')
        time.sleep(0.01)


@pytest.fixture
def graph():
    rclpy.init(args=[])
    store = WeatherLocationStore(':memory:')
    release = Event()
    resolver_started = Event()
    block = [False]

    def resolve(location):
        resolver_started.set()
        if block[0]:
            release.wait(5)
        return [{'location': location, 'latitude': 36.36,
                 'longitude': 127.36, 'timezone': 'Asia/Seoul'}]

    weather = create_weather_action_node(
        client=SimpleNamespace(fetch=lambda: state_at(time.time())),
        location_store=store, location_resolver=resolve,
    )
    node = Node('resident_weather_test')
    query = ResidentWeatherQuery(node, timeout_s=1.0)
    node.create_timer(0.01, query.drain)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(weather)
    executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        wait_until(lambda: all(
            client.server_is_ready() for client in query._actions._clients.values()))
        yield SimpleNamespace(query=query, store=store, weather=weather, release=release,
                              block=block, resolver_started=resolver_started)
    finally:
        release.set()
        weather.close()
        executor.shutdown(timeout_sec=5)
        thread.join(5)
        query.close()
        node.destroy_node()
        weather.destroy_node()
        store.close()
        rclpy.shutdown()


def test_weather_and_location_work_without_a_manager(graph):
    assert graph.query.execute('standby-weather')['status'] == 'fresh'
    assert graph.query.set_location('standby-location', '대전 유성구') == {
        'status': 'location_set', 'location': '대전 유성구',
    }
    assert graph.store.get()['location'] == '대전 유성구'


def test_timed_out_location_resolution_cannot_commit_later(graph):
    graph.block[0] = True
    graph.query._timeout_s = 0.15
    with pytest.raises(TimeoutError):
        graph.query.set_location('standby-timeout', '대전 유성구')
    assert graph.resolver_started.is_set()
    wait_until(lambda: not graph.query._actions._requests)
    graph.release.set()
    wait_until(lambda: not graph.weather._worker.is_alive())
    assert graph.store.get() is None
