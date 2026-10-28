"""Occupancy edits are isolated until explicit, recoverable navigation apply."""

import json
import uuid
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from malbut_bringup.map_edit import MapEdits, apply_ticket, erase, map_lock, recover
from malbut_bringup.user_map import load_slam_map, load_or_build_user_map, save_rooms
from malbut_bringup.zones import read_zones, write_zones


def command():
    return str(uuid.uuid4())


@pytest.fixture
def saved_map(tmp_path):
    pixels = np.full((40, 50), 254, dtype=np.uint8)
    pixels[[0, -1], :] = 0
    pixels[:, [0, -1]] = 0
    pixels[12:16, 14:18] = 0
    pixels[13, 15] = 205
    image = tmp_path / 'home.pgm'
    assert cv2.imwrite(str(image), pixels)
    path = tmp_path / 'home.yaml'
    path.write_text(yaml.safe_dump({'image': image.name, 'resolution': .05,
                    'origin': [-2.1, 3.4, .63], 'negate': 0, 'mode': 'trinary',
                    'occupied_thresh': .65, 'free_thresh': .196}))
    return path


def region(path):
    transform = load_slam_map(path).transform
    # MapTransform.world takes cell indices; these are exact cell edges.
    return [[transform.world(x-.5, y-.5) for x, y in [(13,11),(19,11),(19,17),(13,17)]]]


def save(path):
    edits = MapEdits(path)
    previous = edits.preview()
    result = edits.save(previous['revision'], region(path), command())
    return edits, previous, result


def test_save_only_removes_selected_occupied_cells_and_keeps_live_map(saved_map):
    original_yaml = saved_map.read_bytes()
    original_image = load_slam_map(saved_map).image.copy()
    edits, before, result = save(saved_map)
    assert result['changedCells'] == 15
    edited = load_slam_map(edits.revision_path(result['revision'])).image
    assert edited[13, 15] == 205
    assert np.all(edited[12:16, 14:18][original_image[12:16,14:18] == 0] == 255)
    assert np.all(edited[0] == 0)
    assert saved_map.read_bytes() == original_yaml
    np.testing.assert_array_equal(load_slam_map(saved_map).image, original_image)
    reopened = MapEdits(saved_map).preview()
    assert reopened['appliedRevision'] == before['revision']
    assert reopened['revision'] == result['revision']


@pytest.mark.parametrize('polygons', [[], [[[0,0],[1,1],[2,2]]],
                         [[[0,0],[2,2],[0,3],[3,0]]], [[[0,0],[1,0],[float('nan'),1]]]])
def test_invalid_selection_never_changes_saved_files(saved_map, polygons):
    edits = MapEdits(saved_map)
    before = edits.preview()
    with pytest.raises(ValueError):
        edits.save(before['revision'], polygons, command())
    assert edits.preview()['revision'] == before['revision']


def test_overlap_counts_once_and_boundary_cells_are_included(saved_map):
    slam = load_slam_map(saved_map)
    polygons = region(saved_map)
    result, count = erase(slam, polygons + polygons)
    assert count == 15
    assert result[13,15] == 205


def test_stale_and_duplicate_requests(saved_map):
    edits = MapEdits(saved_map)
    before = edits.preview()
    token = command()
    first = edits.save(before['revision'], region(saved_map), token)
    assert edits.save(before['revision'], region(saved_map), token) == first
    with pytest.raises(ValueError, match='다른 수정본'):
        edits.save(before['revision'], region(saved_map), command())


def test_apply_preserves_latest_room_edits_and_zones_then_restore_is_separate(saved_map):
    edits, before, result = save(saved_map)
    user_map = load_or_build_user_map(saved_map, 'real-home')
    rooms = [f for f in user_map['features'] if f['properties']['role'] == 'room']
    rooms[0]['properties']['name'] = '거실'
    save_rooms(saved_map, 'real-home', {'map_id': 'real-home', 'map_revision': user_map['map_revision'], 'rooms': rooms})
    zone = {'type': 'Feature', 'properties': {'role': 'semantic_zone', 'behavior': 'restricted', 'name': '현관'},
            'geometry': {'type': 'Polygon', 'coordinates': [[[0,0],[1,0],[1,1],[0,0]]]}}
    write_zones(saved_map, [zone])
    activated = []
    ticket = edits.ticket(result['revision'], result['revision'], command())
    def activate(path):
        assert load_slam_map(Path(path)).image[12,14] == 255
        activated.append(path)
    apply_ticket(ticket, activate)
    assert activated == [str(saved_map)]
    assert edits.preview()['appliedRevision'] == result['revision']
    assert read_zones(saved_map) == [zone]
    refreshed = load_or_build_user_map(saved_map, 'real-home')
    assert [f for f in refreshed['features'] if f['properties']['role'] == 'room'] == rooms
    # Redelivery after restart does not activate a second time.
    apply_ticket(ticket, activate)
    assert len(activated) == 1
    restored = edits.save(result['revision'], None, command(), restore=True)
    assert edits.preview()['appliedRevision'] == result['revision']
    assert load_slam_map(saved_map).image[12,14] == 255
    apply_ticket(edits.ticket(restored['revision'], restored['revision'], command()), activate=lambda _: None)
    assert load_slam_map(saved_map).image[12,14] == 0
    assert read_zones(saved_map) == [zone]


def test_failed_activation_restores_yaml_image_and_semantics(saved_map):
    edits, before, result = save(saved_map)
    load_or_build_user_map(saved_map, 'real-home')
    write_zones(saved_map, [])
    previous = {p.name: p.read_bytes() for p in saved_map.parent.iterdir() if p.is_file()}
    def fail(_):
        raise RuntimeError('localization failed')
    with pytest.raises(RuntimeError):
        apply_ticket(edits.ticket(result['revision'], result['revision'], command()), fail)
    for name, content in previous.items():
        assert (saved_map.parent / name).read_bytes() == content
    assert edits.preview()['appliedRevision'] == before['revision']


def test_interrupted_apply_recovers_before_open(saved_map):
    from malbut_bringup.map_edit import _backup, _json
    edits, _, _ = save(saved_map)
    state = edits.state()
    original = saved_map.read_bytes()
    _json(edits.directory / 'applying.json', {'files': _backup(saved_map), 'state': state})
    saved_map.write_text('broken yaml')
    with map_lock(saved_map):
        recover(saved_map)
    assert saved_map.read_bytes() == original
    assert not (edits.directory / 'applying.json').exists()


def test_external_map_replacement_invalidates_draft_and_open_starts_new_generation(saved_map):
    edits, _, result = save(saved_map)
    pixels = load_slam_map(saved_map).image
    pixels[20,20] = 0
    cv2.imwrite(str(saved_map.with_suffix('.pgm')), pixels)
    with pytest.raises(ValueError, match='외부'):
        edits.save(result['revision'], region(saved_map), command())
    edits.reset_external()
    assert edits.preview()['revision'] != result['revision']
    assert edits.revision_path(result['revision']).exists()


def test_raw_mode_rejected(saved_map):
    metadata = yaml.safe_load(saved_map.read_text())
    metadata['mode'] = 'raw'
    saved_map.write_text(yaml.safe_dump(metadata))
    with pytest.raises(ValueError, match='trinary'):
        MapEdits(saved_map).preview()


@pytest.mark.parametrize('negate', [0, 1])
def test_erased_cells_are_free_even_with_small_threshold_and_negated_map(saved_map, negate):
    from malbut_bringup.map_edit import occupancy
    metadata = yaml.safe_load(saved_map.read_text())
    metadata.update(free_thresh=.001, negate=negate)
    pixels = load_slam_map(saved_map).image
    pixels[pixels == 254] = 255
    if negate:
        pixels = 255-pixels
    pixels[13,15] = 205
    cv2.imwrite(str(saved_map.with_suffix('.pgm')), pixels)
    saved_map.write_text(yaml.safe_dump(metadata))
    edits, _, result = save(saved_map)
    edited = load_slam_map(edits.revision_path(result['revision']))
    occupied, free = occupancy(edited)
    assert free[12,14] and not occupied[12,14]
    assert edited.image[13,15] == 205
