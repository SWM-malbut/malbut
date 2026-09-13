"""Bounded KMA observation and full-day forecast requests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import math
import os
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode
from urllib.request import urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .weather import (
    MAX_RESPONSE_BYTES, SOURCE, WeatherForecast, WeatherState,
    _number, _text, _timestamp,
)


_SEOUL = ZoneInfo('Asia/Seoul')
_ENDPOINT = 'https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/'
_VILLAGE = 'getVilageFcst'
_OBSERVATION = 'getUltraSrtNcst'
_ROWS = 2000
_FORECAST_CATEGORIES = {'SKY', 'PTY', 'POP', 'TMN', 'TMX'}
_SEVERITY = {0: 0, 2: 1, 3: 2, 61: 3, 68: 4, 71: 5, 80: 6}


class KmaWeatherError(ValueError):
    """Only a stable code may cross the credential-bearing HTTP boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code='INVALID_DATA'):
    raise KmaWeatherError(code)


def kma_grid(latitude, longitude):
    """Convert WGS84 coordinates with KMA's 5 km Lambert grid constants."""
    latitude, longitude = _number(latitude), _number(longitude)
    if not -89 < latitude < 89 or not -180 <= longitude <= 180:
        raise ValueError('invalid KMA coordinates')
    radians = math.pi / 180
    first, second = 30 * radians, 60 * radians
    sn = math.log(math.cos(first) / math.cos(second)) / math.log(
        math.tan(math.pi / 4 + second / 2)
        / math.tan(math.pi / 4 + first / 2))
    sf = math.tan(math.pi / 4 + first / 2) ** sn * math.cos(first) / sn
    scale = 6371.00877 / 5 * sf
    origin = scale / math.tan(math.pi / 4 + 38 * radians / 2) ** sn
    radius = scale / math.tan(math.pi / 4 + latitude * radians / 2) ** sn
    angle = ((longitude - 126 + 180) % 360 - 180) * radians * sn
    nx = math.floor(radius * math.sin(angle) + 43.5)
    ny = math.floor(origin - radius * math.cos(angle) + 136.5)
    if not 1 <= nx <= 149 or not 1 <= ny <= 253:
        raise ValueError('coordinates outside KMA forecast grid')
    return nx, ny


def _base_time(now, *, observation=False):
    # The July 2026 KMA guide publishes both products ten minutes after base.
    eligible = now - timedelta(minutes=10)
    if observation:
        return eligible.replace(minute=0, second=0, microsecond=0)
    for hours in range(4):
        candidate = (eligible - timedelta(hours=hours)).replace(
            minute=0, second=0, microsecond=0)
        if candidate.hour in (2, 5, 8, 11, 14, 17, 20, 23):
            return candidate
    _fail()


def _api_error(code):
    if code in ('20', '30', '31', '32', '33'):
        _fail('KMA_AUTH_FAILED')
    if code in ('22', '23'):
        _fail('KMA_RATE_LIMITED')
    _fail('KMA_UNAVAILABLE')


def _hour(day, hour):
    if (not isinstance(day, str) or not isinstance(hour, str)
            or len(day) != 8 or len(hour) != 4 or not hour.endswith('00')):
        _fail()
    value = datetime.strptime(day + hour, '%Y%m%d%H%M').replace(tzinfo=_SEOUL)
    if value.strftime('%Y%m%d%H%M') != day + hour:
        _fail()
    return value


def _value(category, raw, *, observation):
    # These KMA categories have fixed units: degrees C and POP percent.
    if not isinstance(raw, str) or not raw or len(raw) > 32:
        _fail()
    value = float(raw)
    if not math.isfinite(value):
        _fail()
    if category in ('T1H', 'TMN', 'TMX'):
        if not -100 <= value <= 70:
            _fail()
    elif category == 'POP':
        if not 0 <= value <= 100:
            _fail()
    elif category == 'SKY':
        if value not in (1, 3, 4):
            _fail()
    elif category == 'PTY':
        if value not in (range(8) if observation else range(5)):
            _fail()
    return value


def _code(sky, precipitation):
    # KMA PTY does not specify intensity. Preserve type without guessing it.
    if precipitation in (1, 5):
        return 61
    if precipitation in (2, 6):
        return 68
    if precipitation in (3, 7):
        return 71
    if precipitation == 4:
        return 80
    return {1: 0, 3: 2, 4: 3}[sky]


class KmaWeatherClient:
    """Fetch at most five responses; never retain a URL or expose the key."""

    def __init__(self, location, latitude, longitude, timezone='Asia/Seoul',
                 *, service_key=None, clock=time.time):
        self.location = _text(location)
        self.latitude, self.longitude = _number(latitude), _number(longitude)
        self.nx, self.ny = kma_grid(self.latitude, self.longitude)
        if timezone != 'Asia/Seoul':
            raise ValueError('KMA weather requires Asia/Seoul timezone')
        self.timezone = timezone
        key = os.environ.get('KMA_SERVICE_KEY') if service_key is None else service_key
        self._service_key = unquote(key.strip()) if isinstance(key, str) else ''
        self._clock = clock

    def _request(self, product, base):
        params = {
            'serviceKey': self._service_key, 'pageNo': 1, 'numOfRows': _ROWS,
            'dataType': 'JSON', 'base_date': base.strftime('%Y%m%d'),
            'base_time': base.strftime('%H%M'), 'nx': self.nx, 'ny': self.ny,
        }
        try:
            with urlopen(_ENDPOINT + product + '?' + urlencode(params),
                         timeout=6.0) as response:
                if response.status in (401, 403):
                    _fail('KMA_AUTH_FAILED')
                if response.status == 429:
                    _fail('KMA_RATE_LIMITED')
                if response.status != 200:
                    _fail('KMA_UNAVAILABLE')
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                _fail()
            if body.lstrip().startswith(b'<'):
                document = ElementTree.fromstring(body)
                code = document.findtext('.//returnReasonCode')
                if code is not None:
                    _api_error(code)
                _fail()
            payload = json.loads(body)
            result = payload['response']
            if result['header']['resultCode'] != '00':
                _api_error(result['header']['resultCode'])
            data = result['body']
            items = data['items']['item']
            if (data['dataType'] != 'JSON' or type(data['pageNo']) is not int
                    or data['pageNo'] != 1 or type(data['totalCount']) is not int
                    or type(data['numOfRows']) is not int
                    or not 0 < data['numOfRows'] <= _ROWS
                    or not isinstance(items, list)
                    or not 0 < len(items) == data['totalCount'] <= data['numOfRows']):
                _fail()
            return self._items(items, product, base)
        except KmaWeatherError:
            raise
        except HTTPError as error:
            if error.code in (401, 403):
                raise KmaWeatherError('KMA_AUTH_FAILED') from None
            if error.code == 429:
                raise KmaWeatherError('KMA_RATE_LIMITED') from None
            raise KmaWeatherError('KMA_UNAVAILABLE') from None
        except (KeyError, TypeError, ValueError, ElementTree.ParseError):
            raise KmaWeatherError('INVALID_DATA') from None
        except Exception:
            raise KmaWeatherError('KMA_UNAVAILABLE') from None

    def _items(self, items, product, base):
        observation = product == _OBSERVATION
        relevant = {'T1H', 'PTY'} if observation else _FORECAST_CATEGORIES
        records, seen = {}, {}
        for item in items:
            if (item['baseDate'] != base.strftime('%Y%m%d')
                    or item['baseTime'] != base.strftime('%H%M')
                    or type(item['nx']) is not int or item['nx'] != self.nx
                    or type(item['ny']) is not int or item['ny'] != self.ny):
                _fail()
            category = item['category']
            if not isinstance(category, str) or not 1 <= len(category) <= 8:
                _fail()
            valid = base if observation else _hour(item['fcstDate'], item['fcstTime'])
            if not observation and not base < valid <= base + timedelta(days=6):
                _fail()
            raw = item['obsrValue' if observation else 'fcstValue']
            if not isinstance(raw, str) or not 0 < len(raw) <= 64:
                _fail()
            value = _value(category, raw, observation=observation) if category in relevant else raw
            identity = (valid, category)
            if identity in seen and seen[identity] != value:
                _fail()
            seen[identity] = value
            if category in relevant:
                records[identity] = value
        return records

    def fetch(self):
        """Combine a full-day baseline with the latest available forecast."""
        if (not self._service_key or len(self._service_key) > 512
                or any(ord(char) < 33 or ord(char) > 126 for char in self._service_key)):
            _fail('KMA_KEY_REQUIRED')
        try:
            started = _timestamp(self._clock())
            now = datetime.fromtimestamp(started, _SEOUL)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # 20:00 also supplies the preceding 23:00 SKY during midnight's
            # ten-minute wait for the first observation of the new day.
            baseline = midnight - timedelta(hours=4)
            latest, observation = _base_time(now), _base_time(now, observation=True)
            bases = {baseline, latest}
            # Today's final TMN appears at 02:00 and final TMX at 11:00;
            # later products omit them, so retain those published revisions.
            bases.update(midnight + timedelta(hours=hour) for hour in (2, 11)
                         if midnight + timedelta(hours=hour) < latest)
            requests = [(_VILLAGE, base) for base in sorted(bases)]
            requests.append((_OBSERVATION, observation))
            with ThreadPoolExecutor(max_workers=len(requests)) as pool:
                futures = [pool.submit(self._request, product, base)
                           for product, base in requests]
                responses = [future.result() for future in futures]
            forecasts, current = {}, responses[-1]
            for (_, base), response in zip(requests[:-1], responses[:-1]):
                if (base == midnight + timedelta(hours=2)
                        and (midnight + timedelta(hours=6), 'TMN') not in response):
                    _fail()
                if (base == midnight + timedelta(hours=11)
                        and (midnight + timedelta(hours=15), 'TMX') not in response):
                    _fail()
                forecasts.update(response)
            daily = []
            for offset in (0, 1):
                day = midnight + timedelta(days=offset)
                hours = [day + timedelta(hours=hour) for hour in range(24)]
                codes = [_code(forecasts[hour, 'SKY'], forecasts[hour, 'PTY']) for hour in hours]
                daily.append(WeatherForecast(
                    day.date().isoformat(),
                    forecasts[day + timedelta(hours=15), 'TMX'],
                    forecasts[day + timedelta(hours=6), 'TMN'],
                    max(forecasts[hour, 'POP'] for hour in hours),
                    max(codes, key=_SEVERITY.__getitem__),
                ))
            fetched = _timestamp(self._clock())
            valid_at = observation.timestamp()
            if (datetime.fromtimestamp(fetched, _SEOUL).date() != now.date()
                    or fetched < started or not 0 <= fetched - valid_at <= 4200):
                _fail()
            return WeatherState(
                fetched, valid_at, self.location, self.latitude, self.longitude,
                SOURCE, self.timezone, current[observation, 'T1H'],
                _code(forecasts[observation, 'SKY'], current[observation, 'PTY']),
                tuple(daily),
            )
        except KmaWeatherError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise KmaWeatherError('INVALID_DATA') from None
