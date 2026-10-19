"""Drive near an uncertain fall suspicion and back: pure decisions, no ROS.

The node supplies Nav2 and TF. Limits are the user's 2026-10-08 decisions
(60 s to arrive, stop 1 m before the spot) and the person follower's values.
"""

from collections import OrderedDict
from dataclasses import dataclass
import math

APPROACH_LIMIT_S = 60.0
# The approach stops on the circle around the spot on the side it was first seen
# from, so the photos before and after show the same side (the user, 2026-10-17).
# Looking again from where it was first seen asks for that whole distance.
APPROACH_STANDOFF_MAX_M = 10.0
# Already inside a stop circle this small, the robot looks from where it stands
# (the user, 2026-10-19): 22:33 it saw the person 0.6 m away, turned round to
# drive back out to 1 m in a narrow room, failed and stopped 1.35 m off to the side.
NEAR_M = 1.0
RETURN_LIMIT_S = 60.0
# Only part of the body was seen from 1 m (2026-10-11): move back to the
# distance the fall rules ask for, within the user's one minute.
VIEW_LIMIT_S = 60.0
# 2026-10-13 16:51 the view backed toward a fixed 2.5 m, was blocked at 1.6 m and
# drove off to a far place, losing the person; the camera showed the whole body
# from 1.23 m on the way back. The view now watches the detector and stops once
# the person's torso is in view (the user, 2026-10-13), at most the asked distance
# from the spot. When the robot cannot back away, places around the spot are
# tried nearest first: they have to see the spot on the costmap and be near by
# path (at most twice the straight line, judged once, from where the view began).
VIEW_RADII_M = (1.5, 2.0, 2.5, 3.0)
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


def goal_error(phase, x, y, standoff_m, reason='', from_x=math.nan, from_y=math.nan):
    """Why a goal cannot be accepted, or None. ``from_x``/``from_y`` NaN mean unknown."""
    if phase not in PHASES:
        return 'phase must be approach, view, return or hold'
    values = (x, y, standoff_m)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in values):
        return 'coordinates must be finite numbers'
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or math.isinf(v)
           for v in (from_x, from_y)):
        return 'the first-seen point must be finite numbers, or NaN when unknown'
    if phase == 'approach' and not 0.3 <= standoff_m <= APPROACH_STANDOFF_MAX_M:
        return f'standoff must be within 0.3-{APPROACH_STANDOFF_MAX_M:.0f} m'
    if phase == 'view' and not 0.3 <= standoff_m <= VIEW_RADII_M[-1]:
        return f'view distance must be within 0.3-{VIEW_RADII_M[-1]:.1f} m'
    if reason not in ('', *RETURN_REASONS) or (reason and phase != 'return'):
        return 'reason must be not_a_person, off_floor or revisit, on a return only'
    return None


def first_seen(from_x, from_y, robot):
    """Where the suspicion was first seen from, or the robot's position when unknown."""
    if _number(from_x) and _number(from_y):
        return (float(from_x), float(from_y))
    return (robot.x, robot.y)


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


def ring_places(reference, target, radius):
    """Places on the circle around the spot: straight back toward ``reference``
    first, then around it both ways, ending on the opposite side."""
    base = math.atan2(reference[1] - target[1], reference[0] - target[0])
    steps = int(round(180.0 / VIEW_TURN_STEP_DEG))
    turns = [0.0]
    for k in range(1, steps):
        turns += [k * VIEW_TURN_STEP_DEG, -k * VIEW_TURN_STEP_DEG]
    turns.append(180.0)
    return [(target[0] + radius * math.cos(base + math.radians(turn)),
             target[1] + radius * math.sin(base + math.radians(turn))) for turn in turns]


def view_places(robot, target, distance):
    """Where the body may be seen from, nearest ring first, at most ``distance`` away.

    Each ring starts straight back from the spot (seen from the robot's side)
    and goes around it both ways.
    """
    return [place for radius in VIEW_RADII_M if BODY_CLEAR_M < radius <= distance + 1e-9
            for place in ring_places(robot, target, radius)]


def approach_places(reference, target, standoff_m):
    """Where to stop: the standoff circle from the first-seen side, then wider circles.

    The wider ones are where the whole body is looked for; with the standoff
    circle blocked (furniture, a closed door) the robot looks from the nearest
    of them it can reach (the user, 2026-10-17).
    """
    return [place for radius in (standoff_m, *(r for r in VIEW_RADII_M if r > standoff_m + 1e-9))
            for place in ring_places(reference, target, radius)]


def already_near(robot, target, standoff_m):
    """Inside the stop circle already: look from here, no drive.

    Only for a circle of at most 1 m: looking again from where it was first seen
    farther away still goes there.
    """
    return standoff_m <= NEAR_M and math.dist((robot.x, robot.y), target) <= standoff_m


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


# ------------------------------------------------------------ torso in view
# The pose detector (/homecam/person_poses, about five frames a second, also
# while the robot moves) gives each person's box and COCO keypoints in image
# fractions. The torso is in view with a shoulder and a hip or knee: from 12 cm
# a seated person's thighs may hide the hips, and the head may be cut off at the
# top, which the judgement after the move (Cloud, twice) does not need.
PERSON_MIN_CONFIDENCE = 0.45  # The detector's strong person.
KEYPOINT_MIN_CONFIDENCE = 0.5  # The detector's visible keypoint.
# YOLO places a made-up shoulder or head on the top edge of a cut-off body (a
# cross-legged person, 2026-10-07): such a point does not count, and the torso
# has to stay in view this long.
TOP_EDGE = 0.03
TORSO_STABLE_S = 0.5
POSE_MAX_AGE_S = 1.0
# The camera's 74 degrees (person_following.yaml); only the person whose box
# spans the spot's direction, give or take this much of the image, is watched.
CAMERA_HFOV_RAD = 1.291543646
TARGET_MARGIN = 0.2
# The detector never picked the person out (dark, covered, only feet, or no
# detector frames at all): the view backs straight away, camera on the spot, to
# where a seated person's head shows (the 2026-10-11 reason for 2.5 m) or until
# blocked, and Cloud judges there. It goes nowhere else: blind, a place off to
# the side only loses the person (2026-10-13 16:51; the user, 2026-10-13).
UNSEEN_VIEW_M = 2.5
SHOULDERS = ('left_shoulder', 'right_shoulder')
LOWER_TORSO = ('left_hip', 'right_hip', 'left_knee', 'right_knee')


def spot_image_x(robot, target, hfov=CAMERA_HFOV_RAD):
    """Where the spot falls across the image (0 left, 1 right), or None out of view."""
    bearing = normalize_angle(math.atan2(target[1] - robot.y, target[0] - robot.x) - robot.yaw)
    half = hfov / 2.0
    if abs(bearing) >= half:
        return None
    return 0.5 - math.tan(bearing) / (2.0 * math.tan(half))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _keypoint(pose, names):
    """(confidence, y) of the most confident keypoint of ``names``, or (0, None)."""
    best = (0.0, None)
    for point in pose.get('keypoints') or ():
        if (isinstance(point, dict) and point.get('name') in names
                and _number(point.get('confidence')) and _number(point.get('y'))
                and point['confidence'] > best[0]):
            best = (float(point['confidence']), float(point['y']))
    return best


def target_pose(persons, robot, target, hfov=CAMERA_HFOV_RAD):
    """Return the detected pose of the person at the spot: their box spans its direction."""
    x = spot_image_x(robot, target, hfov)
    if x is None or not isinstance(persons, list):
        return None
    best = None
    for person in persons:
        pose = person.get('pose') if isinstance(person, dict) else None
        box = pose.get('box') if isinstance(pose, dict) else None
        if (not isinstance(box, dict) or not _number(pose.get('boxConfidence'))
                or not all(_number(box.get(side)) for side in ('left', 'right'))):
            continue
        if pose['boxConfidence'] < PERSON_MIN_CONFIDENCE:
            continue
        if not box['left'] - TARGET_MARGIN <= x <= box['right'] + TARGET_MARGIN:
            continue  # Someone else in the room.
        if best is None or pose['boxConfidence'] > best['boxConfidence']:
            best = pose
    return best


def torso_in_view(pose):
    """Tell whether a shoulder clear of the top edge and a hip or knee are visible."""
    confidence, y = _keypoint(pose, SHOULDERS)
    return (confidence >= KEYPOINT_MIN_CONFIDENCE and y is not None and y >= TOP_EDGE
            and _keypoint(pose, LOWER_TORSO)[0] >= KEYPOINT_MIN_CONFIDENCE)


def view_score(pose):
    """How much of the body is in view: the visible keypoints."""
    return sum(1 for point in pose.get('keypoints') or ()
               if isinstance(point, dict) and _number(point.get('confidence'))
               and point['confidence'] >= KEYPOINT_MIN_CONFIDENCE)


class TorsoWatch:
    """Whether the torso has stayed in view, and where the body was seen best."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.since = None
        self.last = None
        self.best = None  # (score, Pose2D of the robot)

    def observe(self, stamp, robot, pose):
        """One detector frame; ``pose`` is the person at the spot, or None."""
        if self.last is not None and stamp < self.last:
            return  # An older frame arriving late.
        if pose is not None and torso_in_view(pose):
            if self.since is None:
                self.since = stamp
        else:
            self.since = None
        self.last = stamp
        if pose is not None:
            score = view_score(pose)
            if score and (self.best is None or score > self.best[0]):
                self.best = (score, robot)

    def seen(self, now):
        return (self.since is not None and self.last - self.since >= TORSO_STABLE_S
                and now - self.last <= POSE_MAX_AGE_S)
