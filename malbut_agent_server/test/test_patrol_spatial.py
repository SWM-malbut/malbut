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


def test_actual_object_region_persists_and_can_be_searched_after_restart(capture):
    path, metadata = capture

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
