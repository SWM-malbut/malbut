"""Rate-limited YOLO26 pose inference and normalized observations."""

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np


COCO_KEYPOINT_NAMES = (
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)


@dataclass(frozen=True)
class PoseKeypoint:
    """One COCO keypoint in normalized source-image coordinates."""

    name: str
    x: float
    y: float
    confidence: float


@dataclass(frozen=True)
class PersonPose:
    """One person's pose in normalized source-image coordinates."""

    box_confidence: float
    box: Tuple[float, float, float, float]
    keypoints: Tuple[PoseKeypoint, ...]
    visible_keypoints: int

    def as_dict(self) -> Dict[str, object]:
        """Return a stable JSON-compatible local ROS contract."""
        return {
            "present": True,
            "boxConfidence": self.box_confidence,
            "box": {
                "left": self.box[0],
                "top": self.box[1],
                "right": self.box[2],
                "bottom": self.box[3],
            },
            "visibleKeypoints": self.visible_keypoints,
            "keypoints": [
                {
                    "name": point.name,
                    "x": point.x,
                    "y": point.y,
                    "confidence": point.confidence,
                }
                for point in self.keypoints
            ],
        }


class PersonPoseEstimator:
    """Run a YOLO26 pose model independently of general object detection."""

    def __init__(
        self,
        model_path: str,
        confidence_threshold: float = 0.45,
        keypoint_threshold: float = 0.5,
        input_size: int = 640,
    ) -> None:
        """Load one fixed-shape end-to-end YOLO26 pose graph."""
        path = Path(model_path).expanduser()
        if not model_path or not path.is_file():
            raise FileNotFoundError(
                f"YOLO pose ONNX model not found: {model_path!r}"
            )
        try:
            import onnxruntime as ort
        except ImportError as error:
            raise RuntimeError(
                "ONNX Runtime is required for the YOLO26 pose model"
            ) from error
        try:
            self._session = ort.InferenceSession(
                str(path), providers=["CPUExecutionProvider"]
            )
            self._input_name = self._session.get_inputs()[0].name
        except Exception as error:
            raise RuntimeError(
                f"cannot load YOLO pose ONNX model: {error}"
            ) from error
        self._confidence = confidence_threshold
        self._keypoint_threshold = keypoint_threshold
        self._input_size = input_size

    def estimate(self, bgr_frame: np.ndarray) -> Optional[PersonPose]:
        """Compatibility API: return the strongest qualifying pose."""
        poses = self.estimate_all(bgr_frame)
        return poses[0] if poses else None

    def estimate_all(
        self, bgr_frame: np.ndarray, *, confidence_threshold: Optional[float] = None
    ) -> Tuple[PersonPose, ...]:
        """Infer once and retain distinct person poses above the given floor.

        A lower floor is for tracking candidates, not confirmed detections.
        The default preserves the existing confidence threshold.
        """
        threshold = self._confidence if confidence_threshold is None else confidence_threshold
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("pose confidence threshold must be in [0, 1]")
        if (
            not isinstance(bgr_frame, np.ndarray)
            or bgr_frame.ndim != 3
            or bgr_frame.shape[2] != 3
            or bgr_frame.size == 0
        ):
            raise ValueError("YOLO pose input must be a non-empty BGR image")
        try:
            blob = cv2.dnn.blobFromImage(
                bgr_frame,
                scalefactor=1.0 / 255.0,
                size=(self._input_size, self._input_size),
                swapRB=True,
                crop=False,
            )
            output = self._session.run(
                None, {self._input_name: blob}
            )[0]
        except cv2.error as error:
            raise RuntimeError(
                f"YOLO pose ONNX inference failed: {error}"
            ) from error
        except Exception as error:
            raise RuntimeError(
                f"YOLO pose ONNX inference failed: {error}"
            ) from error

        predictions = np.squeeze(output)
        if predictions.ndim == 1 and predictions.shape[0] == 57:
            predictions = predictions.reshape(1, -1)
        if predictions.ndim != 2:
            raise ValueError("YOLO26 pose output must be a detection matrix")
        if predictions.shape[0] == 57 and predictions.shape[1] != 57:
            predictions = predictions.T
        if predictions.shape[1] != 57:
            raise ValueError(
                "YOLO26 pose output must contain 57 values per detection"
            )

        poses = []
        for row in predictions:
            if not np.all(np.isfinite(row)):
                continue
            class_value = float(row[5])
            class_id = int(round(class_value))
            confidence = float(row[4])
            if (
                abs(class_value - class_id) > 1e-6
                or class_id != 0
                or confidence < threshold
                or confidence > 1.0
            ):
                continue
            pose = self._parse_pose(row)
            if pose.box[0] < pose.box[2] and pose.box[1] < pose.box[3]:
                poses.append(pose)
        poses.sort(key=lambda pose: (-pose.box_confidence, pose.box))
        # Low-score end-to-end outputs can contain near-identical duplicates.
        # Keep overlapping people unless their boxes are almost identical.
        distinct = []
        for pose in poses:
            if not any(box_iou(pose.box, kept.box) >= 0.85 for kept in distinct):
                distinct.append(pose)
        return tuple(distinct)

    def _parse_pose(self, row: np.ndarray) -> PersonPose:
        scale = float(self._input_size)
        box = tuple(
            min(1.0, max(0.0, float(value) / scale))
            for value in row[:4]
        )
        raw_keypoints = row[6:].reshape(len(COCO_KEYPOINT_NAMES), 3)
        keypoints = tuple(
            PoseKeypoint(
                name=name,
                x=min(1.0, max(0.0, float(values[0]) / scale)),
                y=min(1.0, max(0.0, float(values[1]) / scale)),
                confidence=min(1.0, max(0.0, float(values[2]))),
            )
            for name, values in zip(COCO_KEYPOINT_NAMES, raw_keypoints)
        )
        return PersonPose(
            box_confidence=float(row[4]),
            box=box,
            keypoints=keypoints,
            visible_keypoints=sum(
                point.confidence >= self._keypoint_threshold
                for point in keypoints
            ),
        )


def box_iou(left: Tuple[float, ...], right: Tuple[float, ...]) -> float:
    """Intersection over union of two normalized XYXY boxes."""
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = ((left[2] - left[0]) * (left[3] - left[1])
             + (right[2] - right[0]) * (right[3] - right[1]) - intersection)
    return intersection / union if union > 0 else 0.0


class PersonPoseGate:
    """Limit pose inference rate without requiring a person detection."""

    def __init__(self, inference_fps: float) -> None:
        """Create a monotonic-time gate for the requested maximum rate."""
        self._interval_sec = 1.0 / inference_fps
        self._last_inference_at: Optional[float] = None

    def should_infer(self, now: float) -> bool:
        """Reserve this frame for pose inference when it is due."""
        if (
            self._last_inference_at is not None
            and now - self._last_inference_at + 1e-9 < self._interval_sec
        ):
            return False
        self._last_inference_at = now
        return True

    def reset(self) -> None:
        """Forget the previous sample time after privacy-state changes."""
        self._last_inference_at = None
