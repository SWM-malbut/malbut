"""Pinhole projection from aligned depth pixels to camera coordinates."""

from dataclasses import dataclass
import math
from typing import Sequence, Tuple

from malbut_perception.detector.base import BoundingBox


@dataclass(frozen=True)
class CameraIntrinsics:
    """Minimal rectified pinhole camera model."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    def __post_init__(self) -> None:
        """Validate the pinhole camera model."""
        values = (self.fx, self.fy, self.cx, self.cy)
        if not all(math.isfinite(value) for value in values):
            raise ValueError('camera intrinsics must be finite')
        if self.fx <= 0.0 or self.fy <= 0.0:
            raise ValueError('camera focal lengths must be positive')
        if self.width <= 0 or self.height <= 0:
            raise ValueError('camera dimensions must be positive')

    @classmethod
    def from_camera_matrix(
        cls,
        matrix: Sequence[float],
        width: int,
        height: int,
    ):
        """Construct from a ROS CameraInfo row-major K matrix."""
        if len(matrix) != 9:
            raise ValueError('camera matrix must contain nine values')
        return cls(
            fx=float(matrix[0]),
            fy=float(matrix[4]),
            cx=float(matrix[2]),
            cy=float(matrix[5]),
            width=int(width),
            height=int(height),
        )


def project_pixel(
    intrinsics: CameraIntrinsics,
    u: float,
    v: float,
    depth_m: float,
) -> Tuple[float, float, float]:
    """Project into ROS optical coordinates: x right, y down, z forward."""
    if not math.isfinite(depth_m) or depth_m <= 0.0:
        raise ValueError('depth must be positive and finite')
    x = (u - intrinsics.cx) * depth_m / intrinsics.fx
    y = (v - intrinsics.cy) * depth_m / intrinsics.fy
    return x, y, depth_m


def projected_box_size(
    intrinsics: CameraIntrinsics,
    bbox: BoundingBox,
    depth_m: float,
    thickness_m: float = 0.35,
) -> Tuple[float, float, float]:
    """Approximate camera-axis box size from pixels and median depth."""
    width_m = bbox.width * depth_m / intrinsics.fx
    height_m = bbox.height * depth_m / intrinsics.fy
    return width_m, height_m, thickness_m
