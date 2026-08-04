"""Tests for detector post-processing that do not require a model file."""

import cv2
import numpy as np
import pytest

from homecam_detector.yolo import YoloOnnxDetector, class_aware_nms


class _FakeNet:
    def __init__(self, output: np.ndarray) -> None:
        self._output = output

    def setInput(self, _blob: np.ndarray) -> None:
        pass

    def forward(self) -> np.ndarray:
        return self._output


def _detector_with_output(output: np.ndarray) -> YoloOnnxDetector:
    detector = YoloOnnxDetector.__new__(YoloOnnxDetector)
    detector._net = _FakeNet(output)
    detector._confidence = 0.45
    detector._input_size = 640
    return detector


def test_nms_keeps_overlapping_different_classes() -> None:
    boxes = [[0, 0, 100, 100], [0, 0, 100, 100]]
    confidences = [0.95, 0.90]
    labels = ["person", "cat"]
    assert sorted(class_aware_nms(boxes, confidences, labels, 0.45)) == [0, 1]


def test_nms_suppresses_overlapping_same_class() -> None:
    boxes = [[0, 0, 100, 100], [1, 1, 100, 100]]
    confidences = [0.95, 0.90]
    labels = ["person", "person"]
    assert class_aware_nms(boxes, confidences, labels, 0.45) == [0]


def test_detect_converts_blob_and_nms_opencv_errors(monkeypatch) -> None:
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    detector = _detector_with_output(np.zeros((2, 7), dtype=np.float32))

    def fail_blob(*_args, **_kwargs):
        raise cv2.error("blob failure")

    monkeypatch.setattr(cv2.dnn, "blobFromImage", fail_blob)
    with pytest.raises(RuntimeError, match="inference failed"):
        detector.detect(frame)

    monkeypatch.undo()
    predictions = np.array(
        [
            [320.0, 320.0, 100.0, 100.0, 0.9, 0.9, 0.1],
            [300.0, 300.0, 80.0, 80.0, 0.8, 0.8, 0.2],
        ],
        dtype=np.float32,
    )
    detector = _detector_with_output(predictions)

    def fail_nms(*_args, **_kwargs):
        raise cv2.error("nms failure")

    monkeypatch.setattr(cv2.dnn, "NMSBoxes", fail_nms)
    with pytest.raises(RuntimeError, match="post-processing failed"):
        detector.detect(frame)


def test_detect_skips_non_finite_predictions() -> None:
    predictions = np.full((2, 7), np.nan, dtype=np.float32)
    detector = _detector_with_output(predictions)
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    assert detector.detect(frame) == {}
