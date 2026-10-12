"""Where the drive goes to see the whole body, and which view goals it takes."""

import math

import pytest

from malbut_fall_approach.fall_approach import Pose2D, backup_distance, goal_error, view_points

# The distance the Agent's fall rules ask for (VIEW_STANDOFF_M there).
VIEW_STANDOFF_M = 2.5


def test_view_points_go_straight_back_first_then_around_the_spot():
    points = view_points((3.0, 1.0), (2.0, 1.0), VIEW_STANDOFF_M)
    assert points[0] == pytest.approx((2.0 + VIEW_STANDOFF_M, 1.0))
    assert all(math.dist(p, (2.0, 1.0)) == pytest.approx(VIEW_STANDOFF_M) for p in points)
    assert points[3] == pytest.approx((2.0, 1.0 + VIEW_STANDOFF_M))
    assert len(points) == 5


def test_backs_up_only_when_facing_the_spot_and_short_of_the_distance():
    # 1 m in front of the spot after the approach, facing it: 1.5 m back.
    assert backup_distance(Pose2D(1.0, 0.0, math.pi), (0.0, 0.0), VIEW_STANDOFF_M) == \
        pytest.approx(1.5)
    # 20 degrees off still backs up; side-on or turned away does not.
    assert backup_distance(Pose2D(1.0, 0.0, math.pi + math.radians(20)), (0.0, 0.0),
                           VIEW_STANDOFF_M) == pytest.approx(1.5)
    assert backup_distance(Pose2D(1.0, 0.0, math.pi / 2), (0.0, 0.0), VIEW_STANDOFF_M) == 0.0
    assert backup_distance(Pose2D(1.0, 0.0, 0.0), (0.0, 0.0), VIEW_STANDOFF_M) == 0.0
    # Already as far as asked, or standing on the spot: nothing to back.
    assert backup_distance(Pose2D(2.45, 0.0, math.pi), (0.0, 0.0), VIEW_STANDOFF_M) == 0.0
    assert backup_distance(Pose2D(0.0, 0.0, 0.0), (0.0, 0.0), VIEW_STANDOFF_M) == 0.0


def test_view_and_return_goals_are_validated():
    assert goal_error('view', 1.0, 2.0, VIEW_STANDOFF_M) is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'off_floor') is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'not_a_person') is None
    assert goal_error('return', 0.0, 0.0, 1.0, 'revisit') is None  # Reported, not closed.
    assert goal_error('approach', 1.0, 2.0, 1.0, 'revisit') is not None
    assert goal_error('view', 1.0, 2.0, 3.5) is not None
    assert goal_error('return', 0.0, 0.0, 1.0, 'tired') is not None
    assert goal_error('approach', 1.0, 2.0, 1.0, 'off_floor') is not None
