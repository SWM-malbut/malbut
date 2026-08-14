"""OpenCV-DNN YOLO person detector with letterboxed box projection."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from malbut_perception.dnn import configure_network_target
from malbut_perception.onnx_runtime import load_onnx_network

from .base import BoundingBox, ImageDetection, PersonDetector
from .hog_detector import _nms


@dataclass(frozen=True)
class LetterboxTransform:
    """Geometry needed to map model pixels back to the source image."""

    scale: float
    pad_x: float
    pad_y: float
    source_width: int
    source_height: int


def letterbox(image: np.ndarray, input_size: int):
    """Resize while preserving aspect ratio and return its transform."""
    height, width = image.shape[:2]
    scale = min(input_size / float(width), input_size / float(height))
    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))
    resized = cv2.resize(
        image,
        (resized_width, resized_height),
        interpolation=cv2.INTER_LINEAR,
    )
    pad_x = (input_size - resized_width) // 2
    pad_y = (input_size - resized_height) // 2
    canvas = np.full((input_size, input_size, 3), 114, dtype=np.uint8)
    canvas[
        pad_y:pad_y + resized_height,
        pad_x:pad_x + resized_width,
    ] = resized
    return canvas, LetterboxTransform(
        scale=scale,
        pad_x=float(pad_x),
        pad_y=float(pad_y),
        source_width=width,
        source_height=height,
    )


def _prediction_rows(output: np.ndarray) -> np.ndarray:
    predictions = np.asarray(output)
    predictions = np.squeeze(predictions)
    if predictions.ndim == 1:
        predictions = predictions.reshape(1, -1)
    if predictions.ndim != 2:
        return np.empty((0, 0), dtype=np.float32)
    if predictions.shape[0] in (6, 84, 85) and (
        predictions.shape[0] < predictions.shape[1]
    ):
        predictions = predictions.T
    return predictions


def decode_yolo_people(
    output: np.ndarray,
    transform: LetterboxTransform,
    confidence_threshold: float,
    nms_threshold: float,
) -> List[ImageDetection]:
    """Decode common one-to-many and end-to-end YOLO detect exports."""
    rows = _prediction_rows(output)
    if rows.ndim != 2 or rows.shape[1] < 6 or rows.shape[0] == 0:
        return []

    rows = rows[np.all(np.isfinite(rows), axis=1)]
    if rows.shape[0] == 0:
        return []

    if rows.shape[1] == 6:
        scores = rows[:, 4]
        class_indices = rows[:, 5].astype(np.int64)
        model_boxes = rows[:, :4]
    else:
        class_offset = 4 if rows.shape[1] == 84 else 5
        class_scores = rows[:, class_offset:]
        class_indices = np.argmax(class_scores, axis=1)
        scores = class_scores[np.arange(rows.shape[0]), class_indices]
        if class_offset == 5:
            scores = scores * rows[:, 4]
        centers = rows[:, :4]
        half_sizes = centers[:, 2:4] * 0.5
        model_boxes = np.column_stack(
            (centers[:, 0:2] - half_sizes, centers[:, 0:2] + half_sizes)
        )

    keep = (class_indices == 0) & (scores >= confidence_threshold)
    if not np.any(keep):
        return []
    scores = scores[keep]
    model_boxes = model_boxes[keep]

    boxes = model_boxes.astype(np.float64, copy=True)
    boxes[:, (0, 2)] = (
        boxes[:, (0, 2)] - transform.pad_x
    ) / transform.scale
    boxes[:, (1, 3)] = (
        boxes[:, (1, 3)] - transform.pad_y
    ) / transform.scale
    positive = (boxes[:, 2] > boxes[:, 0]) & (
        boxes[:, 3] > boxes[:, 1]
    )
    boxes = boxes[positive]
    scores = scores[positive]
    if boxes.shape[0] == 0:
        return []

    boxes[:, (0, 2)] = np.clip(
        boxes[:, (0, 2)], 0.0, float(transform.source_width)
    )
    boxes[:, (1, 3)] = np.clip(
        boxes[:, (1, 3)], 0.0, float(transform.source_height)
    )
    positive = (boxes[:, 2] > boxes[:, 0]) & (
        boxes[:, 3] > boxes[:, 1]
    )
    boxes = boxes[positive]
    scores = scores[positive]

    candidates = [
        ImageDetection(
            BoundingBox(float(left), float(top), float(right), float(bottom)),
            min(1.0, float(score)),
        )
        for (left, top, right, bottom), score in zip(boxes, scores)
    ]
    return _nms(candidates, nms_threshold)


class YoloPersonDetector(PersonDetector):
    """Run a compatible COCO YOLO ONNX detector through OpenCV DNN."""

    def __init__(
        self,
        model_path: str,
        confidence_threshold: float = 0.20,
        nms_threshold: float = 0.45,
        input_size: int = 640,
        dnn_target: str = 'auto',
        inference_backend: str = 'auto',
        network: Optional[object] = None,
    ) -> None:
        """Load the ONNX network and configure inference thresholds."""
        if not 0.0 < confidence_threshold <= 1.0:
            raise ValueError('confidence_threshold must be in (0, 1]')
        if not 0.0 < nms_threshold < 1.0:
            raise ValueError('nms_threshold must be in (0, 1)')
        if input_size <= 0:
            raise ValueError('input_size must be positive')
        if network is None:
            path = Path(model_path).expanduser()
            if not path.is_file():
                raise FileNotFoundError(
                    f'YOLO ONNX model not found: {str(path)!r}'
                )
            network, resolved_target = load_onnx_network(
                path, inference_backend, dnn_target
            )
        else:
            resolved_target = configure_network_target(network, dnn_target)
        self._net = network
        self._confidence_threshold = confidence_threshold
        self._nms_threshold = nms_threshold
        self._input_size = input_size
        self._resolved_target = resolved_target

    @property
    def resolved_target(self) -> str:
        """Return the actual OpenCV execution target."""
        return self._resolved_target

    def detect(self, bgr_image: np.ndarray) -> List[ImageDetection]:
        """Return COCO person detections from a BGR camera frame."""
        if not isinstance(bgr_image, np.ndarray) or bgr_image.size == 0:
            raise ValueError('detector input must be a non-empty image')
        if bgr_image.ndim != 3 or bgr_image.shape[2] != 3:
            raise ValueError('detector input must be a BGR image')
        prepared, transform = letterbox(bgr_image, self._input_size)
        blob = cv2.dnn.blobFromImage(
            prepared,
            scalefactor=1.0 / 255.0,
            size=(self._input_size, self._input_size),
            swapRB=True,
            crop=False,
        )
        try:
            self._net.setInput(blob)
            output = self._net.forward()
        except cv2.error as error:
            raise RuntimeError(f'YOLO inference failed: {error}') from error
        return decode_yolo_people(
            output,
            transform,
            self._confidence_threshold,
            self._nms_threshold,
        )
