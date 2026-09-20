"""Tests for YOLO26 pose post-processing and inference rate limiting."""

import numpy as np
import pytest

from homecam_detector.pose import PersonPoseEstimator, PersonPoseGate


class _FakeOrtSession:
    def __init__(self, output: np.ndarray) -> None:
        self._output = output
        self.inputs = []

    def run(self, outputs, inputs):
        self.inputs.append((outputs, inputs))
        return [self._output]


def _estimator_with_output(output: np.ndarray) -> PersonPoseEstimator:
    estimator = PersonPoseEstimator.__new__(PersonPoseEstimator)
    estimator._session = _FakeOrtSession(output)
    estimator._input_name = "images"
    estimator._confidence = 0.45
    estimator._keypoint_threshold = 0.5
    estimator._input_size = 640
    return estimator


def _pose_row(confidence: float = 0.9) -> np.ndarray:
    row = np.zeros(57, dtype=np.float32)
    row[:6] = [64.0, 128.0, 576.0, 640.0, confidence, 0.0]
    for index in range(17):
        offset = 6 + index * 3
        row[offset] = 64.0 + index
        row[offset + 1] = 128.0 + index
        row[offset + 2] = 0.8 if index < 12 else 0.2
    return row


def test_pose_estimator_returns_normalized_highest_person() -> None:
    output = np.array(
        [[_pose_row(0.8), _pose_row(0.95)]], dtype=np.float32
    )
    estimator = _estimator_with_output(output)

    pose = estimator.estimate(np.zeros((400, 640, 3), dtype=np.uint8))

    assert pose is not None
    assert pose.box_confidence == pytest.approx(0.95)
    assert pose.box == pytest.approx((0.1, 0.2, 0.9, 1.0))
    assert pose.visible_keypoints == 12
    assert len(pose.keypoints) == 17
    assert pose.keypoints[0].name == "nose"
    assert pose.keypoints[0].x == pytest.approx(0.1)
    assert pose.as_dict()["present"] is True
    assert estimator._session.inputs[0][1]["images"].shape == (
        1,
        3,
        640,
        640,
    )


def test_pose_estimator_rejects_wrong_shape_and_skips_bad_rows() -> None:
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    wrong = _estimator_with_output(np.zeros((1, 300, 56), dtype=np.float32))
    with pytest.raises(ValueError, match="57 values"):
        wrong.estimate(frame)

    row = _pose_row(0.2)
    row[0] = np.nan
    estimator = _estimator_with_output(np.array([[row]], dtype=np.float32))
    assert estimator.estimate(frame) is None


def test_pose_gate_only_limits_rate() -> None:
    gate = PersonPoseGate(inference_fps=5.0)
    assert gate.should_infer(1.0)
    assert not gate.should_infer(1.19)
    assert gate.should_infer(1.20)
    gate.reset()
    assert gate.should_infer(1.21)


def test_pose_gate_does_not_repeat_same_frame_time() -> None:
    gate = PersonPoseGate(inference_fps=5.0)
    assert gate.should_infer(0.0)
    assert not gate.should_infer(0.0)
    assert not gate.should_infer(0.1)
    assert gate.should_infer(0.2)


def test_estimate_all_keeps_distinct_people_and_weak_candidates():
    left = _pose_row(0.8)
    left[:4] = [10, 10, 210, 310]
    right = _pose_row(0.3)
    right[:4] = [310, 50, 600, 200]
    estimator = _estimator_with_output(np.array([[right, left]]))
    frame = np.zeros((400, 640, 3), dtype=np.uint8)
    assert len(estimator.estimate_all(frame)) == 1
    poses = estimator.estimate_all(frame, confidence_threshold=0.1)
    assert len(poses) == 2
    assert [p.box_confidence for p in poses] == pytest.approx([0.8, 0.3])
    # One session.run per API call; not one inference per person.
    assert len(estimator._session.inputs) == 2


def test_estimate_all_filters_duplicate_and_degenerate_boxes():
    high = _pose_row(0.9)
    duplicate = _pose_row(0.4)
    invalid = _pose_row(0.8)
    invalid[:4] = [50, 10, 20, 40]
    estimator = _estimator_with_output(np.array([[high, duplicate, invalid]]))
    poses = estimator.estimate_all(np.zeros((16, 16, 3), np.uint8), confidence_threshold=0.1)
    assert len(poses) == 1
    assert poses[0].box_confidence == pytest.approx(0.9)


@pytest.mark.parametrize("threshold", [-1, 1.1, float("nan"), float("inf")])
def test_estimate_all_rejects_bad_candidate_floor(threshold):
    estimator = _estimator_with_output(np.array([[_pose_row()]]))
    with pytest.raises(ValueError, match="threshold"):
        estimator.estimate_all(np.zeros((16, 16, 3), np.uint8), confidence_threshold=threshold)
    assert estimator._session.inputs == []


def test_estimate_all_handles_empty_output_without_marking_failure():
    estimator = _estimator_with_output(np.zeros((1, 0, 57), np.float32))
    assert estimator.estimate_all(np.zeros((16, 16, 3), np.uint8)) == ()
