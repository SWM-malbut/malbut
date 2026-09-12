"""Exercise the shared frontier algorithm using only synthetic occupancy grids."""

import math
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_autoslam.frontier import (
    FRONTIER_CELL_CAP,
    FRONTIER_DISTANCE_PENALTY_CELLS_PER_M,
    MapGrid,
    find_frontiers,
    map_grid_from_message,
    map_statistics,
)


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
    """Preserve the existing simulation's safe-boundary and blacklist behavior."""
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
    assert find_frontiers(
        grid, (1.5, 2.0), minimum_clearance_m=0.2,
        blacklisted=((candidate.x, candidate.y),),
    ) == []


@pytest.mark.parametrize('occupancy', [-1, 0, 100])
def test_map_without_free_unknown_boundary_has_no_frontier(occupancy):
    """A uniform grid does not fabricate a new exploration destination."""
    cells = np.full((20, 20), occupancy, dtype=np.int16)
    grid = MapGrid(20, 20, 0.1, 0.0, 0.0, 0.0, cells)
    assert find_frontiers(grid, (1.0, 1.0)) == []


def test_frontier_clusters_keep_distance_weighted_ordering():
    """Rank real extracted clusters using the established exploration utility."""
    cells = np.full((50, 130), -1, dtype=np.int16)
    cells[10:35, 10:35] = 0
    cells[10:40, 90:120] = 0
    grid = MapGrid(130, 50, 0.1, 0.0, 0.0, 0.0, cells)
    candidates = find_frontiers(grid, (1.5, 2.0), minimum_clearance_m=0.2)
    assert len(candidates) == 2
    assert candidates[0].distance_m < candidates[1].distance_m
    utilities = [min(item.cell_count, FRONTIER_CELL_CAP)
                 - FRONTIER_DISTANCE_PENALTY_CELLS_PER_M * item.distance_m
                 for item in candidates]
    assert utilities == sorted(utilities, reverse=True)
