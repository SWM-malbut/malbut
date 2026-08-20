"""Tests for conservative occupancy-grid candidate generation."""

import math
from types import SimpleNamespace

import pytest

from malbut_roaming.geometry import Point2D, yaw_to_quaternion
from malbut_roaming.grid_map import GridMap


def _room_grid():
    values = []
    for y in range(7):
        for x in range(7):
            boundary = x in {0, 6} or y in {0, 6}
            values.append(100 if boundary or (x, y) == (3, 3) else 0)
    return GridMap(
        width=7,
        height=7,
        resolution=1.0,
        origin_x=-1.0,
        origin_y=-2.0,
        origin_yaw=0.0,
        occupancy=values,
    )


def test_candidates_exclude_occupied_unknown_and_clearance_margin():
    """Unsafe cells and their configured safety margin may never be goals."""
    values = list(_room_grid().occupancy)
    values[2 * 7 + 2] = -1
    grid = GridMap(7, 7, 1.0, -1.0, -2.0, 0.0, values)
    candidates = grid.candidates(spacing_m=1.0, minimum_clearance_m=1.0)
    cells = {candidate.key for candidate in candidates}
    assert (3, 3) not in cells
    assert (2, 2) not in cells
    assert not any(
        cell_x in {0, 6} or cell_y in {0, 6}
        for cell_x, cell_y in cells
    )
    assert all(candidate.clearance >= 1.0 for candidate in candidates)


def test_map_boundary_is_treated_as_unknown_space_outside_the_map():
    """A free-valued edge cell is unsafe because the robot can leave the map."""
    grid = GridMap(5, 5, 1.0, 0.0, 0.0, 0.0, [0] * 25)
    cells = {
        candidate.key
        for candidate in grid.candidates(1.0, minimum_clearance_m=1.0)
    }
    assert cells == {
        (1, 1), (2, 1), (3, 1),
        (1, 2), (2, 2), (3, 2),
        (1, 3), (2, 3), (3, 3),
    }


def test_requested_spacing_is_never_rounded_down():
    """Metric candidate spacing must be a lower bound, not a rough hint."""
    grid = GridMap(9, 3, 1.0, 0.0, 0.0, 0.0, [0] * 27)
    candidates = grid.candidates(1.1, minimum_clearance_m=0.0)
    x_values = sorted(candidate.x for candidate in candidates)
    assert x_values
    assert all(
        second - first >= 1.1
        for first, second in zip(x_values, x_values[1:])
    )


def test_rotated_map_origin_is_applied_to_coordinates_and_messages():
    """Map origin translation and orientation both affect Nav2 goal positions."""
    grid = GridMap(
        width=1,
        height=1,
        resolution=2.0,
        origin_x=10.0,
        origin_y=20.0,
        origin_yaw=math.pi / 2.0,
        occupancy=[0],
    )
    assert grid.cell_to_world(0, 0) == pytest.approx(Point2D(9.0, 21.0))

    quaternion = yaw_to_quaternion(math.pi / 2.0)
    message = SimpleNamespace(
        info=SimpleNamespace(
            width=1,
            height=1,
            resolution=2.0,
            origin=SimpleNamespace(
                position=SimpleNamespace(x=10.0, y=20.0),
                orientation=SimpleNamespace(
                    x=quaternion[0],
                    y=quaternion[1],
                    z=quaternion[2],
                    w=quaternion[3],
                ),
            ),
        ),
        data=[0],
    )
    from_message = GridMap.from_message(message)
    assert from_message.cell_to_world(0, 0) == pytest.approx(
        Point2D(9.0, 21.0)
    )


def test_occupancy_at_resolves_world_points_and_rejects_outside_points():
    grid = GridMap(
        width=2,
        height=2,
        resolution=0.5,
        origin_x=-1.0,
        origin_y=2.0,
        origin_yaw=0.0,
        occupancy=(0, 50, 75, 100),
    )

    assert grid.occupancy_at(Point2D(-0.75, 2.25)) == 0
    assert grid.occupancy_at(Point2D(-0.25, 2.75)) == 100
    assert grid.occupancy_at(Point2D(-1.01, 2.25)) is None


def test_nearest_candidate_honors_the_snap_distance():
    """A target goal may not teleport across an occupied room."""
    grid = _room_grid()
    candidates = grid.candidates(1.0, 1.0)
    candidate = grid.nearest_candidate(
        candidates,
        Point2D(1.6, 0.6),
        maximum_distance_m=0.2,
    )
    assert candidate is not None
    assert candidate.key == (2, 2)
    assert grid.nearest_candidate(
        candidates,
        Point2D(100.0, 100.0),
        maximum_distance_m=1.0,
    ) is None


@pytest.mark.parametrize(
    ('factory', 'message'),
    [
        (
            lambda: GridMap(0, 1, 1.0, 0.0, 0.0, 0.0, []),
            'dimensions',
        ),
        (
            lambda: GridMap(1, 1, 0.0, 0.0, 0.0, 0.0, [0]),
            'resolution',
        ),
        (
            lambda: GridMap(2, 2, 1.0, 0.0, 0.0, 0.0, [0]),
            'occupancy data',
        ),
    ],
)
def test_invalid_grid_construction_is_rejected(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def test_invalid_candidate_and_coordinate_requests_are_rejected():
    grid = _room_grid()
    with pytest.raises(ValueError, match='maximum_free_occupancy'):
        grid.candidates(1.0, 0.0, maximum_free_occupancy=101)
    with pytest.raises(ValueError, match='maximum_distance_m'):
        grid.nearest_candidate((), Point2D(0.0, 0.0), -1.0)
    with pytest.raises(ValueError, match='outside'):
        grid.cell_to_world(7, 0)
