"""Exercise on-demand weather Actions with fake ROS and no external HTTP."""

from datetime import datetime
from dataclasses import replace
import sys
from threading import Event, Thread
from types import ModuleType, SimpleNamespace
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

import pytest
import yaml

from malbut_agent_server import weather_action
from malbut_agent_server.weather import (
    OpenMeteoClient, SOURCE, WeatherForecast, WeatherState,
)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    now = datetime(2026, 9, 12, 12, tzinfo=ZoneInfo('Asia/Seoul')).timestamp()
    snapshot = WeatherState(
        now + 0.25, now - 600, '시험 지역', 37, 127, SOURCE,
        'Asia/Seoul', 23, 2,
        (WeatherForecast('2026-09-12', 25, 18, 0, 3),
         WeatherForecast('2026-09-13', 24, 19, 16, 51)),
    )
    state = SimpleNamespace(
        params={'location': '시험 지역', 'latitude': 37.0, 'longitude': 127.0},
        destroyed=0, fetches=0, snapshot=snapshot, lifecycle=[], nodes=[],
        outcome=snapshot, started=Event(), release=Event(), block=False,
        database_path=str(tmp_path / 'weather-location.sqlite3'),
    )

    class FloatFields:
        def __setattr__(self, name, value):
            if name in ('latitude', 'longitude', 'temperature_c',
                        'temperature_max_c', 'temperature_min_c',
                        'precipitation_probability_max_pct'):
                assert type(value) is float
            object.__setattr__(self, name, value)

    class StateMessage(FloatFields):
        def __init__(self):
            self.fetched_at = SimpleNamespace(sec=0, nanosec=0)
            self.valid_at = SimpleNamespace(sec=0, nanosec=0)
            self.daily = []

    class Result:
        def __init__(self, *, error_code='', message=''):
            self.weather = StateMessage()
            self.error_code, self.message = error_code, message

    class Node:
        def __init__(self, name):
            state.node_name = name
            state.nodes.append(self)

        def declare_parameter(self, name, default):
            return SimpleNamespace(value=state.params.get(name, default))

        def destroy_node(self):
            state.destroyed += 1
            state.lifecycle.append('destroy_node')

    class ActionServer:
        def __init__(self, node, action_type, endpoint, **callbacks):
            if endpoint == weather_action.WEATHER_ACTION:
                state.server = self
                state.endpoint = endpoint
            else:
                state.location_server = self
            self.node = node
            for name, callback in callbacks.items():
                setattr(self, name, callback)

        def destroy(self):
            state.lifecycle.append('destroy_server')

    class Client(OpenMeteoClient):
        def fetch(self):
            state.fetches += 1
            state.started.set()
            if state.block:
                assert state.release.wait(3), 'Test HTTP worker was not released'
            if isinstance(state.outcome, Exception):
                raise state.outcome
            return state.outcome

    class Executor:
        def __init__(self, *, num_threads):
            assert num_threads >= 2

        def add_node(self, node):
            self.node = node

        def spin(self):
            state.lifecycle.append('spin')
            raise KeyboardInterrupt

        def shutdown(self):
            assert self.node._closed.is_set()
            state.lifecycle.append('executor_shutdown')

    ros = ModuleType('rclpy')
    ros.init = lambda args: state.lifecycle.append(('init', args))
    ros.ok = lambda: True
    ros.shutdown = lambda: state.lifecycle.append('shutdown')
    node_module = ModuleType('rclpy.node')
    node_module.Node = Node
    actions = ModuleType('rclpy.action')
    actions.ActionServer = ActionServer
    actions.GoalResponse = actions.CancelResponse = SimpleNamespace(
        ACCEPT='accept', REJECT='reject',
    )
    groups = ModuleType('rclpy.callback_groups')
    groups.ReentrantCallbackGroup = type('ReentrantCallbackGroup', (), {})
    executors = ModuleType('rclpy.executors')
    executors.MultiThreadedExecutor = Executor
    executors.ExternalShutdownException = type('ExternalShutdown', (Exception,), {})
    messages = ModuleType('malbut_interfaces.msg')
    messages.WeatherState, messages.WeatherForecast = StateMessage, FloatFields
    action_types = ModuleType('malbut_interfaces.action')
    action_types.GetWeather = SimpleNamespace(Result=Result, Feedback=SimpleNamespace)
    action_types.ExecuteMission = SimpleNamespace(
        Result=SimpleNamespace, Feedback=SimpleNamespace,
    )
    for name, module in {
        'rclpy': ros, 'rclpy.node': node_module, 'rclpy.action': actions,
        'rclpy.callback_groups': groups, 'rclpy.executors': executors,
        'malbut_interfaces.msg': messages, 'malbut_interfaces.action': action_types,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(weather_action, 'OpenMeteoClient', Client)
    monkeypatch.setattr(weather_action, 'DEFAULT_WEATHER_LOCATION_PATH', state.database_path)

    def forbid_location_lookup(query):
        raise AssertionError('Unexpected external location lookup')

    monkeypatch.setattr(weather_action, 'resolve_weather_location', forbid_location_lookup)
    state.client = Client('시험 지역', 37.0, 127.0)
    yield state
    state.release.set()
    for node in state.nodes:
        node.close()
        if node._worker is not None:
            node._worker.join(3)
            assert not node._worker.is_alive()
        node.destroy_node()


class Handle:
    def __init__(self, request=None):
        self.is_cancel_requested = False
        self.status = 'executing'
        self.feedback = []
        self.request = request

    @property
    def is_active(self):
        return self.status in ('accepted', 'executing', 'canceling')

    def publish_feedback(self, value):
        self.feedback.append(value.state)

    def succeed(self):
        assert not self.is_cancel_requested
        self.status = 'succeeded'

    def abort(self):
        self.status = 'aborted'

    def canceled(self):
        assert self.is_cancel_requested
        self.status = 'canceled'


def start(runtime, *, timeout_s=1.0):
    node = weather_action.create_weather_action_node(
        client=runtime.client, timeout_s=timeout_s,
    )
    assert node._store.path == ':memory:'
    assert runtime.server.goal_callback(None) == 'accept'
    handle, returned = Handle(), {}
    thread = Thread(target=lambda: returned.update(
        result=runtime.server.execute_callback(handle),
    ))
    thread.start()
    assert runtime.started.wait(2)
    return node, handle, returned, thread


def test_start_has_no_http_and_each_goal_fetches_once_with_typed_result(runtime):
    node = weather_action.create_weather_action_node()
    assert runtime.fetches == 0
    assert runtime.endpoint == '/malbut/weather/get'
    for expected in (1, 2):
        assert runtime.server.goal_callback(None) == 'accept'
        handle = Handle()
        result = runtime.server.execute_callback(handle)
        assert handle.status == 'succeeded' and handle.feedback == ['FETCHING']
        assert runtime.fetches == expected
        assert result.error_code == ''
        assert result.weather.temperature_c == 23.0
        assert result.weather.fetched_at.nanosec == 250_000_000
        assert result.weather.source == SOURCE
        assert [item.precipitation_probability_max_pct for item in result.weather.daily] == [
            0.0, 16.0,
        ]
        node._worker.join(2)


def location(name='수원시 우만동', latitude=37.28):
    return dict(location=name, latitude=latitude, longitude=127.03,
                timezone='Asia/Seoul')


def location_request(query='수원시 우만동'):
    return SimpleNamespace(
        capability_id='set_weather_location',
        arguments_yaml=yaml.safe_dump({'location': query}, allow_unicode=True),
    )


def save_location(runtime, query='수원시 우만동'):
    request = location_request(query)
    assert runtime.location_server.goal_callback(request) == 'accept'
    handle = Handle(request)
    result = runtime.location_server.execute_callback(handle)
    return handle, yaml.safe_load(result.result_yaml)


def test_missing_saved_location_asks_for_location_without_weather_fetch(runtime):
    runtime.params.clear()
    node = weather_action.create_weather_action_node()
    assert runtime.fetches == 0
    assert runtime.server.goal_callback(None) == 'accept'
    handle = Handle()
    result = runtime.server.execute_callback(handle)
    assert handle.status == 'aborted'
    assert result.error_code == result.message == 'LOCATION_REQUIRED'
    assert result.weather.daily == [] and runtime.fetches == 0
    node._worker.join(2)


def test_saved_location_overrides_manual_config_and_is_read_for_each_goal(runtime, monkeypatch):
    client_type = weather_action.OpenMeteoClient
    options = []

    def client(**values):
        options.append(values)
        runtime.outcome = replace(runtime.snapshot, **values)
        return client_type(**values)

    monkeypatch.setattr(weather_action, 'OpenMeteoClient', client)
    node = weather_action.create_weather_action_node()
    assert options == [dict(location='시험 지역', latitude=37.0,
                            longitude=127.0, timezone='Asia/Seoul')]
    options.clear()
    for expected in (location(), location('용인시 동백동', 37.27)):
        node._store.set(expected)
        assert runtime.server.goal_callback(None) == 'accept'
        handle = Handle()
        result = runtime.server.execute_callback(handle)
        assert handle.status == 'succeeded' and result.error_code == ''
        assert result.weather.location == expected['location']
        assert result.weather.latitude == expected['latitude']
        assert options[-1] == expected
        node._worker.join(2)
    assert runtime.fetches == 2


def test_setting_and_correction_survive_weather_node_restart(runtime, monkeypatch):
    runtime.params.clear()
    options = []
    client_type = weather_action.OpenMeteoClient
    values = location()

    def client(**configured):
        options.append(configured)
        runtime.outcome = replace(runtime.snapshot, **configured)
        return client_type(**configured)

    monkeypatch.setattr(weather_action, 'OpenMeteoClient', client)
    node = weather_action.create_weather_action_node(location_resolver=lambda query: [values])
    for expected in (location(), location('용인시 동백동', 37.27)):
        values = expected
        handle, result = save_location(runtime, expected['location'])
        assert handle.status == 'succeeded' and handle.feedback == ['RESOLVING']
        assert result == {'status': 'location_set', 'location': expected['location']}
        assert runtime.fetches == 0, 'Saving a location must not fetch weather'
    node._worker.join(2)
    node.destroy_node()
    restarted = weather_action.create_weather_action_node()
    assert runtime.server.goal_callback(None) == 'accept'
    handle = Handle()
    result = runtime.server.execute_callback(handle)
    assert handle.status == 'succeeded'
    assert result.weather.location == values['location']
    assert options == [values]
    assert runtime.fetches == 1
    restarted._worker.join(2)


@pytest.mark.parametrize('resolved,status', [
    ([], 'location_not_found'),
    ([location('서울 중구'), location('부산 중구')], 'location_ambiguous'),
])
def test_unresolved_location_keeps_previous_saved_location(runtime, resolved, status):
    node = weather_action.create_weather_action_node(location_resolver=lambda query: resolved)
    previous = node._store.set(location())
    handle, result = save_location(runtime, '중구')
    assert handle.status == 'succeeded'
    expected = {'status': status}
    if resolved:
        expected['candidates'] = [item['location'] for item in resolved]
    assert result == expected
    assert node._store.get() == previous
    assert runtime.fetches == 0


def test_location_lookup_failure_keeps_previous_saved_location(runtime):
    def fail(query):
        raise OSError('PRIVATE-UPSTREAM-BODY')

    node = weather_action.create_weather_action_node(location_resolver=fail)
    previous = node._store.set(location())
    handle, result = save_location(runtime)
    assert handle.status == 'aborted' and result == {'status': 'unavailable'}
    assert node._store.get() == previous
    assert runtime.fetches == 0


def test_location_save_failure_never_reports_success(runtime, monkeypatch):
    node = weather_action.create_weather_action_node(
        location_resolver=lambda query: [location('용인시 동백동')],
    )
    previous = node._store.set(location())

    def fail(value):
        raise OSError('PRIVATE-DATABASE-ERROR')

    monkeypatch.setattr(node._store, 'set', fail)
    handle, result = save_location(runtime)
    assert handle.status == 'aborted' and result == {'status': 'unavailable'}
    assert node._store.get() == previous


@pytest.mark.parametrize('goal', [
    SimpleNamespace(capability_id='get_weather', arguments_yaml='location: 수원'),
    SimpleNamespace(capability_id='set_weather_location', arguments_yaml='- 수원'),
    SimpleNamespace(capability_id='set_weather_location', arguments_yaml='location: 수원\nextra: x'),
    SimpleNamespace(capability_id='set_weather_location', arguments_yaml='location: ['),
    location_request(''), location_request('   '), location_request('x' * 121),
    location_request(True), location_request('수원\n시'),
])
def test_invalid_location_goals_are_rejected_before_query_or_save(runtime, goal):
    node = weather_action.create_weather_action_node()
    assert runtime.location_server.goal_callback(goal) == 'reject'
    assert node._store.get() is None
    assert not node._active and runtime.fetches == 0


@pytest.mark.parametrize('end', ['cancel', 'timeout', 'close'])
def test_late_location_resolution_never_saves_after_interruption(runtime, end):
    started = Event()

    def resolve(query):
        started.set()
        assert runtime.release.wait(3)
        return [location('용인시 동백동', 37.27)]

    node = weather_action.create_weather_action_node(
        timeout_s=0.1 if end == 'timeout' else 1.0, location_resolver=resolve,
    )
    previous = node._store.set(location())
    request = location_request()
    assert runtime.location_server.goal_callback(request) == 'accept'
    handle, returned = Handle(request), {}
    thread = Thread(target=lambda: returned.update(
        result=runtime.location_server.execute_callback(handle),
    ))
    thread.start()
    assert started.wait(2)
    assert runtime.server.goal_callback(None) == 'reject'
    assert runtime.location_server.goal_callback(request) == 'reject'
    if end == 'cancel':
        assert runtime.location_server.cancel_callback(handle) == 'accept'
        handle.is_cancel_requested = True
    elif end == 'close':
        node.close()
    thread.join(2)
    assert not thread.is_alive()
    code = {'cancel': 'CANCELED', 'timeout': 'TIMEOUT', 'close': 'SHUTTING_DOWN'}[end]
    assert returned['result'].message == code
    assert yaml.safe_load(returned['result'].result_yaml) == {'status': 'unavailable'}
    assert runtime.server.goal_callback(None) == 'reject'
    runtime.release.set()
    node._worker.join(2)
    assert not node._worker.is_alive() and runtime.fetches == 0
    assert node._store.get() == previous


@pytest.mark.parametrize('outcome,code', [
    (TimeoutError('PRIVATE-UPSTREAM-BODY'), 'TIMEOUT'),
    (URLError(TimeoutError('PRIVATE-UPSTREAM-BODY')), 'TIMEOUT'),
    (URLError('PRIVATE-UPSTREAM-BODY'), 'FETCH_FAILED'),
    (HTTPError('https://example.invalid', 503, 'PRIVATE-UPSTREAM-BODY', None, None),
     'FETCH_FAILED'),
    (OSError('PRIVATE-UPSTREAM-BODY'), 'FETCH_FAILED'),
    (ValueError('PRIVATE-UPSTREAM-BODY'), 'INVALID_DATA'),
    (object(), 'INVALID_DATA'),
])
def test_fetch_failure_aborts_without_weather_or_upstream_body(runtime, outcome, code):
    runtime.outcome = outcome
    node = weather_action.create_weather_action_node(client=runtime.client)
    assert runtime.server.goal_callback(None) == 'accept'
    handle = Handle()
    result = runtime.server.execute_callback(handle)
    assert handle.status == 'aborted'
    assert result.error_code == code
    assert result.weather.daily == []
    assert 'PRIVATE' not in result.message
    assert runtime.fetches == 1
    node.destroy_node()


@pytest.mark.parametrize('end,code,status', [
    ('cancel', 'CANCELED', 'canceled'),
    ('timeout', 'TIMEOUT', 'aborted'),
    ('close', 'SHUTTING_DOWN', 'aborted'),
])
def test_late_http_result_is_discarded_and_busy_until_worker_finishes(
    runtime, end, code, status,
):
    runtime.block = True
    node, handle, returned, thread = start(
        runtime, timeout_s=0.1 if end == 'timeout' else 1.0,
    )
    assert node._worker.daemon
    assert runtime.server.goal_callback(None) == 'reject'
    assert runtime.location_server.goal_callback(location_request()) == 'reject'
    if end == 'cancel':
        assert runtime.server.cancel_callback(handle) == 'accept'
        handle.is_cancel_requested = True
    elif end == 'close':
        node.close()
    thread.join(2)
    assert not thread.is_alive()
    result = returned['result']
    assert handle.status == status and result.error_code == code
    assert runtime.server.goal_callback(None) == 'reject'
    assert result.weather.daily == [] and runtime.fetches == 1
    runtime.release.set()
    node._worker.join(2)
    assert result.weather.daily == [] and result.error_code == code
    if end != 'close':
        assert runtime.server.goal_callback(None) == 'accept'
        next_handle = Handle()
        next_result = runtime.server.execute_callback(next_handle)
        assert next_handle.status == 'succeeded' and next_result.error_code == ''
        assert runtime.fetches == 2
    else:
        assert runtime.server.goal_callback(None) == 'reject'


def test_cancel_transition_wins_over_already_available_http_result(runtime):
    runtime.block = True
    node, handle, returned, thread = start(runtime)
    assert runtime.server.cancel_callback(handle) == 'accept'
    runtime.release.set()
    node._worker.join(2)
    assert handle.status == 'executing'
    handle.is_cancel_requested = True
    thread.join(2)
    assert not thread.is_alive()
    assert returned['result'].error_code == 'CANCELED'
    assert handle.status == 'canceled'


@pytest.mark.parametrize('finish_http', [True, False])
def test_previous_goal_cancel_cannot_affect_current_goal(runtime, finish_http):
    node = weather_action.create_weather_action_node(
        client=runtime.client, timeout_s=0.1,
    )
    assert runtime.server.goal_callback(None) == 'accept'
    previous = Handle()
    assert runtime.server.execute_callback(previous).error_code == ''
    node._worker.join(2)
    assert previous.status == 'succeeded'
    runtime.block = True
    runtime.started.clear()
    assert runtime.server.goal_callback(None) == 'accept'
    current, returned = Handle(), {}
    thread = Thread(target=lambda: returned.update(
        result=runtime.server.execute_callback(current),
    ))
    thread.start()
    try:
        assert runtime.started.wait(2)
        assert runtime.server.cancel_callback(previous) == 'reject'
        if finish_http:
            runtime.release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert returned['result'].error_code == ('' if finish_http else 'TIMEOUT')
        assert current.status == ('succeeded' if finish_http else 'aborted')
        assert not current.is_cancel_requested
        assert runtime.fetches == 2
    finally:
        runtime.release.set()
        node.close()
        thread.join(2)


def test_closed_accepted_goal_does_not_start_http(runtime):
    node = weather_action.create_weather_action_node(client=runtime.client)
    assert runtime.server.goal_callback(None) == 'accept'
    node.close()
    handle = Handle()
    result = runtime.server.execute_callback(handle)
    assert result.error_code == 'SHUTTING_DOWN' and handle.status == 'aborted'
    assert runtime.fetches == 0
    assert runtime.server.goal_callback(None) == 'reject'


@pytest.mark.parametrize('changes', [
    {'location': ''}, {'latitude': float('nan')}, {'longitude': float('nan')},
    {'timezone': 'unknown'},
])
def test_missing_configuration_fails_before_http(runtime, changes):
    runtime.params.update(changes)
    with pytest.raises(ValueError):
        weather_action.create_weather_action_node()
    assert runtime.fetches == 0 and runtime.destroyed == 1


@pytest.mark.parametrize('timeout', [True, 0, -1, float('inf'), '10'])
def test_invalid_timeout_fails_before_http(runtime, timeout):
    with pytest.raises(ValueError):
        weather_action.create_weather_action_node(client=runtime.client, timeout_s=timeout)
    assert runtime.fetches == 0


def test_main_closes_before_executor_and_node_cleanup(runtime):
    assert weather_action.main(['--ros-args']) == 0
    assert runtime.fetches == 0
    assert runtime.lifecycle == [
        ('init', ['--ros-args']), 'spin', 'executor_shutdown',
        'destroy_server', 'destroy_server', 'destroy_node', 'shutdown',
    ]


def test_missing_ros_performs_no_http(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, 'rclpy', None)
    assert weather_action.main([]) == 2
    assert 'rclpy is required' in capsys.readouterr().err
