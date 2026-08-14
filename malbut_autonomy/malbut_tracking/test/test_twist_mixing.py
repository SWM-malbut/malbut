"""Tests for independent mecanum translation and target-facing yaw."""

import pytest

from malbut_tracking.twist_mixing import (
    HeadingControllerSettings,
    limit_angular_acceleration,
    target_angular_velocity,
)


SETTINGS = HeadingControllerSettings(
    proportional_gain=1.25,
    rate_feedforward_gain=0.15,
    deadband_rad=0.04,
    maximum_speed_rps=0.40,
    maximum_acceleration_rps2=1.20,
)


def test_heading_control_is_bounded_and_preserves_direction_sign():
    assert target_angular_velocity(0.2, 0.0, SETTINGS) == pytest.approx(0.25)
    assert target_angular_velocity(-2.0, 0.0, SETTINGS) == -0.40
    assert target_angular_velocity(2.0, 0.0, SETTINGS) == 0.40


def test_small_heading_error_is_deadbanded():
    assert target_angular_velocity(0.02, 0.0, SETTINGS) == 0.0


def test_yaw_acceleration_is_limited_independently_of_translation():
    assert limit_angular_acceleration(0.0, 0.4, 0.05, 1.2) == pytest.approx(
        0.06
    )
    assert limit_angular_acceleration(0.2, -0.4, 0.05, 1.2) == pytest.approx(
        0.14
    )
