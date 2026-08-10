"""Dependency-free OpenCV HOG fallback person detector."""

import math
from typing import List

import cv2
import numpy as np

from .base import BoundingBox, ImageDetection, PersonDetector


def _nms(detections: List[ImageDetection], threshold: float):
    """Apply deterministic score-ordered non-maximum suppression."""
    remaining = sorted(detections, key=lambda item: item.score, reverse=True)
    kept: List[ImageDetection] = []
    while remaining:
        selected = remaining.pop(0)
        kept.append(selected)
        remaining = [
            candidate
            for candidate in remaining
            if selected.bbox.iou(candidate.bbox) < threshold
        ]
    return kept


class HogPersonDetector(PersonDetector):
    """Use OpenCV's built-in pedestrian detector when no DNN is installed."""

    def __init__(
        self,
        hit_threshold: float = 0.0,
        nms_threshold: float = 0.45,
        scale: float = 1.05,
    ) -> None:
        """Configure the built-in OpenCV pedestrian detector."""
        if not math.isfinite(hit_threshold):
            raise ValueError('hit_threshold must be finite')
        if not 0.0 < nms_threshold < 1.0:
            raise ValueError('nms_threshold must be in (0, 1)')
        if scale <= 1.0:
            raise ValueError('scale must be above 1.0')
        self._hit_threshold = hit_threshold
        self._nms_threshold = nms_threshold
        self._scale = scale
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, bgr_image: np.ndarray) -> List[ImageDetection]:
        """Return HOG pedestrian boxes from the supplied camera frame."""
        if not isinstance(bgr_image, np.ndarray) or bgr_image.size == 0:
            raise ValueError('detector input must be a non-empty image')
        if bgr_image.ndim != 3 or bgr_image.shape[2] != 3:
            raise ValueError('detector input must be a BGR image')
        boxes, weights = self._hog.detectMultiScale(
            bgr_image,
            hitThreshold=self._hit_threshold,
            winStride=(8, 8),
            padding=(8, 8),
            scale=self._scale,
        )
        height, width = bgr_image.shape[:2]
        detections: List[ImageDetection] = []
        for (left, top, box_width, box_height), weight in zip(boxes, weights):
            box = BoundingBox(
                float(left),
                float(top),
                float(left + box_width),
                float(top + box_height),
            ).clipped(width, height)
            if box is None:
                continue
            confidence = 1.0 / (1.0 + math.exp(-float(weight)))
            detections.append(ImageDetection(box, confidence))
        return _nms(detections, self._nms_threshold)
