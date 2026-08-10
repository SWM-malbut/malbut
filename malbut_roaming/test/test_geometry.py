"""Unit tests for planar geometry helpers."""

import math

import pytest

from malbut_roaming.geometry import (
    Point2D,
    distance,
    quaternion_to_yaw,
    standoff_point,
    yaw_to_quaternion,
)


@pytest.mark.parametrize(
    'yaw',
    [-math.pi, -1.2, 0.0, 1.7, math.pi],
)
def test_yaw_quaternion_round_trip(yaw):
    """Planar pose conversion must preserve headings across the full range."""
    quaternion = yaw_to_quaternion(yaw)
    assert quaternion_to_yaw(*quaternion) == pytest.approx(yaw)
    assert math.sqrt(sum(value * value for value in quaternion)) == pytest.approx(
        1.0
    )


def test_distance_is_symmetric_and_uses_both_axes():
    """Policy scoring and target speed depend on Euclidean map distance."""
    first = Point2D(-1.0, 2.0)
    second = Point2D(2.0, 6.0)
    assert distance(first, second) == pytest.approx(5.0)
    assert distance(second, first) == pytest.approx(5.0)


def test_standoff_point_stops_short_and_faces_the_target():
    """A following goal must remain between observer and target."""
    point, yaw = standoff_point(
        Point2D(0.0, 0.0),
        Point2D(3.0, 0.0),
        1.2,
    )
    assert point.x == pytest.approx(1.8)
    assert point.y == pytest.approx(0.0)
    assert yaw == pytest.approx(0.0)

    point, yaw = standoff_point(
        Point2D(1.0, 1.0),
        Point2D(1.0, 3.0),
        0.5,
    )
    assert point.x == pytest.approx(1.0)
    assert point.y == pytest.approx(2.5)
    assert yaw == pytest.approx(math.pi / 2.0)


def test_standoff_does_not_move_when_target_is_already_close():
    """The robot may not back away unexpectedly from a close observation."""
    observer = Point2D(1.0, 2.0)
    point, yaw = standoff_point(observer, Point2D(1.2, 2.0), 0.5)
    assert point == observer
    assert yaw == pytest.approx(0.0)


def test_negative_standoff_distance_is_rejected():
    with pytest.raises(ValueError, match='non-negative'):
        standoff_point(Point2D(0.0, 0.0), Point2D(1.0, 0.0), -0.1)
