"""Named movement follows the active map's applied web rooms."""

from dataclasses import FrozenInstanceError
import math

import pytest
import yaml

from malbut_agent_server import speech_navigation
from malbut_agent_server.speech_navigation import (
    NavigationTargetError, NavigationTargets, explicitly_names_location, matches_navigation_location,
)
from navigation_user_map import rewrite, room, rooms, write_user_map


@pytest.fixture
def catalog(tmp_path):
    return write_user_map(tmp_path)


def fails(code, resolver, name, active_map):
    with pytest.raises(NavigationTargetError) as caught:
        resolver.resolve(name, active_map)
    assert caught.value.code == code
    assert str(caught.value) == code


def test_applied_web_point_is_immutable_and_serializes_only_manager_arguments(catalog):
    _, map_path, _, _ = catalog
    target = NavigationTargets().resolve('  거실  ', str(map_path))
    assert target.location == '거실'
    assert target.map_path == str(map_path.resolve())
    assert len(target.digest) == 64
    assert target.arguments == {
        'pose': {
            'header': {'frame_id': 'map'},
            'pose': {
                'position': {'x': 1.25, 'y': -2.5, 'z': 0.0},
                'orientation': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 1.0},
            },
        },
        'behavior_tree': '',
    }
    with pytest.raises(FrozenInstanceError):
        target.x = 0
    detached = target.arguments
    detached['pose']['pose']['position']['x'] = 100
    assert target.arguments['pose']['pose']['position']['x'] == 1.25


def test_no_map_never_resolves_and_switching_reads_the_other_maps_rooms(catalog, tmp_path):
    _, map_path, _, _ = catalog
    targets = NavigationTargets()
    for absent in (None, '', 'home.yaml', '/nonexistent/home.yaml'):
        fails('map_unavailable', targets, '거실', absent)
    _, other, _, _ = write_user_map(tmp_path, map_name='other', rooms=[room('안방', 2, 3)])
    assert targets.names(str(other)) == ('안방',)
    fails('target_not_found', targets, '거실', str(other))
    assert targets.resolve('안방', str(other)).arguments['pose']['pose']['position']['x'] == 2
    link = tmp_path / 'selected.yaml'
    link.symlink_to(map_path)
    assert targets.resolve('거실', str(link)).map_path == str(map_path)


def test_unconfigured_names_never_get_a_default_pose(catalog):
    _, map_path, _, _ = catalog
    for name in ('주방', 'living_room', '거실 중앙', '1.25,-2.5', '거실로 가'):
        fails('target_not_found', NavigationTargets(), name, str(map_path))
    for name in ('', ' ', None, ['거실'], '거\x00실', 'x' * 129):
        fails('target_invalid', NavigationTargets(), name, str(map_path))


@pytest.mark.parametrize('value', [True, '1.0', math.inf, math.nan, 10_001, 10**400, None])
def test_invalid_representative_point_never_resolves(catalog, value):
    path, map_path, _, document = catalog
    rooms(document)[0]['properties']['representative_point'][0] = value
    rewrite(path, document)
    fails('catalog_invalid', NavigationTargets(), '거실', str(map_path))


@pytest.mark.parametrize('change', [
    lambda value: value.update(frame_id='odom'),
    lambda value: value.update(format='unknown'),
    lambda value: value.update(type='Polygon'),
    lambda value: value.update(features={}),
    lambda value: rooms(value)[0]['properties'].pop('representative_point'),
    lambda value: rooms(value)[0]['properties'].update(representative_point=[1, 2, 3]),
    lambda value: rooms(value)[0]['properties'].update(name=''),
])
def test_invalid_user_map_never_resolves(catalog, change):
    path, map_path, _, document = catalog
    change(document)
    rewrite(path, document)
    fails('catalog_invalid', NavigationTargets(), '거실', str(map_path))


def test_map_revision_must_match_the_actual_occupancy_interpretation(catalog):
    path, selected, image, document = catalog
    original_yaml = selected.read_text()
    original_image = image.read_bytes()
    targets = NavigationTargets()
    for field, value in [('resolution', 0.2), ('origin', [-4, -5, 0]), ('negate', 1),
                         ('free_thresh', 0.15), ('occupied_thresh', 0.7)]:
        metadata = yaml.safe_load(original_yaml)
        metadata[field] = value
        selected.write_text(yaml.safe_dump(metadata))
        fails('map_mismatch', targets, '거실', str(selected))
    selected.write_text(original_yaml)
    image.write_bytes(original_image[:-1] + b'\x00')
    fails('map_mismatch', targets, '거실', str(selected))
    image.write_bytes(original_image)
    document['map_revision'] = 'rev-stale'
    rewrite(path, document)
    fails('map_mismatch', targets, '거실', str(selected))


def test_normalized_duplicate_names_are_ambiguous(catalog):
    path, map_path, _, document = catalog
    document['features'].append(room(' 거실 ', 2, 3, 'room-2'))
    rewrite(path, document)
    fails('target_ambiguous', NavigationTargets(), '거실', str(map_path))


def test_unicode_normalization_matches_names_but_rejects_collisions(catalog):
    path, map_path, _, document = catalog
    rooms(document)[0]['properties']['name'] = '카페'
    rewrite(path, document)
    assert NavigationTargets().resolve('카페', str(map_path)).location == '카페'
    document['features'].append(room('카페', 3, 4, 'room-2'))
    rewrite(path, document)
    fails('target_ambiguous', NavigationTargets(), '카페', str(map_path))


def test_geometry_or_centroid_without_saved_representative_point_is_never_used(catalog):
    path, map_path, _, document = catalog
    properties = rooms(document)[0]['properties']
    properties['centroid'] = properties.pop('representative_point')
    rewrite(path, document)
    fails('catalog_invalid', NavigationTargets(), '거실', str(map_path))


def test_every_resolution_reloads_applied_room_edits_and_deletions(catalog):
    path, map_path, _, document = catalog
    targets = NavigationTargets()
    first = targets.resolve('거실', str(map_path))
    assert targets.resolve('거실', str(map_path)) == first
    rooms(document)[0]['properties']['representative_point'][0] = 4.5
    rewrite(path, document)
    second = targets.resolve('거실', str(map_path))
    assert second.x == 4.5 and second.digest != first.digest
    rooms(document)[0]['properties']['name'] = '안방'
    rewrite(path, document)
    assert targets.names(str(map_path)) == ('안방',)
    fails('target_not_found', targets, '거실', str(map_path))
    document['features'] = [f for f in document['features'] if f not in rooms(document)]
    rewrite(path, document)
    assert targets.names(str(map_path)) == ()
    fails('target_not_found', targets, '안방', str(map_path))
    path.unlink()
    fails('catalog_unavailable', targets, '거실', str(map_path))


def test_web_rooms_save_is_visible_without_restarting_the_resolver(catalog):
    from navigation_user_map import save_rooms

    _, selected, _, document = catalog
    targets = NavigationTargets()
    assert targets.names(str(selected)) == ('거실',)
    save_rooms(selected, document['map_id'], {
        'map_id': document['map_id'], 'map_revision': document['map_revision'],
        'rooms': [room('안방', 2.0, 3.0)],
    })
    assert targets.names(str(selected)) == ('안방',)
    assert (targets.resolve('안방', str(selected)).x, targets.resolve('안방', str(selected)).y
            ) == (2.0, 3.0)


def test_missing_applied_user_map_never_builds_or_uses_legacy_destinations(catalog):
    path, selected, _, _ = catalog
    path.unlink()
    (selected.parent / 'speech-targets.yaml').write_text('locations: {안방: {x: 1, y: 2}}')
    fails('catalog_unavailable', NavigationTargets(), '안방', str(selected))
    assert not path.exists()


def test_user_map_and_map_inputs_are_bounded(catalog, monkeypatch):
    path, map_path, image, _ = catalog
    targets = NavigationTargets()
    for name, size in [('MAX_MAP_IMAGE_BYTES', image.stat().st_size - 1),
                       ('MAX_MAP_BYTES', 1), ('MAX_USER_MAP_BYTES', 1)]:
        with monkeypatch.context() as context:
            context.setattr(speech_navigation, name, size)
            fails('catalog_unavailable', targets, '거실', str(map_path))


def test_invalid_or_missing_image_refuses_resolution(catalog):
    _, map_path, image, _ = catalog
    targets = NavigationTargets()
    image.unlink()
    fails('catalog_unavailable', targets, '거실', str(map_path))
    image.write_bytes(b'not an occupancy image')
    fails('catalog_invalid', targets, '거실', str(map_path))
    map_path.write_text('image: [home.pgm]\n')
    fails('catalog_invalid', targets, '거실', str(map_path))


def test_duplicate_yaml_keys_and_aliases_are_rejected(catalog):
    _, map_path, _, _ = catalog
    for raw in ('image: home.pgm\nimage: home.pgm',
                'image: &image home.pgm\nother: *image',
                '!!python/object/apply:os.system [echo unsafe]'):
        map_path.write_text(raw)
        fails('catalog_invalid', NavigationTargets(), '거실', str(map_path))


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
