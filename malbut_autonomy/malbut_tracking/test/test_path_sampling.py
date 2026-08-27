"""Tests for Nav2 path inspection."""

import pytest
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path

from malbut_tracking.path_sampling import (
    initial_path_heading,
    path_length_m,
)


def _pose(x, y):
    pose = PoseStamped()
    pose.pose.position.x = float(x)
    pose.pose.position.y = float(y)
    pose.pose.orientation.w = 1.0
    return pose


def test_initial_path_heading_uses_actual_first_movement_direction():
    """Initial alignment follows the route, not the final goal orientation."""
    path = Path()
    path.poses = [
        _pose(0, 0),
        _pose(0, 0),
        _pose(0, 1),
        _pose(1, 1),
    ]
    assert initial_path_heading(path) == pytest.approx(0.5 * 3.14159265)


def test_path_length_uses_all_nav2_segments():
    path = Path()
    path.poses = [_pose(0, 0), _pose(3, 0), _pose(3, 4)]
    assert path_length_m(path) == pytest.approx(7.0)
