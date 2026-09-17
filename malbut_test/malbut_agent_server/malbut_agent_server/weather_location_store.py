"""Persist the robot's chosen weather location and resolve user-supplied regions."""

from functools import lru_cache
from importlib.resources import files
from itertools import product
import json
from pathlib import Path
import re
import sqlite3
import threading

from .weather import _number, _text, _zone


DEFAULT_WEATHER_LOCATION_PATH = str(
    Path.home() / '.local/share/malbut/weather-location.sqlite3'
)


class WeatherLocationError(Exception):
    """A bounded local region lookup failure safe to return through the Manager."""

    def __init__(self, code):
        self.code = code
        super().__init__({
            'LOCATION_INVALID_QUERY': 'Specify a city or neighborhood name',
            'LOCATION_LOOKUP_FAILED': 'Location lookup failed',
            'LOCATION_TIMEOUT': 'Location lookup timed out',
            'LOCATION_INVALID_DATA': 'Location lookup returned invalid data',
        }[code])


def _validated_location(value):
    if not isinstance(value, dict):
        raise ValueError('invalid saved location')
    try:
        label = _text(value['location']).strip()
        latitude = _number(value['latitude'])
        longitude = _number(value['longitude'])
        timezone = _zone(value['timezone']).key
    except (KeyError, TypeError) as error:
        raise ValueError('invalid saved location') from error
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError('invalid saved coordinates')
    return {
        'location': label, 'latitude': latitude, 'longitude': longitude,
        'timezone': timezone,
    }


class WeatherLocationStore:
    """Keep one robot-wide location, visible to other processes after commit."""

    def __init__(self, path=DEFAULT_WEATHER_LOCATION_PATH):
        if not str(path).strip():
            raise ValueError('location database path must not be empty')
        self.path = str(Path(path).expanduser()) if path != ':memory:' else path
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=1.0, check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            'CREATE TABLE IF NOT EXISTS weather_location ('
            'singleton INTEGER PRIMARY KEY CHECK(singleton = 1), '
            'location TEXT NOT NULL, latitude REAL NOT NULL, '
            'longitude REAL NOT NULL, timezone TEXT NOT NULL)'
        )
        self._connection.commit()
        if self.path != ':memory:':
            Path(self.path).chmod(0o600)

    def get(self):
        """Read the current committed location without geocoding or IP lookup."""
        with self._lock:
            row = self._connection.execute(
                'SELECT location, latitude, longitude, timezone '
                'FROM weather_location WHERE singleton = 1'
            ).fetchone()
        return None if row is None else _validated_location(dict(row))

    def set(self, location):
        """Atomically replace the single saved location after validation."""
        value = _validated_location(location)
        with self._lock, self._connection:
            self._connection.execute(
                'INSERT INTO weather_location '
                '(singleton, location, latitude, longitude, timezone) '
                'VALUES (1, :location, :latitude, :longitude, :timezone) '
                'ON CONFLICT(singleton) DO UPDATE SET '
                'location = excluded.location, latitude = excluded.latitude, '
                'longitude = excluded.longitude, timezone = excluded.timezone',
                value,
            )
        return value

    def close(self):
        """Release the SQLite connection."""
        with self._lock:
            self._connection.close()


def resolve_weather_location(query):
    """Resolve a Korean administrative region using the bundled KMA gazetteer.

    The JSON preserves KMA's source URL, edition and source-file checksum. No
    network request, IP discovery, average coordinate or city fallback is used.
    Numbered administrative neighborhoods remain separate clarification choices.
    """
    if (not isinstance(query, str) or not 2 <= len(query.strip()) <= 120
            or not re.fullmatch(r'[가-힣A-Za-z0-9 .·-]+', query)):
        raise WeatherLocationError('LOCATION_INVALID_QUERY')
    key = query.replace(' ', '').replace('·', '.')
    try:
        exact, parents = _region_index()
    except OSError:
        raise WeatherLocationError('LOCATION_LOOKUP_FAILED') from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise WeatherLocationError('LOCATION_INVALID_DATA') from None
    matches = exact.get(key)
    if matches is None:
        # e.g. Suwon has district rows but no city row: offer those districts,
        # never choose a representative district or average their coordinates.
        matches = parents.get(key, (None, []))[1]
    return [dict(value) for value in matches[:5]]


def _aliases(parts):
    province_names = {
        '서울특별시': '서울', '부산광역시': '부산', '대구광역시': '대구',
        '인천광역시': '인천', '광주광역시': '광주', '대전광역시': '대전',
        '울산광역시': '울산', '세종특별자치시': '세종', '경기도': '경기',
        '강원특별자치도': '강원', '충청북도': '충북', '충청남도': '충남',
        '전북특별자치도': '전북', '전라남도': '전남', '경상북도': '경북',
        '경상남도': '경남', '제주특별자치도': '제주',
    }
    aliases = set()
    for mask in range(1 << (len(parts) - 1)):
        included = [part for i, part in enumerate(parts[:-1]) if mask & (1 << i)]
        included.append(parts[-1])
        variants = []
        for part in included:
            names = {part}
            if part in province_names:
                names.add(province_names[part])
            elif part.endswith(('시', '군')):
                names.add(part[:-1])
            if part == parts[-1]:
                # 우만동 means either 우만1동 or 우만2동. Keep both, with
                # their original names and coordinates, for the user to choose.
                names.add(re.sub(r'(?:제)?[0-9]+(?:\.[0-9]+)*동$', '동', part))
            variants.append(names)
        aliases.update(''.join(value) for value in product(*variants))
    return aliases


@lru_cache(maxsize=1)
def _region_index():
    """Build exact and nearest administrative-child indexes once per process."""
    path = files(__package__).joinpath('data/weather_regions.json')
    payload = json.loads(path.read_text(encoding='utf-8'))
    rows = payload['rows']
    if payload['version'] != 1 or len(rows) != payload['row_count'] or not rows:
        raise ValueError('invalid bundled region data')
    exact, parents = {}, {}
    codes = set()
    for row in rows:
        if (not isinstance(row, list) or len(row) != 6
                or not isinstance(row[0], str) or not row[0].isdigit()
                or row[0] in codes):
            raise ValueError('invalid region row')
        codes.add(row[0])
        province, district, neighborhood = row[1:4]
        if (not isinstance(province, str) or not province
                or not isinstance(district, str) or not isinstance(neighborhood, str)):
            raise ValueError('invalid region hierarchy')
        parts = [province]
        if district:
            match = re.fullmatch(r'(.+?시)(.+구)', district)
            parts.extend(match.groups() if match else [district])
        if neighborhood:
            parts.append(neighborhood)
        value = _validated_location({
            'location': ' '.join(parts), 'latitude': row[4],
            'longitude': row[5], 'timezone': 'Asia/Seoul',
        })
        for alias in _aliases(parts):
            exact.setdefault(alias, []).append(value)
        for size in range(1, len(parts)):
            depth = len(parts) - size
            for alias in _aliases(parts[:size]):
                previous = parents.get(alias)
                if previous is None or depth < previous[0]:
                    parents[alias] = (depth, [value])
                elif depth == previous[0] and value not in previous[1]:
                    previous[1].append(value)
    return exact, parents
