"""Small CPU-only image-difference detector used alongside optional YOLO."""

from typing import Optional

import cv2
import numpy as np


class FrameMotionDetector:
    """Detect large changed regions while continuously adapting its reference."""

    def __init__(self, area_ratio: float = 0.02) -> None:
        if not 0.0 < area_ratio <= 1.0:
            raise ValueError("area_ratio must be in (0, 1]")
        self._area_ratio = area_ratio
        self._background: Optional[np.ndarray] = None

    def reset(self) -> None:
        """Forget the background frame."""
        self._background = None

    def detect(self, bgr_frame: np.ndarray) -> bool:
        """Update the background and return whether enough pixels changed."""
        gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        if self._background is None:
            self._background = gray.astype(np.float32)
            return False

        background_u8 = cv2.convertScaleAbs(self._background)
        difference = cv2.absdiff(background_u8, gray)
        _, mask = cv2.threshold(difference, 25, 255, cv2.THRESH_BINARY)
        mask = cv2.dilate(mask, None, iterations=2)
        changed_ratio = float(cv2.countNonZero(mask)) / float(mask.size)
        cv2.accumulateWeighted(gray, self._background, 0.05)
        return changed_ratio >= self._area_ratio
