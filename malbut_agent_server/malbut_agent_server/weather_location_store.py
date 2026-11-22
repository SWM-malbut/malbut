"""Persist the robot's chosen weather location and resolve user-supplied regions."""

from difflib import SequenceMatcher
from functools import lru_cache
from importlib.resources import files
from itertools import product
import json
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata

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


_DIGIT_TO_HANGUL = str.maketrans('123456789', '일이삼사오육칠팔구')
_HANGUL_TO_DIGIT = {
    '일': '1', '이': '2', '삼': '3', '사': '4', '오': '5',
    '육': '6', '칠': '7', '팔': '8', '구': '9',
}
_ASR_LEAF_RE = re.compile(r'([가-힣0-9.]+)(동|읍|면|가|구|리)')
_ASR_LEAF_MIN_SCORE = 0.80
_ASR_LEAF_MIN_MARGIN = 0.15


def _normalize_asr_numbers(key):
    """Normalize Korean STT digit/word swaps in administrative region names."""
    normalized = re.sub(
        r'(?<=[가-힣])([일이삼사오육칠팔구])(?=(?:동|가)$)',
        lambda match: _HANGUL_TO_DIGIT[match.group(1)],
        key,
    )
    return re.sub(
        r'[1-9](?![0-9.]*(?:동|가|리|로|번)(?:$|[가-힣]))',
        lambda match: match.group(0).translate(_DIGIT_TO_HANGUL),
        normalized,
    )


def _resolve_asr_leaf(normalized_key, parent_leaves):
    """Resolve one unambiguous parent-scoped Hangul ASR slip without guessing digits."""
    best_prefix = ''
    for cut in range(len(normalized_key) - 2, 1, -1):
        prefix = normalized_key[:cut]
        if prefix in parent_leaves:
            best_prefix = prefix
            break
    if not best_prefix:
        return []
    leaf_query = normalized_key[len(best_prefix):]
    match = _ASR_LEAF_RE.fullmatch(leaf_query)
    if match is None:
        return []
    q_stem, q_unit = match.groups()
    q_non_hangul = ''.join(char for char in q_stem if not '\uac00' <= char <= '\ud7a3')
    q_hangul = ''.join(char for char in q_stem if '\uac00' <= char <= '\ud7a3')
    if len(q_hangul) < 2:
        return []
    q_nfd = unicodedata.normalize('NFD', q_hangul)
    scored = []
    seen = set()
    for cand_leaf, value in parent_leaves[best_prefix]:
        location = value['location']
        if location in seen:
            continue
        seen.add(location)
        cand_match = _ASR_LEAF_RE.fullmatch(cand_leaf)
        if cand_match is None or cand_match.group(2) != q_unit:
            continue
        c_stem = re.sub(r'제([0-9]+(?:\.[0-9]+)*)$', r'\1', cand_match.group(1))
        c_non_hangul = ''.join(char for char in c_stem if not '\uac00' <= char <= '\ud7a3')
        if c_non_hangul != q_non_hangul:
            continue
        c_hangul = ''.join(char for char in c_stem if '\uac00' <= char <= '\ud7a3')
        if len(c_hangul) < 2:
            continue
        ratio = SequenceMatcher(
            None, q_nfd, unicodedata.normalize('NFD', c_hangul), autojunk=False,
        ).ratio()
        scored.append((ratio, value))
    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_value = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= _ASR_LEAF_MIN_SCORE and best_score - runner_up >= _ASR_LEAF_MIN_MARGIN:
        return [dict(best_value)]
    return []


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
        exact, parents, parent_leaves = _region_index()
    except OSError:
        raise WeatherLocationError('LOCATION_LOOKUP_FAILED') from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise WeatherLocationError('LOCATION_INVALID_DATA') from None
    matches = exact.get(key)
    if matches is None:
        # e.g. Suwon has district rows but no city row: offer those districts,
        # never choose a representative district or average their coordinates.
        matches = parents.get(key, (None, []))[1]
    if not matches:
        normalized = _normalize_asr_numbers(key)
        if normalized != key:
            matches = exact.get(normalized)
            if matches is None:
                matches = parents.get(normalized, (None, []))[1]
        if not matches:
            matches = _resolve_asr_leaf(normalized, parent_leaves)
    return [dict(value) for value in matches[:5]]


def _aliases(parts):
    province_names = {
        '서울특별시': ('서울', '서울시'), '부산광역시': ('부산', '부산시'),
        '대구광역시': ('대구', '대구시'), '인천광역시': ('인천', '인천시'),
        '광주광역시': ('광주',), '대전광역시': ('대전', '대전시'),
        '울산광역시': ('울산', '울산시'), '세종특별자치시': ('세종', '세종시'),
        '경기도': ('경기',), '강원특별자치도': ('강원',),
        '충청북도': ('충북',), '충청남도': ('충남',),
        '전북특별자치도': ('전북',), '전라남도': ('전남',),
        '경상북도': ('경북',), '경상남도': ('경남',),
        '제주특별자치도': ('제주',),
    }
    aliases = set()
    for mask in range(1 << (len(parts) - 1)):
        included = [part for i, part in enumerate(parts[:-1]) if mask & (1 << i)]
        included.append(parts[-1])
        variants = []
        for part in included:
            names = {part}
            if part in province_names:
                names.update(province_names[part])
            elif part.endswith(('시', '군')):
                names.add(part[:-1])
            if part == parts[-1]:
                # 우만동 means either 우만1동 or 우만2동. Keep both, with
                # their original names and coordinates, for the user to choose.
                names.add(re.sub(r'(?:제)?[0-9]+(?:\.[0-9]+)*동$', '동', part))
                names.add(re.sub(r'제([0-9]+(?:\.[0-9]+)*동)$', r'\1', part))
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
    exact, parents, parent_leaves = {}, {}, {}
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
        leaf = parts[-1]
        for size in range(1, len(parts)):
            depth = len(parts) - size
            for alias in _aliases(parts[:size]):
                previous = parents.get(alias)
                if previous is None or depth < previous[0]:
                    parents[alias] = (depth, [value])
                elif depth == previous[0] and value not in previous[1]:
                    previous[1].append(value)
                parent_leaves.setdefault(alias, []).append((leaf, value))
    return exact, parents, parent_leaves
