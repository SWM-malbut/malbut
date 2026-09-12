"""Validated on-demand Open-Meteo data and typed Action result decoding."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
import json
import math
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SOURCE = 'Open-Meteo weather model'
MAX_RESPONSE_BYTES = 1024 * 1024
CONDITIONS = {
    0: '맑음', 1: '대체로 맑음', 2: '부분적으로 흐림', 3: '흐림',
    45: '안개', 48: '착빙 안개',
    51: '약한 이슬비', 53: '이슬비', 55: '강한 이슬비',
    56: '약한 어는 이슬비', 57: '강한 어는 이슬비',
    61: '약한 비', 63: '비', 65: '강한 비', 66: '약한 어는 비', 67: '강한 어는 비',
    71: '약한 눈', 73: '눈', 75: '강한 눈', 77: '눈 알갱이',
    80: '약한 소나기', 81: '소나기', 82: '강한 소나기',
    85: '약한 눈 소나기', 86: '강한 눈 소나기',
    95: '뇌우', 96: '우박 동반 뇌우', 99: '강한 우박 동반 뇌우',
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('weather number must be finite')
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError('weather number is out of range') from error
    if not math.isfinite(result):
        raise ValueError('weather number must be finite')
    return result


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or any(ord(char) < 32 for char in value)):
        raise ValueError('invalid weather text')
    return value


def _timestamp(value):
    if isinstance(value, (int, float)):
        result = _number(value)
    else:
        seconds, nanos = value.sec, value.nanosec
        if (type(seconds) is not int or type(nanos) is not int
                or not 0 <= nanos < 1_000_000_000):
            raise ValueError('invalid ROS weather timestamp')
        result = seconds + nanos / 1_000_000_000
    if result < 0:
        raise ValueError('weather timestamp must be nonnegative')
    try:
        datetime.fromtimestamp(result, dt_timezone.utc)
    except (OverflowError, OSError) as error:
        raise ValueError('weather timestamp is out of range') from error
    return result


def _zone(value):
    try:
        return ZoneInfo(_text(value))
    except ZoneInfoNotFoundError as error:
        raise ValueError('unknown weather timezone') from error


def _weather_code(value):
    if type(value) is not int or value not in CONDITIONS:
        raise ValueError('unknown WMO weather code')


@dataclass(frozen=True)
class WeatherForecast:
    """One local date, with the day's most severe weather condition."""

    date: str
    temperature_max_c: float
    temperature_min_c: float
    precipitation_probability_max_pct: float
    weather_code: int

    def __post_init__(self):
        for name in ('temperature_max_c', 'temperature_min_c',
                     'precipitation_probability_max_pct'):
            object.__setattr__(self, name, _number(getattr(self, name)))
        if (not isinstance(self.date, str)
                or date.fromisoformat(self.date).isoformat() != self.date):
            raise ValueError('forecast date must be ISO YYYY-MM-DD')
        if _number(self.temperature_min_c) > _number(self.temperature_max_c):
            raise ValueError('forecast minimum exceeds maximum')
        if not 0 <= _number(self.precipitation_probability_max_pct) <= 100:
            raise ValueError('invalid precipitation probability')
        _weather_code(self.weather_code)


@dataclass(frozen=True)
class WeatherState:
    """A model snapshot; current and daily values are not observations."""

    fetched_at: float
    valid_at: float
    location: str
    latitude: float
    longitude: float
    source: str
    timezone: str
    temperature_c: float
    weather_code: int
    daily: tuple

    def __post_init__(self):
        for name in ('fetched_at', 'valid_at'):
            object.__setattr__(self, name, _timestamp(getattr(self, name)))
        for name in ('latitude', 'longitude', 'temperature_c'):
            object.__setattr__(self, name, _number(getattr(self, name)))
        if self.valid_at > self.fetched_at + 60:
            raise ValueError('weather validity exceeds its fetch time')
        _text(self.location)
        if (not -90 <= self.latitude <= 90
                or not -180 <= self.longitude <= 180):
            raise ValueError('invalid weather coordinates')
        if self.source != SOURCE:
            raise ValueError('weather source must identify Open-Meteo model')
        zone = _zone(self.timezone)
        _number(self.temperature_c)
        _weather_code(self.weather_code)
        object.__setattr__(self, 'daily', tuple(self.daily))
        today = datetime.fromtimestamp(self.fetched_at, zone).date()
        expected = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
        if (len(self.daily) != 2
                or not all(isinstance(item, WeatherForecast)
                           for item in self.daily)
                or [item.date for item in self.daily] != expected):
            raise ValueError('forecast must match fetched today and tomorrow')


def _from_message(message):
    if isinstance(message, WeatherState):
        return message
    try:
        return WeatherState(
            _timestamp(message.fetched_at), _timestamp(message.valid_at),
            message.location, message.latitude, message.longitude,
            message.source,
            message.timezone, message.temperature_c, message.weather_code,
            tuple(WeatherForecast(item.date, item.temperature_max_c,
                                  item.temperature_min_c,
                                  item.precipitation_probability_max_pct,
                                  item.weather_code)
                  for item in message.daily),
        )
    except (AttributeError, TypeError, OverflowError) as error:
        raise ValueError('invalid weather message') from error


class OpenMeteoClient:
    """Fetch one bounded response; API access occurs only in fetch()."""

    def __init__(self, location, latitude, longitude, timezone='Asia/Seoul',
                 *, clock=time.time):
        self.location = _text(location)
        self.latitude, self.longitude = _number(latitude), _number(longitude)
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ValueError('invalid weather coordinates')
        self.timezone = _text(timezone)
        if self.timezone != 'auto':
            _zone(self.timezone)
        self._clock = clock

    def fetch(self):
        """Request current and today/tomorrow data with unambiguous epochs."""
        params = {
            'latitude': self.latitude, 'longitude': self.longitude,
            'current': 'temperature_2m,weather_code',
            'daily': ('weather_code,temperature_2m_max,temperature_2m_min,'
                      'precipitation_probability_max'),
            'forecast_days': 2, 'timezone': self.timezone,
            'timeformat': 'unixtime', 'temperature_unit': 'celsius',
        }
        url = 'https://api.open-meteo.com/v1/forecast?' + urlencode(params)
        with urlopen(url, timeout=10.0) as response:
            if response.status != 200:
                raise ValueError('weather HTTP request failed')
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError('weather response exceeds size limit')
        payload = json.loads(body)
        fetched_at = _timestamp(self._clock())
        try:
            if not isinstance(payload, dict) or payload.get('error'):
                raise ValueError('invalid weather response')
            response_timezone = _text(payload['timezone'])
            _zone(response_timezone)
            if self.timezone != 'auto' and response_timezone != self.timezone:
                raise ValueError('unexpected weather timezone')
            current, daily = payload['current'], payload['daily']
            if (payload['current_units']['time'] != 'unixtime'
                    or payload['daily_units']['time'] != 'unixtime'
                    or payload['current_units']['temperature_2m'] != '°C'
                    or payload['daily_units']['temperature_2m_max'] != '°C'
                    or payload['daily_units']['temperature_2m_min'] != '°C'
                    or payload['daily_units'][
                        'precipitation_probability_max'] != '%'):
                raise ValueError('unexpected weather units')
            fields = ('time', 'temperature_2m_max', 'temperature_2m_min',
                      'precipitation_probability_max', 'weather_code')
            if any(not isinstance(daily[key], list) or len(daily[key]) != 2
                   for key in fields):
                raise ValueError('invalid daily weather arrays')
            offset = payload['utc_offset_seconds']
            if type(offset) is not int or not -50400 <= offset <= 50400:
                raise ValueError('invalid weather UTC offset')
            # Daily epochs use the response's fixed offset, including at DST.
            forecasts = tuple(WeatherForecast(
                datetime.fromtimestamp(
                    _timestamp(daily['time'][index]) + offset,
                    dt_timezone.utc).date().isoformat(),
                daily['temperature_2m_max'][index],
                daily['temperature_2m_min'][index],
                daily['precipitation_probability_max'][index],
                daily['weather_code'][index],
            ) for index in range(2))
            state = WeatherState(
                fetched_at, _timestamp(current['time']), self.location,
                self.latitude, self.longitude, SOURCE, response_timezone,
                current['temperature_2m'], current['weather_code'], forecasts,
            )
            if state.valid_at > fetched_at + 60:
                raise ValueError('weather timestamp is in the future')
            return state
        except (KeyError, TypeError, AttributeError, OverflowError) as error:
            raise ValueError('incomplete weather response') from error


def context_from_weather(message_or_state, *, clock=time.time):
    """Validate one result and format model evidence without retaining it."""
    state = _from_message(message_or_state) if message_or_state is not None else None
    now = _timestamp(clock())
    zone = _zone(state.timezone) if state is not None else dt_timezone.utc
    checked_at = datetime.fromtimestamp(now, zone)
    result = {'status': 'unavailable', 'checked_at': checked_at.isoformat()}
    if state is None:
        return result
    if state.fetched_at > now + 60 or state.valid_at > now + 60:
        raise ValueError('weather timestamp is in the future')
    result.update(
        status='stale', location=state.location, source=state.source,
        timezone=state.timezone,
        fetched_at=datetime.fromtimestamp(state.fetched_at, zone).isoformat(),
        valid_at=datetime.fromtimestamp(state.valid_at, zone).isoformat(),
    )
    if (now - state.fetched_at > 1800 or now - state.valid_at > 3600
            or state.daily[0].date != checked_at.date().isoformat()):
        return result
    result.update(
        status='fresh', latitude=state.latitude, longitude=state.longitude,
        current={'time': result['valid_at'],
                 'temperature_c': state.temperature_c,
                 'weather_code': state.weather_code,
                 'condition': CONDITIONS[state.weather_code]},
        daily=[{**asdict(item), 'condition': CONDITIONS[item.weather_code]}
               for item in state.daily],
    )
    return result


def _result_timestamp(value):
    if not isinstance(value, dict) or set(value) != {'sec', 'nanosec'}:
        raise ValueError('invalid Action weather timestamp')
    seconds, nanos = value['sec'], value['nanosec']
    if (type(seconds) is not int or type(nanos) is not int
            or not 0 <= nanos < 1_000_000_000):
        raise ValueError('invalid Action weather timestamp')
    return _timestamp(seconds + nanos / 1_000_000_000)


def decode_weather_result(value: dict, *, clock=time.time) -> dict:
    """Decode the typed GetWeather Result serialized by Manager as YAML."""
    if (not isinstance(value, dict)
            or set(value) != {'weather', 'error_code', 'message'}
            or value['error_code'] != ''
            or not isinstance(value['message'], str)):
        raise ValueError('invalid successful weather Action result')
    try:
        data = dict(value['weather'])
        for name in ('fetched_at', 'valid_at'):
            data[name] = _result_timestamp(data[name])
        if not isinstance(data['daily'], list) or len(data['daily']) != 2:
            raise ValueError('invalid weather forecast result')
        data['daily'] = tuple(WeatherForecast(**item) for item in data['daily'])
        return context_from_weather(WeatherState(**data), clock=clock)
    except (KeyError, TypeError, AttributeError, OverflowError,
            RecursionError) as error:
        raise ValueError('invalid weather Action result schema') from error
