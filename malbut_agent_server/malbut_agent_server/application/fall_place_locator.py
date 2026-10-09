"""Map position of a Cloud box from aligned depth and the camera pose; not identity.

The ROS adapter stores one geometry per accepted RGB frame under the same
monitor time as the frame. Missing depth, intrinsics or a map pose leaves the
place unknown, and callers then fall back to image positions. Bounds are
development values, not measured accuracy.
"""

from collections import OrderedDict
from dataclasses import dataclass
import math
from typing import Optional, Tuple

import numpy as np

MIN_DEPTH_M = 0.3
MAX_DEPTH_M = 5.0
# Centre part of the box: edges often hit the floor or the wall behind.
ROI_SCALE = 0.45
MIN_SAMPLES = 5


@dataclass(frozen=True)
class FrameGeometry:
    """One RGB frame's depth (mm, aligned to RGB, may be downsampled) and pose."""

    depth_mm: np.ndarray
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    # map <- camera optical frame (REP-103: z forward, x right, y down).
    translation: Tuple[float, float, float]
    rotation: Tuple[float, float, float, float]

    def __post_init__(self):
        if (not isinstance(self.depth_mm, np.ndarray) or self.depth_mm.ndim != 2
                or self.depth_mm.dtype != np.uint16 or 0 in self.depth_mm.shape):
            raise ValueError('depth must be a 2D uint16 millimetre image')
        values = (self.fx, self.fy, self.cx, self.cy, *self.translation, *self.rotation)
        if (len(self.translation) != 3 or len(self.rotation) != 4
                or not all(math.isfinite(v) for v in values)
                or self.fx <= 0 or self.fy <= 0 or self.width < 1 or self.height < 1):
            raise ValueError('invalid camera geometry')
        if abs(math.sqrt(sum(v * v for v in self.rotation)) - 1) > 1e-3:
            raise ValueError('rotation must be a unit quaternion')


def _rotate(q, v):
    x, y, z, w = q
    # v' = v + 2w(q x v) + 2 q x (q x v)
    cx, cy, cz = y * v[2] - z * v[1], z * v[0] - x * v[2], x * v[1] - y * v[0]
    dx, dy, dz = y * cz - z * cy, z * cx - x * cz, x * cy - y * cx
    return v[0] + 2 * (w * cx + dx), v[1] + 2 * (w * cy + dy), v[2] + 2 * (w * cz + dz)


def mount_corrected(translation, rotation, *, pitch_rad=0.0, roll_rad=0.0, height_m=0.0):
    """
    Return (translation, rotation) of map <- camera optical with the measured mount.

    The robot model puts the camera level; measured against the floor on
    2026-10-09 it looks 1.6 degrees down with its left side 1.6 degrees low and
    sits 0.4 cm lower. A real optical ray is turned into the model's optical
    frame (down about x, then right side down about z), and the camera lowered.
    """
    pitch = (math.sin(-pitch_rad / 2), 0.0, 0.0, math.cos(-pitch_rad / 2))
    roll = (0.0, 0.0, math.sin(roll_rad / 2), math.cos(roll_rad / 2))
    correction = _multiply(roll, pitch)
    return ((translation[0], translation[1], translation[2] + height_m),
            _multiply(tuple(rotation), correction))


def _multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def map_point(geometry: FrameGeometry, box) -> Optional[Tuple[float, float]]:
    """Median depth inside the box centre, projected through the box centre."""
    point = map_point3d(geometry, box)
    return None if point is None else point[:2]


def map_point3d(geometry: FrameGeometry, box) -> Optional[Tuple[float, float, float]]:
    """The same point with its height above the map plane (the floor)."""
    left, top, right, bottom = box
    rows, cols = geometry.depth_mm.shape
    centre_x, centre_y = (left + right) / 2, (top + bottom) / 2
    half_w, half_h = (right - left) * ROI_SCALE / 2, (bottom - top) * ROI_SCALE / 2
    x0 = max(0, min(cols - 1, math.floor((centre_x - half_w) * cols)))
    x1 = max(x0 + 1, min(cols, math.ceil((centre_x + half_w) * cols)))
    y0 = max(0, min(rows - 1, math.floor((centre_y - half_h) * rows)))
    y1 = max(y0 + 1, min(rows, math.ceil((centre_y + half_h) * rows)))
    depth = geometry.depth_mm[y0:y1, x0:x1].astype(np.float64) / 1000
    valid = depth[(depth >= MIN_DEPTH_M) & (depth <= MAX_DEPTH_M)]
    if valid.size < MIN_SAMPLES:
        return None
    z = float(np.median(valid))
    u, v = centre_x * geometry.width, centre_y * geometry.height
    camera = ((u - geometry.cx) * z / geometry.fx, (v - geometry.cy) * z / geometry.fy, z)
    x, y, z = _rotate(geometry.rotation, camera)
    return (x + geometry.translation[0], y + geometry.translation[1],
            z + geometry.translation[2])


class FallPlaceLocator:
    """Bounded per-frame geometry; Cloud replies arrive up to ~20 s after capture."""

    def __init__(self, *, retention_s=60.0, max_frames=400):
        self.retention_s, self.max_frames = retention_s, max_frames
        self._frames = OrderedDict()

    def add(self, captured_at, geometry: FrameGeometry):
        if not isinstance(geometry, FrameGeometry):
            raise ValueError('invalid frame geometry')
        if self._frames and captured_at <= next(reversed(self._frames)):
            return
        self._frames[captured_at] = geometry
        while (len(self._frames) > self.max_frames
               or next(iter(self._frames)) < captured_at - self.retention_s):
            self._frames.popitem(last=False)

    def clear(self):
        self._frames.clear()

    def locate(self, captured_at, box):
        geometry = self._frames.get(captured_at)
        return map_point(geometry, box) if geometry is not None else None

    def locate_near(self, observed_at, box, *, tolerance_s=0.25):
        """Pose runs on its own frames: use the nearest RGB geometry within tolerance."""
        point = self.locate3d_near(observed_at, box, tolerance_s=tolerance_s)
        return None if point is None else point[:2]

    def locate3d(self, captured_at, box):
        geometry = self._frames.get(captured_at)
        return map_point3d(geometry, box) if geometry is not None else None

    def locate3d_near(self, observed_at, box, *, tolerance_s=0.25):
        if not self._frames:
            return None
        stamp = min(self._frames, key=lambda t: abs(t - observed_at))
        if abs(stamp - observed_at) > tolerance_s:
            return None
        return map_point3d(self._frames[stamp], box)
