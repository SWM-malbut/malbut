"""The drive geometry for looking at an uncertain suspicion from 1 m."""

import math

import pytest

from malbut_fall_approach.fall_approach import (
    Pose2D, StartPoses, approach_places, facing_turn, first_seen, goal_error, returning_turn,
    ring_places,
)


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


def test_the_first_seen_point_is_optional_but_never_infinite():
    assert goal_error('approach', 1.0, 2.0, 1.0, '', 4.0, 5.0) is None
    assert goal_error('approach', 1.0, 2.0, 1.0, '', math.nan, math.nan) is None
    assert goal_error('approach', 1.0, 2.0, 1.0, '', math.inf, 0.0)
    assert goal_error('approach', 1.0, 2.0, 1.0, '', 0.0, True)
    robot = Pose2D(7.0, 8.0, 0.0)
    assert first_seen(4.0, 5.0, robot) == (4.0, 5.0)
    assert first_seen(math.nan, math.nan, robot) == (7.0, 8.0)
    assert first_seen(4.0, math.nan, robot) == (7.0, 8.0)


def test_looking_again_from_where_it_was_first_seen_may_ask_for_the_whole_distance():
    assert goal_error('approach', 1.0, 2.0, 6.5) is None
    assert goal_error('approach', 1.0, 2.0, 10.5)
    assert goal_error('view', 1.0, 2.0, 3.5)  # The whole-body view stays within 3 m.


def test_the_stop_goes_round_the_circle_from_the_first_seen_side():
    # Seen from straight above the spot (4, 0): the first place is 1 m above it.
    places = ring_places((4.0, 6.0), (4.0, 0.0), 1.0)
    assert len(places) == 12
    assert places[0] == pytest.approx((4.0, 1.0))
    assert places[1] == pytest.approx((4.0 + math.cos(math.radians(120)),
                                       math.sin(math.radians(120))))
    assert places[2] == pytest.approx((4.0 + math.cos(math.radians(60)),
                                       math.sin(math.radians(60))))
    assert places[-1] == pytest.approx((4.0, -1.0))  # The far side last.


def test_wider_circles_follow_the_standoff_circle():
    places = approach_places((0.0, 0.0), (3.0, 0.0), 1.0)
    radii = [round(math.dist(p, (3.0, 0.0)), 6) for p in places]
    assert radii == [1.0] * 12 + [1.5] * 12 + [2.0] * 12 + [2.5] * 12 + [3.0] * 12
    # Looking again from where it was first seen: that point itself comes first.
    again = approach_places((0.0, 4.0), (3.0, 0.0), 5.0)
    assert len(again) == 12 and again[0] == pytest.approx((0.0, 4.0))
