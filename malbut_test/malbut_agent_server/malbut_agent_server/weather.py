"""Validated KMA weather data and typed Action result decoding."""

from array import array
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from itertools import groupby
import math
from operator import itemgetter
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from malbut_agent_server.conversation_preferences import DEFAULTS


SOURCE = '기상청 초단기실황·단기예보'
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_OBSERVATION_AGE_S = 4200
CONDITIONS = {
    0: '맑음', 1: '대체로 맑음', 2: '부분적으로 흐림', 3: '흐림',
    45: '안개', 48: '착빙 안개',
    51: '약한 이슬비', 53: '이슬비', 55: '강한 이슬비',
    56: '약한 어는 이슬비', 57: '강한 어는 이슬비',
    61: '비', 63: '비', 65: '강한 비', 66: '약한 어는 비', 67: '강한 어는 비',
    68: '비 또는 눈', 71: '눈', 73: '눈', 75: '강한 눈', 77: '눈 알갱이',
    80: '소나기', 81: '소나기', 82: '강한 소나기',
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
    hourly_weather_codes: tuple = ()
    hourly_precipitation_probability_pct: tuple = ()

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
        codes = self.hourly_weather_codes
        probabilities = self.hourly_precipitation_probability_pct
        if (not isinstance(codes, (tuple, list, array))
                or not isinstance(probabilities, (tuple, list, array))
                or len(codes) != len(probabilities) or len(codes) not in (0, 24)):
            raise ValueError('weather needs both complete hourly series or neither')
        for code in codes:
            _weather_code(code)
        probabilities = tuple(_number(value) for value in probabilities)
        if any(not 0 <= value <= 100 for value in probabilities):
            raise ValueError('invalid hourly precipitation probability')
        object.__setattr__(self, 'hourly_weather_codes', tuple(codes))
        object.__setattr__(self, 'hourly_precipitation_probability_pct', probabilities)


@dataclass(frozen=True)
class WeatherState:
    """Regional KMA observations and forecasts, not robot sensor readings."""

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
            raise ValueError('weather source must identify KMA observations and forecasts')
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
                                  item.weather_code, getattr(item, 'hourly_weather_codes', ()),
                                  getattr(item, 'hourly_precipitation_probability_pct', ()))
                  for item in message.daily),
        )
    except (AttributeError, TypeError, OverflowError) as error:
        raise ValueError('invalid weather message') from error


def _remaining_today(forecast, hour):
    codes = forecast.hourly_weather_codes[hour:]
    periods = []
    for code, group in groupby(enumerate(codes, hour), key=itemgetter(1)):
        if code >= 51:
            hours = [index for index, _ in group]
            periods.append(f'{hours[0]}시부터 {hours[-1] + 1}시까지 {CONDITIONS[code]}')
    return {
        'from_hour': hour,
        'condition': CONDITIONS[max(codes)],
        'precipitation_periods': periods,
        'precipitation_probability_max_pct': max(
            forecast.hourly_precipitation_probability_pct[hour:]),
    }


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
    if (now - state.fetched_at > 1800 or now - state.valid_at > MAX_OBSERVATION_AGE_S
            or state.daily[0].date != checked_at.date().isoformat()):
        return result
    result.update(
        status='fresh', latitude=state.latitude, longitude=state.longitude,
        current={'time': result['valid_at'],
                 'temperature_c': state.temperature_c,
                 'weather_code': state.weather_code,
                 'condition': CONDITIONS[state.weather_code]},
        daily=[{'date': item.date, 'temperature_max_c': item.temperature_max_c,
                'temperature_min_c': item.temperature_min_c,
                'precipitation_probability_max_pct': item.precipitation_probability_max_pct,
                'weather_code': item.weather_code, 'condition': CONDITIONS[item.weather_code]}
               for item in state.daily],
    )
    if state.daily[0].hourly_weather_codes:
        result['remaining_today'] = _remaining_today(state.daily[0], checked_at.hour)
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


def basic_forecast_day(utterance):
    """Recognize self-contained daily questions; other wording follows ordinary dialogue."""
    match = re.fullmatch(
        r'(오늘|내일)의?날씨(?:예보)?(?:는|가|를)?'
        r'(?:알려(?:줘요?|주세요)|어때요?|어떤가요|어떻(?:니|나요))?[.!?]*',
        ''.join(utterance.split()),
    )
    return match.group(1) if match else None


def _today_reply(weather, forecast, checked_at, location):
    low, high = forecast.temperature_min_c, forecast.temperature_max_c
    remaining = weather.get('remaining_today')
    if remaining is None:
        probability = forecast.precipitation_probability_max_pct
        return (f'오늘 하루 전체 예보는 {CONDITIONS[forecast.weather_code]}입니다. '
                f'{location} 기준 최저 {low:g}도, 최고 {high:g}도, '
                f'하루 최대 강수 확률은 {probability:g}퍼센트예요. '
                '남은 시간의 강수 여부는 확인되지 않았어요.')

    periods = remaining['precipitation_periods']
    condition = remaining['condition']
    probability = _number(remaining['precipitation_probability_max_pct'])
    if (remaining['from_hour'] != checked_at.hour or condition not in CONDITIONS.values()
            or not 0 <= probability <= 100 or not isinstance(periods, list) or len(periods) > 24):
        raise ValueError('invalid remaining-day forecast')
    if periods:
        headline = ', '.join(_text(period) for period in periods[:2]) + ' 예보가 있어요.'
        if len(periods) > 2:
            headline += ' 그 뒤에도 강수 예보가 있어요.'
    else:
        sky = {'맑음': '맑겠고', '대체로 맑음': '대체로 맑겠고',
               '부분적으로 흐림': '구름이 끼는 시간대가 있겠고',
               '흐림': '흐린 시간대가 있겠고'}.get(condition, f'{condition} 예보가 있고')
        headline = f'오늘 남은 시간에는 {sky}, 비나 눈 예보는 없어요.'

    current = weather['current']
    code = current['weather_code']
    _weather_code(code)
    advise_umbrella = bool(periods) or probability >= 50
    if code >= 51:
        observed_at = datetime.fromisoformat(current['time'])
        date_label = '어제' if observed_at.date() < checked_at.date() else '오늘'
        headline = (f'{date_label} {observed_at.hour}시 관측 결과는 '
                    f'{CONDITIONS[code]}입니다. ' + headline)
        if observed_at.date() == checked_at.date() and observed_at.hour == checked_at.hour:
            advise_umbrella = True
    advice = ''
    if advise_umbrella:
        advice = ' 우산을 챙기세요.'
    elif low < 15:
        advice = ' 겉옷을 챙기세요.'
    return (f'{headline} {location}의 오늘 최저 {low:g}도, 최고 {high:g}도이고, '
            f'남은 시간의 최대 강수 확률은 {probability:g}퍼센트예요.{advice}')


def _can_read_weather(weather, settings):
    if weather.get('status') != 'fresh':
        return False
    defaults = dict(DEFAULTS, response_mode='natural')
    if settings.get('length') == '짧고 간단하게':
        defaults['length'] = settings['length']
    return all(defaults.get(key) == value for key, value in settings.items())


def current_temperature_reply(utterance, weather, settings):
    """Read a single current outdoor temperature for the configured area."""
    if not re.fullmatch(
        r'(?:(?:오늘|지금|현재)(?:여기|이곳)?|(?:여기|이곳)(?:지금|현재)?)?'
        r'기온(?:이|은)?몇도(?:야|예요|인가요|지|죠)?[?!.]*', ''.join(utterance.split()),
    ) or not _can_read_weather(weather, settings):
        return None
    try:
        location = _text(weather['location'])
        current = weather['current']
        temperature = _number(current['temperature_c'])
        observed = datetime.fromisoformat(current['time'])
        checked = datetime.fromisoformat(weather['checked_at'])
        if (observed.tzinfo is None or checked.tzinfo is None
                or observed.astimezone(checked.tzinfo).date() != checked.date()
                or not -60 <= (checked - observed).total_seconds() <= MAX_OBSERVATION_AGE_S):
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    sign = '영하 ' if temperature < 0 else ''
    return f'{location} 기준 현재 기온은 {sign}{abs(temperature):g}도예요.'


def daily_forecast_reply(utterance, weather, settings):
    """Read an ordinary daily forecast after the model selected and ran get_weather."""
    if not _can_read_weather(weather, settings):
        return None
    day = basic_forecast_day(utterance)
    if day is None:
        return None
    offset = int(day == '내일')
    try:
        location = _text(weather['location'])
        checked_at = datetime.fromisoformat(weather['checked_at'])
        requested = checked_at.date() + timedelta(days=offset)
        record = dict(weather['daily'][offset])
        record.pop('condition', None)
        forecast = WeatherForecast(**record)
        if forecast.date != requested.isoformat():
            return None
        if offset == 0:
            return _today_reply(weather, forecast, checked_at, location)
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None

    low, high = forecast.temperature_min_c, forecast.temperature_max_c
    probability = forecast.precipitation_probability_max_pct
    if forecast.weather_code >= 51 or probability >= 50:
        advice = '우산을 챙기세요.'
    elif low < 15:
        advice = '겉옷을 챙기세요.'
    else:
        advice = '가볍게 입으세요.'
    return (f'{day} 예보는 {CONDITIONS[forecast.weather_code]}입니다. '
            f'{location} 기준 최저 {low:g}도, 최고 {high:g}도, '
            f'최대 강수 확률은 {probability:g}퍼센트예요. '
            f'{advice}')
