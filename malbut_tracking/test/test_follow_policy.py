"""Unit tests for safe follow and Nav2 goal-update decisions."""

from dataclasses import replace

import pytest

from malbut_tracking.follow_policy import (
    directed_recovery_turn,
    FollowCommand,
    FollowSettings,
    decide_follow_motion,
)
from malbut_tracking.geometry import Point2D


@pytest.fixture
def settings():
    """Return representative household follow settings."""
    return FollowSettings(
        desired_distance_m=1.2,
        minimum_distance_m=0.20,
        distance_tolerance_m=0.15,
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
        Point2D(0.15, 0.0),
        settings,
    )
    assert decision.command == FollowCommand.RETREAT
    assert decision.goal.position.x == pytest.approx(-1.05)
    assert decision.reason == 'minimum distance safety retreat'


def test_minimum_requested_distance_clamps_the_retreat_band(settings):
    """Allow 0.2 m without allowing tolerance to lower the minimum distance."""
    close = replace(settings, desired_distance_m=0.2, distance_tolerance_m=0.1)
    close.validate()
    for target_x, expected in ((0.19, FollowCommand.RETREAT), (0.2, FollowCommand.ALIGN)):
        decision = decide_follow_motion(Point2D(0.0, 0.0), Point2D(target_x, 0.0), close)
        assert decision.command == expected
    with pytest.raises(ValueError, match='at least minimum distance'):
        replace(close, desired_distance_m=0.19).validate()


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


def test_recovery_turn_uses_the_last_camera_exit_side_first():
    """The first camera-loss turn must ignore LiDAR disagreement."""
    assert directed_recovery_turn(-0.20, 0.80, 0.70) == pytest.approx(-0.70)
    assert directed_recovery_turn(0.30, -0.90, 0.70) == pytest.approx(0.70)
    assert directed_recovery_turn(0.0, -0.90, 0.70) == pytest.approx(-0.90)


@pytest.mark.parametrize('target_x', [0.8, 0.81, 0.89, 1.0, 1.11, 1.19, 1.2])
def test_target_inside_the_single_twenty_centimetre_band_does_not_translate(target_x):
    """Both exact boundaries and the whole requested band permit alignment only."""
    settings = FollowSettings(1.0, 0.2, 0.2, 0.75)
    decision = decide_follow_motion(
        Point2D(0.0, 0.0), Point2D(target_x, 0.0), settings,
    )
    assert decision.command == FollowCommand.ALIGN


@pytest.mark.parametrize('target_x,expected', [
    (0.79, FollowCommand.RETREAT), (1.21, FollowCommand.NAVIGATE),
    (0.19, FollowCommand.RETREAT),
])
def test_motion_outside_the_single_band_preserves_the_hard_minimum(target_x, expected):
    """There is no separate entry/release threshold depending on prior motion."""
    settings = FollowSettings(1.0, 0.2, 0.2, 0.75)
    assert decide_follow_motion(
        Point2D(0.0, 0.0), Point2D(target_x, 0.0), settings,
    ).command == expected


@pytest.mark.parametrize('desired,target_x,expected', [
    (0.6, 0.4, FollowCommand.ALIGN), (0.6, 0.8, FollowCommand.ALIGN),
    (0.6, 0.39, FollowCommand.RETREAT), (0.6, 0.81, FollowCommand.NAVIGATE),
    (0.3, 0.15, FollowCommand.RETREAT),
])
def test_single_band_follows_requested_distance_and_keeps_minimum(desired, target_x, expected):
    """The same +/-0.20 m rule also applies to non-default Action distances."""
    settings = FollowSettings(desired, 0.2, 0.2, 0.75)
    assert decide_follow_motion(
        Point2D(0.0, 0.0), Point2D(target_x, 0.0), settings,
    ).command == expected
