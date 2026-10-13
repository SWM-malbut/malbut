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
# Only part of the body was seen from 1 m (2026-10-11): move back to the
# distance the fall rules ask for, within the user's one minute.
VIEW_LIMIT_S = 60.0
# 2026-10-12 21:16 the view drive went to a fixed point the spot could not be
# seen from, the long way round. A view spot now has to see the spot on the
# costmap and be near by path (the user's rules, 2026-10-12): the asked
# distance first, then 0.5 m nearer or farther; at most twice the straight line.
VIEW_RING_OFFSETS_M = (0.0, -0.5, 0.5)
VIEW_TURN_STEP_DEG = 30.0
VIEW_DETOUR_FACTOR = 2.0
VIEW_DETOUR_SLACK_M = 0.05
# The person lies around the spot and is an obstacle on the costmap: sight is
# checked up to this far from it.
BODY_CLEAR_M = 1.0
# Nav2 raw costmap values (nav2_msgs/Costmap).
INSCRIBED_COST = 253
LETHAL_COST = 254
# Turning away to drive to the spot behind the robot failed beside a bed
# (2026-10-12 16:32): the half turn alone used most of Nav2's 10 s / 0.5 m
# progress check. Facing the spot, the robot backs up instead, camera on the
# person; slow, as it moves blind to the camera.
BACKUP_SPEED_MPS = 0.1
BACKUP_FACING_RAD = math.radians(30.0)
BACKUP_MIN_M = 0.1
# revisit: back from the spot after waiting, the guardian told what it saw (2026-10-12).
RETURN_REASONS = ('not_a_person', 'off_floor', 'revisit')
# No path to the spot itself (inside furniture or a keepout edge): try points
# pulled back toward the robot, like the follower's goal pullback.
PULLBACK_STEP_M = 0.5
PULLBACK_TRIES = 2
FACE_TOLERANCE_RAD = 0.10
PHASES = ('approach', 'view', 'return', 'hold')
# A fall check keeps a HIGH mission the whole time (the user, 2026-10-13): approach
# and view hold the wheels after their drive, and hold only holds them, until the
# next step replaces the goal or it is canceled. Bounded like the fall rules' wait.
HOLDING_PHASES = ('approach', 'view', 'hold')
HOLD_LIMIT_S = 660.0
OUTCOMES = ('arrived', 'returned', 'no_map', 'no_path', 'timeout', 'failed')


@dataclass(frozen=True)
class Pose2D:
    x: float
    y: float
    yaw: float


def normalize_angle(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def goal_error(phase, x, y, standoff_m, reason=''):
    """Why a goal cannot be accepted, or None."""
    if phase not in PHASES:
        return 'phase must be approach, view, return or hold'
    values = (x, y, standoff_m)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in values):
        return 'coordinates must be finite numbers'
    if phase in ('approach', 'view') and not 0.3 <= standoff_m <= 3.0:
        return 'standoff must be within 0.3-3.0 m'
    if reason not in ('', *RETURN_REASONS) or (reason and phase != 'return'):
        return 'reason must be not_a_person, off_floor or revisit, on a return only'
    return None


@dataclass(frozen=True)
class CostGrid:
    """Nav2's raw costmap: row-major costs, 0 free .. 254 lethal, 255 unknown."""

    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    width: int
    height: int
    data: bytes

    def cost(self, x, y):
        """Return the cost under a map point, or None off the grid."""
        dx, dy = x - self.origin_x, y - self.origin_y
        cos, sin = math.cos(self.origin_yaw), math.sin(self.origin_yaw)
        col = math.floor((cos * dx + sin * dy) / self.resolution)
        row = math.floor((-sin * dx + cos * dy) / self.resolution)
        if 0 <= col < self.width and 0 <= row < self.height:
            return self.data[row * self.width + col]
        return None


def can_stand(grid, point):
    """Known and outside the robot's inscribed footprint of any obstacle."""
    cost = grid.cost(*point)
    return cost is not None and cost < INSCRIBED_COST


def sight_clear(grid, point, target):
    """No wall or furniture between the point and the spot, short of the body.

    Only lethal cells block: unknown cells and cells off the grid do not, so a
    spot beyond the map can still be seen. Inflation is clearance, not an object.
    """
    length = math.dist(point, target)
    reach = length - BODY_CLEAR_M
    if reach <= 0.0:
        return True
    step = grid.resolution / 2.0
    for i in range(int(reach / step) + 2):
        t = min(i * step, reach) / length
        if grid.cost(point[0] + (target[0] - point[0]) * t,
                     point[1] + (target[1] - point[1]) * t) == LETHAL_COST:
            return False
    return True


def view_rings(robot, target, distance):
    """Where the whole body may be seen, by distance from the spot.

    Each ring starts straight back from the spot (seen from the robot's side)
    and goes around it both ways.
    """
    base = math.atan2(robot[1] - target[1], robot[0] - target[0])
    steps = int(round(180.0 / VIEW_TURN_STEP_DEG))
    turns = [0.0]
    for k in range(1, steps):
        turns += [k * VIEW_TURN_STEP_DEG, -k * VIEW_TURN_STEP_DEG]
    turns.append(180.0)
    rings = []
    for offset in VIEW_RING_OFFSETS_M:
        radius = distance + offset
        if radius <= BODY_CLEAR_M:
            continue
        rings.append([(target[0] + radius * math.cos(base + math.radians(turn)),
                       target[1] + radius * math.sin(base + math.radians(turn)))
                      for turn in turns])
    return rings


def within_detour(robot, goal, length):
    """Check the path is at most twice the straight line (the user, 2026-10-12)."""
    return length <= VIEW_DETOUR_FACTOR * math.dist(robot, goal) + VIEW_DETOUR_SLACK_M


def backup_distance(robot, target, distance):
    """How far to back straight away from the spot, or 0 when not facing it."""
    gap = math.hypot(target[0] - robot.x, target[1] - robot.y)
    if gap <= 1e-6:
        return 0.0
    off = normalize_angle(math.atan2(target[1] - robot.y, target[0] - robot.x) - robot.yaw)
    back = distance - gap
    return back if abs(off) <= BACKUP_FACING_RAD and back >= BACKUP_MIN_M else 0.0


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
