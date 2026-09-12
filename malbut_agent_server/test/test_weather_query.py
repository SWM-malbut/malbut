"""Verify Manager ownership, result identity and bounded dialogue waits."""

from concurrent.futures import CancelledError
from threading import Event, Thread, get_ident
import time

import pytest
import yaml

from malbut_agent_server.weather_query import ManagerWeatherQuery
from test_weather import result_value


class Manager:
    def __init__(self, kind='succeeded', result=None):
        self.calls = []
        self.kind = kind
        self.result = result or yaml.safe_dump(result_value(time.time()))
        self.cancels = []

    def submit(self, capability_id, arguments, request_id):
        self.calls.append((capability_id, arguments, request_id, get_ident()))

    def cancel(self, key):
        self.cancels.append((key, get_ident()))

    def snapshot(self, key):
        return {
            'request_id': key, 'capability_id': 'get_weather',
            'kind': self.kind, 'terminal': self.kind != 'accepted',
            'result_yaml': self.result,
        }


def start(query, request_id='request-1', *, location=None):
    result = {}
    done = Event()

    def run():
        try:
            result['value'] = (query.execute(request_id) if location is None
                               else query.set_location(request_id, location))
        except Exception as error:
            result['error'] = error
        finally:
            done.set()

    thread = Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 2
    while not query._pending and not done.is_set():
        if time.monotonic() >= deadline:
            raise AssertionError('Weather request was not queued')
        time.sleep(0.001)
    return result, done, thread


def finish(done, thread):
    assert done.wait(2), 'Worker did not finish'
    thread.join(2)
    assert not thread.is_alive()


def test_worker_does_not_touch_ros_until_owning_thread_drains():
    manager = Manager()
    query = ManagerWeatherQuery(manager)
    result, done, thread = start(query)
    try:
        assert manager.calls == []
        assert not done.is_set()
        query.drain()
        finish(done, thread)
        assert result['value']['status'] == 'fresh'
        assert manager.calls == [
            ('get_weather', {}, 'weather-query:request-1', get_ident()),
        ]
        assert thread.ident != get_ident()
    finally:
        query.close()


def test_only_matching_manager_result_completes_waiting_tool():
    manager = Manager(kind='accepted')
    query = ManagerWeatherQuery(manager)
    result, done, thread = start(query)
    try:
        query.drain()
        event = {**manager.snapshot('weather-query:request-1'),
                 'kind': 'succeeded', 'terminal': True}
        assert not query.handle({**event, 'capability_id': 'navigate'})
        assert query.handle({**event, 'request_id': 'weather-query:different'})
        assert not done.is_set()
        assert query.handle(event)
        finish(done, thread)
        assert result['value']['status'] == 'fresh'
    finally:
        query.close()


@pytest.mark.parametrize('kind', ['unavailable', 'rejected', 'failed', 'unknown', 'canceled'])
def test_manager_failure_never_returns_weather_values(kind):
    query = ManagerWeatherQuery(Manager(kind))
    result, done, thread = start(query)
    try:
        query.drain()
        finish(done, thread)
        assert isinstance(result['error'], RuntimeError)
        assert 'value' not in result
    finally:
        query.close()


@pytest.mark.parametrize('raw', [
    '[]', 'context_json: not-json', 'unexpected: value', 'x' * 16385,
])
def test_invalid_action_result_is_not_exposed_as_weather(raw):
    query = ManagerWeatherQuery(Manager(result=raw))
    result, done, thread = start(query)
    try:
        query.drain()
        finish(done, thread)
        assert isinstance(result['error'], ValueError)
        assert 'value' not in result
    finally:
        query.close()


def test_timeout_is_not_retried_and_late_event_is_consumed():
    manager = Manager(kind='accepted')
    query = ManagerWeatherQuery(manager, timeout_s=0.05)
    result, done, thread = start(query)
    query.drain()
    finish(done, thread)
    assert isinstance(result['error'], TimeoutError)
    assert query.handle({**manager.snapshot('weather-query:request-1'),
                         'kind': 'succeeded', 'terminal': True})
    query.drain()
    assert len(manager.calls) == 1
    assert manager.cancels == [('weather-query:request-1', get_ident())]
    query.drain()
    assert len(manager.cancels) == 1
    assert query._pending == {}
    query.close()


def test_close_releases_waiting_worker_before_ros_shutdown():
    query = ManagerWeatherQuery(Manager(kind='accepted'))
    result, done, thread = start(query)
    query.drain()
    query.close()
    query.drain()
    finish(done, thread)
    assert query._manager.cancels == [('weather-query:request-1', get_ident())]
    assert isinstance(result['error'], CancelledError)
    with pytest.raises(CancelledError):
        query.execute('after-close')


@pytest.mark.parametrize('kind,code,expected', [
    ('failed', 'LOCATION_REQUIRED', 'location_required'),
    ('failed', 'FETCH_FAILED', None),
    ('unknown', 'LOCATION_REQUIRED', None),
    ('succeeded', 'LOCATION_REQUIRED', None),
])
def test_only_known_aborted_missing_location_requests_user_input(kind, code, expected):
    value = result_value(time.time())
    value['error_code'] = code
    query = ManagerWeatherQuery(Manager(kind, yaml.safe_dump(value)))
    snapshot = query._manager.snapshot
    query._manager.snapshot = lambda key: {**snapshot(key), 'ros_status': 6}
    result, done, thread = start(query)
    try:
        query.drain()
        finish(done, thread)
        if expected:
            assert result == {'value': {'status': expected}}
        else:
            assert 'error' in result and 'value' not in result
    finally:
        query.close()


class LocationManager(Manager):
    def snapshot(self, key):
        return {**super().snapshot(key), 'capability_id': 'set_weather_location'}


@pytest.mark.parametrize('context', [
    {'status': 'location_set', 'location': '경기도 수원시 팔달구 우만1동'},
    {'status': 'location_ambiguous', 'candidates': ['수원시 우만1동', '수원시 우만2동']},
    {'status': 'location_not_found'},
])
def test_setting_wraps_only_location_and_waits_for_matching_manager(context):
    envelope = {'mission_id': 'child', 'message': '', 'result_yaml': yaml.safe_dump(context)}
    manager = LocationManager(result=yaml.safe_dump(envelope))
    query = ManagerWeatherQuery(manager)
    result, done, thread = start(query, location='수원시 우만1동')
    try:
        assert manager.calls == []
        query.drain()
        finish(done, thread)
        assert result == {'value': context}
        capability, arguments, request_id, owner = manager.calls[0]
        assert capability == 'set_weather_location'
        assert set(arguments) == {'arguments_yaml'}
        assert yaml.safe_load(arguments['arguments_yaml']) == {'location': '수원시 우만1동'}
        assert request_id == 'weather-query:set:request-1' and owner == get_ident()
    finally:
        query.close()


@pytest.mark.parametrize('context', [
    {'status': 'location_set'}, {'status': 'location_set', 'location': ''},
    {'status': 'location_set', 'location': '수원', 'latitude': 37},
    {'status': 'location_ambiguous', 'candidates': ['수원']},
    {'status': 'location_ambiguous', 'candidates': ['수원', {}]},
    {'status': 'fresh'}, [],
])
def test_malformed_setting_reply_never_claims_saved(context):
    envelope = {'mission_id': 'child', 'message': '', 'result_yaml': yaml.safe_dump(context)}
    query = ManagerWeatherQuery(LocationManager(result=yaml.safe_dump(envelope)))
    result, done, thread = start(query, location='수원시 우만1동')
    try:
        query.drain()
        finish(done, thread)
        assert isinstance(result['error'], ValueError) and 'value' not in result
    finally:
        query.close()
