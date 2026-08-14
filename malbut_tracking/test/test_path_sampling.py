"""Tests for choosing tracking waypoints along a Nav2 route."""

import math

import pytest
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path

from malbut_tracking.path_sampling import (
    path_length,
    sample_path_waypoint,
    truncate_path,
)


def _pose(x, y):
    pose = PoseStamped()
    pose.pose.position.x = float(x)
    pose.pose.position.y = float(y)
    pose.pose.orientation.w = 1.0
    return pose


def test_waypoint_follows_an_l_shaped_path_not_the_direct_diagonal():
    """The bounded point must remain on the planner's first corridor."""
    waypoint = sample_path_waypoint(
        [_pose(0, 0), _pose(2, 0), _pose(2, 2)],
        lookahead_m=1.0,
        final_yaw=math.pi / 2,
    )
    assert waypoint is not None
    assert waypoint.position.x == pytest.approx(1.0)
    assert waypoint.position.y == pytest.approx(0.0)
    assert waypoint.yaw == pytest.approx(0.0)


def test_short_path_uses_the_final_standoff_pose():
    """A route shorter than lookahead should finish at its safe endpoint."""
    waypoint = sample_path_waypoint(
        [_pose(0, 0), _pose(0.3, 0.4)],
        lookahead_m=1.0,
        final_yaw=1.2,
    )
    assert waypoint is not None
    assert waypoint.position.x == pytest.approx(0.3)
    assert waypoint.position.y == pytest.approx(0.4)
    assert waypoint.yaw == pytest.approx(1.2)


def test_path_length_supports_midpoint_recovery_staging():
    """Recovery can split an arbitrary planned route by travelled distance."""
    path = Path()
    path.poses = [_pose(0, 0), _pose(3, 0), _pose(3, 4)]
    assert path_length(path) == pytest.approx(7.0)


def test_tracking_control_does_not_change_planned_path_or_orientation():
    """Follower logic is not allowed to alter the bounded planner path."""
    path = Path()
    path.header.frame_id = 'map'
    path.poses = [_pose(0, 0), _pose(2, 0), _pose(2, 2)]
    bounded = truncate_path(path, lookahead_m=2.5)
    assert bounded is not None
    output, waypoint = bounded
    positions = [
        (pose.pose.position.x, pose.pose.position.y)
        for pose in output.poses
    ]
    assert positions == [(0.0, 0.0), (2.0, 0.0), (2.0, 0.5)]
    assert waypoint.position.x == pytest.approx(2.0)
    assert waypoint.position.y == pytest.approx(0.5)
    yaw = 2.0 * math.atan2(
        output.poses[-1].pose.orientation.z,
        output.poses[-1].pose.orientation.w,
    )
    assert yaw == pytest.approx(0.0)
    assert output.poses[0].pose.orientation == path.poses[0].pose.orientation
