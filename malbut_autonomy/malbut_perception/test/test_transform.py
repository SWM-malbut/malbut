"""Tests for optional TF projection of camera points."""

import math

from geometry_msgs.msg import TransformStamped

from malbut_perception.target_localizer_node import transform_point


def test_transform_point_rotates_and_translates():
    transform = TransformStamped()
    transform.transform.translation.x = 1.0
    transform.transform.translation.y = 2.0
    transform.transform.rotation.z = math.sin(math.pi / 4.0)
    transform.transform.rotation.w = math.cos(math.pi / 4.0)
    point = transform_point((1.0, 0.0, 0.0), transform)
    assert math.isclose(point[0], 1.0, abs_tol=1e-9)
    assert math.isclose(point[1], 3.0, abs_tol=1e-9)
    assert math.isclose(point[2], 0.0, abs_tol=1e-9)
