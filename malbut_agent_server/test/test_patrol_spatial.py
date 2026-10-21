"""Capture region is never used as an object's region: geometry and durable evidence tests."""

import copy
import hashlib
import io
import json
from uuid import uuid4

import numpy as np
import pytest

from malbut_agent_server.patrol_memory import PatrolMemoryStore, read_capture, validate_analysis
from malbut_agent_server.patrol_observer import PatrolObservationWorker
from malbut_agent_server.patrol_spatial import localize_objects, read_spatial, region_at


def region(identity, name, x0, x1):
    return {'id': identity, 'name': name,
            'polygons': [[[[x0, -2], [x1, -2], [x1, 2], [x0, 2], [x0, -2]]]]}


REGIONS = [region('living', '거실', -3, 0), region('workroom', '작업실', 0, 3)]
ANALYSIS = {'summary': '빨간 상자가 보인다.', 'objects': [
    {'name': '빨간 상자', 'location': '사진 가운데', 'confidence': 'high',
     'image_bbox': [350, 350, 650, 650]}], 'uncertainties': []}


@pytest.fixture
def capture(tmp_path):
    folder = tmp_path / 'captures'
    folder.mkdir()
    identifier = uuid4().hex
    path = folder / (identifier + '.json')
    image = b'\xff\xd8test\xff\xd9'
    # Optical +Z -> map +X; camera is in the living room, visible object in workroom.
    metadata = {'version': 2, 'observation_id': identifier, 'patrol_id': uuid4().hex,
                'map_id': 'a' * 64, 'image_sha256': hashlib.sha256(image).hexdigest(),
                'captured_at': '2026-10-21T05:00:00+00:00', 'simulated': True,
                'map_frame': 'map', 'room_map': 'regions.geojson', 'camera_frame': 'optical',
                'image_stamp': {'sec': 42, 'nanosec': 0},
                'camera_pose': {'x': -1., 'y': 0., 'yaw': 0.},
                'capture_rooms': ['거실'], 'capture_regions': [{'id': 'living', 'name': '거실'}],
                'spatial': {'status': 'available', 'depth_shape': [100, 100],
                            'depth_stamp': {'sec': 42, 'nanosec': 0},
                            'intrinsics': {'fx': 100., 'fy': 100., 'cx': 50., 'cy': 50.},
                            'map_from_camera': {'translation': [-1., 0., .2],
                                                'rotation_xyzw': [-.5, .5, -.5, .5]},
                            'optical_frame': 'optical'}}
    regions = json.dumps({'version': 1, 'regions': REGIONS}, ensure_ascii=False).encode()
    buffer = io.BytesIO()
    np.save(buffer, np.full((100, 100), 2., np.float32), allow_pickle=False)
    depth = buffer.getvalue()
    metadata['regions_sha256'] = hashlib.sha256(regions).hexdigest()
    metadata['spatial']['depth_sha256'] = hashlib.sha256(depth).hexdigest()
    for suffix, contents in [('.jpg', image), ('.regions.json', regions), ('.depth.npy', depth)]:
        path.with_name(identifier + suffix).write_bytes(contents)
    path.write_text(json.dumps(metadata, ensure_ascii=False))
    return path, metadata


@pytest.mark.parametrize('calibration', [
    None,
    {'image_space': 'raw', 'distortion_model': 'plumb_bob',
     'distortion_coefficients': [.1, -.03, 0., 0., .01]},
    {'image_space': 'rectified', 'distortion_model': 'none', 'distortion_coefficients': []},
])
def test_actual_object_region_persists_and_can_be_searched_after_restart(capture, calibration):
    path, metadata = capture
    if calibration is not None:
        metadata['spatial']['calibration'] = calibration
        path.write_text(json.dumps(metadata, ensure_ascii=False))

    class Observer:
        model = 'fixture-vision'

        def available(self):
            return True

        def analyze(self, metadata, image):
            return copy.deepcopy(ANALYSIS)
    store = PatrolMemoryStore(path.parent.parent, 'owner', simulation=True)
    worker = PatrolObservationWorker(store, Observer(), autostart=False)
    assert worker.run_once()
    reopened = PatrolMemoryStore(store.directory, 'owner', simulation=True)
    result = reopened.search('owner', {'query': '작업실 상자', 'since': None,
                                       'until': None, 'limit': 5})
    item, = result['observations']
    assert item['capture_regions'] == [{'id': 'living', 'name': '거실'}]
    spatial = item['objects'][0]['spatial']
    assert spatial['status'] == 'located'
    assert spatial['region'] == {'id': 'workroom', 'name': '작업실'}
    assert spatial['map_position'] == {'x': 1., 'y': 0., 'z': .2}
    # Snapshot names for other rooms must not make irrelevant searches match.
    assert not reopened.search('owner', {
        'query': '주방', 'since': None, 'until': None, 'limit': 5})['observations']


@pytest.mark.parametrize('suffix', ['.jpg', '.depth.npy', '.regions.json'])
def test_modified_evidence_is_never_used(capture, suffix):
    path, metadata = capture
    store = PatrolMemoryStore(path.parent.parent, 'owner', simulation=True)
    store.ingest()
    regions, depth = read_spatial(path, metadata)
    store.complete(path.stem, localize_objects(ANALYSIS, metadata, regions, depth), 'fixture')
    sidecar = path.with_name(path.stem + suffix)
    sidecar.write_bytes(sidecar.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        read_capture(path)
    assert not store.search('owner', {
        'query': None, 'since': None, 'until': None, 'limit': 5})['observations']


def test_depth_stamp_or_transform_mismatch_is_rejected(capture):
    path, metadata = capture
    metadata['spatial']['depth_stamp']['nanosec'] = 100000000
    with pytest.raises(ValueError, match='unsynchronized'):
        read_spatial(path, metadata)
    metadata['spatial']['depth_stamp']['nanosec'] = 0
    metadata['spatial']['map_from_camera']['rotation_xyzw'] = [0, 0, 0, 0]
    with pytest.raises(ValueError, match='transform'):
        read_spatial(path, metadata)


@pytest.mark.parametrize('value,reason', [
    (np.nan, 'insufficient_depth'), (0, 'insufficient_depth'), (np.inf, 'insufficient_depth')])
def test_missing_depth_never_inherits_camera_room(capture, value, reason):
    _, metadata = capture
    output = localize_objects(ANALYSIS, metadata, REGIONS, np.full((100, 100), value))
    spatial = output['objects'][0]['spatial']
    assert spatial['region'] is None and spatial['reason'] == reason


def test_boundary_overlap_holes_and_multipolygons_are_conservative():
    assert region_at([.05, 0], REGIONS, .15) == (None, 'region_boundary')
    assert region_at([4, 0], REGIONS, .15) == (None, 'outside_named_regions')
    overlapping = REGIONS + [region('overlap', '겹침', .1, 2)]
    assert region_at([1, 0], overlapping, .1)[1] == 'overlapping_regions'
    holed = region('hole', '구역', 0, 3)
    holed['polygons'][0].append([[.5, -.5], [1.5, -.5], [1.5, .5], [.5, .5]])
    assert region_at([1, 0], [holed], .1)[1] == 'outside_named_regions'
    holed['polygons'] += region('other', '구역', 5, 7)['polygons']
    assert region_at([6, 0], [holed], .1)[0]['id'] == 'hole'


def test_mixed_depth_uncertain_identity_and_missing_bbox_remain_unknown(capture):
    _, metadata = capture
    depth = np.full((100, 100), 2.)
    depth[:, 50:] = 4.
    result = localize_objects(ANALYSIS, metadata, REGIONS, depth)
    assert result['objects'][0]['spatial']['reason'] == 'mixed_depth'
    for key, value in [('confidence', 'uncertain'), ('image_bbox', None)]:
        analysis = copy.deepcopy(ANALYSIS)
        analysis['objects'][0][key] = value
        result = localize_objects(analysis, metadata, REGIONS, np.full((100, 100), 2.))
        assert result['objects'][0]['spatial']['reason'] == 'object_not_grounded'


def test_rotated_map_transform_moves_object_into_capture_region(capture):
    _, metadata = capture
    # 180 degrees around map Z, composed with the original optical orientation.
    metadata['spatial']['map_from_camera']['rotation_xyzw'] = [-.5, -.5, .5, .5]
    metadata['spatial']['map_from_camera']['translation'][0] = -.5
    result = localize_objects(ANALYSIS, metadata, REGIONS, np.full((100, 100), 1.))
    spatial = result['objects'][0]['spatial']
    assert spatial['region']['id'] == 'living'
    assert spatial['map_position']['x'] == -1.5


def sloped_surface():
    _, x = np.indices((100, 100))
    # A planar face seen at an angle, with substantial but continuous depth variation.
    depth = (1 / (.5 + .9 * (x - 50) / 100)).astype(np.float32)
    analysis = copy.deepcopy(ANALYSIS)
    analysis['objects'][0]['image_bbox'] = [200, 200, 800, 800]
    return analysis, depth


def test_sloped_surface_uses_measured_center_and_persists_its_region(capture):
    path, metadata = capture
    analysis, depth = sloped_surface()
    buffer = io.BytesIO()
    np.save(buffer, depth, allow_pickle=False)
    data = buffer.getvalue()
    path.with_suffix('.depth.npy').write_bytes(data)
    metadata['spatial']['depth_sha256'] = hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(metadata))
    regions, stored_depth = read_spatial(path, metadata)
    result = localize_objects(analysis, metadata, regions, stored_depth)
    spatial = result['objects'][0]['spatial']
    assert spatial['status'] == 'located'
    assert spatial['region']['name'] == '작업실'
    assert spatial['map_position'] == {'x': 1., 'y': 0., 'z': .2}
    assert spatial['depth_support'] == 'planar_center_patch'
    store = PatrolMemoryStore(path.parent.parent, 'owner', simulation=True)
    store.ingest()
    store.complete(path.stem, result, 'fixture')
    reopened = PatrolMemoryStore(store.directory, 'owner', simulation=True)
    recalled = reopened.search('owner', {
        'query': '작업실 상자', 'since': None, 'until': None, 'limit': 5})
    assert recalled['observations'][0]['objects'][0]['spatial'] == spatial


@pytest.mark.parametrize('kind', ['step', 'missing_center', 'occluded_center'])
def test_plane_fallback_never_fills_holes_or_accepts_depth_discontinuities(capture, kind):
    _, metadata = capture
    analysis, depth = sloped_surface()
    if kind == 'step':
        depth[:, :50], depth[:, 50:] = 1.9, 2.1
    else:
        depth[47:54, 47:54] = 0. if kind == 'missing_center' else 1.
    spatial = localize_objects(analysis, metadata, REGIONS, depth)['objects'][0]['spatial']
    assert spatial['status'] == 'unknown'
    expected = 'insufficient_depth' if kind == 'missing_center' else 'mixed_depth'
    assert spatial['reason'] == expected


def test_planar_anchor_does_not_hide_surface_spanning_multiple_regions(capture):
    _, metadata = capture
    analysis, depth = sloped_surface()
    regions = [region('one', '첫 방', -3, 1.4), region('two', '둘째 방', 1.4, 3)]
    spatial = localize_objects(analysis, metadata, regions, depth)['objects'][0]['spatial']
    assert spatial['status'] == 'unknown'
    assert spatial['region'] is None and spatial['reason'] == 'surface_crosses_regions'


def test_raw_distortion_is_removed_before_map_projection(capture):
    _, metadata = capture
    metadata['spatial']['calibration'] = {
        'image_space': 'raw', 'distortion_model': 'plumb_bob',
        'distortion_coefficients': [.5, 0., 0., 0., 0.]}
    analysis = copy.deepcopy(ANALYSIS)
    analysis['objects'][0]['image_bbox'] = [700, 400, 900, 600]
    result = localize_objects(analysis, metadata, REGIONS, np.full((100, 100), 2.))
    spatial = result['objects'][0]['spatial']
    # Raw x=.3, pure radial k1=.5: solve x + .5*x**3 = .3 independently.
    root = next(value.real for value in np.roots([.5, 0, 1, -.3])
                if abs(value.imag) < 1e-9)
    assert spatial['region']['id'] == 'workroom'
    assert spatial['map_position']['x'] == 1.
    assert spatial['map_position']['y'] == pytest.approx(-2*root, abs=.001)
    assert abs(spatial['map_position']['y'] - (-.6)) > .01


def test_rectified_and_legacy_pinhole_captures_have_the_same_projection(capture):
    _, metadata = capture
    depth = np.full((100, 100), 2.)
    legacy = localize_objects(ANALYSIS, metadata, REGIONS, depth)
    metadata['spatial']['calibration'] = {
        'image_space': 'rectified', 'distortion_model': 'none', 'distortion_coefficients': []}
    assert localize_objects(ANALYSIS, metadata, REGIONS, depth) == legacy


@pytest.mark.parametrize('calibration', [
    {'image_space': 'rectified', 'distortion_model': 'plumb_bob',
     'distortion_coefficients': [.1, 0, 0, 0, 0]},
    {'image_space': 'raw', 'distortion_model': 'fisheye', 'distortion_coefficients': []},
    {'image_space': 'raw', 'distortion_model': 'plumb_bob',
     'distortion_coefficients': [float('nan'), 0, 0, 0, 0]},
    {'image_space': 'raw', 'distortion_model': 'plumb_bob', 'distortion_coefficients': [.1]},
])
def test_untrusted_or_inconsistent_calibration_is_rejected(capture, calibration):
    path, metadata = capture
    metadata['spatial']['calibration'] = calibration
    with pytest.raises(ValueError, match='calibration'):
        read_spatial(path, metadata)


def test_model_cannot_assign_its_own_room_or_invalid_bbox():
    analysis = copy.deepcopy(ANALYSIS)
    analysis['objects'][0]['spatial'] = {'region': {'name': '주방'}}
    with pytest.raises(ValueError):
        validate_analysis(analysis)
    del analysis['objects'][0]['spatial']
    for bbox in [[0, 0, 1001, 300], [500, 100, 100, 500], [0, 0, 1.0, 1], []]:
        analysis['objects'][0]['image_bbox'] = bbox
        with pytest.raises(ValueError):
            validate_analysis(analysis)
