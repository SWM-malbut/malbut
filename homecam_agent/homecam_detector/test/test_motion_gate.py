"""Tests for read-only odometry motion suppression."""

from homecam_detector.motion_gate import MotionGate


def test_missing_or_stale_odom_suppresses_generic_motion() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    assert not gate.generic_motion_allowed(10.0)
    gate.update(0.0, 0.0, 10.0)
    assert not gate.generic_motion_allowed(10.5)
    assert not gate.generic_motion_allowed(12.1)


def test_stationary_period_enables_generic_motion() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    gate.update(0.0, 0.0, 10.0)
    gate.update(0.0, 0.0, 10.8)
    assert gate.generic_motion_allowed(11.0)


def test_movement_immediately_disables_and_requires_new_stable_period() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    gate.update(0.0, 0.0, 1.0)
    gate.update(0.0, 0.0, 2.0)
    assert gate.generic_motion_allowed(2.0)
    gate.update(0.2, 0.0, 2.1)
    assert not gate.generic_motion_allowed(2.1)
    gate.update(0.0, 0.0, 3.0)
    assert not gate.generic_motion_allowed(3.9)
    gate.update(0.0, 0.0, 4.0)
    assert gate.generic_motion_allowed(4.0)


def test_non_finite_odometry_is_never_treated_as_stationary() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    gate.update(0.0, 0.0, 1.0)
    gate.update(0.0, 0.0, 2.0)
    assert gate.generic_motion_allowed(2.0)
    gate.update(float("nan"), 0.0, 2.1)
    assert not gate.generic_motion_allowed(2.1)
    gate.update(0.0, float("inf"), 2.2)
    assert not gate.generic_motion_allowed(2.2)


def test_navigation_suppresses_motion_and_requires_post_run_stabilization() -> None:
    gate = MotionGate(stationary_after_sec=2.0, odom_timeout_sec=3.0)
    gate.update(0.0, 0.0, 1.0)
    gate.update(0.0, 0.0, 3.0)
    assert gate.generic_motion_allowed(3.0)

    assert gate.set_navigation_active(True)
    gate.update(0.0, 0.0, 4.0)
    assert not gate.generic_motion_allowed(4.0)
    assert not gate.set_navigation_active(True)

    assert gate.set_navigation_active(False)
    gate.update(0.0, 0.0, 5.1)
    assert not gate.generic_motion_allowed(7.0)
    gate.update(0.0, 0.0, 7.1)
    assert gate.generic_motion_allowed(7.1)
