"""Test weather values and typed Action boundaries without network requests."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta
import json
import socket
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from malbut_agent_server.weather import (
    SOURCE, WeatherForecast, WeatherState,
    context_from_weather, daily_forecast_reply, decode_weather_result,
)
from malbut_agent_server.prompting import bounded_weather_context


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


def result_value(now=NOW):
    data = asdict(state_at(now))
    for name in ('fetched_at', 'valid_at'):
        data[name] = {'sec': int(data[name]), 'nanosec': 0}
    data['daily'] = list(data['daily'])
    return {'weather': data, 'error_code': '', 'message': ''}


def test_result_is_json_safe_model_evidence_without_http_or_retention(monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError('Result decoding must not create network sockets')

    monkeypatch.setattr(socket, 'socket', forbid_network)
    assert context_from_weather(None, clock=lambda: NOW)['status'] == 'unavailable'
    context = decode_weather_result(result_value(), clock=lambda: NOW)
    assert context['status'] == 'fresh'
    assert context['checked_at'] == '2026-09-12T12:00:00+09:00'
    assert context['current'] == {
        'time': '2026-09-12T11:50:00+09:00', 'temperature_c': 24.5,
        'weather_code': 2, 'condition': '부분적으로 흐림',
    }
    assert context['source'] == SOURCE
    assert context['daily'][1]['condition'] == '비'
    json.dumps(context, allow_nan=False)
    context['daily'][0]['temperature_max_c'] = 999
    again = decode_weather_result(result_value(), clock=lambda: NOW)
    assert again['daily'][0]['temperature_max_c'] == 27.0


@pytest.mark.parametrize('fetched_age,valid_age,status', [
    (1800, 4200, 'fresh'), (1801, 4200, 'stale'), (0, 4201, 'stale'),
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
    'invalid_source', 'previous_provider', 'future', 'invalid_temperature',
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
    elif failure == 'previous_provider':
        value['weather']['source'] = 'Open-Meteo weather model'
    elif failure == 'future':
        value['weather']['fetched_at']['sec'] = int(NOW + 61)
    elif failure == 'invalid_temperature':
        value['weather']['temperature_c'] = float('nan')
    with pytest.raises(ValueError):
        decode_weather_result(value, clock=lambda: NOW)


@pytest.mark.parametrize('changes', [
    {'temperature_c': True}, {'temperature_c': float('inf')},
    {'weather_code': False}, {'source': 'live physical sensor'},
    {'source': 'Open-Meteo weather model'},
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


def test_kma_source_and_numeric_fields_survive_typed_result():
    snapshot = replace(state_at(), latitude=37, longitude=127, temperature_c=23)
    assert snapshot.source == '기상청 초단기실황·단기예보'
    assert type(snapshot.temperature_c) is float
    assert type(snapshot.latitude) is float
    assert all(type(item.precipitation_probability_max_pct) is float
               for item in snapshot.daily)


@pytest.mark.parametrize('code,condition', [
    (61, '비'), (68, '비 또는 눈'), (71, '눈'), (80, '소나기'),
])
def test_kma_precipitation_conditions_do_not_invent_intensity(code, condition):
    snapshot = replace(
        state_at(), weather_code=code,
        daily=tuple(replace(item, weather_code=code) for item in state_at().daily),
    )
    context = context_from_weather(snapshot, clock=lambda: NOW)
    assert context['current']['condition'] == condition
    assert all(item['condition'] == condition for item in context['daily'])


@pytest.mark.parametrize('codes, probabilities', [
    ([], [0] * 24), ([0] * 24, [0] * 23), ([0] * 23, [0] * 23),
    (None, []), ('', ''), ([True] * 24, [0] * 24),
    ([8] * 24, [0] * 24), ([0] * 24, [101] * 24), ([0] * 24, [float('nan')] * 24),
])
def test_invalid_or_partial_hourly_series_is_rejected(codes, probabilities):
    value = result_value()
    value['weather']['daily'][0].update(hourly_weather_codes=codes,
                                        hourly_precipitation_probability_pct=probabilities)
    with pytest.raises(ValueError):
        decode_weather_result(value, clock=lambda: NOW)


def test_cached_result_drops_rain_at_hour_boundary_and_preserves_daily_forecast():
    fetched = NOW - 60
    value = result_value(fetched)
    today = value['weather']['daily'][0]
    today.update(hourly_weather_codes=[0] * 10 + [61, 61] + [3] * 12,
                 hourly_precipitation_probability_pct=[30] * 10 + [80, 75] + [10] * 12,
                 weather_code=61, precipitation_probability_max_pct=80)
    before = decode_weather_result(value, clock=lambda: fetched)
    after = decode_weather_result(value, clock=lambda: NOW)
    assert before['remaining_today'] == {
        'from_hour': 11, 'condition': '비', 'precipitation_periods': ['11시부터 12시까지 비'],
        'precipitation_probability_max_pct': 75,
    }
    assert after['remaining_today'] == {
        'from_hour': 12, 'condition': '흐림', 'precipitation_periods': [],
        'precipitation_probability_max_pct': 10,
    }
    assert before['daily'] == after['daily']
    assert after['daily'][0]['precipitation_probability_max_pct'] == 80
    reply = daily_forecast_reply('오늘 날씨 알려줘', after, {})
    assert '흐린 시간대' in reply and '10퍼센트' in reply and '우산' not in reply


def test_observed_rain_has_its_own_time_even_when_forecast_is_dry():
    snapshot = state_at(NOW + 1200)
    today = replace(snapshot.daily[0], hourly_weather_codes=[0] * 24,
                    hourly_precipitation_probability_pct=[0] * 24)
    snapshot = replace(snapshot, weather_code=61, valid_at=NOW, daily=(today, snapshot.daily[1]))
    context = context_from_weather(snapshot, clock=lambda: NOW + 1200)
    reply = daily_forecast_reply('오늘 날씨 알려줘', context, {})
    assert '12시 관측 결과는 비' in reply and '우산' in reply and '0퍼센트' in reply
    assert '그쳤' not in reply
    del context['current']['time']
    assert daily_forecast_reply('오늘 날씨 알려줘', context, {}) is None


def test_separate_wet_periods_fit_the_model_context_at_midnight():
    now = NOW - 12 * 3600
    snapshot = state_at(now)
    today = replace(snapshot.daily[0], hourly_weather_codes=[61, 71] * 12,
                    hourly_precipitation_probability_pct=[95] * 24)
    snapshot = replace(snapshot, daily=(today, replace(today, date=snapshot.daily[1].date)))
    context = bounded_weather_context(context_from_weather(snapshot, clock=lambda: now))
    periods = context['remaining_today']['precipitation_periods']
    assert len(periods) == 24
    assert periods[:2] == ['0시부터 1시까지 비', '1시부터 2시까지 눈']
    assert periods[-1] == '23시부터 24시까지 눈'
    assert 'hourly_weather_codes' not in context['daily'][0]
