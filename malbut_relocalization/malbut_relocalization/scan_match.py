"""
Score how well one LiDAR scan fits the saved map at a candidate pose.

This is AMCL's likelihood-field idea reduced to one number: the share of scan
endpoints that land within a small distance of a mapped obstacle. A robot moved
while powered off puts most endpoints in free space at its old pose.

"""

from dataclasses import dataclass
import math

import numpy as np

# map_server's trinary occupied value; occupied_thresh 0.65 also yields 100.
OCCUPIED = 65


@dataclass(frozen=True)
class DistanceField:
    """Distance from each map cell to the nearest occupied cell, in meters."""

    distance: np.ndarray
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    known_free: np.ndarray


@dataclass(frozen=True)
class ScanAssessment:
    """Raw overlap and a separate, conservative geometry decision."""

    ratio: float
    accepted: bool
    reason: str
    support_sectors: int = 0
    residual_m: float = math.inf
    improvement_m: float = 0.0

    @property
    def summary(self):
        return (f'{self.reason}: raw overlap {self.ratio:.0%}, '
                f'wall residual {self.residual_m:.3f} m, '
                f'{self.support_sectors}/8 supported directions')


def distance_field(width, height, resolution, origin, data):
    """Build the field from OccupancyGrid geometry and row-major data."""
    import cv2

    grid = np.asarray(data, dtype=np.int16).reshape(height, width)
    free = (grid < OCCUPIED).astype(np.uint8)
    distance = (cv2.distanceTransform(free, cv2.DIST_L2, 5) * resolution
                if not free.all() else np.full(grid.shape, np.inf))
    return DistanceField(distance, resolution, *origin, grid == 0)


def match_ratio(field, scan, laser_pose, robot_pose, *, hit_distance_m, max_range_m):
    """
    Return (ratio, beams) for one scan at a robot pose in the map frame.

    laser_pose and robot_pose are (x, y, yaw): the laser in the base frame and
    the base in the map frame. Beams without a return are ignored; endpoints
    outside the map count as misses.
    """
    ranges = np.asarray(scan.ranges, dtype=float)
    angles = scan.angle_min + np.arange(ranges.size) * scan.angle_increment
    valid = (np.isfinite(ranges) & (ranges > max(scan.range_min, 0.0))
             & (ranges < min(scan.range_max, max_range_m)))
    if not valid.any():
        return 0.0, 0
    ranges, angles = ranges[valid], angles[valid] + laser_pose[2] + robot_pose[2]
    laser_x, laser_y = _transform(robot_pose, laser_pose[0], laser_pose[1])
    x = laser_x + ranges * np.cos(angles) - field.origin_x
    y = laser_y + ranges * np.sin(angles) - field.origin_y
    cos, sin = math.cos(field.origin_yaw), math.sin(field.origin_yaw)
    column = np.floor((cos * x + sin * y) / field.resolution).astype(int)
    row = np.floor((-sin * x + cos * y) / field.resolution).astype(int)
    height, width = field.distance.shape
    inside = (column >= 0) & (column < width) & (row >= 0) & (row < height)
    hits = np.zeros(ranges.size, dtype=bool)
    hits[inside] = field.distance[row[inside], column[inside]] <= hit_distance_m
    return float(hits.mean()), int(ranges.size)


def _transform(pose, x, y):
    cos, sin = math.cos(pose[2]), math.sin(pose[2])
    return pose[0] + cos * x - sin * y, pose[1] + sin * x + cos * y


def assess_scan(field, scan, laser_pose, robot_pose, *, hit_distance_m,
                max_range_m, minimum_ratio=0.95):
    """
    Distinguish limited map differences from a displaced AMCL hypothesis.

    At most 20% of returns may be excluded; the remaining walls must constrain
    translation and heading across six directions. Nearby alternatives are
    validation probes only: this function never generates a replacement pose.
    A flat score in any axis is ambiguous, not evidence of an environment change.
    """
    import cv2

    ratio, beams = match_ratio(field, scan, laser_pose, robot_pose,
                               hit_distance_m=hit_distance_m, max_range_m=max_range_m)
    ranges = np.asarray(scan.ranges, dtype=float)
    valid = (np.isfinite(ranges) & (ranges > max(scan.range_min, 0.0))
             & (ranges < min(scan.range_max, max_range_m)))
    angles = (scan.angle_min + np.arange(ranges.size) * scan.angle_increment)[valid]
    ranges = ranges[valid]
    if beams < 80:
        return ScanAssessment(ratio, False, 'insufficient_scan')
    lx = laser_pose[0] + ranges * np.cos(angles + laser_pose[2])
    ly = laser_pose[1] + ranges * np.sin(angles + laser_pose[2])
    keep = math.ceil(0.8 * beams)

    def distances(pose):
        x, y = _transform(pose, lx, ly)
        x, y = x - field.origin_x, y - field.origin_y
        c, s = math.cos(field.origin_yaw), math.sin(field.origin_yaw)
        columns = ((c*x + s*y) / field.resolution - 0.5).astype(np.float32)
        rows = ((-s*x + c*y) / field.resolution - 0.5).astype(np.float32)
        # Interpolate at cell centres to avoid whole-cell jumps in this diagnostic.
        return cv2.remap(field.distance.astype(np.float32, copy=False),
                         columns.reshape(1, -1), rows.reshape(1, -1),
                         cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                         borderValue=max_range_m).ravel()

    def residual(pose):
        d = distances(pose)
        return float(np.sqrt(np.mean(np.partition(d, keep-1)[:keep] ** 2)))

    d = distances(robot_pose)
    sectors = np.floor(np.mod(angles, 2*math.pi) / (math.pi/4)).astype(int)
    supported = sum(np.count_nonzero((sectors == sector) & (d <= hit_distance_m)) >= 5
                    and np.mean(d[sectors == sector] <= hit_distance_m) >= 0.5
                    for sector in range(8) if np.any(sectors == sector))
    error = residual(robot_pose)
    if supported < 6 or ratio < 0.8 * minimum_ratio or error > hit_distance_m * 0.55:
        return ScanAssessment(ratio, False, 'insufficient_wall_agreement', supported, error)
    # A coherent pose error improves many walls together. Localized new/removed
    # objects cannot justify accepting a pose with such a better nearby alignment.
    x, y, yaw = robot_pose
    best = min(residual((x+dx, y+dy, yaw+da))
               for dx in (-0.05, 0.0, 0.05) for dy in (-0.05, 0.0, 0.05)
               for da in (-0.05, 0.0, 0.05))
    improvement = error - best
    if improvement > 0.01:
        return ScanAssessment(ratio, False, 'pose_mismatch', supported, error, improvement)
    alternatives = [(x+dx, y+dy, yaw+da) for dx, dy, da in (
        (-0.2, 0., 0.), (0.2, 0., 0.), (0., -0.2, 0.), (0., 0.2, 0.),
        (0., 0., -0.15), (0., 0., 0.15))]
    if min(residual(p) for p in alternatives) - error < 0.015:
        return ScanAssessment(ratio, False, 'ambiguous_geometry', supported, error, improvement)
    reason = 'consistent' if ratio >= minimum_ratio else 'limited_map_difference'
    return ScanAssessment(ratio, True, reason, supported, error, improvement)


def alignment_is_unique(field, scan, laser_pose, robot_pose, *, max_range_m, check=lambda: None):
    """
    Reject a distinct, similarly plausible alignment elsewhere on the map.

    Coarse candidates cover known free space and all headings; eight distinct
    minima are refined. All returns contribute to the clipped loss, so a small
    asymmetric landmark cannot disappear just because it is in the trimmed tail.
    This only validates AMCL and never publishes a fitted pose.
    """
    import cv2

    ranges = np.asarray(scan.ranges, dtype=float)
    valid = (np.isfinite(ranges) & (ranges > max(scan.range_min, 0.0))
             & (ranges < min(scan.range_max, max_range_m)))
    angles = (scan.angle_min + np.arange(ranges.size)*scan.angle_increment)[valid]
    ranges = ranges[valid]
    if ranges.size < 80:
        return False
    points = np.array([laser_pose[0] + ranges*np.cos(angles+laser_pose[2]),
                       laser_pose[1] + ranges*np.sin(angles+laser_pose[2])])
    c, s = math.cos(field.origin_yaw), math.sin(field.origin_yaw)

    def costs(poses):
        yaw = poses[:, 2, None]
        x = poses[:, 0, None] + np.cos(yaw)*points[0] - np.sin(yaw)*points[1] - field.origin_x
        y = poses[:, 1, None] + np.sin(yaw)*points[0] + np.cos(yaw)*points[1] - field.origin_y
        columns = ((c*x+s*y)/field.resolution-.5).astype(np.float32)
        rows = ((-s*x+c*y)/field.resolution-.5).astype(np.float32)
        d = cv2.remap(field.distance.astype(np.float32, copy=False), columns, rows,
                      cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=max_range_m)
        return np.mean(np.minimum(d, .2)**2, axis=1)

    stride = max(1, round(.25/field.resolution))
    rows, columns = np.nonzero(field.known_free[::stride, ::stride])
    x, y = (columns*stride+.5)*field.resolution, (rows*stride+.5)*field.resolution
    positions = np.column_stack((field.origin_x+c*x-s*y, field.origin_y+s*x+c*y))
    headings = np.arange(-math.pi, math.pi, math.pi/12)
    poses = np.column_stack((np.repeat(positions, len(headings), axis=0),
                             np.tile(headings, len(positions))))
    if not len(poses):
        return False
    losses = []
    for start in range(0, len(poses), 128):
        check()
        losses.extend(costs(poses[start:start+128]))
    losses = np.asarray(losses)
    current = costs(np.asarray([robot_pose]))[0]

    def separated(candidates, pose, distance=.4, angle=.4):
        delta = candidates[:, 2]-pose[2]
        return ((np.linalg.norm(candidates[:, :2]-pose[:2], axis=1) > distance)
                | (np.abs(np.arctan2(np.sin(delta), np.cos(delta))) > angle))

    # Refine independent minima, not eight grid cells in the same basin.
    remaining = separated(poses, np.asarray(robot_pose))
    for _ in range(8):
        if not remaining.any():
            break
        index = np.argmin(np.where(remaining, losses, np.inf))
        best = poses[index].copy()
        remaining &= separated(poses, best, .5, .5)
        for step in (1., .5, .25):
            check()
            offsets = np.array([(dx, dy, da) for dx in (-.125, 0., .125)
                                for dy in (-.125, 0., .125)
                                for da in (-math.pi/24, 0., math.pi/24)]) * step
            candidates = best + offsets
            values = costs(candidates)
            best = candidates[np.argmin(values)]
        if (separated(np.asarray([best]), np.asarray(robot_pose))[0]
                and costs(np.asarray([best]))[0] <= current + .001):
            return False
    return True
