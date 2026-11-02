"""Shared frame calculations must preserve independent people and surface decisions."""

import copy

import numpy as np
import pytest

from malbut_agent_server import patrol_spatial as spatial
from test_patrol_spatial import region


@pytest.fixture
def scene():
    metadata = {'spatial': {
        'status': 'available',
        'intrinsics': {'fx': 100., 'fy': 100., 'cx': 50., 'cy': 50.},
        'map_from_camera': {'translation': [-1., 0., .2],
                            'rotation_xyzw': [-.5, .5, -.5, .5]}}}
    depth = np.full((100, 100), 4., np.float32)
    depth[15:85, 10:30] = 1.8
    depth[15:85, 60:80] = 2.4
    objects = []
    for name, kind, x in [('앞 사람', 'person', 0), ('뒤 사람', 'person', 500),
                           ('앞 상자', 'box', 0)]:
        objects.append({'name': name, 'kind': kind, 'location': '사진 안',
                        'confidence': 'high', 'image_bbox': [50+x, 100, 350+x, 900],
                        'image_anchors': [[150+x, 300], [250+x, 300], [150+x, 600]]})
    # A visible background patch must stay unknown even after a box used it.
    objects.append(dict(objects[0], name='배경을 짚은 사람',
                        image_anchors=[[320, 300], [320, 500]]))
    analysis = {'summary': '두 사람과 상자', 'objects': objects, 'uncertainties': []}
    regions = [region('a', '앞방', -3, 1.1), region('b', '뒷방', 1.1, 3)]
    return analysis, metadata, regions, depth


@pytest.mark.parametrize('order', [(0, 1, 2, 3), (3, 2, 1, 0), (2, 0, 3, 1)])
@pytest.mark.parametrize('boundary', [False, True])
def test_shared_geometry_matches_separate_people_and_boxes_without_order_leakage(
        scene, order, boundary):
    analysis, metadata, regions, depth = scene
    if boundary:
        regions = [region('a', '앞방', -3, .8), region('b', '뒷방', .8, 3)]
    analysis['objects'] = [analysis['objects'][i] for i in order]
    original, saved_depth = copy.deepcopy(analysis), depth.copy()
    expected = [spatial.localize_objects(dict(analysis, objects=[obj]), metadata,
                                         regions, depth)['objects'][0]
                for obj in analysis['objects']]
    result = spatial.localize_objects(analysis, metadata, regions, depth)
    assert result['objects'] == expected
    assert analysis == original
    np.testing.assert_array_equal(depth, saved_depth)
    located = {obj['name']: obj['spatial'] for obj in result['objects']}
    assert located['뒤 사람']['region']['id'] == 'b'
    assert located['배경을 짚은 사람']['reason'] == 'person_background_anchors'
    for name in ('앞 사람', '앞 상자'):
        assert located[name]['status'] == ('unknown' if boundary else 'located')
        assert located[name]['region'] == (None if boundary else {'id': 'a', 'name': '앞방'})


@pytest.mark.parametrize('count', [1, 8])
def test_only_one_full_frame_projection_and_read_only_points(scene, monkeypatch, count):
    analysis, metadata, regions, depth = scene
    analysis['objects'] = [copy.deepcopy(analysis['objects'][2]) for _ in range(count)]
    original_map, original_arrays = spatial._map_points, spatial._DepthGeometry.arrays
    full_projections, shared = [], []

    def project(points, calibration):
        if np.shape(points) == (depth.size, 3):
            full_projections.append(len(points))
        return original_map(points, calibration)

    def arrays(geometry):
        valid, points = original_arrays(geometry)
        assert not valid.flags.writeable and not points.flags.writeable
        shared.append(points)
        return valid, points

    monkeypatch.setattr(spatial, '_map_points', project)
    monkeypatch.setattr(spatial._DepthGeometry, 'arrays', arrays)
    result = spatial.localize_objects(analysis, metadata, regions, depth)
    assert all(obj['spatial']['status'] == 'located' for obj in result['objects'])
    assert full_projections == [depth.size]
    assert len(shared) == count and all(points is shared[0] for points in shared)


def test_no_full_frame_allocation_for_uncertain_or_missing_depth(scene, monkeypatch):
    analysis, metadata, regions, depth = scene
    analysis['objects'] = [dict(obj, confidence='uncertain') for obj in analysis['objects']]

    def unexpected(_geometry):
        raise AssertionError('unusable observations must not allocate full frame geometry')

    monkeypatch.setattr(spatial._DepthGeometry, 'arrays', unexpected)
    for source in (depth, None):
        result = spatial.localize_objects(analysis, metadata, regions, source)
        assert all(obj['spatial']['status'] == 'unknown' for obj in result['objects'])
    assert spatial.localize_objects(dict(analysis, objects=[]), metadata, regions, depth)[
        'objects'] == []


@pytest.mark.parametrize('changed', ['depth', 'transform', 'calibration'])
def test_geometry_does_not_survive_a_photo_or_reuse_changed_inputs(scene, changed):
    analysis, metadata, regions, depth = scene
    analysis['objects'] = [analysis['objects'][2]]
    first = spatial.localize_objects(analysis, metadata, regions, depth)
    if changed == 'depth':
        depth[15:85, 10:30] = 2.4
    elif changed == 'transform':
        metadata['spatial']['map_from_camera']['translation'][0] += .6
    else:
        metadata['spatial']['intrinsics']['cx'] += 10
    second = spatial.localize_objects(analysis, metadata, regions, depth)
    fresh = spatial.localize_objects(copy.deepcopy(analysis), copy.deepcopy(metadata),
                                     regions, depth.copy())
    assert second == fresh and second != first
    assert second['objects'][0]['spatial']['status'] == 'located'
