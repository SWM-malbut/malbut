"""Use measured anchors without fabricating depth or crossing object/room boundaries."""

import copy

import numpy as np
import pytest

from malbut_agent_server.patrol_spatial import _surface_link, _connected_group, localize_objects
from test_patrol_spatial import ANALYSIS, REGIONS, capture as spatial_capture


@pytest.fixture
def capture(tmp_path):
    return spatial_capture.__wrapped__(tmp_path)


def test_curved_surface_is_connected_but_steps_and_missing_strips_are_not():
    x = np.linspace(0, 2*np.pi, 100)
    depth = np.tile(2+.18*np.sin(x), (100, 1)).astype(np.float32)
    assert _surface_link((10, 50), (90, 50), depth)
    depth[:, 50:] += .3
    assert not _surface_link((10, 50), (90, 50), depth)
    depth[:] = 2.
    depth[:, 45:55] = 0.
    assert not _surface_link((10, 50), (90, 50), depth)
    assert _connected_group(np.array([[1, 1, 0], [1, 1, 1], [0, 1, 1]]))
    assert not _connected_group(np.array([[1, 1, 0], [1, 1, 0], [0, 0, 1]]))


def test_large_furniture_surface_is_allowed_but_room_crossing_stays_unknown(capture):
    _, metadata = capture
    analysis = copy.deepcopy(ANALYSIS)
    analysis['objects'][0].update(image_bbox=[0, 0, 1000, 1000],
                                  image_anchors=[[100, 400], [500, 500], [900, 600]])
    depth = np.full((100, 100), 2., np.float32)
    result = localize_objects(analysis, metadata, REGIONS, depth)['objects'][0]['spatial']
    assert result['status'] == 'located' and result['anchor_count'] == 3
    rooms = [{'id': name, 'name': name, 'polygons': [[
        [[-3, y0], [3, y0], [3, y1], [-3, y1], [-3, y0]]]]}
        for name, y0, y1 in [('one', -2, 0), ('two', 0, 2)]]
    result = localize_objects(analysis, metadata, rooms, depth)['objects'][0]['spatial']
    assert result['status'] == 'unknown' and result['region'] is None
