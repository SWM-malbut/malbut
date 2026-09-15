"""Exercise the follower's alignment using current world headings and TF."""

import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tf2_ros import TransformException

from malbut_tracking.navigation import MotionMode
from malbut_tracking.person_follower_node import PersonFollowerNode


@pytest.fixture
def follower():
    """Use only the real alignment method, with motion and TF mocked."""
    motion = SimpleNamespace(mode=None, busy=False, stopping=False)

    def start_spin(angle, allowance):
        motion.mode = MotionMode.SPIN
        motion.busy = True
        return True

    def cancel():
        motion.mode = None
        motion.stopping = motion.busy

    motion.spin = Mock(side_effect=start_spin)
    motion.cancel = Mock(side_effect=cancel)
    return SimpleNamespace(
        _nav2=motion,
        _path_planner=SimpleNamespace(cancel=Mock()),
        _last_goal_position=None,
        _alignment_target_yaw=None,
        _robot_pose=Mock(return_value=(None, 0.0)),
        get_parameter=lambda name: SimpleNamespace(value=0.10),
        _turn_allowance=lambda angle: 3.0,
        _warn_periodically=Mock(),
    )


def align(follower, target_yaw, robot_yaw=0.0):
    follower._robot_pose.return_value = (None, robot_yaw)
    PersonFollowerNode._align_with_target(follower, target_yaw)


def test_same_world_heading_does_not_restart_as_robot_rotates(follower):
    align(follower, 1.0)
    align(follower, 1.0, robot_yaw=0.25)
    align(follower, 1.04, robot_yaw=0.50)
    follower._nav2.spin.assert_called_once_with(1.0, 3.0)
    follower._nav2.cancel.assert_not_called()


def test_changed_heading_stops_then_recomputes_relative_angle_from_fresh_tf(follower):
    align(follower, 1.0)
    align(follower, -0.8, robot_yaw=0.2)
    follower._nav2.cancel.assert_called_once()
    assert follower._alignment_target_yaw is None
    assert follower._nav2.stopping
    align(follower, -0.4, robot_yaw=0.3)
    assert follower._nav2.spin.call_count == 1
    assert follower._nav2.cancel.call_count == 1
    follower._nav2.busy = False
    follower._nav2.stopping = False
    align(follower, -0.5, robot_yaw=0.35)
    assert follower._nav2.spin.call_count == 2
    assert follower._nav2.spin.call_args.args[0] == pytest.approx(-0.85)
    assert follower._alignment_target_yaw == -0.5


def test_small_heading_changes_accumulate_against_dispatched_heading(follower):
    align(follower, 1.0)
    align(follower, 1.06)
    follower._nav2.cancel.assert_not_called()
    align(follower, 1.12)
    follower._nav2.cancel.assert_called_once()


def test_world_heading_wraparound_is_not_a_large_change(follower):
    align(follower, math.pi - 0.02)
    align(follower, -math.pi + 0.02)
    follower._nav2.cancel.assert_not_called()
    assert follower._nav2.spin.call_count == 1


def test_navigation_stops_before_any_relative_spin_is_sent(follower):
    follower._nav2.mode = MotionMode.NAVIGATE
    follower._nav2.busy = True
    align(follower, 1.0)
    follower._nav2.cancel.assert_called_once()
    follower._nav2.spin.assert_not_called()
    follower._nav2.busy = False
    follower._nav2.stopping = False
    align(follower, 0.8, robot_yaw=0.3)
    assert follower._nav2.spin.call_args.args[0] == pytest.approx(0.5)


def test_alignment_discards_queued_motion_while_previous_goal_is_stopping(follower):
    follower._nav2.mode = MotionMode.NAVIGATE
    follower._nav2.busy = True
    follower._nav2.stopping = True
    align(follower, 1.0)
    follower._nav2.cancel.assert_called_once()
    follower._nav2.spin.assert_not_called()
    align(follower, 0.8)
    assert follower._nav2.cancel.call_count == 1


def test_centered_target_cancels_rotation_without_replacement(follower):
    align(follower, 1.0)
    align(follower, 1.0, robot_yaw=0.95)
    follower._nav2.cancel.assert_called_once()
    assert follower._alignment_target_yaw is None
    assert follower._nav2.spin.call_count == 1


def test_missing_current_tf_does_not_dispatch_a_stale_relative_angle(follower):
    follower._robot_pose.side_effect = TransformException('not ready')
    PersonFollowerNode._align_with_target(follower, 1.0)
    follower._nav2.spin.assert_not_called()
    follower._warn_periodically.assert_called_once()
