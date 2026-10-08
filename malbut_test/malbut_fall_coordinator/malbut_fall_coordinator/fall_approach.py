"""Drive near an uncertain fall suspicion and back: pure decisions, no ROS.

The node supplies Nav2 and TF. Limits are the user's 2026-10-08 decisions
(60 s to arrive, stop 1 m before the spot) and the person follower's values.
"""

from collections import OrderedDict
from dataclasses import dataclass
import math

STANDOFF_M = 1.0
APPROACH_LIMIT_S = 60.0
RETURN_LIMIT_S = 60.0
# No path to the spot itself (inside furniture or a keepout edge): try points
# pulled back toward the robot, like the follower's goal pullback.
PULLBACK_STEP_M = 0.5
PULLBACK_TRIES = 2
FACE_TOLERANCE_RAD = 0.10
PHASES = ('approach', 'return')
OUTCOMES = ('arrived', 'returned', 'no_map', 'no_path', 'timeout', 'failed')


@dataclass(frozen=True)
class Pose2D:
    x: float
    y: float
    yaw: float


def normalize_angle(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def goal_error(phase, x, y, standoff_m):
    """Why a goal cannot be accepted, or None."""
    if phase not in PHASES:
        return 'phase must be approach or return'
    values = (x, y, standoff_m)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in values):
        return 'coordinates must be finite numbers'
    if phase == 'approach' and not 0.3 <= standoff_m <= 3.0:
        return 'standoff must be within 0.3-3.0 m'
    return None


def plan_targets(robot, target):
    """The spot first, then points pulled back toward the robot, never past it."""
    targets = [target]
    dx, dy = robot[0] - target[0], robot[1] - target[1]
    gap = math.hypot(dx, dy)
    for step in range(1, PULLBACK_TRIES + 1):
        pulled = step * PULLBACK_STEP_M
        if pulled >= gap - STANDOFF_M:
            break
        targets.append((target[0] + dx / gap * pulled, target[1] + dy / gap * pulled))
    return targets


def standoff_route(points, target, standoff_m):
    """Nav2's route up to where it first comes within standoff_m of the spot.

    The follower's path_to_standoff rule on plain (x, y) points, so this
    package does not pull the tracking package (and its detectors) in.
    """
    if not points:
        return []
    route = [points[0]]
    if math.dist(points[0], target) <= standoff_m:
        return route
    for start, end in zip(points, points[1:]):
        fraction = _circle_entry(start, end, target, standoff_m)
        if fraction is not None:
            route.append((start[0] + fraction * (end[0] - start[0]),
                          start[1] + fraction * (end[1] - start[1])))
            return route
        route.append(end)
    return route


def _circle_entry(start, end, centre, radius):
    """First fraction of the segment on the circle, or None."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    ox, oy = start[0] - centre[0], start[1] - centre[1]
    a = dx * dx + dy * dy
    b = 2.0 * (ox * dx + oy * dy)
    c = ox * ox + oy * oy - radius * radius
    discriminant = b * b - 4.0 * a * c
    if a <= 1e-18 or discriminant < 0.0:
        return None
    root = math.sqrt(discriminant)
    hits = [t for t in ((-b - root) / (2.0 * a), (-b + root) / (2.0 * a)) if 0.0 <= t <= 1.0]
    return min(hits) if hits else None


def route_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def facing_turn(robot, target):
    """Relative yaw that points the camera at the spot; 0 within tolerance."""
    want = math.atan2(target[1] - robot.y, target[0] - robot.x)
    turn = normalize_angle(want - robot.yaw)
    return 0.0 if abs(turn) <= FACE_TOLERANCE_RAD else turn


def returning_turn(robot, start):
    turn = normalize_angle(start.yaw - robot.yaw)
    return 0.0 if abs(turn) <= FACE_TOLERANCE_RAD else turn


class StartPoses:
    """Where each check began, so the robot can go back; bounded, newest kept."""

    def __init__(self, limit=32):
        self._poses = OrderedDict()
        self._limit = limit

    def remember(self, request_id, pose):
        if request_id in self._poses:
            return  # A retried approach keeps the first start.
        self._poses[request_id] = pose
        while len(self._poses) > self._limit:
            self._poses.popitem(last=False)

    def get(self, request_id):
        return self._poses.get(request_id)

    def forget(self, request_id):
        self._poses.pop(request_id, None)
