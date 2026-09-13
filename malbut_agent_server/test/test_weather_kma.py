"""KMA request scheduling, full-day merging and credential-safe failures."""

from datetime import datetime, timedelta
import io
import json
import threading
import traceback
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from malbut_agent_server import weather_kma
from malbut_agent_server.weather import SOURCE, context_from_weather
from malbut_agent_server.weather_kma import (
    KmaWeatherClient, KmaWeatherError, kma_grid,
)


ZONE = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 9, 13, 12, 20, tzinfo=ZONE)
SECRET = 'test+secret/key=='


def payload_for(product, base, now=NOW):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    baseline = base < midnight
    items = []

    def item(category, value, valid=None):
        record = {
            'baseDate': base.strftime('%Y%m%d'), 'baseTime': base.strftime('%H%M'),
            'category': category, 'nx': 61, 'ny': 121,
        }
        if valid is None:
            record['obsrValue'] = str(value)
        else:
            record.update(fcstDate=valid.strftime('%Y%m%d'),
                          fcstTime=valid.strftime('%H%M'), fcstValue=str(value))
        items.append(record)

    if product == 'getUltraSrtNcst':
        item('T1H', 21.5)
        item('PTY', 0)
    else:
        valid = base + timedelta(hours=1)
        while valid < midnight + timedelta(days=2):
            today = valid.date() == now.date()
            precipitation = 4 if today and valid.hour == 18 else 0
            if not today and valid.hour == 9:
                precipitation = 2
            probability = 90 if today and valid.hour == 5 else 10
            if precipitation:
                probability = 60 if today else 70
            item('SKY', 4 if baseline else 1, valid)
            item('PTY', precipitation, valid)
            item('POP', probability, valid)
            if valid.hour == 6 and (not today or baseline or base.hour == 2):
                item('TMN', 18 if today and baseline else 17, valid)
            if valid.hour == 15 and (not today or baseline or base.hour in (2, 5, 8, 11)):
                maximum = 28 if today and base.hour == 11 else (26 if today else 27)
                item('TMX', 25 if baseline else maximum, valid)
            valid += timedelta(hours=1)
    return {'response': {
        'header': {'resultCode': '00', 'resultMsg': 'NORMAL_SERVICE'},
        'body': {'dataType': 'JSON', 'pageNo': 1, 'numOfRows': 2000,
                 'totalCount': len(items), 'items': {'item': items}},
    }}


@pytest.fixture
def http(monkeypatch):
    state = SimpleNamespace(
        calls=[], reads=[], mutate=lambda product, base, payload: None,
        now=NOW, status=200, body=None, failure=None, barrier=None,
    )

    class Response(io.BytesIO):
        def read(self, size=-1):
            state.reads.append(size)
            return super().read(size)

    def request(url, timeout):
        state.calls.append((url, timeout))
        if state.barrier is not None:
            state.barrier.wait(timeout=2)
        if state.failure is not None:
            raise state.failure
        query = parse_qs(urlsplit(url).query)
        product = urlsplit(url).path.rsplit('/', 1)[1]
        base = datetime.strptime(
            query['base_date'][0] + query['base_time'][0], '%Y%m%d%H%M',
        ).replace(tzinfo=ZONE)
        payload = payload_for(product, base, state.now)
        state.mutate(product, base, payload)
        body = json.dumps(payload).encode() if state.body is None else state.body
        response = Response(body)
        response.status = state.status
        return response

    monkeypatch.setattr(weather_kma, 'urlopen', request)
    return state


def client(now=NOW, **kwargs):
    return KmaWeatherClient(
        '경기도 수원시 팔달구 우만1동', 37.2825388888888, 127.031452777777,
        service_key=SECRET, clock=lambda: now.timestamp(), **kwargs,
    )


def rows(payload):
    return payload['response']['body']['items']['item']


def remove_rows(payload, predicate):
    body = payload['response']['body']
    body['items']['item'] = [row for row in rows(payload) if not predicate(row)]
    body['totalCount'] = len(body['items']['item'])


@pytest.mark.parametrize('latitude, longitude, expected', [
    (37.2825388888888, 127.031452777777, (61, 121)),
    (37.5635694444444, 126.980008333333, (60, 127)),
    (37.488201, 126.929810, (59, 125)),
])
def test_grid_matches_official_coordinates(latitude, longitude, expected):
    assert kma_grid(latitude, longitude) == expected


@pytest.mark.parametrize('latitude, longitude', [
    (True, 127), (float('nan'), 127), (float('inf'), 127),
    (90, 127), (-90, 127), (37, 181), (0, 0), (51.5, -0.1),
])
def test_coordinates_outside_kma_grid_are_rejected(latitude, longitude):
    with pytest.raises(ValueError):
        kma_grid(latitude, longitude)


@pytest.mark.parametrize('hour, request_count', [(12, 4), (14, 5)])
def test_fetch_requests_available_products_in_parallel_and_merges_full_days(
    http, hour, request_count,
):
    http.now = NOW.replace(hour=hour)
    weather = client(http.now)
    assert http.calls == []
    http.barrier = threading.Barrier(request_count)
    result = weather.fetch()
    assert result.source == SOURCE == '기상청 초단기실황·단기예보'
    assert result.temperature_c == 21.5
    assert result.weather_code == 0
    assert result.valid_at == http.now.replace(minute=0).timestamp()
    assert [entry.date for entry in result.daily] == ['2026-09-13', '2026-09-14']
    # The morning's 90% remains in today's maximum after a noon request.
    assert result.daily[0].precipitation_probability_max_pct == 90
    assert result.daily[0].temperature_min_c == 17
    assert result.daily[0].temperature_max_c == 28
    assert result.daily[0].weather_code == 80
    assert result.daily[1].temperature_min_c == 17
    assert result.daily[1].temperature_max_c == 27
    assert result.daily[1].precipitation_probability_max_pct == 70
    assert result.daily[1].weather_code == 68
    assert context_from_weather(result, clock=lambda: http.now.timestamp())['status'] == 'fresh'
    assert len(http.calls) == request_count
    assert http.reads == [weather_kma.MAX_RESPONSE_BYTES + 1] * request_count
    assert {timeout for _, timeout in http.calls} == {6.0}
    for url, _ in http.calls:
        assert urlsplit(url).netloc == 'apis.data.go.kr'
        query = parse_qs(urlsplit(url).query)
        assert query['nx'] == ['61'] and query['ny'] == ['121']
        assert query['numOfRows'] == ['2000']
        assert query['serviceKey'] == [SECRET]


@pytest.mark.parametrize('hour, minute, expected_village, expected_observation', [
    (0, 5, ('20260912', '2300'), ('20260912', '2300')),
    (0, 10, ('20260912', '2300'), ('20260913', '0000')),
    (2, 9, ('20260912', '2300'), ('20260913', '0100')),
    (2, 10, ('20260913', '0200'), ('20260913', '0200')),
    (11, 9, ('20260913', '0800'), ('20260913', '1000')),
    (11, 10, ('20260913', '1100'), ('20260913', '1100')),
    (23, 59, ('20260913', '2300'), ('20260913', '2300')),
])
def test_schedule_obeys_ten_minute_availability_and_midnight_coverage(
    http, hour, minute, expected_village, expected_observation,
):
    http.now = NOW.replace(hour=hour, minute=minute)
    result = client(http.now).fetch()
    schedules = []
    for url, _ in http.calls:
        query = parse_qs(urlsplit(url).query)
        schedules.append((urlsplit(url).path.rsplit('/', 1)[1],
                          (query['base_date'][0], query['base_time'][0])))
    assert ('getVilageFcst', ('20260912', '2000')) in schedules
    assert ('getVilageFcst', expected_village) in schedules
    assert ('getUltraSrtNcst', expected_observation) in schedules
    assert result.daily[0].date == '2026-09-13'


@pytest.mark.parametrize('hour, minute, today_bases, minimum, maximum', [
    (2, 9, [], 18, 25),
    (2, 10, ['0200'], 17, 26),
    (5, 10, ['0200', '0500'], 17, 26),
    (11, 9, ['0200', '0800'], 17, 26),
    (11, 10, ['0200', '1100'], 17, 28),
    (14, 10, ['0200', '1100', '1400'], 17, 28),
])
def test_latest_official_daily_extrema_are_retained_after_later_products_omit_them(
    http, hour, minute, today_bases, minimum, maximum,
):
    http.now = NOW.replace(hour=hour, minute=minute)
    result = client(http.now).fetch()
    requested = []
    for url, _ in http.calls:
        if urlsplit(url).path.endswith('getVilageFcst'):
            query = parse_qs(urlsplit(url).query)
            requested.append((query['base_date'][0], query['base_time'][0]))
    expected = [('20260912', '2000')]
    expected += [('20260913', base) for base in today_bases]
    if not today_bases:
        expected.append(('20260912', '2300'))
    assert sorted(requested) == sorted(expected)
    assert len(requested) == len(set(requested))
    assert result.daily[0].temperature_min_c == minimum
    assert result.daily[0].temperature_max_c == maximum


@pytest.mark.parametrize('hour, category, forecast_hour', [
    (2, 'TMN', '0600'), (11, 'TMX', '1500'),
])
def test_missing_official_extrema_revision_cannot_fall_back_to_previous_day(
    http, hour, category, forecast_hour,
):
    http.now = NOW.replace(hour=14, minute=10)

    def mutate(product, base, payload):
        if product == 'getVilageFcst' and base.date() == NOW.date() and base.hour == hour:
            remove_rows(payload, lambda row: row['category'] == category
                        and row['fcstDate'] == '20260913'
                        and row['fcstTime'] == forecast_hour)

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client(http.now).fetch()


def test_extra_official_revisions_merge_all_categories_in_base_time_order(http):
    http.now = NOW.replace(hour=14, minute=10)

    def mutate(product, base, payload):
        if product != 'getVilageFcst':
            return
        for row in rows(payload):
            if row['category'] in ('POP', 'PTY'):
                row['fcstValue'] = '0'
            if (row['category'] == 'POP' and row['fcstDate'] == '20260913'
                    and row['fcstTime'] == '2000'):
                row['fcstValue'] = str({20: 10, 2: 20, 11: 30, 14: 40}[base.hour])

    http.mutate = mutate
    assert client(http.now).fetch().daily[0].precipitation_probability_max_pct == 40


def test_missing_key_is_reported_only_on_fetch_without_http(http, monkeypatch):
    monkeypatch.delenv('KMA_SERVICE_KEY', raising=False)
    weather = KmaWeatherClient('서울', 37.5, 127)
    assert http.calls == []
    with pytest.raises(KmaWeatherError, match='^KMA_KEY_REQUIRED$'):
        weather.fetch()
    assert http.calls == []


@pytest.mark.parametrize('key', ['', ' ', '\n', 'secret\nvalue', 'x' * 513])
def test_invalid_key_never_reaches_http(http, key):
    weather = KmaWeatherClient('서울', 37.5, 127, service_key=key)
    with pytest.raises(KmaWeatherError, match='^KMA_KEY_REQUIRED$'):
        weather.fetch()
    assert http.calls == []


def test_encoded_environment_key_preserves_literal_plus(http, monkeypatch):
    monkeypatch.setenv('KMA_SERVICE_KEY', ' test%2Bsecret%2Fkey%3D%3D ')
    weather = KmaWeatherClient(
        '우만1동', 37.2825388888888, 127.031452777777,
        clock=lambda: NOW.timestamp(),
    )
    weather.fetch()
    assert SECRET not in repr(weather)
    for url, _ in http.calls:
        assert parse_qs(urlsplit(url).query)['serviceKey'] == [SECRET]


@pytest.mark.parametrize('code, expected', [
    ('20', 'KMA_AUTH_FAILED'), ('30', 'KMA_AUTH_FAILED'),
    ('31', 'KMA_AUTH_FAILED'), ('32', 'KMA_AUTH_FAILED'),
    ('33', 'KMA_AUTH_FAILED'), ('22', 'KMA_RATE_LIMITED'),
    ('23', 'KMA_RATE_LIMITED'),
    ('03', 'KMA_UNAVAILABLE'), ('99', 'KMA_UNAVAILABLE'),
])
def test_api_error_codes_are_sanitized(http, code, expected):
    def mutate(product, base, payload):
        payload['response']['header'] = {'resultCode': code, 'resultMsg': SECRET}

    http.mutate = mutate
    with pytest.raises(KmaWeatherError) as raised:
        client().fetch()
    assert raised.value.code == expected
    assert SECRET not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.parametrize('status, expected', [
    (401, 'KMA_AUTH_FAILED'), (403, 'KMA_AUTH_FAILED'),
    (429, 'KMA_RATE_LIMITED'), (500, 'KMA_UNAVAILABLE'),
])
@pytest.mark.parametrize('raise_http_error', [True, False])
def test_http_statuses_and_exception_urls_cannot_expose_key(
    http, status, expected, raise_http_error,
):
    if raise_http_error:
        http.failure = HTTPError('https://example.com/' + SECRET, status, SECRET, {}, None)
    else:
        http.status = status
    with pytest.raises(KmaWeatherError) as raised:
        client().fetch()
    assert raised.value.code == expected
    assert SECRET not in ''.join(traceback.format_exception(raised.value))


def test_transport_exception_text_is_not_exposed(http):
    http.failure = OSError('https://example.com/' + SECRET)
    with pytest.raises(KmaWeatherError, match='^KMA_UNAVAILABLE$') as raised:
        client().fetch()
    assert SECRET not in ''.join(traceback.format_exception(raised.value))


def test_gateway_xml_error_is_classified_without_disclosing_body(http):
    http.body = (
        '<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>' + SECRET
        + '</errMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader>'
        '</OpenAPI_ServiceResponse>'
    ).encode()
    with pytest.raises(KmaWeatherError, match='^KMA_AUTH_FAILED$') as raised:
        client().fetch()
    assert SECRET not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.parametrize('body', [
    b'not-json-secret', b'<broken', b'null', b'[]', b'{}',
    b'x' * (weather_kma.MAX_RESPONSE_BYTES + 1),
])
def test_invalid_or_oversized_body_is_rejected(http, body):
    http.body = body
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('field, value', [
    ('dataType', 'XML'), ('pageNo', 2), ('pageNo', True),
    ('numOfRows', 0), ('numOfRows', 2001), ('numOfRows', True),
    ('totalCount', 99999), ('totalCount', True),
])
def test_page_identity_and_truncation_are_rejected(http, field, value):
    def mutate(product, base, payload):
        payload['response']['body'][field] = value

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('field, value', [
    ('baseDate', '20260901'), ('baseTime', '0100'), ('nx', 60), ('ny', 127),
    ('nx', '61'), ('ny', True), ('category', ''), ('category', None),
])
def test_item_base_and_grid_identity_are_rejected(http, field, value):
    def mutate(product, base, payload):
        rows(payload)[0][field] = value

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('category, value', [
    ('SKY', '2'), ('SKY', 'nan'), ('PTY', '5'), ('PTY', '-1'),
    ('POP', '-1'), ('POP', '101'), ('POP', '50%'), ('POP', 50),
    ('TMN', '-900'), ('TMX', '100'), ('TMX', 'inf'), ('TMX', '25 C'),
])
def test_forecast_category_units_ranges_and_missing_sentinels(http, category, value):
    def mutate(product, base, payload):
        if product == 'getVilageFcst':
            for row in rows(payload):
                if row['category'] == category:
                    row['fcstValue'] = value

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('category, value', [
    ('T1H', '-900'), ('T1H', '99'), ('T1H', 'nan'), ('T1H', '20 C'),
    ('PTY', '8'), ('PTY', '0.5'),
])
def test_observation_values_cannot_be_missing_or_invalid(http, category, value):
    def mutate(product, base, payload):
        if product == 'getUltraSrtNcst':
            for row in rows(payload):
                if row['category'] == category:
                    row['obsrValue'] = value

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('category, hour', [
    ('POP', '0500'), ('SKY', '0500'), ('PTY', '0500'),
    ('TMN', '0600'), ('TMX', '1500'),
])
def test_missing_full_day_values_fail_instead_of_using_remaining_day(
    http, category, hour,
):
    def mutate(product, base, payload):
        if product == 'getVilageFcst':
            remove_rows(payload, lambda row: row['category'] == category
                        and row['fcstDate'] == '20260913' and row['fcstTime'] == hour)

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('category', ['T1H', 'PTY'])
def test_observation_requires_temperature_and_precipitation(http, category):
    def mutate(product, base, payload):
        if product == 'getUltraSrtNcst':
            remove_rows(payload, lambda row: row['category'] == category)

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('conflict', [True, False])
def test_duplicates_cannot_conflict_but_identical_values_are_safe(http, conflict):
    def mutate(product, base, payload):
        row = dict(rows(payload)[0])
        if conflict:
            row['obsrValue' if product == 'getUltraSrtNcst' else 'fcstValue'] = '3'
        rows(payload).append(row)
        payload['response']['body']['totalCount'] += 1

    http.mutate = mutate
    if conflict:
        with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
            client().fetch()
    else:
        assert client().fetch().temperature_c == 21.5


@pytest.mark.parametrize('field, value', [
    ('fcstTime', '1230'), ('fcstTime', '2400'), ('fcstTime', '100'),
    ('fcstDate', '20260230'), ('fcstDate', '20260930'), ('fcstDate', '20260901'),
])
def test_forecast_times_must_be_valid_hourly_and_after_base(http, field, value):
    def mutate(product, base, payload):
        if product == 'getVilageFcst':
            rows(payload)[0][field] = value

    http.mutate = mutate
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        client().fetch()


@pytest.mark.parametrize('precipitation, code', [
    (0, 0), (1, 61), (2, 68), (3, 71), (4, 80), (5, 61), (6, 68), (7, 71),
])
def test_current_precipitation_type_does_not_invent_intensity(http, precipitation, code):
    def mutate(product, base, payload):
        if product == 'getUltraSrtNcst':
            rows(payload)[1]['obsrValue'] = str(precipitation)

    http.mutate = mutate
    assert client().fetch().weather_code == code


@pytest.mark.parametrize('finished', [NOW - timedelta(seconds=1), NOW + timedelta(hours=2)])
def test_clock_regression_or_stale_observation_is_rejected(http, finished):
    times = iter([NOW.timestamp(), finished.timestamp()])
    weather = client()
    weather._clock = lambda: next(times)
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        weather.fetch()


def test_crossing_midnight_cannot_relabel_yesterdays_forecasts(http):
    http.now = NOW.replace(hour=23, minute=59, second=59)
    times = iter([http.now.timestamp(), (http.now + timedelta(seconds=2)).timestamp()])
    weather = client(http.now)
    weather._clock = lambda: next(times)
    with pytest.raises(KmaWeatherError, match='^INVALID_DATA$'):
        weather.fetch()


@pytest.mark.parametrize('zone', ['auto', 'UTC', 'America/New_York', '', None])
def test_kma_requires_korean_timezone(http, zone):
    with pytest.raises(ValueError, match='KMA weather requires'):
        client(timezone=zone)
    assert http.calls == []
