"""Unit tests for target-following geometry."""

import math

import pytest

from malbut_tracking.geometry import (
    Point2D,
    directed_search_offsets,
    make_follow_goal,
    normalize_angle,
    predict_search_heading,
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
    """Search direction must always remain within a single rotation."""
    assert normalize_angle(3.0 * math.pi) == pytest.approx(math.pi)
    assert normalize_angle(-3.0 * math.pi) == pytest.approx(-math.pi)


@pytest.mark.parametrize(
    ('last_bearing', 'expected_sign'),
    [(-0.45, -1.0), (0.35, 1.0)],
)
def test_search_starts_where_target_left_camera(last_bearing, expected_sign):
    """Search first checks its center, then expands toward the exit side."""
    offsets = directed_search_offsets(last_bearing, 0.8, 1.57, 5)
    assert offsets[0] == 0.0
    assert math.copysign(1.0, offsets[1]) == expected_sign
    assert all(abs(offset) <= 1.57 for offset in offsets)


def test_search_can_expand_to_the_full_surroundings():
    """Long loss recovery must eventually inspect behind the robot."""
    offsets = directed_search_offsets(0.35, 0.8, math.pi, 9)
    assert offsets[0] == 0.0
    assert offsets[1] > 0.0
    assert any(abs(offset) > math.pi * 0.75 for offset in offsets)


def test_search_heading_uses_absolute_bearing_and_bounded_motion_trend():
    """Recovery must not reinterpret an old relative angle at a new yaw."""
    heading = predict_search_heading(
        last_target_yaw=1.0,
        target_yaw_rate_rps=0.4,
        observation_age_s=2.0,
        maximum_horizon_s=0.5,
    )
    assert heading == pytest.approx(1.2)
