"""Short-lived evidence for a keepout exit, independent of Action success history."""

import math
from pathlib import Path

import cv2
import numpy as np
import yaml

from .scan_match import distance_field, match_ratio


class LocalizationConfidence:
    """Require three fresh, broad scans matching the exact selected saved map."""

    def __init__(self, map_file=None):
        self.field = self.expected = None
        self.reset()
        if not map_file:
            return
        try:
            path = Path(map_file).expanduser()
            metadata = yaml.safe_load(path.read_text())
            if metadata.get('mode', 'trinary') != 'trinary':
                return
            image_path = path.parent / metadata['image']
            image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                return
            origin = tuple(float(v) for v in metadata['origin'])
            resolution = float(metadata['resolution'])
            if (len(origin) != 3 or not all(math.isfinite(v) for v in origin)
                    or origin[2] != 0 or not math.isfinite(resolution) or resolution <= 0):
                return
            occupied = image / 255.0 if metadata.get('negate', 0) else (255 - image) / 255.0
            grid = np.full(image.shape, -1, dtype=np.int8)
            grid[occupied > float(metadata.get('occupied_thresh', 0.65))] = 100
            grid[occupied < float(metadata.get('free_thresh', 0.196))] = 0
            self.expected = np.flipud(grid).copy()
            height, width = grid.shape
            self.field = distance_field(width, height, resolution, origin, self.expected)
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, cv2.error):
            self.field = self.expected = None

    def reset(self):
        self.last_stamp = 0.0
        self.good_scans = 0

    def check(self, grid, scan, covariance, robot_pose, laser_pose, now):
        """Require fresh scan evidence, not covariance alone or replayed scans."""
        stamp = scan.header.stamp.sec + scan.header.stamp.nanosec / 1e9
        if not self._valid(grid, scan, covariance, robot_pose, laser_pose, now, stamp):
            self.reset()
            return False
        ratio, beams = match_ratio(
            self.field, scan, laser_pose, robot_pose, hit_distance_m=0.10, max_range_m=8.0)
        if beams < 60 or ratio < 0.95:
            self.reset()
            return False
        if stamp < self.last_stamp:
            self.reset()
        if stamp - self.last_stamp > 0.5:
            self.good_scans = 0
        if stamp > self.last_stamp:
            self.good_scans += 1
        self.last_stamp = stamp
        return self.good_scans >= 3

    def _valid(self, grid, scan, covariance, robot, laser, now, stamp):
        if (self.field is None or grid is None or grid.header.frame_id != 'map'
                or stamp <= 0 or not 0 <= now - stamp <= 0.5
                or not all(math.isfinite(v) for v in (*robot, *laser))
                or len(covariance) != 36 or not all(math.isfinite(v) for v in covariance)):
            return False
        matrix = np.asarray(covariance).reshape(6, 6)[np.ix_([0, 1, 5], [0, 1, 5])]
        if (not np.allclose(matrix, matrix.T) or np.linalg.eigvalsh(matrix).min() < -1e-12
                or any(not 0 <= covariance[i] <= 0.04 for i in (0, 7, 35))):
            return False
        info = grid.info
        q = info.origin.orientation
        geometry = (info.resolution, info.origin.position.x, info.origin.position.y,
                    q.x, q.y, q.z, q.w)
        if (not all(math.isfinite(v) for v in geometry)
                or self.expected.shape != (info.height, info.width)
                or abs(info.resolution - self.field.resolution) > 1e-6
                or abs(info.origin.position.x - self.field.origin_x) > 1e-6
                or abs(info.origin.position.y - self.field.origin_y) > 1e-6
                or any(abs(v) > 1e-6 for v in (q.x, q.y, q.z))
                or not math.isfinite(q.w) or abs(abs(q.w) - 1) > 1e-6
                or not np.array_equal(self.expected.ravel(), grid.data)):
            return False
        # A few returns along a single wall cannot establish a reliable position.
        ranges = np.asarray(scan.ranges)
        if (not math.isfinite(scan.angle_increment) or scan.angle_increment == 0
                or not math.isfinite(scan.angle_min)
                or not math.isfinite(scan.range_min) or scan.range_min < 0
                or not math.isfinite(scan.range_max) or scan.range_max <= scan.range_min):
            return False
        valid = np.isfinite(ranges) & (ranges > max(scan.range_min, 0)) & (
            ranges < min(scan.range_max, 8.0))
        angles = np.sort((scan.angle_min + np.flatnonzero(valid) * scan.angle_increment)
                         % (2 * math.pi))
        if len(angles) < 60:
            return False
        gaps = np.diff(np.concatenate((angles, [angles[0] + 2 * math.pi])))
        return 2 * math.pi - gaps.max() >= math.pi
