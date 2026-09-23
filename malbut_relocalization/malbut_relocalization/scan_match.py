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


def distance_field(width, height, resolution, origin, data):
    """Build the field from OccupancyGrid geometry and row-major data."""
    import cv2

    grid = np.asarray(data, dtype=np.int16).reshape(height, width)
    free = (grid < OCCUPIED).astype(np.uint8)
    if not free.all():
        distance = cv2.distanceTransform(free, cv2.DIST_L2, 5) * resolution
    else:
        distance = np.full(grid.shape, np.inf)
    return DistanceField(distance, resolution, *origin)


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
