"""Tests for image detector geometry and YOLO decoding."""

import numpy as np

from malbut_perception.detector.base import BoundingBox
from malbut_perception.detector.yolo_detector import (
    LetterboxTransform,
    decode_yolo_people,
    letterbox,
)


def test_bounding_box_clip_and_iou():
    box = BoundingBox(-10.0, 5.0, 50.0, 45.0).clipped(40, 30)
    assert box == BoundingBox(0.0, 5.0, 40.0, 30.0)
    assert box.center == (20.0, 17.5)
    assert box.iou(BoundingBox(20.0, 5.0, 40.0, 30.0)) == 0.5


def test_letterbox_keeps_source_aspect_ratio():
    image = np.zeros((400, 640, 3), dtype=np.uint8)
    prepared, transform = letterbox(image, 640)
    assert prepared.shape == (640, 640, 3)
    assert transform.scale == 1.0
    assert transform.pad_x == 0.0
    assert transform.pad_y == 120.0


def test_yolo26_decode_keeps_only_person_and_removes_overlap():
    output = np.zeros((1, 84, 100), dtype=np.float32)
    # The 640x400 source is letterboxed with 120 vertical model pixels.
    output[0, 0:4, 0] = [200.0, 320.0, 200.0, 300.0]
    output[0, 4, 0] = 0.90
    output[0, 0:4, 1] = [202.0, 320.0, 198.0, 296.0]
    output[0, 4, 1] = 0.80
    output[0, 0:4, 2] = [450.0, 320.0, 100.0, 200.0]
    output[0, 5, 2] = 0.99  # COCO bicycle, not person.
    transform = LetterboxTransform(1.0, 0.0, 120.0, 640, 400)

    detections = decode_yolo_people(output, transform, 0.20, 0.45)

    assert len(detections) == 1
    assert detections[0].class_id == 'person'
    assert detections[0].score > 0.89
    assert detections[0].bbox == BoundingBox(100.0, 50.0, 300.0, 350.0)


def test_end_to_end_nms_export_shape_is_supported():
    output = np.array(
        [
            [10.0, 20.0, 110.0, 220.0, 0.8, 0.0],
            [200.0, 20.0, 250.0, 120.0, 0.9, 2.0],
        ],
        dtype=np.float32,
    )
    transform = LetterboxTransform(1.0, 0.0, 0.0, 640, 640)
    detections = decode_yolo_people(output, transform, 0.20, 0.45)
    assert len(detections) == 1
    assert detections[0].bbox == BoundingBox(10.0, 20.0, 110.0, 220.0)


def test_end_to_end_output_is_not_filtered_by_external_nms():
    output = np.array(
        [
            [10.0, 20.0, 110.0, 220.0, 0.8, 0.0],
            [11.0, 21.0, 111.0, 221.0, 0.7, 0.0],
        ],
        dtype=np.float32,
    )
    transform = LetterboxTransform(1.0, 0.0, 0.0, 640, 640)
    detections = decode_yolo_people(output, transform, 0.20, 0.45)
    assert len(detections) == 2


def test_decode_ignores_nonfinite_and_outside_predictions():
    output = np.zeros((1, 5, 85), dtype=np.float32)
    output[0, 0, :6] = [320.0, 320.0, 200.0, 300.0, 0.9, 0.9]
    output[0, 1, :6] = [320.0, 320.0, 200.0, 300.0, np.nan, 0.9]
    output[0, 2, :6] = [-200.0, 320.0, 100.0, 100.0, 0.9, 0.9]
    output[0, 3, :6] = [320.0, 320.0, -10.0, 100.0, 0.9, 0.9]
    output[0, 4, :6] = [320.0, 320.0, 200.0, 300.0, 0.9, 0.1]
    output[0, 4, 6] = 0.95
    transform = LetterboxTransform(1.0, 0.0, 120.0, 640, 400)

    detections = decode_yolo_people(output, transform, 0.20, 0.45)

    assert len(detections) == 1
    assert detections[0].bbox == BoundingBox(220.0, 50.0, 420.0, 350.0)
