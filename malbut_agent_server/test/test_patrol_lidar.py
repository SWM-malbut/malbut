"""A laser return must match the same visible body surface before comparison."""

import hashlib
import io
import json

import numpy as np
import pytest

from malbut_agent_server.patrol_lidar import compare_person_lidar, read_lidar
from malbut_agent_server.patrol_memory import read_capture
from malbut_agent_server.patrol_spatial import _support_clear, localize_objects
from test_patrol_people import PERSON, body_depth
from test_patrol_spatial import capture as spatial_capture, region


@pytest.fixture
def capture(tmp_path):
    return spatial_capture.__wrapped__(tmp_path)


def scan():
    angles = np.linspace(-.23, .23, 47)
    return {'status': 'available', 'stamp': {'sec': 42, 'nanosec': 0},
            'angle_min': float(angles[0]), 'angle_increment': .01,
            'time_increment': 0., 'range_min': .03, 'range_max': 8.,
            'map_from_lidar': {'translation': [-1., 0., .2],
                               'rotation_xyzw': [0., 0., 0., 1.]},
            'ranges': (2/np.cos(angles)).astype(np.float32)}


def legs():
    depth = body_depth()
    depth[depth == np.float32(2.1)] = 2.
    return depth


def test_same_body_surface_and_map_transform(capture):
    _, metadata = capture
    laser = scan()
    matched = compare_person_lidar(PERSON['objects'][0], metadata, legs(), laser)
    assert matched['status'] == 'matched' and matched['matched_count'] >= 10
    assert matched['median_abs_error_m'] < .0001
    # Both sensors translated together must preserve correspondence.
    metadata['spatial']['map_from_camera']['translation'][1] += 10
    laser['map_from_lidar']['translation'][1] += 10
    assert compare_person_lidar(PERSON['objects'][0], metadata, legs(), laser) == matched


@pytest.mark.parametrize('fault', ['stale', 'other_surface', 'occluded', 'ambiguous'])
def test_wrong_surface_or_time_cannot_be_used(capture, fault):
    _, metadata = capture
    laser, depth, boxes = scan(), legs(), []
    if fault == 'stale':
        laser['stamp']['sec'] -= 1
    elif fault == 'other_surface':
        laser['ranges'] *= .75
    elif fault == 'occluded':
        depth[depth == 2] = 1.
    else:
        boxes = [PERSON['objects'][0]['image_bbox']]
    result = compare_person_lidar(PERSON['objects'][0], metadata, depth, laser, boxes)
    assert result['status'] == 'unmatched'
    assert result.get('matched_count', 0) == 0


def test_beam_acquisition_time_is_checked_separately(capture):
    _, metadata = capture
    laser = scan()
    laser['time_increment'] = .01
    result = compare_person_lidar(PERSON['objects'][0], metadata, legs(), laser)
    assert result.get('matched_count', 0) < 5
    assert result.get('max_time_offset_s', 0) <= .05


def test_lidar_does_not_overwrite_ambiguous_person_room(capture):
    _, metadata = capture
    regions = [region('a', '왼쪽', -3, 1), region('b', '오른쪽', 1, 3)]
    result = localize_objects(PERSON, metadata, regions, legs(), scan())['objects'][0]['spatial']
    assert result['status'] == 'unknown' and result['region'] is None
    assert result['lidar_comparison']['status'] == 'matched'
    assert result['reobserve_hint']['purpose'] == 'aim_only_not_object_position'
    assert result['map_position'] is None
    assert result['reobserve_hint']['xy'][0] == 1.
    assert abs(result['reobserve_hint']['xy'][1]) < .6


def test_parallel_body_support_near_boundary_keeps_full_uncertainty_margin(capture):
    _, metadata = capture
    regions = [region('a', '촬영방', -3, .75), region('b', '대상방', .75, 3)]
    result = localize_objects(PERSON, metadata, regions, legs())['objects'][0]['spatial']
    assert result['status'] == 'located' and result['region']['name'] == '대상방'
    assert result['boundary_margin_m'] >= .16
    # The same body actually straddling a boundary must remain unknown.
    regions = [region('a', '왼쪽', -3, 1), region('b', '오른쪽', 1, 3)]
    assert localize_objects(PERSON, metadata, regions, legs())['objects'][0][
        'spatial']['status'] == 'unknown'


def test_footprint_cannot_bridge_holes_or_concave_boundaries():
    regions = [region('a', '방', -2, 2)]
    regions[0]['polygons'][0].append([[-.1, -.1], [.1, -.1], [.1, .1], [-.1, .1]])
    assert not _support_clear([[-.8, 0], [.8, 0]], regions, .12)
    # A hole wholly within the footprint must also be rejected.
    assert not _support_clear([[-1, -1], [1, -1], [0, 1]], regions, .12)
    assert _support_clear([[-1, -1], [-.8, -1]], regions, .12)


def test_lidar_sidecar_integrity_checked_before_inference(capture):
    path, metadata = capture
    laser = scan()
    stream = io.BytesIO()
    np.save(stream, laser.pop('ranges'), allow_pickle=False)
    data = stream.getvalue()
    metadata['lidar'] = dict(laser, count=47, sha256=hashlib.sha256(data).hexdigest())
    sidecar = path.with_name(path.stem+'.lidar.npy')
    sidecar.write_bytes(data)
    path.write_text(json.dumps(metadata))
    assert len(read_lidar(path, metadata)['ranges']) == 47
    read_capture(path)
    sidecar.write_bytes(data+b'changed')
    with pytest.raises(ValueError):
        read_capture(path)


def test_one_bad_anchor_on_foreground_cannot_name_it_as_a_body_surface(capture):
    _, metadata = capture
    person = dict(PERSON['objects'][0], image_anchors=[[350, 350], [650, 350], [350, 650]])
    depth = legs()
    depth[60:72, 28:43] = 1.
    result = compare_person_lidar(person, metadata, depth, scan())
    assert result['status'] == 'unmatched' and result['matched_count'] == 0


def test_unanchored_visible_leg_across_boundary_vetoes_room(capture):
    _, metadata = capture
    person = dict(PERSON['objects'][0], image_anchors=[[350, 250], [350, 350], [350, 450]])
    analysis = dict(PERSON, objects=[person])
    depth = legs()
    depth[15:85, 58:73] = 2.6
    regions = [region('a', '첫 구역', -3, 1.4), region('b', '옆 구역', 1.4, 3)]
    result = localize_objects(analysis, metadata, regions, depth)['objects'][0]['spatial']
    assert result['status'] == 'unknown'
    assert result['reason'] == 'visible_body_crosses_region'


def test_comparison_evidence_survives_store_restart(capture):
    from malbut_agent_server.patrol_memory import PatrolMemoryStore, validate_analysis

    path, metadata = capture
    regions = [region('a', '촬영방', -3, 0), region('b', '대상방', 0, 3)]
    analysis = localize_objects(PERSON, metadata, regions, legs(), scan())
    validate_analysis(analysis, localized=True)
    store = PatrolMemoryStore(path.parent.parent, 'owner', simulation=True)
    store.ingest()
    store.complete(path.stem, analysis, 'test')
    restarted = PatrolMemoryStore(store.directory, 'owner', simulation=True)
    result = restarted.search('owner', dict(query='사람', since=None, until=None, limit=5))
    stored = result['observations'][0]['objects'][0]['spatial']
    assert stored['region']['name'] == '대상방'
    assert stored['lidar_comparison']['status'] == 'matched'


def test_navigation_hint_is_stored_but_never_returned_as_an_answer_location(capture):
    from malbut_agent_server.patrol_memory import PatrolMemoryStore

    path, metadata = capture
    regions = [region('a', '왼쪽', -3, 1), region('b', '오른쪽', 1, 3)]
    analysis = localize_objects(PERSON, metadata, regions, legs(), scan())
    assert analysis['objects'][0]['spatial']['reobserve_hint']
    store = PatrolMemoryStore(path.parent.parent, 'owner', simulation=True)
    store.ingest()
    store.complete(path.stem, analysis, 'test')
    result = store.search('owner', dict(query='사람', since=None, until=None, limit=5))
    spatial = result['observations'][0]['objects'][0]['spatial']
    assert spatial['region'] is None and spatial['map_position'] is None
    assert 'reobserve_hint' not in spatial
    with store.connect() as connection:
        stored = json.loads(connection.execute('SELECT analysis FROM observations').fetchone()[0])
    assert stored['objects'][0]['spatial']['reobserve_hint']
