"""Tests for leaving a costmap cell no plan can start from."""

import math

from malbut_gazebo.inscribed_escape import (
    INSCRIBED_COST,
    clearance_towards,
    nearest_obstacle_angle,
    plan_escape,
)


ANGLE_MIN = -math.pi
INCREMENT = math.pi / 180.0


def _ranges(**sectors) -> list:
    """Build a 360-sample scan with the named bearings set closer."""
    values = [8.0] * 360
    for name, (degrees, distance) in sectors.items():
        index = int((math.radians(degrees) - ANGLE_MIN) / INCREMENT)
        values[index % 360] = distance
    return values


def test_nearest_obstacle_ignores_invalid_returns():
    values = [math.inf, float("nan"), 0.0, 2.0, 0.4]
    angle, distance = nearest_obstacle_angle(values, ANGLE_MIN, INCREMENT)

    assert distance == 0.4
    assert angle == ANGLE_MIN + 4 * INCREMENT


def test_nearest_obstacle_is_none_without_any_valid_return():
    assert nearest_obstacle_angle(
        [math.inf, float("nan")], ANGLE_MIN, INCREMENT
    ) is None


def test_clearance_only_considers_its_own_sector():
    values = _ranges(front=(0.0, 0.3), rear=(180.0, 2.5))

    forward = clearance_towards(values, ANGLE_MIN, INCREMENT, 0.0, 0.52)
    backward = clearance_towards(values, ANGLE_MIN, INCREMENT, math.pi, 0.52)

    assert forward == 0.3
    assert backward == 2.5


def test_escape_reverses_away_from_a_wall_in_front():
    """The only direction that frees the robot must be the one commanded."""
    velocity = plan_escape(
        INSCRIBED_COST, 0.0, 0.2, 2.5,
        speed=0.10, required_clearance=0.35,
    )

    assert velocity == -0.10


def test_escape_drives_forward_when_the_wall_is_behind():
    velocity = plan_escape(
        INSCRIBED_COST, math.pi, 2.5, 0.2,
        speed=0.10, required_clearance=0.35,
    )

    assert velocity == 0.10


def test_escape_refuses_when_the_open_side_is_not_open_enough():
    """Never trade one blocked side for another."""
    velocity = plan_escape(
        INSCRIBED_COST, 0.0, 0.2, 0.2,
        speed=0.10, required_clearance=0.35,
    )

    assert velocity == 0.0


def test_escape_stays_idle_outside_an_inscribed_cell():
    assert plan_escape(
        INSCRIBED_COST - 1, 0.0, 2.5, 2.5,
        speed=0.10, required_clearance=0.35,
    ) == 0.0
    assert plan_escape(
        None, 0.0, 2.5, 2.5, speed=0.10, required_clearance=0.35
    ) == 0.0


def test_escape_needs_a_known_obstacle_bearing():
    assert plan_escape(
        INSCRIBED_COST, None, 2.5, 2.5,
        speed=0.10, required_clearance=0.35,
    ) == 0.0
