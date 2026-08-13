"""Unit tests for safe follow and Nav2 goal-update decisions."""

import pytest

from malbut_tracking.follow_policy import (
    FollowCommand,
    FollowSettings,
    decide_follow_motion,
    should_update_goal,
    target_loss_timed_out,
)
from malbut_tracking.geometry import Point2D


@pytest.fixture
def settings():
    """Return representative household follow settings."""
    return FollowSettings(
        desired_distance_m=1.2,
        minimum_distance_m=0.65,
        distance_tolerance_m=0.15,
        goal_update_distance_m=0.25,
        goal_update_period_s=0.75,
        maximum_linear_speed_mps=0.30,
        temporary_lost_timeout_s=0.75,
        search_start_timeout_s=1.5,
        target_lost_timeout_s=8.0,
    )


def test_loss_timeout_does_not_expire_before_first_acquisition():
    """An action waiting for its first person must remain active."""
    assert not target_loss_timed_out(None, now_s=100.0, timeout_s=8.0)


def test_loss_timeout_starts_after_person_was_seen():
    """The configured timeout applies only after an acquired person is lost."""
    assert not target_loss_timed_out(10.0, now_s=17.9, timeout_s=8.0)
    assert target_loss_timed_out(10.0, now_s=18.0, timeout_s=8.0)


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


def test_goal_update_uses_motion_or_maximum_refresh_age(settings):
    """Target motion reacts now and a stale goal refreshes by its deadline."""
    previous = Point2D(1.0, 1.0)
    assert should_update_goal(
        previous,
        Point2D(1.3, 1.0),
        0.5,
        settings,
    )
    assert should_update_goal(
        previous,
        Point2D(1.1, 1.0),
        1.0,
        settings,
    )
    assert not should_update_goal(
        previous,
        Point2D(1.1, 1.0),
        0.5,
        settings,
    )


def test_camera_only_goal_updates_use_coarser_thresholds(settings):
    """Far RGB-D tracking should not preempt Nav2 for fine sensor jitter."""
    previous = Point2D(1.0, 1.0)
    assert should_update_goal(
        previous,
        Point2D(1.3, 1.0),
        1.2,
        settings,
        update_distance_m=0.5,
        update_period_s=1.0,
    )
    assert should_update_goal(
        previous,
        Point2D(1.6, 1.0),
        1.2,
        settings,
        update_distance_m=0.5,
        update_period_s=1.0,
    )
