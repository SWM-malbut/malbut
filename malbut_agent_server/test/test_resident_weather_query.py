"""Exercise resident weather without a Manager or network access."""

from concurrent.futures import Future
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from malbut_agent_server.resident_weather_query import ResidentWeatherQuery
from test_weather import result_value
from test_weather_query import finish, start


@pytest.fixture
def resident(monkeypatch):
    modules = {name: ModuleType(name) for name in (
        'malbut_interfaces', 'malbut_interfaces.action', 'rclpy', 'rclpy.action',
        'rosidl_runtime_py', 'rosidl_runtime_py.convert',
        'unique_identifier_msgs', 'unique_identifier_msgs.msg')}
    for name in ('GetWeather', 'ExecuteMission'):
        setattr(modules['malbut_interfaces.action'], name,
                SimpleNamespace(Goal=type(name + 'Goal', (SimpleNamespace,), {}),
                                Result=type(name + 'Result', (SimpleNamespace,), {})))
    modules['unique_identifier_msgs.msg'].UUID = SimpleNamespace
    modules['rosidl_runtime_py.convert'].message_to_yaml = (
        lambda value: yaml.safe_dump(vars(value)))
    clients = {}

    class Client:
        def __init__(self, node, action_type, endpoint):
            self.type, self.endpoint = action_type, endpoint
            self.ready, self.destroyed, self.requests = True, False, []
            clients[endpoint] = self

        def server_is_ready(self):
            return self.ready

        def send_goal_async(self, goal, *, goal_uuid):
            pending = SimpleNamespace(goal=goal, goal_id=goal_uuid, future=Future())
            self.requests.append(pending)
            return pending.future

        def destroy(self):
            self.destroyed = True

    modules['rclpy.action'].ActionClient = Client
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    query = ResidentWeatherQuery(SimpleNamespace(), timeout_s=0.1)
    yield SimpleNamespace(query=query, clients=clients)
    query.close()


def accept(client, *, accepted=True, goal_id=None):
    sent = client.requests[-1]
    handle = SimpleNamespace(goal_id=goal_id or sent.goal_id, accepted=accepted,
                             result=Future(), cancels=[])
    handle.get_result_async = lambda: handle.result
    handle.cancel_goal_async = lambda: handle.cancels.append(True) or Future()
    sent.future.set_result(handle)
    return handle


def test_resident_weather_uses_fixed_public_action_and_existing_decoder(resident):
    query = resident.query
    result, done, thread = start(query)
    query.drain()
    assert set(resident.clients) == {'/malbut/weather/get', '/malbut/weather/location/set'}
    client = resident.clients['/malbut/weather/get']
    assert vars(client.requests[0].goal) == {}
    handle = accept(client)
    handle.result.set_result(SimpleNamespace(
        status=4, result=client.type.Result(**result_value(time.time()))))
    finish(done, thread)
    assert result['value']['status'] == 'fresh'
    assert query._actions._requests == {}, 'terminal weather is not retained'


def test_location_uses_restricted_weather_action_payload(resident):
    result, done, thread = start(resident.query, location='대전 유성구')
    resident.query.drain()
    client = resident.clients['/malbut/weather/location/set']
    goal = client.requests[0].goal
    assert goal.capability_id == 'set_weather_location'
    assert yaml.safe_load(goal.arguments_yaml) == {'location': '대전 유성구'}
    handle = accept(client)
    handle.result.set_result(SimpleNamespace(status=4, result=client.type.Result(
        mission_id='', result_yaml=yaml.safe_dump({
            'status': 'location_set', 'location': '대전 유성구'}), message='')))
    finish(done, thread)
    assert result['value'] == {'status': 'location_set', 'location': '대전 유성구'}


def test_location_required_survives_typed_failure_without_default_weather(resident):
    result, done, thread = start(resident.query)
    resident.query.drain()
    client = resident.clients['/malbut/weather/get']
    handle = accept(client)
    handle.result.set_result(SimpleNamespace(status=6, result=client.type.Result(
        weather={}, error_code='LOCATION_REQUIRED', message='LOCATION_REQUIRED')))
    finish(done, thread)
    assert result['value'] == {'status': 'location_required'}


def test_timed_out_goal_is_not_retried_and_late_acceptance_is_canceled(resident):
    result, done, thread = start(resident.query, location='대전')
    resident.query.drain()
    finish(done, thread)
    assert isinstance(result['error'], TimeoutError)
    resident.query.drain()
    client = resident.clients['/malbut/weather/location/set']
    handle = accept(client)
    assert handle.cancels == [True]
    assert len(client.requests) == 1


@pytest.mark.parametrize('failure', [
    'unavailable', 'rejected', 'identity', 'invalid_status', 'failed'])
def test_unconfirmed_weather_never_returns_values(resident, failure):
    client = resident.clients['/malbut/weather/get']
    client.ready = failure != 'unavailable'
    result, done, thread = start(resident.query)
    resident.query.drain()
    if failure != 'unavailable':
        handle = accept(client, accepted=failure != 'rejected',
                        goal_id=SimpleNamespace(uuid=[0] * 16) if failure == 'identity' else None)
        if failure in ('invalid_status', 'failed'):
            handle.result.set_result(SimpleNamespace(
                status=1 if failure == 'invalid_status' else 6,
                result=client.type.Result(**result_value(time.time()))))
    finish(done, thread)
    assert 'error' in result and 'value' not in result


def test_close_releases_worker_and_cancels_only_its_own_weather_goal(resident):
    result, done, thread = start(resident.query)
    resident.query.drain()
    handle = accept(resident.clients['/malbut/weather/get'])
    resident.query.close()
    finish(done, thread)
    assert 'error' in result
    assert handle.cancels == [True]
    assert all(client.destroyed for client in resident.clients.values())
