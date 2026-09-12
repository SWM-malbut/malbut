"""Test weather HTTP and typed Action boundaries without making network requests."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from malbut_agent_server import weather
from malbut_agent_server.weather import (
    OpenMeteoClient, SOURCE, WeatherForecast, WeatherState,
    context_from_weather, decode_weather_result,
)


NOW = datetime(2026, 9, 12, 12, tzinfo=ZoneInfo('Asia/Seoul')).timestamp()


def state_at(now=NOW, zone='Asia/Seoul'):
    today = datetime.fromtimestamp(now, ZoneInfo(zone)).date()
    return WeatherState(
        now, now - 600, '시험 지역', 37.5, 127.0, SOURCE, zone, 24.5, 2,
        tuple(WeatherForecast((today + timedelta(days=index)).isoformat(),
                              27.0, 18.0, float(index * 40),
                              3 if index == 0 else 61)
              for index in range(2)),
    )


def api_payload(now=NOW, zone='Asia/Seoul'):
    local = datetime.fromtimestamp(now, ZoneInfo(zone))
    offset = int(local.utcoffset().total_seconds())
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    first = midnight.replace(tzinfo=timezone.utc).timestamp() - offset
    return {
        'timezone': zone, 'utc_offset_seconds': offset,
        'current_units': {'time': 'unixtime', 'temperature_2m': '°C'},
        'daily_units': {
            'time': 'unixtime', 'temperature_2m_max': '°C',
            'temperature_2m_min': '°C', 'precipitation_probability_max': '%',
        },
        'current': {
            'time': now - 600, 'temperature_2m': 24.5, 'weather_code': 2,
        },
        'daily': {
            # Open-Meteo uses one response offset and 24-hour daily steps.
            'time': [first, first + 86400],
            'temperature_2m_max': [27.0, 27.0],
            'temperature_2m_min': [18.0, 18.0],
            'precipitation_probability_max': [0.0, 40.0],
            'weather_code': [3, 61],
        },
    }


@pytest.fixture
def http(monkeypatch):
    boundary = SimpleNamespace(
        body=json.dumps(api_payload()).encode(), status=200,
        calls=[], read_sizes=[],
    )

    class Response(io.BytesIO):
        def read(self, size=-1):
            boundary.read_sizes.append(size)
            return super().read(size)

    def open_url(url, timeout):
        boundary.calls.append((url, timeout))
        response = Response(boundary.body)
        response.status = boundary.status
        return response

    monkeypatch.setattr(weather, 'urlopen', open_url)
    return boundary


def test_fetch_is_the_only_http_boundary_with_bounded_response(http):
    client = OpenMeteoClient('시험 지역', 37.5, 127.0, clock=lambda: NOW)
    assert http.calls == []
    assert client.fetch() == state_at()
    url, timeout = http.calls[0]
    assert timeout == 10.0
    assert urlsplit(url).netloc == 'api.open-meteo.com'
    query = parse_qs(urlsplit(url).query)
    assert query['forecast_days'] == ['2']
    assert query['timeformat'] == ['unixtime']
    assert query['timezone'] == ['Asia/Seoul']
    assert query['current'] == ['temperature_2m,weather_code']
    assert http.read_sizes == [weather.MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize('zone', ['Asia/Seoul', 'America/New_York'])
def test_auto_timezone_uses_resolved_zone_for_snapshot_and_local_dates(
    http, zone,
):
    http.body = json.dumps(api_payload(NOW, zone)).encode()
    client = OpenMeteoClient(
        '시험 지역', 37.5, 127.0, 'auto', clock=lambda: NOW,
    )
    assert http.calls == []
    snapshot = client.fetch()
    assert snapshot == state_at(NOW, zone)
    assert parse_qs(urlsplit(http.calls[0][0]).query)['timezone'] == ['auto']
    context = context_from_weather(snapshot, clock=lambda: NOW)
    expected_local = datetime.fromtimestamp(NOW, ZoneInfo(zone))
    assert context['status'] == 'fresh'
    assert context['timezone'] == zone
    assert context['fetched_at'] == expected_local.isoformat()
    assert context['current']['time'] == datetime.fromtimestamp(
        NOW - 600, ZoneInfo(zone),
    ).isoformat()
    assert context['daily'][0]['date'] == expected_local.date().isoformat()
    if zone == 'America/New_York':
        assert context['daily'][0]['date'] == '2026-09-11'


@pytest.mark.parametrize('zone', [
    'missing', 'auto', 'not/a/timezone', '', None, True, 32400, [], {},
])
def test_auto_timezone_rejects_missing_or_invalid_resolved_zone(http, zone):
    payload = api_payload()
    if zone == 'missing':
        del payload['timezone']
    else:
        payload['timezone'] = zone
    http.body = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        OpenMeteoClient(
            '시험 지역', 37.5, 127.0, 'auto', clock=lambda: NOW,
        ).fetch()


def test_integer_api_values_are_normalized_for_ros_float_fields(http):
    payload = api_payload()
    payload['current']['temperature_2m'] = 23
    payload['daily']['precipitation_probability_max'] = [0, 16]
    http.body = json.dumps(payload).encode()
    snapshot = OpenMeteoClient(
        '시험 지역', 37, 127, clock=lambda: NOW,
    ).fetch()
    assert type(snapshot.temperature_c) is float
    assert type(snapshot.latitude) is float
    assert all(type(item.precipitation_probability_max_pct) is float
               for item in snapshot.daily)


@pytest.mark.parametrize('failure', [
    'http', 'size', 'json', 'api_error', 'missing', 'null', 'nan', 'unit',
    'length', 'date', 'future', 'timezone', 'code',
])
def test_invalid_responses_cannot_become_weather_snapshots(http, failure):
    payload = api_payload()
    if failure == 'http':
        http.status = 429
    elif failure == 'size':
        http.body = b' ' * (weather.MAX_RESPONSE_BYTES + 1)
    elif failure == 'json':
        http.body = b'not JSON'
    elif failure == 'api_error':
        payload = {'error': True, 'reason': 'private server error'}
    elif failure == 'missing':
        del payload['current']
    elif failure in ('null', 'nan'):
        payload['current']['temperature_2m'] = (
            None if failure == 'null' else float('nan')
        )
    elif failure == 'unit':
        payload['daily_units']['temperature_2m_max'] = '°F'
    elif failure == 'length':
        payload['daily']['weather_code'].pop()
    elif failure == 'date':
        payload['daily']['time'][1] += 86400
    elif failure == 'future':
        payload['current']['time'] = NOW + 61
    elif failure == 'timezone':
        payload['timezone'] = 'UTC'
    elif failure == 'code':
        payload['current']['weather_code'] = 999
    if failure not in ('size', 'json'):
        http.body = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        OpenMeteoClient('시험 지역', 37.5, 127.0, clock=lambda: NOW).fetch()


@pytest.mark.parametrize('auto_timezone', [False, True])
@pytest.mark.parametrize('month,day,hour,minute,offset', [
    (3, 8, 12, 0, '-04:00'),
    (11, 1, 0, 30, '-04:00'),
    (11, 1, 12, 0, '-05:00'),
])
def test_epoch_forecast_dates_use_api_fixed_offset_across_dst(
    http, month, day, hour, minute, offset, auto_timezone,
):
    zone = 'America/New_York'
    local = datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo(zone))
    now = local.timestamp()
    payload = api_payload(now, zone)
    assert payload['daily']['time'][1] - payload['daily']['time'][0] == 86400
    http.body = json.dumps(payload).encode()
    snapshot = OpenMeteoClient(
        '시험 지역', 37.5, 127.0, 'auto' if auto_timezone else zone,
        clock=lambda: now,
    ).fetch()
    assert snapshot.timezone == zone
    assert [item.date for item in snapshot.daily] == [
        local.date().isoformat(),
        (local.date() + timedelta(days=1)).isoformat(),
    ]
    assert snapshot.valid_at == now - 600
    context = context_from_weather(snapshot, clock=lambda: now)
    assert context['status'] == 'fresh'
    assert context['checked_at'].endswith(offset)


@pytest.mark.parametrize('offset', [
    'missing', None, True, 32400.0, '32400', -50401, 50401,
])
def test_invalid_or_missing_api_utc_offset_is_rejected(http, offset):
    payload = api_payload()
    if offset == 'missing':
        del payload['utc_offset_seconds']
    else:
        payload['utc_offset_seconds'] = offset
    http.body = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        OpenMeteoClient('시험 지역', 37.5, 127.0, clock=lambda: NOW).fetch()


@pytest.mark.parametrize('kwargs', [
    {'location': ''}, {'latitude': float('nan')}, {'latitude': 91},
    {'longitude': 181}, {'longitude': True}, {'timezone': 'not/a/timezone'},
])
def test_invalid_location_configuration_never_calls_http(http, kwargs):
    config = {'location': '시험 지역', 'latitude': 37.5, 'longitude': 127.0}
    with pytest.raises(ValueError):
        OpenMeteoClient(**{**config, **kwargs})
    assert http.calls == []


def result_value(now=NOW):
    data = asdict(state_at(now))
    for name in ('fetched_at', 'valid_at'):
        data[name] = {'sec': int(data[name]), 'nanosec': 0}
    data['daily'] = list(data['daily'])
    return {'weather': data, 'error_code': '', 'message': ''}


def test_result_is_json_safe_model_evidence_without_http_or_retention(http):
    assert context_from_weather(None, clock=lambda: NOW)['status'] == 'unavailable'
    context = decode_weather_result(result_value(), clock=lambda: NOW)
    assert context['status'] == 'fresh'
    assert context['checked_at'] == '2026-09-12T12:00:00+09:00'
    assert context['current'] == {
        'time': '2026-09-12T11:50:00+09:00', 'temperature_c': 24.5,
        'weather_code': 2, 'condition': '부분적으로 흐림',
    }
    assert context['source'] == SOURCE
    assert context['daily'][1]['condition'] == '약한 비'
    json.dumps(context, allow_nan=False)
    context['daily'][0]['temperature_max_c'] = 999
    again = decode_weather_result(result_value(), clock=lambda: NOW)
    assert again['daily'][0]['temperature_max_c'] == 27.0
    assert http.calls == []


@pytest.mark.parametrize('fetched_age,valid_age,status', [
    (1800, 3600, 'fresh'), (1801, 3600, 'stale'), (0, 3601, 'stale'),
    (-60, -60, 'fresh'),
])
def test_result_age_is_rechecked_and_stale_numbers_are_hidden(
    fetched_age, valid_age, status,
):
    snapshot = replace(state_at(), fetched_at=NOW - fetched_age,
                       valid_at=NOW - valid_age)
    context = context_from_weather(snapshot, clock=lambda: NOW)
    assert context['status'] == status
    if status == 'stale':
        assert not {'current', 'daily', 'latitude', 'longitude'} & context.keys()


@pytest.mark.parametrize('field', ['fetched_at', 'valid_at'])
def test_future_result_is_rejected(field):
    with pytest.raises(ValueError):
        context_from_weather(replace(state_at(), **{field: NOW + 61}),
                             clock=lambda: NOW)


def test_midnight_cannot_relabel_yesterdays_forecast_as_today():
    fetched = datetime(
        2026, 9, 12, 23, 59, 30, tzinfo=ZoneInfo('Asia/Seoul'),
    ).timestamp()
    context = context_from_weather(state_at(fetched), clock=lambda: fetched + 45)
    assert context['checked_at'].startswith('2026-09-13')
    assert context['status'] == 'stale'


def test_result_accepts_ros_times_but_rejects_invalid_nanoseconds():
    data = asdict(state_at())
    for name in ('fetched_at', 'valid_at'):
        data[name] = SimpleNamespace(sec=int(data[name]), nanosec=0)
    data['daily'] = [SimpleNamespace(**item) for item in data['daily']]
    message = SimpleNamespace(**data)
    assert context_from_weather(message, clock=lambda: NOW)['status'] == 'fresh'
    message.fetched_at.nanosec = 1_000_000_000
    with pytest.raises(ValueError):
        context_from_weather(message, clock=lambda: NOW)


@pytest.mark.parametrize('failure', [
    'missing', 'extra', 'code', 'message', 'weather', 'timestamp', 'nanos',
    'boolean_seconds', 'missing_daily', 'forecast_type', 'unexpected_field',
    'invalid_source', 'future', 'invalid_temperature',
])
def test_invalid_typed_action_results_are_rejected(failure):
    value = result_value()
    if failure == 'missing':
        del value['weather']
    elif failure == 'extra':
        value['unexpected'] = ''
    elif failure == 'code':
        value['error_code'] = 'FETCH_FAILED'
    elif failure == 'message':
        value['message'] = 1
    elif failure == 'weather':
        value['weather'] = None
    elif failure == 'timestamp':
        value['weather']['fetched_at'] = 1
    elif failure == 'nanos':
        value['weather']['fetched_at']['nanosec'] = 1_000_000_000
    elif failure == 'boolean_seconds':
        value['weather']['fetched_at']['sec'] = True
    elif failure == 'missing_daily':
        value['weather']['daily'] = []
    elif failure == 'forecast_type':
        value['weather']['daily'][0] = 'bad'
    elif failure == 'unexpected_field':
        value['weather']['daily'][0]['unexpected'] = ''
    elif failure == 'invalid_source':
        value['weather']['source'] = 'unknown'
    elif failure == 'future':
        value['weather']['fetched_at']['sec'] = int(NOW + 61)
    elif failure == 'invalid_temperature':
        value['weather']['temperature_c'] = float('nan')
    with pytest.raises(ValueError):
        decode_weather_result(value, clock=lambda: NOW)


@pytest.mark.parametrize('changes', [
    {'temperature_c': True}, {'temperature_c': float('inf')},
    {'weather_code': False}, {'source': 'live physical sensor'},
    {'timezone': 'unknown'}, {'fetched_at': -1}, {'valid_at': NOW + 61},
    {'daily': ()},
])
def test_invalid_dto_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(state_at(), **changes)


@pytest.mark.parametrize('changes', [
    {'date': '20260912'}, {'temperature_min_c': 99},
    {'precipitation_probability_max_pct': -1},
    {'precipitation_probability_max_pct': None},
])
def test_invalid_daily_values_are_rejected(changes):
    with pytest.raises(ValueError):
        replace(state_at().daily[0], **changes)
