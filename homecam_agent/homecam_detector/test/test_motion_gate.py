"""Tests for the read-only odometry context given to fall pose candidates."""

from homecam_detector.motion_gate import MotionGate


def test_missing_or_stale_odom_is_never_stationary() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    assert gate.pose_motion_state(10.0) != "stationary"
    gate.update(0.0, 0.0, 10.0)
    assert gate.pose_motion_state(10.5) != "stationary"
    assert gate.pose_motion_state(12.1) != "stationary"


def test_stationary_period_is_reported_as_stationary() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    gate.update(0.0, 0.0, 10.0)
    gate.update(0.0, 0.0, 10.8)
    assert gate.pose_motion_state(11.0) == "stationary"


def test_movement_requires_a_new_stable_period() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    gate.update(0.0, 0.0, 1.0)
    gate.update(0.0, 0.0, 2.0)
    assert gate.pose_motion_state(2.0) == "stationary"
    gate.update(0.2, 0.0, 2.1)
    assert gate.pose_motion_state(2.1) != "stationary"
    gate.update(0.0, 0.0, 3.0)
    assert gate.pose_motion_state(3.9) != "stationary"
    gate.update(0.0, 0.0, 4.0)
    assert gate.pose_motion_state(4.0) == "stationary"


def test_non_finite_odometry_is_never_treated_as_stationary() -> None:
    gate = MotionGate(stationary_after_sec=1.0, odom_timeout_sec=2.0)
    gate.update(0.0, 0.0, 1.0)
    gate.update(0.0, 0.0, 2.0)
    assert gate.pose_motion_state(2.0) == "stationary"
    gate.update(float("nan"), 0.0, 2.1)
    assert gate.pose_motion_state(2.1) != "stationary"
    gate.update(0.0, float("inf"), 2.2)
    assert gate.pose_motion_state(2.2) != "stationary"


def test_navigation_requires_post_run_stabilization() -> None:
    gate = MotionGate(stationary_after_sec=2.0, odom_timeout_sec=3.0)
    gate.update(0.0, 0.0, 1.0)
    gate.update(0.0, 0.0, 3.0)
    assert gate.pose_motion_state(3.0) == "stationary"

    assert gate.set_navigation_active(True)
    gate.update(0.0, 0.0, 4.0)
    assert gate.pose_motion_state(4.0) != "stationary"
    assert not gate.set_navigation_active(True)

    assert gate.set_navigation_active(False)
    gate.update(0.0, 0.0, 5.1)
    assert gate.pose_motion_state(7.0) != "stationary"
    gate.update(0.0, 0.0, 7.1)
    assert gate.pose_motion_state(7.1) == "stationary"


def test_pose_motion_context_distinguishes_missing_moving_and_stationary():
    gate = MotionGate(stationary_after_sec=1, odom_timeout_sec=2)
    assert gate.pose_motion_state(0) == "unknown"
    gate.update(0, 0, 0)
    assert gate.pose_motion_state(0.5) == "unknown"
    assert gate.pose_motion_state(1) == "stationary"
    gate.update(0.2, 0, 1.2)
    assert gate.pose_motion_state(1.2) == "moving"
    assert gate.pose_motion_state(3.3) == "unknown"
    gate.update(float("nan"), 0, 3.4)
    assert gate.pose_motion_state(3.4) == "unknown"
    gate.update(0, 0, 4)
    assert gate.pose_motion_state(3.9) == "unknown"
    assert gate.pose_motion_state(float("inf")) == "unknown"
