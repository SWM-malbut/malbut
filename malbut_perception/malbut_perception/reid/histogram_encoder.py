"""Dependency-free appearance fallback based on HSV color distribution."""

from typing import List

import cv2
import numpy as np

from malbut_perception.detector.base import ImageDetection

from .base import AppearanceFeature, PersonAppearanceEncoder, normalized_feature
from .crop import person_crop


class HistogramPersonEncoder(PersonAppearanceEncoder):
    """Describe coarse person appearance when no Re-ID model is installed."""

    def __init__(self, minimum_width: int = 16, minimum_height: int = 32):
        """Configure the smallest crop that can produce a descriptor."""
        self._minimum_width = minimum_width
        self._minimum_height = minimum_height

    def encode(
        self,
        bgr_image: np.ndarray,
        detections: List[ImageDetection],
    ) -> List[AppearanceFeature]:
        """Build normalized upper/lower-body HSV histograms."""
        features: List[AppearanceFeature] = []
        for detection in detections:
            crop = person_crop(
                bgr_image,
                detection.bbox,
                self._minimum_width,
                self._minimum_height,
            )
            if crop is None:
                features.append(None)
                continue
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            sections = np.array_split(hsv, 2, axis=0)
            histograms = []
            for section in sections:
                histogram = cv2.calcHist(
                    [section], [0, 1], None, [16, 16], [0, 180, 0, 256]
                )
                histograms.append(histogram.reshape(-1))
            features.append(normalized_feature(np.concatenate(histograms)))
        return features
