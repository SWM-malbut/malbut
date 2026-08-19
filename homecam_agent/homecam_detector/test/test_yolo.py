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


class _FakeOrtSession:
    def __init__(self, output: np.ndarray) -> None:
        self._output = output
        self.inputs = []

    def run(self, outputs, inputs):
        self.inputs.append((outputs, inputs))
        return [self._output]


def _detector_with_output(output: np.ndarray) -> YoloOnnxDetector:
    detector = YoloOnnxDetector.__new__(YoloOnnxDetector)
    detector._net = _FakeNet(output)
    detector._ort_session = None
    detector._ort_input_name = ""
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


def test_detect_supports_yolo26_end_to_end_person_and_pet_classes() -> None:
    predictions = np.array(
        [[
            [10.0, 20.0, 110.0, 220.0, 0.91, 0.0],
            [20.0, 30.0, 100.0, 120.0, 0.82, 15.0],
            [30.0, 40.0, 130.0, 140.0, 0.73, 16.0],
            [0.0, 0.0, 10.0, 10.0, 0.99, 2.0],
            [0.0, 0.0, 10.0, 10.0, np.nan, 0.0],
        ]],
        dtype=np.float32,
    )
    detector = _detector_with_output(predictions)
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    assert detector.detect(frame) == pytest.approx(
        {"person": 0.91, "cat": 0.82, "dog": 0.73}
    )


def test_detect_runs_yolo26_through_onnx_runtime_session() -> None:
    predictions = np.array(
        [[[10.0, 20.0, 110.0, 220.0, 0.91, 0.0]]],
        dtype=np.float32,
    )
    detector = _detector_with_output(np.empty((0, 6), dtype=np.float32))
    session = _FakeOrtSession(predictions)
    detector._ort_session = session
    detector._ort_input_name = "images"

    result = detector.detect(np.zeros((16, 16, 3), dtype=np.uint8))

    assert result == pytest.approx({"person": 0.91})
    assert len(session.inputs) == 1
    assert session.inputs[0][0] is None
    assert session.inputs[0][1]["images"].shape == (1, 3, 640, 640)
