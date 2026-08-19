"""ONNX Runtime / OpenCV-DNN adapter for COCO YOLO models."""

from pathlib import Path
from typing import Dict, List

import cv2
import numpy as np


def class_aware_nms(
    boxes: List[List[int]],
    confidences: List[float],
    labels: List[str],
    confidence_threshold: float,
    nms_threshold: float = 0.45,
) -> List[int]:
    """Run NMS independently per class so overlapping classes survive."""
    selected: List[int] = []
    for label in sorted(set(labels)):
        class_indices = [index for index, value in enumerate(labels) if value == label]
        class_boxes = [boxes[index] for index in class_indices]
        class_confidences = [confidences[index] for index in class_indices]
        kept = cv2.dnn.NMSBoxes(
            class_boxes,
            class_confidences,
            confidence_threshold,
            nms_threshold,
        )
        selected.extend(class_indices[int(index)] for index in np.array(kept).reshape(-1))
    return selected


class YoloOnnxDetector:
    """Run a replaceable YOLO ONNX model and expose only home-camera classes."""

    COCO_TARGETS = {0: "person", 15: "cat", 16: "dog"}

    def __init__(
        self,
        model_path: str,
        confidence_threshold: float = 0.45,
        input_size: int = 640,
    ) -> None:
        path = Path(model_path).expanduser()
        if not model_path or not path.is_file():
            raise FileNotFoundError(f"YOLO ONNX model not found: {model_path!r}")
        self._ort_session = None
        self._ort_input_name = ""
        try:
            import onnxruntime as ort
        except ImportError:
            ort = None
        if ort is not None:
            try:
                self._ort_session = ort.InferenceSession(
                    str(path),
                    providers=["CPUExecutionProvider"],
                )
                self._ort_input_name = self._ort_session.get_inputs()[0].name
            except Exception as error:
                raise RuntimeError(
                    f"cannot load YOLO ONNX model with ONNX Runtime: {error}"
                ) from error
            self._net = None
        else:
            try:
                self._net = cv2.dnn.readNetFromONNX(str(path))
            except cv2.error as error:
                raise RuntimeError(
                    "cannot load YOLO ONNX model; install ONNX Runtime for "
                    f"YOLO26 models: {error}"
                ) from error
        self._confidence = confidence_threshold
        self._input_size = input_size

    def detect(self, bgr_frame: np.ndarray) -> Dict[str, float]:
        """Return the highest confidence for person, dog, and cat."""
        if (
            not isinstance(bgr_frame, np.ndarray)
            or bgr_frame.ndim != 3
            or bgr_frame.shape[2] != 3
            or bgr_frame.size == 0
        ):
            raise ValueError("YOLO input must be a non-empty BGR image")
        try:
            blob = cv2.dnn.blobFromImage(
                bgr_frame,
                scalefactor=1.0 / 255.0,
                size=(self._input_size, self._input_size),
                swapRB=True,
                crop=False,
            )
            if getattr(self, "_ort_session", None) is not None:
                output = self._ort_session.run(
                    None,
                    {self._ort_input_name: blob},
                )[0]
            else:
                self._net.setInput(blob)
                output = self._net.forward()
        except cv2.error as error:
            raise RuntimeError(f"YOLO ONNX inference failed: {error}") from error
        except Exception as error:
            raise RuntimeError(f"YOLO ONNX inference failed: {error}") from error
        predictions = np.squeeze(output)
        if predictions.ndim == 1 and predictions.shape[0] in (6, 84, 85):
            predictions = predictions.reshape(1, -1)
        if predictions.ndim != 2:
            return {}
        if (
            predictions.shape[0] in (6, 84, 85)
            and predictions.shape[0] < predictions.shape[1]
        ):
            predictions = predictions.T

        # Current Malbut perception setup exports YOLO26 end-to-end models as
        # [x1, y1, x2, y2, score, COCO class]. NMS is already inside that
        # graph, and event classification only needs the best class score.
        if predictions.shape[1] == 6:
            result: Dict[str, float] = {}
            for row in predictions:
                if not np.all(np.isfinite(row)):
                    continue
                class_value = float(row[5])
                class_id = int(round(class_value))
                confidence = float(row[4])
                if (
                    abs(class_value - class_id) > 1e-6
                    or class_id not in self.COCO_TARGETS
                    or confidence < self._confidence
                    or confidence > 1.0
                ):
                    continue
                label = self.COCO_TARGETS[class_id]
                result[label] = max(result.get(label, 0.0), confidence)
            return result

        boxes: List[List[int]] = []
        confidences: List[float] = []
        labels: List[str] = []
        source_h, source_w = bgr_frame.shape[:2]
        scale_x = source_w / float(self._input_size)
        scale_y = source_h / float(self._input_size)

        for row in predictions:
            if row.shape[0] < 7 or not np.all(np.isfinite(row)):
                continue
            # Standard YOLOv8 COCO has 4 box values + 80 class scores.
            # YOLOv5 exports have objectness at index 4.
            if row.shape[0] == 84:
                objectness = 1.0
                class_scores = row[4:]
            else:
                objectness = float(row[4])
                class_scores = row[5:]
            class_id = int(np.argmax(class_scores))
            if class_id not in self.COCO_TARGETS:
                continue
            confidence = objectness * float(class_scores[class_id])
            if confidence < self._confidence:
                continue
            center_x, center_y, width, height = [float(value) for value in row[:4]]
            left = int((center_x - width / 2.0) * scale_x)
            top = int((center_y - height / 2.0) * scale_y)
            boxes.append(
                [
                    left,
                    top,
                    max(1, int(width * scale_x)),
                    max(1, int(height * scale_y)),
                ]
            )
            confidences.append(confidence)
            labels.append(self.COCO_TARGETS[class_id])

        if not boxes:
            return {}
        try:
            indices = class_aware_nms(
                boxes,
                confidences,
                labels,
                self._confidence,
            )
        except cv2.error as error:
            raise RuntimeError(f"YOLO ONNX post-processing failed: {error}") from error
        result: Dict[str, float] = {}
        for index in indices:
            label = labels[index]
            result[label] = max(result.get(label, 0.0), confidences[index])
        return result
