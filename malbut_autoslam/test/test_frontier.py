"""Exercise the shared frontier algorithm using only synthetic occupancy grids."""

import math
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_autoslam.frontier import (
    FRONTIER_CELL_CAP,
    FRONTIER_DISTANCE_PENALTY_CELLS_PER_M,
    MapGrid,
    blocked_approach,
    find_frontiers,
    map_grid_from_message,
    map_statistics,
    path_avoids_blocks,
    path_is_known_free,
    search_frontiers,
)


def test_blocked_approach_uses_current_path_segment_and_leaves_escape():
    """Do not blacklist the old start, the robot itself, or an alternative route."""
    path = [(0.0, 0.0), (4.0, 0.0), (4.0, 3.0)]
    block = blocked_approach(path, (3.0, 0.0), 0.3)
    assert block == pytest.approx((3.6, 0.0, 0.3))
    assert not path_avoids_blocks([(3.0, 0.0), (4.0, 0.0)], [block])
    assert path_avoids_blocks([(3.0, 0.0), (2.0, 0.0)], [block])
    assert path_avoids_blocks([(3.0, 0.0), (3.0, 1.0), (4.0, 1.0)], [block])
    assert path_avoids_blocks([(3.0, 0.0)], [block])


def test_blocked_approach_stays_on_path_and_handles_short_endpoint():
    """Follow corners and shrink near an endpoint instead of blocking escape."""
    block = blocked_approach([(0.0, 0.0), (0.2, 0.0), (0.2, 1.0)], (0.0, 0.0), 0.3)
    assert block[:2] == pytest.approx((0.2, 0.4))
    assert path_avoids_blocks([(0.0, 0.0)], [block])
    short = blocked_approach([(0.0, 0.0), (0.2, 0.0)], (0.0, 0.0), 0.3)
    assert short == pytest.approx((0.2, 0.0, 0.1))
    assert blocked_approach([(0.0, 0.0), (0.01, 0.0)], (0.0, 0.0), 0.3) is None


def _message():
    return SimpleNamespace(
        info=SimpleNamespace(
            width=3, height=2, resolution=0.5,
            origin=SimpleNamespace(
                position=SimpleNamespace(x=-2.0, y=3.0),
                orientation=SimpleNamespace(
                    x=0.0, y=0.0, z=math.sin(math.pi / 4),
                    w=math.cos(math.pi / 4)))),
        data=[-1, 0, 19, 20, 65, 100],
        header=SimpleNamespace(stamp=SimpleNamespace(sec=4, nanosec=5)),
    )


def test_message_snapshot_preserves_geometry_and_does_not_alias_input():
    """Use map origin rotation and immutable copied data for every candidate."""
    message = _message()
    grid = map_grid_from_message(message)
    assert grid.cells.shape == (2, 3)
    assert grid.world(0, 0) == pytest.approx((-2.25, 3.25))
    assert grid.origin_yaw == pytest.approx(math.pi / 2)
    assert grid.stamp_ns == 4_000_000_005
    message.data[0] = 100
    assert grid.cells[0, 0] == -1
    with pytest.raises(ValueError):
        grid.cells[0, 0] = 100


@pytest.mark.parametrize('resolution', [0.0, -0.1, math.nan, math.inf])
def test_message_rejects_invalid_resolution(resolution):
    """Invalid scale must not reach clearance or cell-index calculations."""
    message = _message()
    message.info.resolution = resolution
    with pytest.raises(ValueError, match='resolution'):
        map_grid_from_message(message)


@pytest.mark.parametrize('width,height', [(0, 2), (3, 0), (3, 3)])
def test_message_rejects_invalid_dimensions(width, height):
    """Dimensions must match the actual occupancy payload."""
    message = _message()
    message.info.width = width
    message.info.height = height
    with pytest.raises(ValueError, match='dimensions'):
        map_grid_from_message(message)


def test_map_statistics_keep_existing_occupancy_thresholds():
    """Unknown space is not counted as known or as observed free floor area."""
    assert map_statistics(map_grid_from_message(_message())) == {
        'known_area_m2': 1.25,
        'free_area_m2': 0.5,
        'occupied_area_m2': 0.5,
        'known_cells': 5,
    }


def test_frontiers_report_safe_unknown_boundaries():
    """Keep endpoint clearance and choose another approach outside the blacklist."""
    cells = np.full((40, 50), -1, dtype=np.int16)
    cells[10:30, 10:30] = 0
    cells[10:30, 10] = 100
    cells.setflags(write=False)
    grid = MapGrid(50, 40, 0.1, 0.0, 0.0, 0.0, cells)
    candidates = find_frontiers(grid, (1.5, 2.0), minimum_clearance_m=0.2)
    assert candidates
    candidate = candidates[0]
    column = int(candidate.x / grid.resolution)
    row = int(candidate.y / grid.resolution)
    assert grid.cells[row, column] == 0
    assert candidate.clearance_m >= 0.2
    assert candidate.distance_m >= 0.45
    alternatives = find_frontiers(
        grid, (1.5, 2.0), minimum_clearance_m=0.2,
        blacklisted=((candidate.x, candidate.y),),
    )
    assert alternatives
    assert all(math.hypot(item.x - candidate.x, item.y - candidate.y) >= 0.75
               for item in alternatives)


@pytest.mark.parametrize('occupancy', [-1, 0, 100])
def test_map_without_free_unknown_boundary_has_no_frontier(occupancy):
    """An invalid robot location is not mistaken for completed exploration."""
    cells = np.full((20, 20), occupancy, dtype=np.int16)
    grid = MapGrid(20, 20, 0.1, 0.0, 0.0, 0.0, cells)
    if occupancy == 0:
        assert find_frontiers(grid, (1.0, 1.0)) == []
    else:
        with pytest.raises(ValueError, match='outside known free map space'):
            find_frontiers(grid, (1.0, 1.0))


def test_frontier_clusters_keep_distance_weighted_ordering():
    """Rank reachable clusters using the established exploration utility."""
    cells = np.full((60, 100), 100, dtype=np.int16)
    cells[10:50, 10:90] = 0
    cells[:10, 15:35] = -1
    cells[:10, 60:85] = -1
    grid = MapGrid(100, 60, 0.1, 0.0, 0.0, 0.0, cells)
    candidates = find_frontiers(grid, (2.0, 3.0), minimum_clearance_m=0.2)
    assert len(candidates) == 2
    assert candidates[0].distance_m < candidates[1].distance_m
    utilities = [min(item.cell_count, FRONTIER_CELL_CAP)
                 - FRONTIER_DISTANCE_PENALTY_CELLS_PER_M * item.distance_m
                 for item in candidates]
    assert utilities == sorted(utilities, reverse=True)


def test_disconnected_free_space_is_not_a_frontier_destination():
    """Exclude a free island beyond unknown space even when its frontier is larger."""
    cells = np.full((50, 130), -1, dtype=np.int16)
    cells[10:35, 10:35] = 0
    cells[10:40, 90:120] = 0
    grid = MapGrid(130, 50, 0.1, 0.0, 0.0, 0.0, cells)
    candidates = find_frontiers(grid, (1.5, 2.0), minimum_clearance_m=0.2)
    assert len(candidates) == 1
    assert candidates[0].x < 3.5


def test_diagonal_contact_does_not_connect_free_regions():
    """Do not infer a route through the touching corners of obstacle cells."""
    cells = np.full((50, 50), -1, dtype=np.int16)
    cells[5:20, 5:20] = 0
    cells[20:40, 20:40] = 0
    grid = MapGrid(50, 50, 0.1, 0.0, 0.0, 0.0, cells)
    candidates = find_frontiers(grid, (1.0, 1.0), minimum_clearance_m=0.2)
    assert candidates
    assert all(item.x < 2.0 and item.y < 2.0 for item in candidates)


def test_frontier_approach_cannot_jump_across_a_thin_wall():
    """The two sides connect far away, but only the frontier side can observe it."""
    cells = np.full((60, 60), 100, dtype=np.int16)
    cells[10:50, 10:50] = 0
    cells[10:45, 30] = 100
    cells[10:20, 20:30] = -1
    grid = MapGrid(60, 60, 0.1, 0.0, 0.0, 0.0, cells)
    candidates = find_frontiers(grid, (4.0, 2.5), minimum_clearance_m=0.2)
    assert candidates
    assert all(item.x < 3.0 for item in candidates)


def test_reachable_region_respects_rotated_map_origin():
    """Convert world robot position to the correct cell before flood filling."""
    cells = np.full((50, 130), -1, dtype=np.int16)
    cells[10:35, 10:35] = 0
    cells[10:40, 90:120] = 0
    grid = MapGrid(130, 50, 0.1, -2.0, 3.0, math.pi / 2, cells)
    candidates = find_frontiers(grid, grid.world(20, 15), minimum_clearance_m=0.2)
    assert len(candidates) == 1
    assert candidates[0].y < 6.5


def test_robot_outside_map_is_not_completed_exploration():
    """An out-of-bounds pose must abort rather than produce a false empty frontier set."""
    cells = np.zeros((20, 20), dtype=np.int16)
    grid = MapGrid(20, 20, 0.1, 0.0, 0.0, 0.0, cells)
    with pytest.raises(ValueError, match='outside known free map space'):
        find_frontiers(grid, (-0.1, 1.0))
    assert find_frontiers(grid, None) == []


def test_search_keeps_frontiers_that_have_no_usable_approach():
    """Distinguish an empty map boundary from clearance or blacklist exclusion."""
    cells = np.full((14, 14), -1, dtype=np.int16)
    cells[3:11, 3:11] = 0
    grid = MapGrid(14, 14, 0.1, 0.0, 0.0, 0.0, cells)
    blocked = search_frontiers(grid, (0.7, 0.7), minimum_clearance_m=0.8)
    assert blocked.frontier_count == 1
    assert blocked.candidates == []
    excluded = search_frontiers(grid, (0.7, 0.7), blacklisted=((0.7, 0.7),))
    assert excluded.frontier_count == 1
    assert excluded.candidates == []
    tiny = search_frontiers(grid, (0.7, 0.7), minimum_cells=100)
    assert tiny.frontier_count == 0
    assert tiny.candidates == []


def test_nearby_frontier_is_available_when_no_far_approach_exists():
    """A small mapped room must not appear complete due to the minimum goal distance."""
    cells = np.full((14, 14), -1, dtype=np.int16)
    cells[3:11, 3:11] = 0
    grid = MapGrid(14, 14, 0.1, 0.0, 0.0, 0.0, cells)
    result = search_frontiers(grid, (0.7, 0.7))
    assert result.frontier_count == 1
    assert result.candidates
    assert result.candidates[0].distance_m < 0.45
    assert result.candidates[0].clearance_m >= 0.3


def test_long_frontier_uses_near_end_and_faces_local_unknown_space():
    """Do not navigate toward or face the average of an entire long boundary."""
    cells = np.full((40, 140), 100, dtype=np.int16)
    cells[10:30, 10:130] = 0
    cells[:10, 10:130] = -1
    grid = MapGrid(140, 40, 0.1, 0.0, 0.0, 0.0, cells)
    result = search_frontiers(grid, (1.5, 2.5))
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.x < 2.0
    assert candidate.y < 1.7
    assert candidate.yaw == pytest.approx(-math.pi / 2)


@pytest.mark.parametrize('obstacle', [-1, 20, 65, 100])
def test_path_checks_cells_between_sparse_waypoints(obstacle):
    """Free endpoints are insufficient when the segment crosses a blocked cell."""
    cells = np.zeros((10, 10), dtype=np.int16)
    cells[5, 5] = obstacle
    grid = MapGrid(10, 10, 0.1, 0.0, 0.0, 0.0, cells)
    assert not path_is_known_free(grid, [grid.world(5, 1), grid.world(5, 8)])
    assert path_is_known_free(grid, [grid.world(4, 1), grid.world(4, 8)])


def test_path_rejects_diagonal_corner_cutting():
    """A diagonal path cannot pass the shared corner of two occupied side cells."""
    cells = np.zeros((4, 4), dtype=np.int16)
    cells[1, 2] = cells[2, 1] = 100
    grid = MapGrid(4, 4, 0.1, 0.0, 0.0, 0.0, cells)
    assert not path_is_known_free(grid, [grid.world(1, 1), grid.world(2, 2)])


@pytest.mark.parametrize('points', [
    [], None, [(math.nan, 0.2)], [(math.inf, 0.2)], [(0.2,)],
    [(0.2, 0.2, 0.2)], [(-0.1, 0.2)], [(1.0, 0.2)],
])
def test_path_rejects_missing_invalid_or_out_of_bounds_points(points):
    """Unusable planner output must not become permission to move."""
    grid = MapGrid(10, 10, 0.1, 0.0, 0.0, 0.0, np.zeros((10, 10), dtype=np.int16))
    assert not path_is_known_free(grid, points)


def test_path_uses_rotated_map_coordinates_and_accepts_stationary_goal():
    """Check the full route in the saved grid frame, including one-point paths."""
    cells = np.zeros((10, 10), dtype=np.int16)
    grid = MapGrid(10, 10, 0.1, -2.0, 3.0, math.pi / 2, cells)
    points = [grid.world(2, 2), grid.world(2, 7), grid.world(7, 7)]
    assert path_is_known_free(grid, points)
    assert path_is_known_free(grid, points[:1])
    cells[2, 5] = -1
    assert not path_is_known_free(grid, points)
