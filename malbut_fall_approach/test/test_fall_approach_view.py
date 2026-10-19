"""Backing away to see the whole body, the places an approach may stop, and the goals."""

import math

import pytest

from malbut_fall_approach.fall_approach import (
    INSCRIBED_COST, LETHAL_COST, CostGrid, Pose2D, backup_distance, can_stand, goal_error,
    sight_clear,
)

# The farthest the Agent's fall rules let the view go (VIEW_MAX_M there).
VIEW_MAX_M = 3.0


UNKNOWN_COST = 255


def grid(cells, *, size=100, resolution=0.1, origin=(-5.0, -5.0), fill=0):
    """A 10 m x 10 m costmap around the origin; ``cells`` maps (x, y) in m to a cost."""
    data = bytearray([fill]) * (size * size)
    for (x, y), cost in cells.items():
        col = int((x - origin[0]) / resolution)
        row = int((y - origin[1]) / resolution)
        data[row * size + col] = cost
    return CostGrid(resolution, origin[0], origin[1], 0.0, size, size, bytes(data))


def wall(x, y_from, y_to, cost=LETHAL_COST):
    return {(x, y_from + i * 0.05): cost for i in range(int((y_to - y_from) / 0.05) + 1)}


def test_a_wall_between_blocks_sight_but_unknown_and_inflation_do_not():
    spot = (0.0, 0.0)
    assert sight_clear(grid({}), (2.5, 0.0), spot)
    assert not sight_clear(grid(wall(1.5, -0.5, 0.5)), (2.5, 0.0), spot)
    assert sight_clear(grid(wall(1.5, -0.5, 0.5, cost=UNKNOWN_COST)), (2.5, 0.0), spot)
    assert sight_clear(grid(wall(1.5, -0.5, 0.5, cost=INSCRIBED_COST)), (2.5, 0.0), spot)
    # The person lying there is an obstacle too: the last metre is not checked.
    assert sight_clear(grid(wall(0.6, -0.5, 0.5)), (2.5, 0.0), spot)
    # A spot beyond the costmap can still be seen from inside it.
    assert sight_clear(grid({}), (4.0, 0.0), (7.0, 0.0))


def test_the_robot_must_stand_on_known_floor_clear_of_obstacles():
    floor = grid({(1.0, 1.0): LETHAL_COST, (2.0, 2.0): INSCRIBED_COST,
                  (3.0, 3.0): UNKNOWN_COST, (4.0, 4.0): 120})
    assert not can_stand(floor, (1.0, 1.0))
    assert not can_stand(floor, (2.0, 2.0))
    assert not can_stand(floor, (3.0, 3.0))
    assert can_stand(floor, (4.0, 4.0))  # Near a wall but outside the footprint.
    assert not can_stand(floor, (6.0, 0.0))  # Off the costmap.


def test_backs_up_only_when_facing_the_spot_and_short_of_the_distance():
    # 1 m in front of the spot after the approach, facing it: up to 2 m back.
    assert backup_distance(Pose2D(1.0, 0.0, math.pi), (0.0, 0.0), VIEW_MAX_M) == \
        pytest.approx(2.0)
    # 20 degrees off still backs up; side-on or turned away does not.
    assert backup_distance(Pose2D(1.0, 0.0, math.pi + math.radians(20)), (0.0, 0.0),
                           VIEW_MAX_M) == pytest.approx(2.0)
    assert backup_distance(Pose2D(1.0, 0.0, math.pi / 2), (0.0, 0.0), VIEW_MAX_M) == 0.0
    assert backup_distance(Pose2D(1.0, 0.0, 0.0), (0.0, 0.0), VIEW_MAX_M) == 0.0
    # Already as far as asked, or standing on the spot: nothing to back.
    assert backup_distance(Pose2D(2.95, 0.0, math.pi), (0.0, 0.0), VIEW_MAX_M) == 0.0
    assert backup_distance(Pose2D(0.0, 0.0, 0.0), (0.0, 0.0), VIEW_MAX_M) == 0.0


def test_view_and_return_goals_are_validated():
    assert goal_error('view', 1.0, 2.0, VIEW_MAX_M) is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'off_floor') is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'not_a_person') is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'revisit') is None  # Reported, not closed.
    assert goal_error('approach', 1.0, 2.0, 1.0, 'revisit') is not None
    assert goal_error('view', 1.0, 2.0, 3.5) is not None
    assert goal_error('return', 0.0, 0.0, 1.0, 'tired') is not None
    assert goal_error('approach', 1.0, 2.0, 1.0, 'off_floor') is not None
