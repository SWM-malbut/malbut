"""Replaceable image detector backends."""

from .base import BoundingBox, ImageDetection, PersonDetector
from .hog_detector import HogPersonDetector
from .yolo_detector import YoloPersonDetector

__all__ = [
    'BoundingBox',
    'HogPersonDetector',
    'ImageDetection',
    'PersonDetector',
    'YoloPersonDetector',
]
