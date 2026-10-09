"""
Score how well one LiDAR scan fits the saved map at a candidate pose.

This is AMCL's likelihood-field idea reduced to one number: the share of scan
endpoints that land within a small distance of a mapped obstacle. A robot moved
while powered off puts most endpoints in free space at its old pose.

refine() then fits the scan to the map near a pose, as LiDAR robot vacuums do
after a rough search: AMCL's estimate after one turn is a few centimetres and
degrees off, enough to drop the score at a correct place (2026-10-09).
"""

from dataclasses import dataclass
import math

import numpy as np

# map_server's trinary occupied value; occupied_thresh 0.65 also yields 100.
OCCUPIED = 65
# Width of the smooth fit score: an endpoint this far from a wall counts 61%.
FIT_SIGMA_M = 0.1
FAR_M = 100.0


@dataclass(frozen=True)
class DistanceField:
    """Distance from each map cell to the nearest occupied cell, in meters."""

    distance: np.ndarray
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    # Signed distance to the nearest obstacle face: positive in free space,
    # negative inside obstacles, zero halfway between a free and an occupied
    # cell. A fit against it peaks at a wall's face, not anywhere inside it.
    surface: np.ndarray = None


def distance_field(width, height, resolution, origin, data):
    """Build the field from OccupancyGrid geometry and row-major data."""
    import cv2

    grid = np.asarray(data, dtype=np.int16).reshape(height, width)
    occupied = grid >= OCCUPIED
    if not occupied.any():
        return DistanceField(np.full(grid.shape, np.inf), resolution, *origin)
    distance = cv2.distanceTransform((~occupied).astype(np.uint8), cv2.DIST_L2, 5) * resolution
    if occupied.all():
        return DistanceField(distance, resolution, *origin)
    inside = cv2.distanceTransform(occupied.astype(np.uint8), cv2.DIST_L2, 5) * resolution
    surface = np.where(occupied, resolution / 2 - inside, distance - resolution / 2)
    return DistanceField(distance, resolution, *origin, surface=surface)


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


def refine(field, scan, laser_pose, robot_pose, *, max_range_m,
           linear_m=0.3, angular_rad=math.radians(8.0)):
    """
    Return the pose within the window around robot_pose where the scan fits best.

    Every pose on a grid is scored by how close the scan endpoints land to
    mapped obstacles: first 5 cm and 1 degree steps over the whole window, then
    1 cm and 0.2 degree steps around the best one. Ties keep robot_pose.
    """
    points = _base_points(scan, laser_pose, max_range_m)
    if points is None or field.surface is None:
        return robot_pose
    best = tuple(float(value) for value in robot_pose)
    for linear, linear_step, angular, angular_step in (
            (linear_m, 0.05, angular_rad, math.radians(1.0)),
            (0.05, 0.01, math.radians(1.0), math.radians(0.2))):
        best = _grid_search(field, points, best, (linear, linear_step),
                            (angular, angular_step))
    return best[0], best[1], math.atan2(math.sin(best[2]), math.cos(best[2]))


def _base_points(scan, laser_pose, max_range_m):
    """Return valid scan endpoints in the base frame as an (N, 2) array, or None."""
    ranges = np.asarray(scan.ranges, dtype=float)
    angles = scan.angle_min + np.arange(ranges.size) * scan.angle_increment
    valid = (np.isfinite(ranges) & (ranges > max(scan.range_min, 0.0))
             & (ranges < min(scan.range_max, max_range_m)))
    if not valid.any():
        return None
    ranges, angles = ranges[valid], angles[valid] + laser_pose[2]
    return np.column_stack((laser_pose[0] + ranges * np.cos(angles),
                            laser_pose[1] + ranges * np.sin(angles)))


def _grid_search(field, points, center, linear, angular):
    steps = int(round(linear[0] / linear[1]))
    offsets = np.arange(-steps, steps + 1) * linear[1]
    dx, dy = (grid.ravel() for grid in np.meshgrid(offsets, offsets, indexing='ij'))
    steps = int(round(angular[0] / angular[1]))
    turns = np.arange(-steps, steps + 1) * angular[1]
    cos0, sin0 = math.cos(field.origin_yaw), math.sin(field.origin_yaw)
    best, best_score = center, None
    for turn in sorted(turns, key=abs):
        yaw = center[2] + turn
        cos, sin = math.cos(yaw), math.sin(yaw)
        x = (center[0] - field.origin_x + cos * points[:, 0] - sin * points[:, 1])[None, :]
        y = (center[1] - field.origin_y + sin * points[:, 0] + cos * points[:, 1])[None, :]
        x, y = x + dx[:, None], y + dy[:, None]
        surface = _interpolate(field.surface, (cos0 * x + sin0 * y) / field.resolution - 0.5,
                               (-sin0 * x + cos0 * y) / field.resolution - 0.5)
        scores = np.exp(-0.5 * np.square(surface / FIT_SIGMA_M)).mean(axis=1)
        if best_score is None:
            # The window centre is scored first so that only a better fit moves it.
            best_score = scores[(dx == 0) & (dy == 0)][0]
        index = int(np.argmax(scores))
        if scores[index] > best_score + 1e-9:
            best_score = scores[index]
            best = (center[0] + float(dx[index]), center[1] + float(dy[index]), yaw)
    return best


def _interpolate(values, column, row):
    """Bilinearly sample a grid at fractional cell-centre coordinates; outside is far."""
    height, width = values.shape
    left, bottom = np.floor(column).astype(int), np.floor(row).astype(int)
    right_weight, top_weight = column - left, row - bottom
    total = np.zeros(column.shape)
    outside = np.zeros(column.shape)
    for dc, dr, weight in ((0, 0, (1 - right_weight) * (1 - top_weight)),
                           (1, 0, right_weight * (1 - top_weight)),
                           (0, 1, (1 - right_weight) * top_weight),
                           (1, 1, right_weight * top_weight)):
        c, r = left + dc, bottom + dr
        inside = (c >= 0) & (c < width) & (r >= 0) & (r < height)
        total[inside] += weight[inside] * values[r[inside], c[inside]]
        outside[~inside] += weight[~inside]
    # Beyond the map an endpoint fits nothing: far enough to score zero.
    return total + outside * FAR_M


def _transform(pose, x, y):
    cos, sin = math.cos(pose[2]), math.sin(pose[2])
    return pose[0] + cos * x - sin * y, pose[1] + sin * x + cos * y
