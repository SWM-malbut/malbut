"""Exercise durable region changes and exact, bounded geocoding candidates."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from importlib.resources import files
import socket

import pytest

from malbut_agent_server import weather_location_store as module


LOCATION = {
    'location': '경기도 수원시 팔달구 우만1동', 'latitude': 37.2825388888888,
    'longitude': 127.031452777777, 'timezone': 'Asia/Seoul',
}
OTHER_LOCATION = {
    'location': '서울특별시', 'latitude': 37.566,
    'longitude': 126.9784, 'timezone': 'Asia/Seoul',
}


@pytest.fixture
def store(tmp_path):
    value = module.WeatherLocationStore(tmp_path / 'location.sqlite3')
    yield value
    value.close()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('local region lookup must not use the network')

    monkeypatch.setattr(socket, 'socket', blocked)


def test_store_is_empty_without_network_then_survives_restart(tmp_path):
    path = tmp_path / 'nested' / 'location.sqlite3'
    first = module.WeatherLocationStore(path)
    assert first.get() is None
    assert first.set(LOCATION) == LOCATION
    first.close()
    second = module.WeatherLocationStore(path)
    try:
        assert second.get() == LOCATION
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        second.close()


def test_changes_are_visible_to_already_open_reader(tmp_path):
    path = tmp_path / 'shared.sqlite3'
    writer = module.WeatherLocationStore(path)
    reader = module.WeatherLocationStore(path)
    try:
        writer.set(LOCATION)
        assert reader.get() == LOCATION
        writer.set(OTHER_LOCATION)
        assert reader.get() == OTHER_LOCATION
    finally:
        writer.close()
        reader.close()


def test_threaded_reads_never_mix_location_fields(store):
    store.set(LOCATION)

    def operation(index):
        store.set(LOCATION if index % 2 else OTHER_LOCATION)
        assert store.get() in (LOCATION, OTHER_LOCATION)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(operation, range(40)))
    assert store._connection.execute(
        'SELECT count(*) FROM weather_location'
    ).fetchone()[0] == 1


@pytest.mark.parametrize('field,value', [
    ('location', ''), ('location', 'x' * 129), ('location', 'private\nquery'),
    ('latitude', True), ('latitude', 91), ('latitude', float('nan')),
    ('longitude', -181), ('longitude', float('inf')), ('longitude', '127.0'),
    ('timezone', 'auto'), ('timezone', 'Unknown/Zone'), ('timezone', None),
])
def test_invalid_write_preserves_previous_location(store, field, value):
    store.set(LOCATION)
    with pytest.raises(ValueError):
        store.set({**OTHER_LOCATION, field: value})
    assert store.get() == LOCATION


def test_failed_transaction_preserves_previous_location(store):
    store.set(LOCATION)
    store._connection.execute(
        "CREATE TRIGGER reject_update BEFORE UPDATE ON weather_location "
        "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.set(OTHER_LOCATION)
    assert store.get() == LOCATION


def test_source_metadata_does_not_leak_into_weather_client_kwargs(store):
    assert store.set({**LOCATION, 'source': 'GeoNames'}) == LOCATION
    assert store.get() == LOCATION


def test_memory_store_is_isolated_and_return_values_are_copies():
    first = module.WeatherLocationStore(':memory:')
    second = module.WeatherLocationStore(':memory:')
    try:
        result = first.set(LOCATION)
        result['location'] = 'changed outside store'
        assert first.get() == LOCATION
        assert second.get() is None
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('query', [
    '수원시 우만1동', '경기도 수원시 팔달구 우만1동',
    '경기도수원시팔달구우만1동', '수원시팔달구 우만1동',
    '경기 수원 우만1동', '  수원시   우만1동  ',
])
def test_qualified_region_matches_official_coordinates(query):
    assert module.resolve_weather_location(query) == [LOCATION]


@pytest.mark.parametrize('query', ['우만동', '수원시 우만동', '팔달구 우만동'])
def test_numbered_administrative_neighborhoods_require_choice(query):
    result = module.resolve_weather_location(query)
    assert [item['location'] for item in result] == [
        '경기도 수원시 팔달구 우만1동', '경기도 수원시 팔달구 우만2동',
    ]
    assert result[0] == LOCATION
    assert result[1]['latitude'] == 37.2751166666666
    assert result[1]['longitude'] == 127.041852777777


@pytest.mark.parametrize('query,expected', [
    ('수원시', ['장안구', '권선구', '팔달구', '영통구']),
    ('용인시', ['처인구', '기흥구', '수지구']),
    ('수원시 영통동', ['영통1동', '영통2동', '영통3동']),
])
def test_missing_city_or_unnumbered_neighborhood_returns_nearest_choices(query, expected):
    result = module.resolve_weather_location(query)
    assert [item['location'].split()[-1] for item in result] == expected


def test_province_alias_does_not_return_every_child():
    result = module.resolve_weather_location('서울')
    assert len(result) == 1
    assert result[0]['location'] == '서울특별시'
    assert result[0]['latitude'] == 37.5635694444444
    assert result == module.resolve_weather_location('서울특별시')


def test_duplicate_district_names_require_choice_but_parent_disambiguates():
    assert len(module.resolve_weather_location('중구')) == 5
    result = module.resolve_weather_location('부산 중구')
    assert len(result) == 1 and result[0]['location'] == '부산광역시 중구'


@pytest.mark.parametrize('query', [
    '용인시 우만1동', '서울특별시 팔달구 우만1동', '수원시 없는동',
    '서울특별시 없는동', '우만3동', '없는지역', 'Seoul',
])
def test_wrong_parent_or_unknown_leaf_never_falls_back_to_city(query):
    assert module.resolve_weather_location(query) == []


def test_merged_administrative_neighborhood_punctuation():
    first = module.resolve_weather_location('서울 종로구 종로1.2.3.4가동')
    assert len(first) == 1
    assert first[0]['location'] == '서울특별시 종로구 종로1.2.3.4가동'
    assert module.resolve_weather_location('종로구 종로1·2·3·4가동') == first


def test_query_result_mutation_does_not_modify_cached_gazetteer():
    result = module.resolve_weather_location('수원시 우만1동')
    result[0]['location'] = 'outside mutation'
    assert module.resolve_weather_location('수원시 우만1동') == [LOCATION]


@pytest.mark.parametrize('query', [
    None, 42, '', ' ', '가', 'x' * 121, '수원\n우만동', 'https://localhost',
    '수원?secret=value', '<지역>', '서울\x00중구',
])
def test_invalid_query_is_rejected(query):
    with pytest.raises(module.WeatherLocationError) as caught:
        module.resolve_weather_location(query)
    assert caught.value.code == 'LOCATION_INVALID_QUERY'


def test_action_maximum_query_length_is_accepted():
    assert module.resolve_weather_location('가' * 120) == []


def test_failed_or_ambiguous_resolution_preserves_saved_location(store):
    store.set(LOCATION)
    assert module.resolve_weather_location('없는지역') == []
    assert len(module.resolve_weather_location('용인시')) == 3
    assert store.get() == LOCATION


def test_bundled_gazetteer_is_complete_attributed_and_all_rows_resolve():
    data = json.loads(files('malbut_agent_server').joinpath(
        'data/weather_regions.json').read_text(encoding='utf-8'))
    assert data['row_count'] == len(data['rows']) == 3838
    assert data['source_sha256'] == (
        '746b5e5be10430106abccc795a16c16d0f1fd0f081e8ab2765d0adc983a003c1'
    )
    assert data['source_url'].startswith('https://apihub.kma.go.kr/')
    assert '기상청' in data['attribution']
    codes = set()
    for code, province, district, neighborhood, latitude, longitude in data['rows']:
        assert code not in codes
        codes.add(code)
        result = module.resolve_weather_location(province + district + neighborhood)
        assert any(item['latitude'] == latitude and item['longitude'] == longitude
                   for item in result)


@pytest.mark.parametrize('error,code', [
    (FileNotFoundError('private path'), 'LOCATION_LOOKUP_FAILED'),
    (ValueError('private data'), 'LOCATION_INVALID_DATA'),
])
def test_bundled_data_errors_are_safe(monkeypatch, error, code):
    def broken():
        raise error

    monkeypatch.setattr(module, '_region_index', broken)
    with pytest.raises(module.WeatherLocationError) as caught:
        module.resolve_weather_location('수원시 우만동')
    assert caught.value.code == code
    assert 'private' not in str(caught.value)
