"""Image geometry consumed by the depth localizer, not an identity tracker."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class BoundingBox:
    """Axis-aligned image rectangle in pixels."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self):
        """Reject invalid image geometry."""
        if not all(math.isfinite(v) for v in (
            self.left, self.top, self.right, self.bottom
        )) or self.width <= 0 or self.height <= 0:
            raise ValueError('bounding box must be finite and nonempty')

    @property
    def width(self):
        """Return width in pixels."""
        return self.right - self.left

    @property
    def height(self):
        """Return height in pixels."""
        return self.bottom - self.top

    @property
    def center(self):
        """Return the image center."""
        return ((self.left + self.right) / 2, (self.top + self.bottom) / 2)

    def clipped(self, image_width, image_height):
        """Clip a detection to its image before selecting the depth ROI."""
        left = min(max(self.left, 0.0), float(image_width))
        top = min(max(self.top, 0.0), float(image_height))
        right = min(max(self.right, 0.0), float(image_width))
        bottom = min(max(self.bottom, 0.0), float(image_height))
        if right <= left or bottom <= top:
            return None
        return BoundingBox(left, top, right, bottom)


@dataclass(frozen=True)
class PersonBox:
    """An upstream identity and its observed image box."""

    track_id: str
    bbox: BoundingBox
    score: float
    class_id: str = 'person'

    @classmethod
    def from_message(cls, detection):
        """Read a vision_msgs Detection2D without changing its identity."""
        center = detection.bbox.center.position
        width = detection.bbox.size_x
        height = detection.bbox.size_y
        return cls(
            detection.id,
            BoundingBox(center.x - width / 2, center.y - height / 2,
                        center.x + width / 2, center.y + height / 2),
            detection.results[0].hypothesis.score,
            detection.results[0].hypothesis.class_id,
        )
