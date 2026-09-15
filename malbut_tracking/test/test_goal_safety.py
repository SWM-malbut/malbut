"""Tests for global-costmap goal margin and open-space orientation."""

import math
from types import SimpleNamespace

import pytest

from malbut_tracking import goal_safety
from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.geometry import Point2D
from malbut_tracking.goal_safety import (
    find_reachable_approach_goal,
    first_admissible_point_on_ray,
    pad_static_map,
    plan_static_path,
    project_navigation_goal,
    StaticPlanningTimeout,
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


def test_inflated_goal_moves_to_the_open_room_side():
    """A requested point in wall inflation must become a low-cost goal."""
    width = height = 30
    costs = [0] * (width * height)
    for cell_y in range(height):
        for cell_x in range(13, 17):
            costs[cell_y * width + cell_x] = 120
    goal = project_navigation_goal(
        _grid(costs),
        Point2D(1.45, 1.5),
        0.0,
        maximum_cost=80,
        search_radius_m=1.0,
        openness_radius_m=0.6,
        openness_preference_m=0.3,
        heading_probe_distance_m=0.9,
        minimum_heading_clearance_m=0.45,
    )
    assert goal is not None
    assert goal.position_adjusted
    cell = _grid(costs).world_to_cell(goal.position)
    assert cell is not None
    assert _grid(costs).cost(*cell) <= 80


def test_wall_facing_planning_heading_turns_toward_open_half_plane():
    """Planning retains the existing open-heading safety adjustment."""
    width = height = 30
    costs = [0] * (width * height)
    for cell_y in range(height):
        for cell_x in range(17, 20):
            costs[cell_y * width + cell_x] = 120
    goal = project_navigation_goal(
        _grid(costs),
        Point2D(1.45, 1.5),
        0.0,
        maximum_cost=80,
        search_radius_m=0.3,
        openness_radius_m=0.4,
        openness_preference_m=0.0,
        heading_probe_distance_m=0.9,
        minimum_heading_clearance_m=0.45,
    )
    assert goal is not None
    assert goal.heading_adjusted
    assert abs(goal.yaw) <= math.pi / 2 + 1e-9


def test_tracking_goal_is_projected_between_robot_and_person():
    """An occupied person point must move toward the observing robot."""
    width = height = 30
    costs = [0] * (width * height)
    for cell_y in range(height):
        for cell_x in range(13, 17):
            costs[cell_y * width + cell_x] = 120
    robot = Point2D(0.5, 1.55)
    person = Point2D(1.45, 1.55)
    goal = project_navigation_goal(
        _grid(costs),
        person,
        0.0,
        maximum_cost=80,
        search_radius_m=1.0,
        openness_radius_m=0.6,
        openness_preference_m=0.3,
        heading_probe_distance_m=0.9,
        minimum_heading_clearance_m=0.45,
        approach_origin=robot,
    )
    assert goal is not None
    assert robot.x <= goal.position.x < person.x
    assert abs(goal.position.y - person.y) <= 0.1


def test_tracking_goal_falls_back_to_safe_space_around_person():
    """A blocked approach line must retain the surrounding-space fallback."""
    width = height = 30
    costs = [0] * (width * height)
    for cell_x in range(5, 16):
        costs[15 * width + cell_x] = 120
    robot = Point2D(0.55, 1.55)
    person = Point2D(1.45, 1.55)
    goal = project_navigation_goal(
        _grid(costs),
        person,
        0.0,
        maximum_cost=80,
        search_radius_m=1.0,
        openness_radius_m=0.6,
        openness_preference_m=0.3,
        heading_probe_distance_m=0.9,
        minimum_heading_clearance_m=0.45,
        approach_origin=robot,
    )
    assert goal is not None
    assert abs(goal.position.y - person.y) > 0.05


def test_static_slam_path_avoids_fixed_geometry():
    """The cached-map route goes through a real opening, not through a wall."""
    width = height = 10
    costs = [0] * (width * height)
    for cell_y in range(7):
        costs[cell_y * width + 4] = 100
    grid = _grid(costs, width, height)
    path = plan_static_path(
        grid,
        Point2D(0.15, 0.15),
        Point2D(0.85, 0.15),
    )
    assert path is not None
    assert any(point.y >= 0.75 for point in path)
    assert all(
        grid.cost(*grid.world_to_cell(point)) < 65
        for point in path[1:-1]
    )


def test_static_map_padding_is_computed_once_as_navigation_geometry():
    """Static obstacles expand by the configured Nav2 inflation radius."""
    width = height = 15
    costs = [0] * (width * height)
    costs[7 * width + 7] = 100
    padded = pad_static_map(
        _grid(costs, width, height),
        occupied_threshold=65,
        padding_radius_m=0.35,
    )

    assert padded.cost(7, 7) >= 65
    assert padded.cost(10, 7) >= 65
    assert padded.cost(11, 7) < 65
    assert padded.cost(10, 10) < 65


def test_static_path_selects_first_live_safe_cell_from_target():
    """Walk backward from the green point and stop at the first safe cell."""
    width = height = 30
    costs = [0] * (width * height)
    for cell_x in (13, 14, 15):
        costs[15 * width + cell_x] = 120
    requested = Point2D(1.55, 1.55)
    goal = project_navigation_goal(
        _grid(costs),
        requested,
        0.0,
        maximum_cost=80,
        search_radius_m=1.0,
        openness_radius_m=0.6,
        openness_preference_m=0.3,
        heading_probe_distance_m=0.9,
        minimum_heading_clearance_m=0.45,
        static_path=(
            Point2D(0.55, 1.55),
            Point2D(1.05, 1.55),
            Point2D(1.25, 1.55),
            Point2D(1.35, 1.55),
            Point2D(1.45, 1.55),
            Point2D(1.55, 1.55),
        ),
    )
    assert goal is not None
    assert goal.position.x == pytest.approx(1.25)
    assert goal.position.y == pytest.approx(requested.y)


def test_missing_static_path_keeps_original_target_goal_selection():
    """Non-tracking callers retain the existing local safety projection."""
    requested = Point2D(1.45, 1.55)
    goal = project_navigation_goal(
        _grid([0] * (30 * 30)),
        requested,
        0.0,
        maximum_cost=80,
        search_radius_m=1.0,
        openness_radius_m=0.6,
        openness_preference_m=0.0,
        heading_probe_distance_m=0.9,
        minimum_heading_clearance_m=0.45,
        approach_origin=Point2D(0.45, 1.55),
    )
    assert goal is not None
    assert goal.position.x == pytest.approx(requested.x)
    assert goal.position.y == pytest.approx(requested.y)


def test_static_path_budget_expiry_is_not_reported_as_unreachable(monkeypatch):
    """Exhausting the 20 ms budget is distinct from exhausting the search."""
    width = height = 40
    costs = [0] * (width * height)
    for row in range(height):
        costs[row * width + 20] = 100
    ticks = iter([0.0, 0.001, 0.002, 0.010, 0.021])
    monkeypatch.setattr(
        goal_safety, 'time', SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    with pytest.raises(StaticPlanningTimeout):
        plan_static_path(
            _grid(costs, width, height), Point2D(0.55, 0.55), Point2D(3.55, 0.55),
        )


def test_static_path_does_not_join_diagonal_corner_contacts():
    """Diagonal touching free cells remain disconnected behind solid corners."""
    assert plan_static_path(
        _grid([0, 100, 100, 0], 2, 2), Point2D(0.05, 0.05), Point2D(0.15, 0.15),
    ) is None


def test_path_cannot_cut_one_blocked_diagonal_side():
    """Connected endpoints still take the cardinal detour around a corner."""
    grid = _grid([0, 100, 0, 0], 2, 2)
    assert plan_static_path(grid, grid.cell_center(0, 0), grid.cell_center(1, 1)) == (
        grid.cell_center(0, 0), grid.cell_center(0, 1), grid.cell_center(1, 1),
    )


def test_static_path_retains_rotated_grid_and_same_cell_behavior():
    """The time budget changes neither world conversion nor the same-cell case."""
    grid = CostmapGrid(
        frame_id='map', stamp_seconds=2.0, resolution=0.1,
        width=3, height=3, origin=Point2D(-2.0, 5.0),
        origin_yaw=math.pi / 3, costs=(0, 0, 0, 0, -1, 0, 0, 0, 100),
    )
    assert plan_static_path(grid, grid.cell_center(0, 0), grid.cell_center(2, 0)) == (
        grid.cell_center(0, 0), grid.cell_center(1, 0), grid.cell_center(2, 0),
    )
    unknown = grid.cell_center(1, 1)
    assert plan_static_path(grid, unknown, unknown) == (unknown,)
    assert plan_static_path(grid, grid.cell_center(0, 0), unknown) is None
    assert plan_static_path(grid, Point2D(500, 500), unknown) is None


def test_static_path_budget_includes_result_point_conversion(monkeypatch):
    """A completed search cannot exceed its deadline during result construction."""
    clock = [0.0]
    monkeypatch.setattr(goal_safety, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    original_center = CostmapGrid.cell_center

    def slow_center(grid, x, y):
        clock[0] += 0.01
        return original_center(grid, x, y)

    monkeypatch.setattr(CostmapGrid, 'cell_center', slow_center)
    with pytest.raises(StaticPlanningTimeout):
        plan_static_path(_grid([0] * 9, 3, 3), Point2D(0.05, 0.05), Point2D(0.25, 0.05))


@pytest.mark.parametrize('budget', [-1.0, math.inf, math.nan])
def test_static_path_rejects_invalid_time_budget(budget):
    """Malformed budgets never silently disable the planning deadline."""
    with pytest.raises(ValueError):
        plan_static_path(
            _grid([0] * 9, 3, 3), Point2D(0.05, 0.05), Point2D(0.25, 0.05),
            time_budget_s=budget,
        )


def test_static_path_does_not_scan_or_preprocess_the_whole_map():
    """A short query reads only visited cells, even on a very large grid."""
    class SparseFreeCosts:
        """Count reads without allocating a million-cell map."""

        def __init__(self):
            """Start the search-cell access counter."""
            self.reads = 0

        def __len__(self):
            """Report the complete logical grid size."""
            return 1_000_000

        def __getitem__(self, index):
            """Reject any full-grid traversal during a two-cell path request."""
            self.reads += 1
            assert self.reads < 100
            return 0

    costs = SparseFreeCosts()
    grid = CostmapGrid(
        'map', 1.0, 0.1, 1000, 1000, Point2D(0.0, 0.0), 0.0, costs,
    )
    assert plan_static_path(grid, Point2D(0.05, 0.05), Point2D(0.15, 0.05))
    assert 0 < costs.reads < 100


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


@pytest.mark.parametrize('cost', [81, 255, -1])
def test_live_ray_holds_when_robot_start_is_not_admissible(cost):
    """The fallback cannot bypass an unsafe start by jumping to another free cell."""
    assert find_reachable_approach_goal(
        _grid([cost, 0, 0], 3, 1), Point2D(0.05, 0.05), Point2D(0.25, 0.05), 80,
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
