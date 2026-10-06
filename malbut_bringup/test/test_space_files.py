"""The real robot's User Map and Zones for the web map editor (SWM25-237)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from malbut_bringup import cloud_sync
from malbut_bringup.cloud_sync import (
    cloud_map_id, CloudSync, map_payload, MAX_MAP_UPLOAD_BYTES, space_documents,
)
from malbut_bringup.user_map import load_or_build_user_map, save_rooms, user_map_path
from malbut_bringup.web_panel import PanelData, save_zones, zone_view
from malbut_bringup.web_runtime import SavedMapCatalog
from malbut_bringup.zones import read_zones, zones_path


COMMAND_ID = '7f4b5ec0-2a1b-4c3d-8e9f-0a1b2c3d4e5f'
OTHER_ID = '8f4b5ec0-2a1b-4c3d-8e9f-0a1b2c3d4e5f'


def _house(directory, name='home', wall=True):
    """Write a 4 m x 3 m explored room with walls, trinary like autoslam saves it."""
    image = np.full((60, 80), 205, dtype=np.uint8)
    image[5:55, 5:75] = 254
    if wall:
        image[5:55, 5] = image[5:55, 74] = 0
        image[5, 5:75] = image[54, 5:75] = 0
    cv2.imwrite(str(directory / f'{name}.pgm'), image)
    path = directory / f'{name}.yaml'
    path.write_text(f'image: {name}.pgm\nresolution: 0.05\norigin: [-1.0, -1.0, 0.0]\n'
                    'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
    return path


@pytest.fixture
def saved(tmp_path):
    cloud_sync._SPACE_CACHE.clear()
    path = _house(tmp_path)
    runtime = {'mode': 'navigation', 'map': str(path),
               'localization': {'mode': 'LOCALIZATION', 'map': str(path)}}
    return SavedMapCatalog(tmp_path), runtime, path


def _room(identity, name, ring):
    return {'type': 'Feature', 'id': identity,
            'properties': {'role': 'room', 'room_id': identity, 'name': name},
            'geometry': {'type': 'Polygon', 'coordinates': [ring]}}


def _box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def _zone(identity, behavior='restricted', **extra):
    ring = [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5], [0.0, 0.0]]
    return {'type': 'Feature',
            'properties': {'role': 'semantic_zone', 'zone_id': identity, 'behavior': behavior,
                           'name': identity, **extra},
            'geometry': {'type': 'Polygon', 'coordinates': [ring]}}


def test_saved_map_gets_a_user_map_with_one_room_kept_beside_it(saved):
    catalog, runtime, path = saved
    user_map, zones, revision = space_documents(runtime, catalog)
    roles = [feature['properties']['role'] for feature in user_map['features']]
    assert roles == ['walkable_area', 'wall_outline', 'room']
    room = user_map['features'][2]
    assert room['properties']['name'] == '공간 1'
    assert room['geometry'] == user_map['features'][0]['geometry']
    assert user_map['map_id'] == cloud_map_id(runtime) and user_map['map_revision'] == revision
    assert json.loads(user_map_path(path).read_text())['map_revision'] == revision
    # The Zones are the web editor's Feature Collection, plus the developer screen's fields.
    assert zones == {'type': 'FeatureCollection', 'format': 'malbut-semantic-zones-v1',
                     'map_id': cloud_map_id(runtime), 'map_revision': revision,
                     'frame_id': 'map', 'map': 'home.yaml', 'editable': True,
                     'message': '', 'features': []}
    # Not on a saved map (mapping): nothing to edit.
    assert space_documents({'localization': {'mode': 'MAPPING'}}, catalog) == (None, None, None)


def test_rooms_from_the_web_editor_replace_only_the_rooms(saved):
    catalog, runtime, path = saved
    user_map, _zones, revision = space_documents(runtime, catalog)
    map_id = cloud_map_id(runtime)
    rooms = [_room('room-a', '거실', _box(-0.7, -0.7, 1.0, 1.4)),
             _room('room-b', '주방', _box(1.0, -0.7, 2.7, 1.4))]
    saved_map = save_rooms(path, map_id, {'map_id': map_id, 'map_revision': revision,
                                          'rooms': rooms, 'resolution': 0.05})
    assert [feature['properties']['role'] for feature in saved_map['features']] == [
        'walkable_area', 'wall_outline', 'room', 'room']
    assert load_or_build_user_map(path, map_id)['features'][2:] == rooms
    assert load_or_build_user_map(path, map_id)['map_revision'] == revision
    for payload, message in [
        ({'map_id': map_id, 'map_revision': 'rev-other', 'rooms': rooms}, 'changed'),
        ({'map_id': 'real-other', 'map_revision': revision, 'rooms': rooms}, 'changed'),
        ({'map_id': map_id, 'map_revision': revision, 'rooms': []}, '1 to'),
        ({'map_id': map_id, 'map_revision': revision, 'rooms': [rooms[0], rooms[0]]}, 'unique'),
        ({'map_id': map_id, 'map_revision': revision,
          'rooms': [{**rooms[0], 'properties': {'role': 'zone'}}]}, 'role room'),
        ({'map_id': map_id, 'map_revision': revision, 'rooms': rooms, 'shell': 1}, 'Expected'),
    ]:
        with pytest.raises(ValueError, match=message):
            save_rooms(path, map_id, payload)


def test_a_replaced_map_starts_again_from_one_room(saved, tmp_path):
    catalog, runtime, path = saved
    _user_map, _zones, revision = space_documents(runtime, catalog)
    map_id = cloud_map_id(runtime)
    save_rooms(path, map_id, {'map_id': map_id, 'map_revision': revision, 'rooms': [
        _room('room-a', '거실', _box(-0.7, -0.7, 2.7, 1.4))]})
    _house(tmp_path, wall=False)  # autoslam saved a new map under the same name
    rebuilt = load_or_build_user_map(path, map_id)
    assert rebuilt['map_revision'] != revision
    assert rebuilt['features'][-1]['properties']['name'] == '공간 1'


def test_web_zones_keep_every_property_and_the_map_revision_stays(saved):
    catalog, runtime, path = saved
    _user_map, _zones, revision = space_documents(runtime, catalog)
    map_id = cloud_map_id(runtime)
    wall = _zone('zone-wall', geometry_kind='virtual_wall', wall_endpoints=[[0, 0], [0.5, 0.5]],
                 wall_width_m=0.08, color='#b42318')
    goal = _zone('zone-avoid', 'avoid', preferred_goal=[0.25, 0.25])
    collection = {'type': 'FeatureCollection', 'format': 'malbut-semantic-zones-v1',
                  'map_id': map_id, 'map_revision': revision, 'frame_id': 'map',
                  'features': [wall, goal]}
    sync = CloudSync(SimpleNamespace(data=PanelData(), catalog=catalog,
                                     submit=Mock(), node=Mock()), Mock())
    sync.bridge.data.runtime = {**sync.bridge.data.runtime, **runtime}
    sync.last_map_at = 50.0
    sync.dispatch({'id': COMMAND_ID, 'operation': 'zones_apply', 'payload': collection})
    assert sync.pending[COMMAND_ID] == {'ok': True, 'result': {
        'saved': 2, 'nav2_reloaded': True, 'map_id': map_id, 'map_revision': revision}}
    assert sync.last_map_at == 0.0
    assert read_zones(path) == [wall, goal]
    _user_map, zones, after = space_documents(runtime, catalog)
    assert after == revision and zones['features'] == [wall, goal]
    # A draft made for another map version is refused and changes nothing.
    sync.dispatch({'id': OTHER_ID, 'operation': 'zones_apply',
                   'payload': {**collection, 'map_revision': 'rev-old', 'features': []}})
    assert not sync.pending[OTHER_ID]['ok']
    assert 'changed' in sync.pending[OTHER_ID]['result']['error']
    assert read_zones(path) == [wall, goal]


def test_developer_screen_edits_keep_web_only_zone_properties(saved):
    catalog, runtime, path = saved
    _user_map, _zones, revision = space_documents(runtime, catalog)
    wall = _zone('zone-wall', geometry_kind='virtual_wall', wall_endpoints=[[0, 0], [0.5, 0.5]],
                 wall_width_m=0.08, color='#b42318', area_m2=0.25)
    goal = _zone('zone-avoid', 'avoid', preferred_goal=[0.25, 0.25], color='#c48a00')
    from malbut_bringup.zones import apply_zone_collection
    apply_zone_collection(path, {
        'type': 'FeatureCollection', 'format': 'malbut-semantic-zones-v1',
        'map_id': cloud_map_id(runtime), 'map_revision': revision, 'frame_id': 'map',
        'features': [wall, goal]}, cloud_map_id(runtime), revision)
    view = zone_view(runtime, catalog)
    assert [zone['id'] for zone in view['zones']] == ['zone-wall', 'zone-avoid']
    moved = [[0.0, 0.0], [0.6, 0.0], [0.6, 0.6], [0.0, 0.6]]
    save_zones(runtime, catalog, {'map': 'home.yaml', 'zones': [
        {**view['zones'][0], 'name': '문 앞'},  # same corners: still a virtual wall
        {**view['zones'][1], 'points': moved},  # moved: measured values dropped
        {'behavior': 'allow', 'points': moved},  # new zone gets its own id
    ]})
    stored = read_zones(path)
    assert stored[0]['properties'] == {**wall['properties'], 'name': '문 앞'}
    assert stored[1]['properties'] == {**goal['properties']}
    assert stored[2]['properties']['zone_id'].startswith('zone-')
    assert len({item['properties']['zone_id'] for item in stored}) == 3


def test_map_upload_carries_the_user_map_and_trims_it_before_the_size_limit(saved):
    catalog, runtime, _path = saved
    user_map, zones, revision = space_documents(runtime, catalog)
    metadata = {'width': 80, 'height': 60, 'resolution': 0.05,
                'origin': {'x': -1.0, 'y': -1.0, 'yaw': 0.0}}
    payload = map_payload(metadata, b'png', runtime, zones, user_map, revision)
    assert payload['userMap'] == user_map and payload['semanticZones'] == zones
    assert payload['mapRevision'] == revision and payload['finalized']
    edited = map_payload(metadata, b'png', runtime, {**zones, 'features': [_zone('zone-a')]},
                         user_map, revision)
    assert edited['revision'] != payload['revision'] and edited['mapRevision'] == revision
    # A User Map too large for one upload loses the wall outline first, then itself.
    filler = 'x' * (MAX_MAP_UPLOAD_BYTES // 2)
    outline = next(f for f in user_map['features'] if f['properties']['role'] == 'wall_outline')
    large = {**user_map, 'features': [
        *[f for f in user_map['features'] if f is not outline],
        {**outline, 'properties': {**outline['properties'], 'pad': filler * 3}}]}
    trimmed = map_payload(metadata, b'png', runtime, zones, large, revision)
    assert all(f['properties']['role'] != 'wall_outline' for f in trimmed['userMap']['features'])
    huge = {**large, 'pad': filler * 3}
    assert map_payload(metadata, b'png', runtime, zones, huge, revision)['userMap'] is None


def test_deleting_a_map_removes_its_user_map(saved, tmp_path):
    catalog, runtime, path = saved
    space_documents(runtime, catalog)
    other = _house(tmp_path, 'office')
    load_or_build_user_map(other, 'real-office')
    assert user_map_path(other).exists()
    removed = catalog.delete('office.yaml')
    assert 'office.user-map.geojson' in removed and not user_map_path(other).exists()
    assert not zones_path(other).exists()


def test_rooms_save_command_answers_directly_and_uploads_again(saved):
    catalog, runtime, path = saved
    _user_map, _zones, revision = space_documents(runtime, catalog)
    map_id = cloud_map_id(runtime)
    sync = CloudSync(SimpleNamespace(data=PanelData(), catalog=catalog,
                                     submit=Mock(), node=Mock()), Mock())
    sync.bridge.data.runtime = {**sync.bridge.data.runtime, **runtime}
    sync.last_map_at = 50.0
    rooms = [_room('room-a', '거실', _box(-0.7, -0.7, 2.7, 1.4))]
    sync.dispatch({'id': COMMAND_ID, 'operation': 'rooms_save', 'payload': {
        'map_id': map_id, 'map_revision': revision, 'rooms': rooms, 'resolution': 0.05}})
    assert sync.pending[COMMAND_ID] == {'ok': True, 'result': {
        'saved': 1, 'features': 3, 'map_id': map_id, 'map_revision': revision}}
    assert sync.last_map_at == 0.0
    sync.bridge.submit.assert_not_called()
    user_map, _zones, _revision = space_documents(runtime, catalog)
    assert user_map['features'][-1] == rooms[0]
