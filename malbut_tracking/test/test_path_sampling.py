"""Tests for Nav2 path inspection."""

import math

import pytest
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path

from malbut_tracking.path_sampling import (
    initial_path_heading,
    path_length_m,
    path_to_standoff,
)
from malbut_tracking.geometry import Point2D


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


def test_path_stops_at_person_standoff_instead_of_person_position():
    """Full Nav2 planning does not imply driving to the observed body itself."""
    path = Path()
    path.poses = [_pose(0, 0), _pose(1, 0), _pose(2, 0), _pose(3, 0)]
    result = path_to_standoff(path, Point2D(3.0, 0.0), 1.0)
    assert result.poses[-1].pose.position.x == pytest.approx(2.0)
    assert path_length_m(result) == pytest.approx(2.0)
    assert path.poses[-1].pose.position.x == 3.0  # Input remains unchanged.


def test_standoff_preserves_detour_and_faces_person_not_open_space_heading():
    """Trimming follows the planned bend rather than drawing a new shortcut."""
    path = Path()
    path.poses = [_pose(0, 0), _pose(0, 2), _pose(3, 2), _pose(3, 0)]
    result = path_to_standoff(path, Point2D(3.0, 0.0), 1.0)
    assert [(p.pose.position.x, p.pose.position.y) for p in result.poses] == [
        (0.0, 0.0), (0.0, 2.0), (3.0, 2.0), (3.0, 1.0),
    ]
    orientation = result.poses[-1].pose.orientation
    assert 2.0 * math.atan2(orientation.z, orientation.w) == pytest.approx(-math.pi / 2)


def test_standoff_detects_entry_even_if_segment_exits_circle_again():
    """Coarse plan points outside the radius cannot skip the first crossing."""
    path = Path()
    path.poses = [_pose(0, 0), _pose(4, 0)]
    result = path_to_standoff(path, Point2D(2.0, 0.0), 0.5)
    assert result.poses[-1].pose.position.x == pytest.approx(1.5)


def test_standoff_keeps_safe_endpoint_already_outside_radius():
    """A projected goal farther away is not extended toward an obstacle."""
    path = Path()
    path.poses = [_pose(0, 0), _pose(1, 0)]
    result = path_to_standoff(path, Point2D(3.0, 0.0), 1.0)
    assert result.poses[-1].pose.position.x == 1.0


def test_standoff_does_not_advance_when_plan_start_already_inside_band():
    """Motion during planning cannot dispatch an obsolete forward approach."""
    path = Path()
    path.poses = [_pose(2.2, 0), _pose(3, 0)]
    result = path_to_standoff(path, Point2D(3.0, 0.0), 1.0)
    assert len(result.poses) == 1
    assert result.poses[0].pose.position.x == 2.2
