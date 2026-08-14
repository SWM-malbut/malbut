"""Tests for sensor-derived moving-target interest."""

import math

import pytest

from malbut_roaming.geometry import Point2D
from malbut_roaming.target_interest import TargetInterest


def test_motion_is_estimated_from_recent_received_observations():
    """Two fresh sensor-localized poses determine whether interest starts."""
    target = TargetInterest(timeout_seconds=1.5, minimum_speed=0.1)
    target.observe(Point2D(1.0, 1.0), 10.0)
    target.observe(Point2D(1.3, 1.0), 11.0)
    assert target.speed(11.0) == pytest.approx(0.3)
    assert target.is_moving(11.0)


def test_old_history_is_not_mixed_into_current_speed():
    """A reappearing person needs two fresh observations before following."""
    target = TargetInterest(timeout_seconds=1.0, minimum_speed=0.1)
    target.observe(Point2D(0.0, 0.0), 0.0)
    target.observe(Point2D(10.0, 0.0), 10.0)
    assert target.speed(10.0) == 0.0
    assert not target.is_moving(10.0)
    target.observe(Point2D(10.2, 0.0), 10.5)
    assert target.speed(10.5) == pytest.approx(0.4)
    assert target.is_moving(10.5)


def test_stale_target_expires_and_cannot_continue_following():
    """A lost perception track must not become persistent ground truth."""
    target = TargetInterest(timeout_seconds=1.0, minimum_speed=0.1)
    target.observe(Point2D(0.0, 0.0), 2.0)
    target.observe(Point2D(0.2, 0.0), 2.5)
    assert target.latest(3.4) is not None
    assert target.latest(3.6) is None
    assert target.speed(3.6) == 0.0
    assert not target.is_moving(3.6)


def test_non_monotonic_observations_do_not_corrupt_history():
    """Duplicate or older message times are ignored deterministically."""
    target = TargetInterest(timeout_seconds=2.0, minimum_speed=0.1)
    target.observe(Point2D(0.0, 0.0), 5.0)
    target.observe(Point2D(100.0, 0.0), 5.0)
    target.observe(Point2D(100.0, 0.0), 4.0)
    target.observe(Point2D(1.0, 0.0), 6.0)
    assert target.speed(6.0) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ('timeout', 'speed', 'message'),
    [
        (0.0, 0.1, 'timeout_seconds'),
        (math.nan, 0.1, 'timeout_seconds'),
        (1.0, -0.1, 'minimum_speed'),
        (1.0, math.inf, 'minimum_speed'),
    ],
)
def test_invalid_target_configuration_is_rejected(timeout, speed, message):
    with pytest.raises(ValueError, match=message):
        TargetInterest(timeout, speed)


def test_non_finite_observation_is_rejected():
    target = TargetInterest(timeout_seconds=1.0, minimum_speed=0.1)
    with pytest.raises(ValueError, match='finite'):
        target.observe(Point2D(math.nan, 0.0), 1.0)
