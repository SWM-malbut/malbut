"""Unit tests for target-following geometry."""

import math

import pytest

from malbut_tracking.geometry import (
    Point2D,
    make_follow_goal,
    normalize_angle,
    quaternion_to_yaw,
    yaw_to_quaternion,
)


def test_follow_goal_stops_short_and_faces_target():
    """The candidate destination must preserve the desired standoff."""
    goal = make_follow_goal(Point2D(0.0, 0.0), Point2D(3.0, 0.0), 1.2)
    assert goal.position == Point2D(1.8, 0.0)
    assert goal.target_distance == pytest.approx(3.0)
    assert goal.yaw == pytest.approx(0.0)


def test_follow_goal_never_backs_away_from_close_target():
    """A close target produces a hold goal at the current robot position."""
    robot = Point2D(1.0, 2.0)
    goal = make_follow_goal(robot, Point2D(1.2, 2.0), 1.0)
    assert goal.position == robot


def test_follow_goal_limits_one_navigation_segment():
    """A distant camera target must produce a bounded movement segment."""
    goal = make_follow_goal(
        Point2D(0.0, 0.0),
        Point2D(5.0, 0.0),
        1.2,
        maximum_travel=0.8,
    )
    assert goal.position == Point2D(0.8, 0.0)
    assert goal.target_distance == pytest.approx(5.0)


def test_follow_goal_rejects_non_positive_segment_limit():
    """A configured segment limit must always allow forward progress."""
    with pytest.raises(ValueError, match='maximum_travel'):
        make_follow_goal(
            Point2D(0.0, 0.0),
            Point2D(5.0, 0.0),
            1.2,
            maximum_travel=0.0,
        )


@pytest.mark.parametrize('yaw', [-math.pi, -1.0, 0.0, 1.2, math.pi])
def test_planar_quaternion_round_trip(yaw):
    """Follow poses must preserve their target-facing heading."""
    assert quaternion_to_yaw(*yaw_to_quaternion(yaw)) == pytest.approx(yaw)


def test_normalize_angle_wraps_both_directions():
    """A planar bearing must remain within a single rotation."""
    assert normalize_angle(3.0 * math.pi) == pytest.approx(math.pi)
    assert normalize_angle(-3.0 * math.pi) == pytest.approx(-math.pi)
