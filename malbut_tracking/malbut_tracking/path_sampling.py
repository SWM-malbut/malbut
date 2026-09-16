"""Inspect paths returned by Nav2."""

from copy import deepcopy
import math

from nav_msgs.msg import Path

from .geometry import Point2D


def path_to_standoff(path: Path, target: Point2D, standoff_m: float) -> Path:
    """Keep Nav2's route only up to its first entry into the standoff circle."""
    if not math.isfinite(standoff_m) or standoff_m <= 0.0:
        raise ValueError('standoff distance must be finite and positive')
    output = Path()
    output.header = deepcopy(path.header)
    if not path.poses:
        return output
    output.poses.append(deepcopy(path.poses[0]))
    first = path.poses[0].pose.position
    if math.hypot(first.x - target.x, first.y - target.y) > standoff_m:
        for start, end in zip(path.poses, path.poses[1:]):
            origin = start.pose.position
            destination = end.pose.position
            dx, dy = destination.x - origin.x, destination.y - origin.y
            ox, oy = origin.x - target.x, origin.y - target.y
            a = dx * dx + dy * dy
            b = 2.0 * (ox * dx + oy * dy)
            c = ox * ox + oy * oy - standoff_m * standoff_m
            discriminant = b * b - 4.0 * a * c
            fraction = None
            if a > 1e-18 and discriminant >= 0.0:
                root = math.sqrt(discriminant)
                candidates = [
                    value for value in ((-b - root) / (2.0 * a),
                                        (-b + root) / (2.0 * a))
                    if 0.0 <= value <= 1.0
                ]
                if candidates:
                    fraction = min(candidates)
            pose = deepcopy(end)
            if fraction is not None:
                pose.pose.position.x = origin.x + fraction * dx
                pose.pose.position.y = origin.y + fraction * dy
                output.poses.append(pose)
                break
            output.poses.append(pose)
    # Face the person, not an unrelated open-space ray at the old goal. Nav2
    # remains responsible for collision checking the actual rotation.
    last = output.poses[-1].pose
    yaw = math.atan2(target.y - last.position.y, target.x - last.position.x)
    last.orientation.x = last.orientation.y = 0.0
    last.orientation.z = math.sin(yaw * 0.5)
    last.orientation.w = math.cos(yaw * 0.5)
    return output


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
