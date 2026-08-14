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


def predict_search_heading(
    last_target_yaw: float,
    target_yaw_rate_rps: float,
    observation_age_s: float,
    maximum_horizon_s: float,
) -> float:
    """Predict a bounded absolute bearing for the first recovery turn."""
    if observation_age_s < 0.0:
        raise ValueError('observation age must be non-negative')
    if maximum_horizon_s < 0.0:
        raise ValueError('prediction horizon must be non-negative')
    horizon_s = min(observation_age_s, maximum_horizon_s)
    return normalize_angle(last_target_yaw + target_yaw_rate_rps * horizon_s)


def directed_search_offsets(
    last_bearing: float,
    base_angle: float,
    maximum_angle: float,
    maximum_steps: int,
) -> tuple[float, ...]:
    """
    Build a bounded scan around the predicted target heading.

    The caller owns the absolute search center.  Zero is deliberately the
    first offset so the robot looks at that sensor-backed heading before it
    starts a wider alternating scan.  ``last_bearing`` is used only as the
    preferred expansion direction after that first look.
    """
    if base_angle <= 0.0:
        raise ValueError('base angle must be positive')
    if maximum_angle <= 0.0:
        raise ValueError('maximum angle must be positive')
    if maximum_steps <= 0:
        raise ValueError('maximum steps must be positive')

    direction = -1.0 if last_bearing < 0.0 else 1.0
    base_magnitude = min(base_angle, maximum_angle)
    candidates = [0.0]
    magnitude = base_magnitude
    while magnitude < maximum_angle - 1e-6:
        candidates.extend((direction * magnitude, -direction * magnitude))
        magnitude += base_angle
    candidates.extend(
        (direction * maximum_angle, -direction * maximum_angle)
    )
    offsets = []
    for candidate in candidates:
        if any(abs(candidate - existing) < 1e-6 for existing in offsets):
            continue
        offsets.append(candidate)
        if len(offsets) >= maximum_steps:
            break
    return tuple(offsets)


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
