"""Behavior tests for the follower's event-driven orchestration."""

from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
from rclpy.action import GoalResponse

from malbut_tracking.follow_policy import FollowSettings
from malbut_tracking.geometry import Point2D
from malbut_tracking.navigation import MotionMode, Nav2MotionClient
from malbut_tracking.person_follower_node import (
    FollowState,
    PersonFollowerNode,
)
from tf2_ros import TransformException


def test_goal_distance_accepts_minimum_and_retains_zero_as_default():
    """Validate explicit close goals while preserving the omitted-distance default."""
    defaults = FollowSettings(
        desired_distance_m=1.0, minimum_distance_m=0.2, distance_tolerance_m=0.1,
        observation_loss_debounce_s=0.75,
    )
    follower = SimpleNamespace(_default_settings=lambda: defaults)
    for requested, expected in ((0.2, 0.2), (0.75, 0.75), (1.0, 1.0), (0.0, 1.0)):
        actual = PersonFollowerNode._settings_for_goal(
            follower, SimpleNamespace(desired_distance_m=requested),
        )
        assert actual.desired_distance_m == expected
        assert actual.distance_tolerance_m == 0.1
    for requested in (0.19, -1.0, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            PersonFollowerNode._settings_for_goal(
                follower, SimpleNamespace(desired_distance_m=requested),
            )


@pytest.mark.parametrize('active', [False, True])
def test_costmap_frame_changes_only_between_follow_goals(active):
    """Mapless odom targets must not reuse a map-frame estimator history."""
    grid = object()
    follower = SimpleNamespace(
        get_parameter=lambda name: SimpleNamespace(value=True),
        _odometry_frame='odom', _global_frame='map',
        _active_goal=object() if active else None, _result_future=None,
        _obstacle_tracker=Mock(), _camera_estimator=Mock(),
        _last_lidar_stamp_s=4.0, _pending_detection=object(),
        _costmap_grid=Mock(return_value=grid), _latest_global_costmap=None)
    PersonFollowerNode._on_global_costmap(
        follower, SimpleNamespace(header=SimpleNamespace(frame_id='odom')))
    if active:
        assert follower._global_frame == 'map'
        follower._costmap_grid.assert_not_called()
        follower._camera_estimator.reset.assert_not_called()
    else:
        assert follower._global_frame == 'odom' and follower._latest_global_costmap is grid
        follower._obstacle_tracker.reset.assert_called_once()
        follower._camera_estimator.reset.assert_called_once()
        assert follower._pending_detection is None and follower._last_lidar_stamp_s is None


def test_map_transition_discards_old_grid_and_requires_a_current_frame_before_following():
    """A delayed mapless grid must not lock a new Goal into the previous frame."""
    follower = SimpleNamespace(
        _odometry_frame='odom', _localization_switching=False,
        _expected_costmap_frame='odom', _latest_global_costmap=object(),
        _active_goal=None, _result_future=None, _follow_costmap_frame=True,
        get_logger=Mock(), _settings_for_goal=Mock(), _validate_target_request=Mock(),
        _costmap_grid=Mock())
    PersonFollowerNode._on_localization_state(
        follower, SimpleNamespace(data=json.dumps({'mode': 'SWITCHING'})))
    assert follower._latest_global_costmap is None
    assert PersonFollowerNode._goal_callback(follower, object()) == GoalResponse.REJECT
    PersonFollowerNode._on_localization_state(
        follower, SimpleNamespace(data=json.dumps({'mode': 'LOCALIZATION'})))
    assert follower._expected_costmap_frame == 'map'
    PersonFollowerNode._on_global_costmap(
        follower, SimpleNamespace(header=SimpleNamespace(frame_id='odom')))
    follower._costmap_grid.assert_not_called()
    assert PersonFollowerNode._goal_callback(follower, object()) == GoalResponse.REJECT
    follower._latest_global_costmap = object()
    assert PersonFollowerNode._goal_callback(follower, object()) == GoalResponse.ACCEPT


def test_nav2_feedback_forwards_only_the_current_goal():
    """Feedback from a preempted Nav2 goal must not advance recovery."""
    client = Nav2MotionClient.__new__(Nav2MotionClient)
    client._token = 8
    client._mode = MotionMode.NAVIGATE
    client._on_feedback = Mock()
    current_feedback = object()
    message = SimpleNamespace(feedback=current_feedback)

    client._feedback(message, 7, MotionMode.NAVIGATE)
    client._feedback(message, 8, MotionMode.SPIN)
    client._on_feedback.assert_not_called()

    client._feedback(message, 8, MotionMode.NAVIGATE)
    client._on_feedback.assert_called_once_with(
        MotionMode.NAVIGATE,
        current_feedback,
    )


def test_completed_plan_coalesces_exactly_the_latest_observation():
    """One newer observation is planned after an in-flight plan completes."""
    follower = SimpleNamespace(
        _active_goal=object(),
        _state=FollowState.TRACKING,
        _tracking_retry_pending=False,
        _motion_generation=12,
        _last_motion_target=Point2D(4.0, -1.0),
        _last_motion_bearing_only=True,
        _last_motion_velocity=Point2D(0.3, 0.1),
        _last_motion_source_stamp_ns=123_456_789,
        _robot_pose=Mock(return_value=(Point2D(1.0, 2.0), 0.4)),
        _now_seconds=Mock(return_value=25.0),
        _warn_periodically=Mock(),
        _apply_tracking_motion=Mock(),
    )

    PersonFollowerNode._plan_latest_observation_if_pending(follower, 11)

    follower._apply_tracking_motion.assert_called_once_with(
        Point2D(1.0, 2.0),
        Point2D(4.0, -1.0),
        25.0,
        bearing_only=True,
        target_velocity=Point2D(0.3, 0.1),
        source_stamp_ns=123_456_789,
        new_observation=False,
    )


def test_completed_plan_does_not_replay_an_unchanged_observation():
    """Completion alone must not poll and replay stale sensor data."""
    follower = SimpleNamespace(
        _active_goal=object(),
        _state=FollowState.TRACKING,
        _tracking_retry_pending=False,
        _motion_generation=12,
        _last_motion_target=Point2D(4.0, -1.0),
        _apply_tracking_motion=Mock(),
    )

    PersonFollowerNode._plan_latest_observation_if_pending(follower, 12)

    follower._apply_tracking_motion.assert_not_called()


def test_coalesced_observation_retries_when_robot_tf_is_temporarily_missing():
    """A transient TF gap must not permanently discard the newest target."""
    follower = SimpleNamespace(
        _active_goal=object(),
        _state=FollowState.TRACKING,
        _tracking_retry_pending=False,
        _motion_generation=12,
        _last_motion_target=Point2D(4.0, -1.0),
        _robot_pose=Mock(side_effect=TransformException('TF unavailable')),
        _warn_periodically=Mock(),
        _schedule_tracking_navigation_retry=Mock(),
        _apply_tracking_motion=Mock(),
    )

    PersonFollowerNode._plan_latest_observation_if_pending(follower, 11)

    follower._schedule_tracking_navigation_retry.assert_called_once_with()
    follower._apply_tracking_motion.assert_not_called()


def test_nav2_distance_feedback_does_not_reapply_an_application_speed_cap():
    """Distance feedback tracks progress without throttling the controller."""
    follower = SimpleNamespace(
        _remaining_travel_distance_m=0.0,
        _state=FollowState.TRACKING,
    )

    PersonFollowerNode._on_nav2_feedback(
        follower,
        MotionMode.NAVIGATE,
        SimpleNamespace(distance_to_goal=1.25),
    )

    assert follower._remaining_travel_distance_m == 1.25


def test_waiting_for_first_person_does_not_start_blind_search():
    """A request without a first observation must not start recovery motion."""
    follower = SimpleNamespace(
        _loss_timer=Mock(),
        _active_goal=object(),
        _settings=SimpleNamespace(observation_loss_debounce_s=0.75),
        _last_target_received_s=None,
        _state=FollowState.IDLE,
        _now_seconds=Mock(),
        _begin_loss_recovery=Mock(),
        _publish_feedback=Mock(),
    )

    PersonFollowerNode._on_loss_timer(follower)

    follower._loss_timer.cancel.assert_called_once_with()
    follower._loss_timer.reset.assert_not_called()
    follower._now_seconds.assert_not_called()
    follower._begin_loss_recovery.assert_not_called()
    follower._publish_feedback.assert_not_called()


def test_loss_deadline_is_one_shot_and_starts_recovery_once():
    """An expired observation deadline replaces periodic loss polling."""
    follower = SimpleNamespace(
        _loss_timer=Mock(),
        _active_goal=object(),
        _settings=SimpleNamespace(observation_loss_debounce_s=0.75),
        _last_target_received_s=4.0,
        _state=FollowState.TRACKING,
        _now_seconds=Mock(return_value=5.0),
        _begin_loss_recovery=Mock(),
        _publish_feedback=Mock(),
    )
    follower._begin_loss_recovery.side_effect = lambda _now: setattr(
        follower,
        '_state',
        FollowState.RECOVERING,
    )

    PersonFollowerNode._on_loss_timer(follower)
    PersonFollowerNode._on_loss_timer(follower)

    assert follower._loss_timer.cancel.call_count == 2
    follower._loss_timer.reset.assert_not_called()
    follower._begin_loss_recovery.assert_called_once_with(5.0)
    follower._publish_feedback.assert_called_once_with()


def test_cancel_guard_starts_stopping_only_the_requested_active_goal():
    """The cancel event replaces polling ``is_cancel_requested``."""
    active_goal = object()
    follower = SimpleNamespace(
        _active_goal=active_goal,
        _cancel_requested_goal=active_goal,
        _cancel_follow_action=Mock(),
    )

    PersonFollowerNode._on_cancel_guard(follower)

    assert follower._cancel_requested_goal is active_goal
    follower._cancel_follow_action.assert_called_once_with(
        'follow action canceled'
    )
