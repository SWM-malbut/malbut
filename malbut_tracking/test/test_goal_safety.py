"""Tests for global-costmap goal margin and open-space orientation."""

import math

from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.geometry import Point2D
from malbut_tracking.goal_safety import project_navigation_goal


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
