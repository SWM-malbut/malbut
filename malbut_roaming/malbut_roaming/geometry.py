"""Small geometry helpers shared by the roaming policy and ROS adapter."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Point2D:
    """A point in the map frame."""

    x: float
    y: float


def distance(first: Point2D, second: Point2D) -> float:
    """Return planar Euclidean distance between two points."""
    return math.hypot(second.x - first.x, second.y - first.y)


def yaw_to_quaternion(yaw: float) -> tuple[float, float, float, float]:
    """Convert a planar yaw angle to an xyzw quaternion."""
    half_yaw = yaw * 0.5
    return 0.0, 0.0, math.sin(half_yaw), math.cos(half_yaw)


def quaternion_to_yaw(
    x: float,
    y: float,
    z: float,
    w: float,
) -> float:
    """Extract planar yaw from an xyzw quaternion."""
    sin_yaw = 2.0 * (w * z + x * y)
    cos_yaw = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(sin_yaw, cos_yaw)


def standoff_point(
    observer: Point2D,
    target: Point2D,
    distance_m: float,
) -> tuple[Point2D, float]:
    """Return a point short of a target and a yaw facing the target."""
    if distance_m < 0.0:
        raise ValueError('distance_m must be non-negative')
    delta_x = target.x - observer.x
    delta_y = target.y - observer.y
    target_distance = math.hypot(delta_x, delta_y)
    yaw = math.atan2(delta_y, delta_x)
    if target_distance <= distance_m or target_distance == 0.0:
        return observer, yaw
    scale = (target_distance - distance_m) / target_distance
    return Point2D(
        observer.x + delta_x * scale,
        observer.y + delta_y * scale,
    ), yaw
