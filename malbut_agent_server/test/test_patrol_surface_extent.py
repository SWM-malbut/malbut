"""Region claims must follow visible foreground extent, including disconnected limbs."""

import copy

import numpy as np
import pytest

from malbut_agent_server.patrol_spatial import localize_objects
from test_patrol_people import PERSON, body_depth
from test_patrol_spatial import capture as spatial_capture, region, sloped_surface


@pytest.fixture
def capture(tmp_path):
    return spatial_capture.__wrapped__(tmp_path)


@pytest.mark.parametrize('corner', [False, True])
def test_nearby_wall_between_legs_is_not_a_person_crossing_the_room(capture, corner):
    _, metadata = capture
    analysis = copy.deepcopy(PERSON)
    analysis['objects'][0]['image_anchors'] = [[350, 250], [350, 350], [350, 450]]
    depth = np.full((100, 100), 2.8, np.float32)
    # The gap's wall patch is disconnected from the rest of the wall by the legs.
    # Its coplanarity with measured background, not distance alone, excludes it.
    if corner:
        # The background is one depth component but turns into another wall.
        depth[:, 75:] += np.arange(25, dtype=np.float32)[None, :]*.03
    depth[:, 28:43] = 2.
    depth[:, 58:73] = 2.
    regions = [region('a', '사람 방', -3, 1.5), region('b', '벽 쪽', 1.5, 3)]
    spatial = localize_objects(analysis, metadata, regions, depth)['objects'][0]['spatial']
    assert spatial['status'] == 'located' and spatial['region']['id'] == 'a'
    assert spatial['map_position']['x'] == 1.

    # A real disconnected limb reaching the adjacent room still vetoes the claim.
    depth[:, 58:73] = 2.8
    depth[15:85, 58:73] = 2.6
    regions = [region('a', '사람 방', -3, 1.4), region('b', '옆방', 1.4, 3)]
    spatial = localize_objects(analysis, metadata, regions, depth)['objects'][0]['spatial']
    assert spatial['status'] == 'unknown' and spatial['region'] is None
    assert spatial['reason'] == 'visible_body_crosses_region'


def test_all_person_anchors_on_surrounding_wall_cannot_establish_position(capture):
    _, metadata = capture
    analysis = copy.deepcopy(PERSON)
    analysis['objects'][0]['image_anchors'] = [[500, 250], [500, 350], [500, 450]]
    spatial = localize_objects(analysis, metadata, [region('a', '방', -3, 8)],
                               body_depth())['objects'][0]['spatial']
    assert spatial['status'] == 'unknown'
    assert spatial['reason'] == 'person_background_anchors'


@pytest.mark.parametrize('boundary,expected', [(1.4, 'unknown'), (8., 'located')])
def test_anchors_on_one_side_do_not_hide_connected_furniture_extent(capture, boundary, expected):
    _, metadata = capture
    analysis, depth = sloped_surface()
    analysis['objects'][0].update(name='탁자', image_anchors=[[620, 350], [700, 350]])
    regions = [region('a', '첫 방', -3, boundary), region('b', '둘째 방', boundary, 10)]
    spatial = localize_objects(analysis, metadata, regions, depth)['objects'][0]['spatial']
    assert spatial['status'] == expected
    if expected == 'unknown':
        assert spatial['reason'] == 'surface_crosses_regions' and spatial['region'] is None


def test_furniture_footprint_does_not_jump_to_disconnected_far_wall(capture):
    _, metadata = capture
    analysis, depth = sloped_surface()
    analysis['objects'][0].update(name='탁자', image_anchors=[[620, 350], [700, 350]])
    depth[:, :55] = 5.
    regions = [region('a', '첫 방', -3, 1.4), region('b', '둘째 방', 1.4, 8)]
    spatial = localize_objects(analysis, metadata, regions, depth)['objects'][0]['spatial']
    assert spatial['status'] == 'located' and spatial['region']['id'] == 'a'
