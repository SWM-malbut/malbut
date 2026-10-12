"""The drive geometry for looking at an uncertain suspicion from 1 m."""

import math

import pytest

from malbut_fall_approach.fall_approach import (
    PULLBACK_STEP_M, Pose2D, StartPoses, facing_turn, goal_error, plan_targets, returning_turn,
    route_length, standoff_route,
)


def test_pullback_targets_never_pass_the_robot():
    assert plan_targets((5.0, 0.0), (0.0, 0.0)) == [
        (0.0, 0.0), (PULLBACK_STEP_M, 0.0), (2 * PULLBACK_STEP_M, 0.0)]
    assert plan_targets((1.4, 0.0), (0.0, 0.0)) == [(0.0, 0.0)]


def test_turns_face_the_spot_and_restore_the_start_heading():
    robot = Pose2D(1.0, 1.0, 0.0)
    assert facing_turn(robot, (1.0, 3.0)) == pytest.approx(math.pi / 2)
    assert facing_turn(robot, (3.0, 1.05)) == 0.0  # Within tolerance.
    assert returning_turn(robot, Pose2D(0.0, 0.0, -math.pi / 2)) == pytest.approx(-math.pi / 2)


def test_goal_validation_and_start_poses_are_bounded():
    assert goal_error('approach', 1.0, 2.0, 1.0) is None
    assert goal_error('return', 0.0, 0.0, 1.0) is None
    for bad in (('drive', 1.0, 2.0, 1.0), ('approach', math.nan, 2.0, 1.0),
                ('approach', 1.0, 2.0, 0.1), ('approach', True, 2.0, 1.0)):
        assert goal_error(*bad)
    starts = StartPoses(limit=2)
    starts.remember('a', Pose2D(0, 0, 0))
    starts.remember('a', Pose2D(9, 9, 0))
    starts.remember('b', Pose2D(1, 1, 0))
    starts.remember('c', Pose2D(2, 2, 0))
    assert starts.get('a') is None and starts.get('b') == Pose2D(1, 1, 0)
    starts.forget('b')
    assert starts.get('b') is None


def test_the_route_stops_where_it_first_enters_the_standoff_circle():
    route = standoff_route([(0.0, 0.0), (2.0, 0.0), (4.0, 0.0)], (4.0, 0.0), 1.0)
    assert route == [(0.0, 0.0), (2.0, 0.0), pytest.approx((3.0, 0.0))]
    assert route_length(route) == pytest.approx(3.0)


def test_a_route_already_inside_or_never_inside_is_kept_as_is():
    assert standoff_route([(3.5, 0.0), (5.0, 0.0)], (4.0, 0.0), 1.0) == [(3.5, 0.0)]
    far = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    assert standoff_route(far, (2.0, 5.0), 1.0) == far
    assert standoff_route([], (0.0, 0.0), 1.0) == []
