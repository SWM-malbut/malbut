"""Named movement uses only explicit poses bound to Manager's selected map."""

from dataclasses import FrozenInstanceError
import math

import pytest
import yaml

from malbut_agent_server import speech_navigation
from malbut_agent_server.speech_navigation import (
    NavigationTargetError, NavigationTargets, explicitly_names_location, matches_navigation_location,
)


@pytest.fixture
def catalog(tmp_path):
    image = tmp_path / 'home.pgm'
    image.write_bytes(b'P5\n1 1\n255\n\xff')
    map_path = tmp_path / 'home.yaml'
    map_path.write_text('image: home.pgm\nresolution: 0.05\n', encoding='utf-8')
    config = tmp_path / 'speech-targets.yaml'
    value = {
        'map': str(map_path), 'frame_id': 'map',
        'locations': {'거실': {'x': 1.25, 'y': -2.5, 'yaw': math.pi / 2}},
    }
    config.write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')
    return config, map_path, image, value


def rewrite(config, value):
    config.write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')


def fails(code, resolver, name, active_map):
    with pytest.raises(NavigationTargetError) as caught:
        resolver.resolve(name, active_map)
    assert caught.value.code == code
    assert str(caught.value) == code


def test_explicit_pose_is_immutable_and_serializes_only_manager_goal_arguments(catalog):
    config, map_path, _, _ = catalog
    target = NavigationTargets(config).resolve('  거실  ', str(map_path))
    assert target.location == '거실'
    assert target.map_path == str(map_path.resolve())
    assert len(target.digest) == 64
    assert target.arguments == {
        'pose': {
            'header': {'frame_id': 'map'},
            'pose': {
                'position': {'x': 1.25, 'y': -2.5, 'z': 0.0},
                'orientation': {'x': 0.0, 'y': 0.0,
                                'z': math.sin(math.pi / 4),
                                'w': math.cos(math.pi / 4)},
            },
        },
        'behavior_tree': '',
    }
    with pytest.raises(FrozenInstanceError):
        target.x = 0
    detached = target.arguments
    detached['pose']['pose']['position']['x'] = 100
    assert target.arguments['pose']['pose']['position']['x'] == 1.25


def test_no_map_or_different_selected_map_never_resolves(catalog, tmp_path):
    config, map_path, _, _ = catalog
    targets = NavigationTargets(config)
    for absent in (None, '', 'home.yaml', '/nonexistent/home.yaml'):
        fails('map_unavailable', targets, '거실', absent)
    other = tmp_path / 'other.yaml'
    other.write_text('image: home.pgm\n')
    fails('map_mismatch', targets, '거실', str(other))
    link = tmp_path / 'selected.yaml'
    link.symlink_to(map_path)
    assert targets.resolve('거실', str(link)).map_path == str(map_path)


def test_unconfigured_names_never_get_a_default_pose(catalog):
    config, map_path, _, _ = catalog
    for name in ('주방', 'living_room', '거실 중앙', '1.25,-2.5', '거실로 가'):
        fails('target_not_found', NavigationTargets(config), name, str(map_path))
    for name in ('', ' ', None, ['거실'], '거\x00실', 'x' * 129):
        fails('target_invalid', NavigationTargets(config), name, str(map_path))


@pytest.mark.parametrize('field,value', [
    ('x', True), ('x', '1.0'), ('x', math.inf), ('x', math.nan),
    ('x', 10_001), ('x', 10**400), ('y', -10_001),
    ('yaw', math.pi + 0.001), ('yaw', None),
])
def test_invalid_pose_never_resolves(catalog, field, value):
    config, map_path, _, document = catalog
    document['locations']['거실'][field] = value
    rewrite(config, document)
    fails('catalog_invalid', NavigationTargets(config), '거실', str(map_path))


@pytest.mark.parametrize('change', [
    lambda value: value.update(frame_id='odom'),
    lambda value: value.update(extra='unsupported'),
    lambda value: value.update(map='home.yaml'),
    lambda value: value.update(locations={}),
    lambda value: value.update(locations=[]),
    lambda value: value['locations']['거실'].pop('yaw'),
    lambda value: value['locations']['거실'].update(behavior_tree='/custom.xml'),
    lambda value: value['locations'].update({'': {'x': 0, 'y': 0, 'yaw': 0}}),
])
def test_catalog_shape_is_strict(catalog, change):
    config, map_path, _, value = catalog
    change(value)
    rewrite(config, value)
    fails('catalog_invalid', NavigationTargets(config), '거실', str(map_path))


def test_duplicate_yaml_keys_and_aliases_are_rejected(catalog):
    config, map_path, _, _ = catalog
    for raw in (
        f'map: {map_path}\nmap: {map_path}\nframe_id: map\nlocations: {{}}',
        f'map: {map_path}\nframe_id: map\nlocations:\n  거실: {{x: 1, x: 2, y: 0, yaw: 0}}',
        f'map: {map_path}\nframe_id: map\nlocations:\n  거실: &pose {{x: 1, y: 0, yaw: 0}}\n  주방: *pose',
        '!!python/object/apply:os.system [echo unsafe]',
        'a: ' + '[' * 12 + '0' + ']' * 12,
        '---\na: 1\n---\nb: 2',
    ):
        config.write_text(raw, encoding='utf-8')
        fails('catalog_invalid', NavigationTargets(config), '거실', str(map_path))


def test_normalized_duplicate_room_names_are_ambiguous(catalog):
    config, map_path, _, value = catalog
    value['locations'][' 거실 '] = {'x': 2, 'y': 3, 'yaw': 0}
    rewrite(config, value)
    fails('target_ambiguous', NavigationTargets(config), '거실', str(map_path))


def test_unicode_normalization_matches_names_but_rejects_collisions(catalog):
    config, map_path, _, value = catalog
    value['locations'] = {'카페': {'x': 1, 'y': 2, 'yaw': 0}}
    rewrite(config, value)
    assert NavigationTargets(config).resolve('카페', str(map_path)).location == '카페'
    value['locations']['카페'] = {'x': 3, 'y': 4, 'yaw': 0}
    rewrite(config, value)
    fails('target_ambiguous', NavigationTargets(config), '카페', str(map_path))


def test_geometry_without_an_explicit_pose_is_never_centroided(catalog):
    config, map_path, _, value = catalog
    value['locations']['거실'] = {
        'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 0]]],
    }
    rewrite(config, value)
    fails('catalog_invalid', NavigationTargets(config), '거실', str(map_path))


def test_every_resolve_reloads_catalog_and_binds_map_yaml_and_image(catalog):
    config, map_path, image, value = catalog
    targets = NavigationTargets(config)
    first = targets.resolve('거실', str(map_path))
    assert targets.resolve('거실', str(map_path)) == first
    value['locations']['거실']['x'] = 4.5
    rewrite(config, value)
    second = targets.resolve('거실', str(map_path))
    assert second.x == 4.5 and second.digest != first.digest
    map_path.write_text('image: home.pgm\nresolution: 0.10\n', encoding='utf-8')
    third = targets.resolve('거실', str(map_path))
    assert third.digest != second.digest
    image.write_bytes(b'P5\n1 1\n255\n\x00')
    fourth = targets.resolve('거실', str(map_path))
    assert fourth.digest != third.digest
    config.unlink()
    fails('catalog_unavailable', targets, '거실', str(map_path))


def test_config_and_map_inputs_are_bounded(catalog, monkeypatch):
    config, map_path, image, _ = catalog
    targets = NavigationTargets(config)
    monkeypatch.setattr(speech_navigation, 'MAX_MAP_IMAGE_BYTES', image.stat().st_size - 1)
    fails('catalog_invalid', targets, '거실', str(map_path))
    monkeypatch.setattr(speech_navigation, 'MAX_MAP_IMAGE_BYTES', 1024)
    monkeypatch.setattr(speech_navigation, 'MAX_MAP_BYTES', 1)
    fails('catalog_unavailable', targets, '거실', str(map_path))
    monkeypatch.setattr(speech_navigation, 'MAX_MAP_BYTES', 1024)
    monkeypatch.setattr(speech_navigation, 'MAX_CONFIG_BYTES', 1)
    fails('catalog_unavailable', targets, '거실', str(map_path))


def test_invalid_or_missing_map_image_refuses_resolution(catalog):
    config, map_path, image, _ = catalog
    targets = NavigationTargets(config)
    image.unlink()
    fails('catalog_unavailable', targets, '거실', str(map_path))
    map_path.write_text('image: [home.pgm]\n')
    fails('catalog_invalid', targets, '거실', str(map_path))


def test_names_exposes_only_normalized_labels_and_reloads_changes(catalog):
    config, map_path, _, value = catalog
    targets = NavigationTargets(config)
    assert targets.names(str(map_path)) == ('거실',)
    value['locations'] = {' 카페 ': {'x': 1, 'y': 2, 'yaw': 0}}
    rewrite(config, value)
    assert targets.names(str(map_path)) == ('카페',)


@pytest.mark.parametrize('failure', [
    'map_unavailable', 'map_mismatch', 'catalog_invalid', 'catalog_unavailable',
    'image_unavailable', 'image_invalid', 'target_ambiguous',
])
def test_names_requires_valid_catalog_selected_map_and_image(catalog, tmp_path, failure):
    config, map_path, image, value = catalog
    active_map = str(map_path)
    if failure == 'map_unavailable':
        active_map = None
    elif failure == 'map_mismatch':
        other = tmp_path / 'other.yaml'
        other.write_text('image: home.pgm\n', encoding='utf-8')
        active_map = str(other)
    elif failure == 'catalog_invalid':
        value['locations']['거실']['x'] = 'invalid'
        rewrite(config, value)
    elif failure == 'catalog_unavailable':
        config.unlink()
    elif failure == 'image_unavailable':
        image.unlink()
    elif failure == 'image_invalid':
        map_path.write_text('image: [home.pgm]\n', encoding='utf-8')
    elif failure == 'target_ambiguous':
        value['locations'][' 거실 '] = {'x': 0, 'y': 0, 'yaw': 0}
        rewrite(config, value)
    with pytest.raises(NavigationTargetError) as caught:
        NavigationTargets(config).names(active_map)
    assert caught.value.code == failure.replace('image_', 'catalog_')


@pytest.mark.parametrize('utterance,location,locations,expected', [
    ('거실로가', '거실', ('거실',), True),
    ('우리 거실로 좀 가주이소', '거실', ('거실',), True),
    ('거실로 가', '거실', ('거실',), True),
    (' 거실 ', ' 거실 ', ('거실',), True),
    ('카페로 가', '카페', ('카페',), True),
    ('거실로, 아니 주방으로 가', '주방', ('거실', '주방'), True),
    ('기실로 가', '거실', ('거실',), False),
    ('거실로 가', '거실', ('주방',), False),
    ('거실2로 가', '거실', ('거실',), False),
    ('안방으로 가', '방', ('방', '안방'), False),
    ('작은 거실로 가', '거실', ('거실', '작은 거실'), False),
    ('거실 2로 가', '거실', ('거실', '거실 2'), False),
    ('거실로비로 가', '거실', ('거실', '거실로비'), False),
    ('작은 거실 말고 거실로 가', '거실', ('거실', '작은 거실'), True),
    ('거실로 가지 마', '거실', ('거실',), True),
])
def test_explicit_name_binding_preserves_exact_source_spans_without_parsing_intent(
    utterance, location, locations, expected,
):
    assert explicitly_names_location(utterance, location, locations) is expected


@pytest.mark.parametrize('utterance,location,locations,expected', [
    ('거실로 가', '거실', ('거실', '주방'), True),
    ('기실로 가', '거실', ('거실', '주방'), True),
    ('기실로가', '거실', ('거실', '주방'), True),
    ('거슬로 가', '거실', ('거실', '주방'), True),
    ('기실으로 가', '거실', ('거실', '주방'), True),
    ('기실로 가', '거실', ('거실', '주방'), True),
    ('기실', '거실', ('거실', '주방'), True),
    ('기실로, 아니 주방으로 가', '주방', ('거실', '주방'), True),
    ('베란다로 가', '거실', ('거실', '주방'), False),
    ('기실로 가', '거실', ('거실', '고실'), False),
    ('기실로 가', '거실', ('거실', '길'), False),
    ('기실로 가', '거실', ('거실', '기실'), False),
    ('기실로 가', '기실', ('거실', '고실', '기실'), True),
    ('거실로 가', '거실', ('거실', '고실', '기실'), True),
    ('거실2로 가', '거실', ('거실',), False),
    ('기실2로 가', '거실', ('거실',), False),
    ('기실2로 가', '거실2', ('거실2',), True),
    ('거실x로 가', '거실', ('거실',), False),
    ('안방으로 가', '방', ('방', '안방'), False),
    ('안방으로 가', '방', ('방',), False),
    ('작은 기실로 가', '거실', ('거실', '작은 기실'), False),
    ('작은 거실로 가', '거실', ('거실', '작은 거실'), False),
    ('기실 이야기를 하다가 주방으로 가', '거실', ('거실', '주방'), False),
    ('기실 쪽으로 가', '거실', ('거실', '주방'), False),
    ('기실로 가지 마', '거실', ('거실',), True),
])
def test_navigation_name_similarity_requires_one_close_source_candidate(
    utterance, location, locations, expected,
):
    assert matches_navigation_location(utterance, location, locations) is expected
