"""Inspect paths returned by Nav2."""

import math

from nav_msgs.msg import Path


def path_length_m(path: Path) -> float:
    """Return the planar arc length of one Nav2 path."""
    return sum(
        math.hypot(
            float(end.pose.position.x) - float(start.pose.position.x),
            float(end.pose.position.y) - float(start.pose.position.y),
        )
        for start, end in zip(path.poses, path.poses[1:])
    )


def initial_path_heading(path: Path) -> float | None:
    """Return the direction of the first non-zero Nav2 path segment."""
    for start, end in zip(path.poses, path.poses[1:]):
        delta_x = float(end.pose.position.x) - float(
            start.pose.position.x
        )
        delta_y = float(end.pose.position.y) - float(
            start.pose.position.y
        )
        if delta_x * delta_x + delta_y * delta_y > 1e-18:
            return math.atan2(delta_y, delta_x)
    return None
