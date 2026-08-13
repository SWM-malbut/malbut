"""Tracker-neutral types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Sequence

from malbut_perception.reid.base import AppearanceFeature

from malbut_perception.detector.base import ImageDetection


@dataclass(frozen=True)
class TrackedDetection:
    """A current-frame detection with a short-lived image track ID."""

    track_id: int
    detection: ImageDetection
    age: int
    hits: int


class PersonTracker(ABC):
    """Interface for assigning stable IDs to image detections."""

    @abstractmethod
    def update(
        self,
        detections: List[ImageDetection],
        appearance_features: Optional[Sequence[AppearanceFeature]] = None,
    ) -> List[TrackedDetection]:
        """Advance one frame and return confirmed, observed tracks."""
