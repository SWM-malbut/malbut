"""Person-crop extraction shared by appearance encoders."""

from typing import Optional

import numpy as np

from malbut_perception.detector.base import BoundingBox


def person_crop(
    bgr_image: np.ndarray,
    bbox: BoundingBox,
    minimum_width: int,
    minimum_height: int,
) -> Optional[np.ndarray]:
    """Clip and extract a sufficiently large person crop."""
    if not isinstance(bgr_image, np.ndarray) or bgr_image.ndim != 3:
        raise ValueError('appearance input must be a color image')
    image_height, image_width = bgr_image.shape[:2]
    clipped = bbox.clipped(image_width, image_height)
    if clipped is None:
        return None
    left = max(0, int(round(clipped.left)))
    top = max(0, int(round(clipped.top)))
    right = min(image_width, int(round(clipped.right)))
    bottom = min(image_height, int(round(clipped.bottom)))
    if right - left < minimum_width or bottom - top < minimum_height:
        return None
    return bgr_image[top:bottom, left:right]
