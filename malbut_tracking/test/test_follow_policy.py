"""Unit tests for safe follow and Nav2 goal-update decisions."""

import pytest

from malbut_tracking.follow_policy import (
    directed_recovery_turn,
    FollowCommand,
    FollowSettings,
    decide_follow_motion,
    speed_limit_for_travel_distance,
)
from malbut_tracking.geometry import Point2D


@pytest.fixture
def settings():
    """Return representative household follow settings."""
    return FollowSettings(
        desired_distance_m=1.2,
        minimum_distance_m=0.65,
        distance_tolerance_m=0.15,
        minimum_follow_speed_mps=0.10,
        maximum_linear_speed_mps=0.40,
        full_speed_travel_distance_m=1.5,
        observation_loss_debounce_s=0.75,
    )


def test_far_target_creates_nav2_goal(settings):
    """A far selected person should create a target-facing standoff goal."""
    decision = decide_follow_motion(
        Point2D(0.0, 0.0),
        Point2D(3.0, 0.0),
        settings,
    )
    assert decision.command == FollowCommand.NAVIGATE
    assert decision.goal.position.x == pytest.approx(1.8)


def test_far_camera_target_creates_bounded_nav2_segment(settings):
    """Long-range RGB-D tracking should advance through short goals."""
    decision = decide_follow_motion(
        Point2D(0.0, 0.0),
        Point2D(5.0, 0.0),
        settings,
        maximum_travel_m=0.8,
    )
    assert decision.command == FollowCommand.NAVIGATE
    assert decision.goal.position.x == pytest.approx(0.8)


def test_minimum_distance_triggers_safety_retreat(settings):
    """A person inside the minimum distance must trigger reverse motion."""
    decision = decide_follow_motion(
        Point2D(0.0, 0.0),
        Point2D(0.5, 0.0),
        settings,
    )
    assert decision.command == FollowCommand.RETREAT
    assert decision.goal.position.x == pytest.approx(-0.7)
    assert decision.reason == 'minimum distance safety retreat'


def test_target_below_distance_band_triggers_retreat(settings):
    """Distance control should reverse before reaching the hard minimum."""
    decision = decide_follow_motion(
        Point2D(0.0, 0.0),
        Point2D(1.0, 0.0),
        settings,
    )
    assert decision.command == FollowCommand.RETREAT
    assert decision.goal.position.x == pytest.approx(-0.2)


def test_approaching_target_triggers_predictive_retreat(settings):
    """A person walking closer should trigger reverse before crossing limit."""
    decision = decide_follow_motion(
        Point2D(0.0, 0.0),
        Point2D(1.2, 0.0),
        settings,
        target_velocity=Point2D(-0.4, 0.0),
        approach_prediction_horizon_s=0.75,
        approach_speed_threshold_mps=0.10,
    )
    assert decision.command == FollowCommand.RETREAT
    assert decision.goal.position.x == pytest.approx(-0.3)
    assert decision.reason == (
        'approaching target predicted inside distance band'
    )


def test_non_approaching_target_does_not_trigger_predictive_retreat(settings):
    """Sideways or receding motion must not cause unnecessary backing."""
    decision = decide_follow_motion(
        Point2D(0.0, 0.0),
        Point2D(1.2, 0.0),
        settings,
        target_velocity=Point2D(0.2, 0.2),
        approach_prediction_horizon_s=0.75,
        approach_speed_threshold_mps=0.10,
    )
    assert decision.command == FollowCommand.ALIGN


@pytest.mark.parametrize('target_x', [1.2, 1.34])
def test_satisfied_distance_still_aligns_camera(target_x, settings):
    """A nearby moving person must remain centered without translation."""
    decision = decide_follow_motion(
        Point2D(0.0, 0.0),
        Point2D(target_x, 0.0),
        settings,
    )
    assert decision.command == FollowCommand.ALIGN
    assert decision.goal.position == Point2D(0.0, 0.0)


def test_speed_limit_scales_with_remaining_path_length(settings):
    """Short corrections slow down while long paths retain full speed."""
    assert speed_limit_for_travel_distance(0.0, settings) == pytest.approx(
        0.10
    )
    assert speed_limit_for_travel_distance(0.5, settings) == pytest.approx(
        0.20
    )
    assert speed_limit_for_travel_distance(1.0, settings) == pytest.approx(
        0.30
    )
    assert speed_limit_for_travel_distance(1.5, settings) == pytest.approx(
        0.40
    )
    assert speed_limit_for_travel_distance(3.0, settings) == pytest.approx(
        0.40
    )


def test_recovery_turn_uses_the_last_camera_exit_side_first():
    """The first camera-loss turn must ignore LiDAR disagreement."""
    assert directed_recovery_turn(-0.20, 0.80, 0.70) == pytest.approx(-0.70)
    assert directed_recovery_turn(0.30, -0.90, 0.70) == pytest.approx(0.70)
    assert directed_recovery_turn(0.0, -0.90, 0.70) == pytest.approx(-0.90)
