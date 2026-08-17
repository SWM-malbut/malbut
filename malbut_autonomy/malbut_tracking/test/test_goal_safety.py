"""Tests for global-costmap goal margin and open-space orientation."""

import math

from malbut_tracking.costmap_tracking import CostmapGrid
from malbut_tracking.geometry import Point2D
from malbut_tracking.goal_safety import (
    first_admissible_point_on_ray,
    project_navigation_goal,
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
