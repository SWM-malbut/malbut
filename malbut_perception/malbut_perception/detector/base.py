"""Detector-neutral image detection types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
from typing import List, Tuple

import numpy as np


@dataclass(frozen=True)
class BoundingBox:
    """Axis-aligned image box in left, top, right, bottom coordinates."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        """Validate box geometry."""
        values = (self.left, self.top, self.right, self.bottom)
        if not all(math.isfinite(value) for value in values):
            raise ValueError('bounding box coordinates must be finite')
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError(
                'bounding box must have positive width and height'
            )

    @property
    def width(self) -> float:
        """Return box width in pixels."""
        return self.right - self.left

    @property
    def height(self) -> float:
        """Return box height in pixels."""
        return self.bottom - self.top

    @property
    def center(self) -> Tuple[float, float]:
        """Return box center in pixels."""
        return (
            (self.left + self.right) / 2.0,
            (self.top + self.bottom) / 2.0,
        )

    @property
    def area(self) -> float:
        """Return box area in square pixels."""
        return self.width * self.height

    def clipped(self, image_width: int, image_height: int):
        """Clip a box to an image, or return None if it becomes empty."""
        left = min(max(self.left, 0.0), float(image_width))
        top = min(max(self.top, 0.0), float(image_height))
        right = min(max(self.right, 0.0), float(image_width))
        bottom = min(max(self.bottom, 0.0), float(image_height))
        if right <= left or bottom <= top:
            return None
        return BoundingBox(left, top, right, bottom)

    def translated(self, dx: float, dy: float):
        """Return a translated copy of this box."""
        return BoundingBox(
            self.left + dx,
            self.top + dy,
            self.right + dx,
            self.bottom + dy,
        )

    def iou(self, other) -> float:
        """Return intersection-over-union with another box."""
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        union = self.area + other.area - intersection
        return intersection / union if union > 0.0 else 0.0


@dataclass(frozen=True)
class ImageDetection:
    """One classified bounding box produced from an RGB image."""

    bbox: BoundingBox
    score: float
    class_id: str = 'person'

    def __post_init__(self) -> None:
        """Validate the classification fields."""
        if not math.isfinite(self.score) or not 0.0 <= self.score <= 1.0:
            raise ValueError('detection score must be in [0, 1]')
        if not self.class_id:
            raise ValueError('class_id must not be empty')


class PersonDetector(ABC):
    """Interface implemented by image-only person detector backends."""

    @abstractmethod
    def detect(self, bgr_image: np.ndarray) -> List[ImageDetection]:
        """Detect people using only pixels from one BGR image."""
