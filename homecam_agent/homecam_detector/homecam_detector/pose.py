"""Person-gated YOLO26 pose inference and normalized observations."""

from dataclasses import dataclass
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
    """Highest-confidence person pose returned by the secondary model."""

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
    """Run a YOLO26 pose model after the general detector sees a person."""

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
        """Return the highest-confidence pose, or None when none qualifies."""
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
            return None
        if predictions.shape[0] == 57 and predictions.shape[1] != 57:
            predictions = predictions.T
        if predictions.shape[1] != 57:
            raise ValueError(
                "YOLO26 pose output must contain 57 values per detection"
            )

        best = None
        for row in predictions:
            if not np.all(np.isfinite(row)):
                continue
            class_value = float(row[5])
            class_id = int(round(class_value))
            confidence = float(row[4])
            if (
                abs(class_value - class_id) > 1e-6
                or class_id != 0
                or confidence < self._confidence
                or confidence > 1.0
            ):
                continue
            if best is None or confidence > float(best[4]):
                best = row
        if best is None:
            return None

        scale = float(self._input_size)
        box = tuple(
            min(1.0, max(0.0, float(value) / scale))
            for value in best[:4]
        )
        raw_keypoints = best[6:].reshape(len(COCO_KEYPOINT_NAMES), 3)
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
            box_confidence=float(best[4]),
            box=box,
            keypoints=keypoints,
            visible_keypoints=sum(
                point.confidence >= self._keypoint_threshold
                for point in keypoints
            ),
        )


class PersonPoseGate:
    """Limit pose work to person frames and a configured maximum rate."""

    def __init__(self, inference_fps: float) -> None:
        """Create a monotonic-time gate for the requested maximum rate."""
        self._interval_sec = 1.0 / inference_fps
        self._last_inference_at: Optional[float] = None

    def should_infer(self, person_present: bool, now: float) -> bool:
        """Reserve this frame for pose inference when it is due."""
        if not person_present:
            return False
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
