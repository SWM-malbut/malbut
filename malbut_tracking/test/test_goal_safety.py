"""Tests for the live-costmap rays behind the camera-range target and fallback."""

import math

import pytest

from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.geometry import Point2D
from malbut_tracking.goal_safety import (
    find_reachable_approach_goal,
    first_admissible_point_on_ray,
)


def _grid(costs, width=30, height=30):
    return CostmapGrid(
        frame_id='map',
        stamp_seconds=1.0,
        resolution=0.1,
        width=width,
        height=height,
        origin=Point2D(0.0, 0.0),
        origin_yaw=0.0,
        costs=tuple(costs),
    )


def test_bearing_target_uses_first_free_point_beyond_depth_bound():
    """A distant RGB target stays on its ray and skips blocked cells."""
    width = 60
    height = 20
    costs = [0] * (width * height)
    for cell_x in range(30, 36):
        costs[10 * width + cell_x] = 254
    point = first_admissible_point_on_ray(
        _grid(costs, width, height),
        Point2D(0.5, 1.05),
        Point2D(3.05, 1.05),
        maximum_cost=80,
    )
    assert point is not None
    assert point.x >= 3.6
    assert point.y == 1.05


def test_live_ray_returns_exact_requested_standoff_when_clear():
    """The fallback aims at the requested standoff, not beyond it at the person."""
    grid = _grid([0] * 100, 10, 10)
    requested = Point2D(0.85, 0.75)
    assert find_reachable_approach_goal(grid, Point2D(0.15, 0.25), requested, 80) == requested


@pytest.mark.parametrize('blocked_cost', [81, 254, 255, -1])
def test_live_ray_stops_before_first_wall_or_unknown_even_with_free_cells_beyond(blocked_cost):
    """A safe endpoint behind an unsafe cell must never become the fallback."""
    costs = [0] * 100
    for row in range(10):
        costs[row * 10 + 5] = blocked_cost
    grid = _grid(costs, 10, 10)
    result = find_reachable_approach_goal(grid, Point2D(0.15, 0.55), Point2D(0.85, 0.55), 80)
    assert result is not None
    assert 0.15 < result.x < 0.5
    assert result.y == pytest.approx(0.55)
    assert grid.world_to_cell(result) == (4, 5)


@pytest.mark.parametrize('cost', [253, 254, 255, -1])
def test_live_ray_holds_when_robot_start_is_not_admissible(cost):
    """The fallback cannot bypass an unsafe start by jumping to another free cell."""
    assert find_reachable_approach_goal(
        _grid([cost, 0, 0], 3, 1), Point2D(0.05, 0.05), Point2D(0.25, 0.05), 80,
    ) is None


def test_live_ray_can_leave_soft_inflation_without_going_deeper():
    """The goal margin must not trap a collision-free robot already near a wall."""
    grid = _grid([220, 180, 90, 70, 0], 5, 1)
    endpoint = grid.cell_center(4, 0)
    assert find_reachable_approach_goal(
        grid, grid.cell_center(0, 0), endpoint, 80,
    ) == endpoint


@pytest.mark.parametrize('costs', [
    [120, 180, 0],  # Moving deeper into inflation is not an exit.
    [120, 100, 90],  # The whole short ray remains inside the goal margin.
    [220, 253, 0], [220, 254, 0], [220, 255, 0],
])
def test_live_ray_does_not_tunnel_or_stop_inside_soft_inflation(costs):
    """Leaving soft cost does not bypass collision, unknown or endpoint checks."""
    grid = _grid(costs, 3, 1)
    assert find_reachable_approach_goal(
        grid, grid.cell_center(0, 0), grid.cell_center(2, 0), 80,
    ) is None


@pytest.mark.parametrize('costs', [[0, 100, 100, 0], [0, 100, 0, 0]])
def test_live_ray_supercover_stops_at_blocked_diagonal_corners(costs):
    """Touching either blocked side cell forbids crossing the shared corner."""
    result = find_reachable_approach_goal(
        _grid(costs, 2, 2), Point2D(0.05, 0.05), Point2D(0.15, 0.15), 80,
    )
    assert result is not None
    assert 0.05 < result.x < 0.1
    assert 0.05 < result.y < 0.1


def test_live_ray_along_grid_edge_checks_both_touching_rows():
    """A line on a cell boundary cannot ignore the obstacle on its other side."""
    costs = [0] * 20
    costs[4] = 100
    result = find_reachable_approach_goal(
        _grid(costs, 10, 2), Point2D(0.05, 0.1), Point2D(0.95, 0.1), 80,
    )
    assert result is not None
    assert 0.05 < result.x < 0.4


@pytest.mark.parametrize('target', [Point2D(10.0, 0.15), Point2D(-10.0, 0.15)])
def test_live_ray_cannot_leave_the_costmap(target):
    """The last candidate remains inside the verified live costmap extent."""
    grid = _grid([0] * 9, 3, 3)
    result = find_reachable_approach_goal(grid, Point2D(0.15, 0.15), target, 80)
    assert result is not None
    assert grid.world_to_cell(result) is not None
    assert 0.0 < result.x < 0.3


def test_live_ray_respects_rotated_map_origin():
    """The same blocking cell is found when the costmap is rotated in world."""
    grid = CostmapGrid(
        'map', 1.0, 0.1, 4, 2, Point2D(-2.0, 3.0), math.pi / 3,
        (0, 0, 100, 0, 0, 0, 100, 0),
    )
    result = find_reachable_approach_goal(
        grid, grid.cell_center(0, 0), grid.cell_center(3, 0), 80,
    )
    assert result is not None
    assert grid.world_to_cell(result) == (1, 0)


def test_live_ray_rejects_motion_across_immediately_blocked_boundary():
    """A robot exactly at a blocked crossing cannot advance by a zero-length hop."""
    assert find_reachable_approach_goal(
        _grid([0, 100, 0], 3, 1), Point2D(0.2, 0.05), Point2D(0.05, 0.05), 80,
    ) is None
