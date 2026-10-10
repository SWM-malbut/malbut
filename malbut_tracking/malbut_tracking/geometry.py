"""Planar geometry used by the person-following policy."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Point2D:
    """A point in the Nav2 global frame."""

    x: float
    y: float


@dataclass(frozen=True)
class FollowGoal:
    """A safe robot destination that faces the observed target."""

    position: Point2D
    yaw: float
    target_distance: float


def distance(first: Point2D, second: Point2D) -> float:
    """Return Euclidean planar distance between two points."""
    return math.hypot(second.x - first.x, second.y - first.y)


def normalize_angle(angle: float) -> float:
    """Wrap an angle to [-pi, pi]."""
    return math.atan2(math.sin(angle), math.cos(angle))


def yaw_to_quaternion(yaw: float) -> tuple[float, float, float, float]:
    """Convert a planar yaw to an x, y, z, w quaternion."""
    half = yaw * 0.5
    return 0.0, 0.0, math.sin(half), math.cos(half)


def quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    """Extract planar yaw from a quaternion."""
    return math.atan2(
        2.0 * (w * z + x * y),
        1.0 - 2.0 * (y * y + z * z),
    )


def make_follow_goal(
    robot: Point2D,
    target: Point2D,
    desired_distance: float,
    maximum_travel: float | None = None,
) -> FollowGoal:
    """Place one bounded goal toward the requested target standoff."""
    if desired_distance < 0.0:
        raise ValueError('desired_distance must be non-negative')
    if maximum_travel is not None and maximum_travel <= 0.0:
        raise ValueError('maximum_travel must be positive')
    target_distance = distance(robot, target)
    yaw = math.atan2(target.y - robot.y, target.x - robot.x)
    if target_distance <= desired_distance or target_distance <= 1e-9:
        return FollowGoal(robot, yaw, target_distance)
    travel = target_distance - desired_distance
    if maximum_travel is not None:
        travel = min(travel, maximum_travel)
    return FollowGoal(
        Point2D(
            robot.x + travel * math.cos(yaw),
            robot.y + travel * math.sin(yaw),
        ),
        yaw,
        target_distance,
    )
