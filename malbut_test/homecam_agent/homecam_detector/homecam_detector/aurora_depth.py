"""Pure Aurora930 aligned-depth evidence extraction for fall verification."""

import math
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from .pose import PersonPose


TORSO_KEYPOINTS = frozenset(
    {'nose', 'left_shoulder', 'right_shoulder', 'left_hip', 'right_hip'}
)


@dataclass(frozen=True)
class CameraIntrinsics:
    """Pinhole intrinsics for one depth image."""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    def __post_init__(self) -> None:
        values = (self.fx, self.fy, self.cx, self.cy)
        if self.width < 1 or self.height < 1:
            raise ValueError('camera dimensions must be positive')
        if any(not math.isfinite(value) for value in values):
            raise ValueError('camera intrinsics must be finite')
        if self.fx <= 0 or self.fy <= 0:
            raise ValueError('camera focal lengths must be positive')


@dataclass(frozen=True)
class AuroraDepthObservation:
    """Provider-neutral summary, with no raw depth pixels retained."""

    aligned_to_rgb: bool
    stale: bool
    valid_torso_ratio: float
    torso_floor_distance_m: Optional[float]
    person_distance_m: Optional[float]
    sampled_torso_points: int

    @property
    def usable(self) -> bool:
        return (
            self.aligned_to_rgb
            and not self.stale
            and self.valid_torso_ratio >= 0.5
            and self.torso_floor_distance_m is not None
        )

    @property
    def floor_proximity_score(self) -> float:
        """Map 0..0.35m torso-floor distance onto a bounded cue."""
        if not self.usable or self.torso_floor_distance_m is None:
            return 0.0
        return max(0.0, min(1.0, 1.0 - self.torso_floor_distance_m / 0.35))

    def as_dict(self):
        return {
            'alignedToRgb': self.aligned_to_rgb,
            'stale': self.stale,
            'validTorsoRatio': round(self.valid_torso_ratio, 4),
            'torsoFloorDistanceM': self.torso_floor_distance_m,
            'personDistanceM': self.person_distance_m,
            'sampledTorsoPoints': self.sampled_torso_points,
            'usable': self.usable,
            'floorProximityScore': round(self.floor_proximity_score, 4),
        }


class AuroraDepthEvidenceExtractor:
    """Project torso keypoints into the robot base frame using aligned depth."""

    def __init__(
        self,
        *,
        camera_height_m: float = 0.091864,
        camera_pitch_rad: float = 0.0,
        minimum_depth_m: float = 0.3,
        maximum_depth_m: float = 3.0,
        maximum_stamp_delta_s: float = 0.15,
        keypoint_threshold: float = 0.5,
        patch_radius_px: int = 2,
    ) -> None:
        for name, value in (
            ('minimum_depth_m', minimum_depth_m),
            ('maximum_depth_m', maximum_depth_m),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and non-negative')
        if not math.isfinite(camera_height_m) or camera_height_m <= 0:
            raise ValueError('camera_height_m must be finite and positive')
        if (
            not math.isfinite(maximum_stamp_delta_s)
            or maximum_stamp_delta_s <= 0
        ):
            raise ValueError(
                'maximum_stamp_delta_s must be finite and positive'
            )
        if minimum_depth_m >= maximum_depth_m:
            raise ValueError('minimum_depth_m must be below maximum_depth_m')
        if not math.isfinite(camera_pitch_rad):
            raise ValueError('camera_pitch_rad must be finite')
        if not 0 < keypoint_threshold <= 1:
            raise ValueError('keypoint_threshold must be in (0, 1]')
        if patch_radius_px < 0 or patch_radius_px > 10:
            raise ValueError('patch_radius_px must be between 0 and 10')
        self._camera_height_m = camera_height_m
        self._camera_pitch_rad = camera_pitch_rad
        self._minimum_depth_m = minimum_depth_m
        self._maximum_depth_m = maximum_depth_m
        self._maximum_stamp_delta_s = maximum_stamp_delta_s
        self._keypoint_threshold = keypoint_threshold
        self._patch_radius_px = patch_radius_px

    def extract(
        self,
        *,
        depth_image: np.ndarray,
        intrinsics: CameraIntrinsics,
        pose: PersonPose,
        rgb_stamp_s: float,
        depth_stamp_s: float,
        aligned_to_rgb: bool,
        depth_scale_m: Optional[float] = None,
    ) -> AuroraDepthObservation:
        """Return compact evidence; never infer from unaligned pixels."""
        if not isinstance(depth_image, np.ndarray) or depth_image.ndim != 2:
            raise ValueError('depth_image must be a two-dimensional array')
        if depth_image.shape != (intrinsics.height, intrinsics.width):
            raise ValueError('depth_image dimensions do not match intrinsics')
        stale = (
            not math.isfinite(rgb_stamp_s)
            or not math.isfinite(depth_stamp_s)
            or abs(rgb_stamp_s - depth_stamp_s) > self._maximum_stamp_delta_s
        )
        if not aligned_to_rgb or stale:
            return AuroraDepthObservation(
                aligned_to_rgb=aligned_to_rgb,
                stale=stale,
                valid_torso_ratio=0.0,
                torso_floor_distance_m=None,
                person_distance_m=None,
                sampled_torso_points=0,
            )
        scale = self._depth_scale(depth_image, depth_scale_m)
        eligible = [
            point
            for point in pose.keypoints
            if point.name in TORSO_KEYPOINTS
            and point.confidence >= self._keypoint_threshold
        ]
        signed_floor_distances = []
        forward_distances = []
        for point in eligible:
            pixel = self._normalized_pixel(
                point.x,
                point.y,
                intrinsics.width,
                intrinsics.height,
            )
            depth_m = self._sample_depth(depth_image, pixel, scale)
            if depth_m is None:
                continue
            y_optical = (pixel[1] - intrinsics.cy) * depth_m / intrinsics.fy
            height = (
                self._camera_height_m
                - math.sin(self._camera_pitch_rad) * depth_m
                - math.cos(self._camera_pitch_rad) * y_optical
            )
            signed_floor_distances.append(height)
            forward_distances.append(depth_m)
        sampled = len(signed_floor_distances)
        valid_ratio = sampled / len(eligible) if eligible else 0.0
        torso_floor_distance = None
        if signed_floor_distances:
            median_height = float(np.median(signed_floor_distances))
            if median_height < -0.02:
                raise ValueError(
                    'projected torso is below floor; check camera calibration'
                )
            torso_floor_distance = max(0.0, median_height)
        return AuroraDepthObservation(
            aligned_to_rgb=True,
            stale=False,
            valid_torso_ratio=valid_ratio,
            torso_floor_distance_m=(
                torso_floor_distance
            ),
            person_distance_m=(
                float(np.median(forward_distances))
                if forward_distances
                else None
            ),
            sampled_torso_points=sampled,
        )

    @staticmethod
    def _normalized_pixel(
        x: float,
        y: float,
        width: int,
        height: int,
    ) -> Tuple[int, int]:
        column = min(width - 1, max(0, int(round(x * (width - 1)))))
        row = min(height - 1, max(0, int(round(y * (height - 1)))))
        return column, row

    def _sample_depth(
        self,
        image: np.ndarray,
        pixel: Tuple[int, int],
        scale: float,
    ) -> Optional[float]:
        column, row = pixel
        radius = self._patch_radius_px
        patch = image[
            max(0, row - radius):min(image.shape[0], row + radius + 1),
            max(0, column - radius):min(image.shape[1], column + radius + 1),
        ].astype(np.float64)
        values = patch[np.isfinite(patch)] * scale
        valid = values[
            (values >= self._minimum_depth_m)
            & (values <= self._maximum_depth_m)
        ]
        if valid.size == 0:
            return None
        return float(np.median(valid))

    @staticmethod
    def _depth_scale(image: np.ndarray, configured: Optional[float]) -> float:
        if configured is not None:
            if not math.isfinite(configured) or configured <= 0:
                raise ValueError('depth_scale_m must be positive')
            return configured
        if np.issubdtype(image.dtype, np.integer):
            return 0.001
        if np.issubdtype(image.dtype, np.floating):
            return 1.0
        raise ValueError(
            'depth_image dtype must be integer millimeters or float meters'
        )
