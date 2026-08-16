"""Robust depth estimation inside an RGB detection box."""

from dataclasses import dataclass
import math
from typing import Optional

import numpy as np

from malbut_perception.detector.base import BoundingBox


@dataclass(frozen=True)
class DepthEstimate:
    """Median target depth and robust dispersion in metres."""

    distance_m: float
    dispersion_m: float
    sample_count: int


def depth_image_to_metres(
    depth_image: np.ndarray,
    encoding: str,
    fallback_scale: float = 1.0,
) -> np.ndarray:
    """Convert common ROS depth encodings to floating-point metres."""
    if depth_image.ndim != 2:
        raise ValueError('depth image must be single channel')
    normalized_encoding = encoding.upper()
    if normalized_encoding in {'16UC1', 'MONO16'}:
        scale = 0.001
    elif normalized_encoding == '32FC1':
        scale = 1.0
    else:
        if not math.isfinite(fallback_scale) or fallback_scale <= 0.0:
            raise ValueError('fallback depth scale must be positive')
        scale = fallback_scale
    return depth_image.astype(np.float32, copy=False) * scale


def estimate_roi_depth(
    depth_image: np.ndarray,
    encoding: str,
    bbox: BoundingBox,
    roi_scale: float = 0.45,
    minimum_depth_m: float = 0.30,
    maximum_depth_m: float = 3.0,
    minimum_samples: int = 20,
    fallback_scale: float = 1.0,
) -> Optional[DepthEstimate]:
    """Estimate person distance from the central box region using a median."""
    if not 0.0 < roi_scale <= 1.0:
        raise ValueError('roi_scale must be in (0, 1]')
    if minimum_depth_m < 0.0 or maximum_depth_m <= minimum_depth_m:
        raise ValueError('depth limits are invalid')
    if minimum_samples < 1:
        raise ValueError('minimum_samples must be positive')

    depth_metres = depth_image_to_metres(
        depth_image,
        encoding,
        fallback_scale,
    )
    image_height, image_width = depth_metres.shape
    clipped = bbox.clipped(image_width, image_height)
    if clipped is None:
        return None
    center_x, center_y = clipped.center
    half_width = clipped.width * roi_scale / 2.0
    half_height = clipped.height * roi_scale / 2.0
    left = max(0, int(math.floor(center_x - half_width)))
    right = min(image_width, int(math.ceil(center_x + half_width)))
    top = max(0, int(math.floor(center_y - half_height)))
    bottom = min(image_height, int(math.ceil(center_y + half_height)))
    if right <= left or bottom <= top:
        return None
    samples = depth_metres[top:bottom, left:right].reshape(-1)
    valid = samples[
        np.isfinite(samples)
        & (samples >= minimum_depth_m)
        & (samples <= maximum_depth_m)
    ]
    if valid.size < minimum_samples:
        return None
    distance = float(np.median(valid))
    dispersion = float(np.median(np.abs(valid - distance)))
    return DepthEstimate(distance, dispersion, int(valid.size))
