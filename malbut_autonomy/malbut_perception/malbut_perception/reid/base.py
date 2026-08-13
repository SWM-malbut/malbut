"""Interfaces and helpers for person appearance descriptors."""

from abc import ABC, abstractmethod
from typing import List, Optional

import numpy as np

from malbut_perception.detector.base import ImageDetection


AppearanceFeature = Optional[np.ndarray]


def normalized_feature(feature: np.ndarray) -> np.ndarray:
    """Return a finite one-dimensional L2-normalized descriptor."""
    array = np.asarray(feature, dtype=np.float32).reshape(-1)
    if array.size == 0 or not np.all(np.isfinite(array)):
        raise ValueError('appearance feature must be finite and non-empty')
    norm = float(np.linalg.norm(array))
    if norm <= 1e-12:
        raise ValueError('appearance feature has zero norm')
    return array / norm


class PersonAppearanceEncoder(ABC):
    """Create visual identity descriptors from detected person crops."""

    @abstractmethod
    def encode(
        self,
        bgr_image: np.ndarray,
        detections: List[ImageDetection],
    ) -> List[AppearanceFeature]:
        """Return one optional descriptor for each input detection."""
