"""Small geometry helpers shared by patrol adapters and tests."""

import math


def yaw_to_quaternion(yaw: float) -> tuple[float, float, float, float]:
    """Convert a planar yaw angle into an x, y, z, w quaternion."""
    half_yaw = yaw / 2.0
    return 0.0, 0.0, math.sin(half_yaw), math.cos(half_yaw)
