"""Where the drive goes to see the whole body, and which view goals it takes."""

import math

import pytest

from malbut_fall_approach.fall_approach import goal_error, view_points

# The distance the Agent's fall rules ask for (VIEW_STANDOFF_M there).
VIEW_STANDOFF_M = 2.5


def test_view_points_go_straight_back_first_then_around_the_spot():
    points = view_points((3.0, 1.0), (2.0, 1.0), VIEW_STANDOFF_M)
    assert points[0] == pytest.approx((2.0 + VIEW_STANDOFF_M, 1.0))
    assert all(math.dist(p, (2.0, 1.0)) == pytest.approx(VIEW_STANDOFF_M) for p in points)
    assert points[3] == pytest.approx((2.0, 1.0 + VIEW_STANDOFF_M))
    assert len(points) == 5


def test_view_and_return_goals_are_validated():
    assert goal_error('view', 1.0, 2.0, VIEW_STANDOFF_M) is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'off_floor') is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'not_a_person') is None
    assert goal_error('view', 1.0, 2.0, 3.5) is not None
    assert goal_error('return', 0.0, 0.0, 1.0, 'tired') is not None
    assert goal_error('approach', 1.0, 2.0, 1.0, 'off_floor') is not None
