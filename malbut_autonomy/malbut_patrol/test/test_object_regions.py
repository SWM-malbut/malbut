"""Furniture room envelopes never become navigable space or erase room boundaries."""

import json

import cv2
import numpy as np
import pytest

from malbut_patrol.coverage import CoverageGrid
from malbut_patrol.spatial_capture import room_snapshot


def room(identity, x0, x1, holes=()):
    return {'id': identity, 'properties': {'role': 'room', 'name': identity},
            'geometry': {'type': 'Polygon', 'coordinates': [
                [[x0, .5], [x1, .5], [x1, 2.5], [x0, 2.5], [x0, .5]], *holes]}}


def inside(point, regions):
    return {r['id'] for r in regions for poly in r['polygons']
            if cv2.pointPolygonTest(np.asarray(poly[0], np.float32), point, False) > 0
            and all(cv2.pointPolygonTest(np.asarray(h, np.float32), point, False) < 0
                    for h in poly[1:])}


def test_furniture_expansion_is_bounded_and_does_not_modify_navigation():
    cells = np.full((70, 80), 100)
    cells[10:50, 10:40] = 0
    # Unnamed free floor must not be annexed to the nearest room.
    cells[20:30, 40:48] = 0
    grid = CoverageGrid(cells, .05, 0, 0)
    data = json.loads(room_snapshot([room('living', .5, 2.)], grid))
    assert data['version'] == 2
    assert not inside((2.2, 1.8), data['regions'])
    assert inside((2.2, 1.8), data['object_regions']) == {'living'}
    assert not inside((2.6, 1.8), data['object_regions'])
    assert not inside((2.2, 1.25), data['object_regions'])
    np.testing.assert_array_equal(grid.cells, cells)
    np.testing.assert_array_equal(grid.free, cells == 0)
    assert not grid.free.flags.writeable


def test_shared_boundary_holes_and_tied_cells_remain_separate():
    hole = [[.9, 1.], [1.3, 1.], [1.3, 1.5], [.9, 1.5], [.9, 1.]]
    grid = CoverageGrid(np.full((70, 90), 100), .05, 0, 0)
    data = json.loads(room_snapshot([room('left', .5, 2., [hole]),
                                     room('right', 2., 3.5)], grid))
    regions = data['object_regions']
    assert inside((1.8, 1.8), regions) == {'left'}
    assert inside((2.2, 1.8), regions) == {'right'}
    assert not inside((1.1, 1.2), regions)
    # Outside both floor polygons, equidistant furniture must not get a guessed room.
    assert not inside((2., 2.7), regions)


@pytest.mark.parametrize('yaw', [0., .7, -1.2])
def test_rotated_occupancy_origin_preserves_semantic_world_coordinates(yaw):
    grid = CoverageGrid(np.full((80, 80), 100), .05, 4., -2., yaw)
    feature = room('room', .5, 2.)
    points = np.asarray(feature['geometry']['coordinates'][0])
    c, s = np.cos(yaw), np.sin(yaw)
    feature['geometry']['coordinates'][0] = (
        points @ np.array([[c, s], [-s, c]]) + [4., -2.]).tolist()
    data = json.loads(room_snapshot([feature], grid))
    point = tuple(np.array([2.2, 1.8]) @ np.array([[c, s], [-s, c]]) + [4., -2.])
    assert inside(point, data['object_regions']) == {'room'}
    assert not inside(point, data['regions'])
