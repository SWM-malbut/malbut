"""Convert saved-map Zones into a Nav2 keepout mask without ROS."""

import json

import cv2
import numpy as np
import pytest
import yaml

from malbut_bringup.zones import (
    build_mask, empty_mask, read_zones, write_mask, write_zones, zone_feature, ZoneError,
    zones_path,
)


@pytest.fixture
def saved_map(tmp_path):
    """Write a 4 m x 2 m saved map at 0.05 m with origin (-1, -1)."""
    image = np.full((40, 80), 254, dtype=np.uint8)
    cv2.imwrite(str(tmp_path / 'home.pgm'), image)
    path = tmp_path / 'home.yaml'
    path.write_text('image: home.pgm\nresolution: 0.05\norigin: [-1.0, -1.0, 0.0]\n'
                    'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
    return path


def _cell(x, y):
    """Image column and row of map point (x, y); row 0 is the top."""
    return int((y + 1.0) // 0.05), int((x + 1.0) // 0.05)


def _value(mask, x, y):
    row_from_bottom, column = _cell(x, y)
    return mask[mask.shape[0] - 1 - row_from_bottom, column]


def test_restricted_zone_is_lethal_with_a_footprint_buffer(saved_map):
    """Nav2 checks restricted cost at the robot center; the buffer keeps the body out."""
    zone = zone_feature('restricted', [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]])
    mask, geometry = build_mask(saved_map, [zone])
    assert geometry == {'resolution': 0.05, 'origin': [-1.0, -1.0, 0.0]}
    assert mask.shape == (40, 80)
    assert _value(mask, 0.25, 0.25) == 100
    assert _value(mask, 0.66, 0.25) == 100  # 0.16 m outside: within the 0.2 m buffer
    assert _value(mask, 0.8, 0.25) == 0
    assert _value(mask, -0.5, -0.5) == 0


def test_avoid_is_traversable_cost_and_restricted_wins_overlaps(saved_map):
    """Keep the old Zone costs: avoid 70 (passable), restricted 100, allow 0."""
    avoid = zone_feature('avoid', [[-0.9, -0.9], [0.9, -0.9], [0.9, 0.9], [-0.9, 0.9]])
    restricted = zone_feature('restricted', [[0.0, 0.0], [0.3, 0.0], [0.3, 0.3], [0.0, 0.3]])
    allow = zone_feature('allow', [[-0.9, -0.9], [-0.5, -0.9], [-0.5, -0.5], [-0.9, -0.5]])
    mask, _ = build_mask(saved_map, [avoid, restricted, allow], restricted_buffer_m=0.0)
    assert _value(mask, -0.6, 0.6) == 70
    assert _value(mask, 0.15, 0.15) == 100
    assert _value(mask, -0.7, -0.7) == 70  # allow never lowers another Zone's cost
    assert _value(mask, 1.5, 0.5) == 0


def test_mask_files_use_raw_mode_so_costs_reach_nav2(saved_map, tmp_path):
    """map_server 'raw' passes 0..100 through; trinary would collapse the costs."""
    mask, geometry = build_mask(saved_map, [zone_feature(
        'avoid', [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]])])
    output = write_mask(tmp_path / 'cache/zone_mask.yaml', mask, geometry)
    metadata = yaml.safe_load(output.read_text())
    assert metadata == {'image': 'zone_mask.pgm', 'mode': 'raw', 'resolution': 0.05,
                        'origin': [-1.0, -1.0, 0.0], 'negate': 0,
                        'occupied_thresh': 1.0, 'free_thresh': 0.0}
    assert np.array_equal(cv2.imread(str(output.with_suffix('.pgm')), cv2.IMREAD_UNCHANGED),
                          mask)
    empty = yaml.safe_load(empty_mask(tmp_path / 'empty.yaml').read_text())
    assert empty['mode'] == 'raw'
    assert cv2.imread(str(tmp_path / 'empty.pgm'), cv2.IMREAD_UNCHANGED).tolist() == [[0]]


def test_zone_files_belong_to_one_version_of_one_map(saved_map):
    """A replaced map with the same name must not inherit old Zones."""
    assert read_zones(saved_map) == []
    zone = zone_feature('restricted', [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5]], 'sofa')
    write_zones(saved_map, [zone])
    stored = json.loads(zones_path(saved_map).read_text())
    assert stored['format'] == 'malbut-semantic-zones-v1' and stored['frame_id'] == 'map'
    assert read_zones(saved_map)[0]['properties'] == {
        'role': 'semantic_zone', 'behavior': 'restricted', 'name': 'sofa'}
    cv2.imwrite(str(saved_map.with_suffix('.pgm')), np.zeros((40, 80), dtype=np.uint8))
    with pytest.raises(ZoneError, match='different version'):
        read_zones(saved_map)


@pytest.mark.parametrize('behavior, points, message', [
    ('closed', [[0, 0], [1, 0], [1, 1]], 'unsupported'),
    ('restricted', [[0, 0], [1, 0]], 'closed with 3 to 64'),
    ('restricted', [[0, 0], [0.05, 0], [0.05, 0.05]], 'area'),
    ('avoid', [[0, 0], [float('nan'), 0], [1, 1]], 'finite'),
])
def test_invalid_zones_are_rejected(behavior, points, message):
    """Bad input never reaches a mask."""
    with pytest.raises(ZoneError, match=message):
        zone_feature(behavior, points)


def test_rotated_maps_are_refused(saved_map):
    """Nav2 filter masks are axis-aligned with the map frame."""
    saved_map.write_text(saved_map.read_text().replace('[-1.0, -1.0, 0.0]', '[-1.0, -1.0, 0.3]'))
    with pytest.raises(ZoneError, match='zero map origin yaw'):
        build_mask(saved_map, [])
