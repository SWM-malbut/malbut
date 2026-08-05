"""OpenCV-DNN YOLO person detector with letterboxed box projection."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

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
    """Decode common YOLOv5/v8/v11 detect exports and keep COCO person."""
    candidates: List[ImageDetection] = []
    for row in _prediction_rows(output):
        if row.size < 6 or not np.all(np.isfinite(row)):
            continue
        if row.size == 6:
            left, top, right, bottom, score, class_index = row
            if int(class_index) != 0 or score < confidence_threshold:
                continue
            model_box = (left, top, right, bottom)
        else:
            if row.size == 84:
                objectness = 1.0
                class_scores = row[4:]
            else:
                objectness = float(row[4])
                class_scores = row[5:]
            class_index = int(np.argmax(class_scores))
            score = objectness * float(class_scores[class_index])
            if class_index != 0 or score < confidence_threshold:
                continue
            center_x, center_y, width, height = row[:4]
            model_box = (
                center_x - width / 2.0,
                center_y - height / 2.0,
                center_x + width / 2.0,
                center_y + height / 2.0,
            )

        left = (float(model_box[0]) - transform.pad_x) / transform.scale
        top = (float(model_box[1]) - transform.pad_y) / transform.scale
        right = (float(model_box[2]) - transform.pad_x) / transform.scale
        bottom = (float(model_box[3]) - transform.pad_y) / transform.scale
        if right <= left or bottom <= top:
            continue
        box = BoundingBox(left, top, right, bottom).clipped(
            transform.source_width,
            transform.source_height,
        )
        if box is not None:
            candidates.append(ImageDetection(box, min(1.0, float(score))))
    return _nms(candidates, nms_threshold)


class YoloPersonDetector(PersonDetector):
    """Run a compatible COCO YOLO ONNX detector through OpenCV DNN."""

    def __init__(
        self,
        model_path: str,
        confidence_threshold: float = 0.20,
        nms_threshold: float = 0.45,
        input_size: int = 640,
        dnn_target: str = 'cpu',
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
            try:
                network = cv2.dnn.readNetFromONNX(str(path))
            except cv2.error as error:
                raise RuntimeError(
                    f'cannot load YOLO ONNX model: {error}'
                ) from error
        self._net = network
        self._confidence_threshold = confidence_threshold
        self._nms_threshold = nms_threshold
        self._input_size = input_size
        self._configure_target(dnn_target)

    def _configure_target(self, target: str) -> None:
        targets = {
            'cpu': (
                cv2.dnn.DNN_BACKEND_OPENCV,
                cv2.dnn.DNN_TARGET_CPU,
            ),
            'cuda': (
                cv2.dnn.DNN_BACKEND_CUDA,
                cv2.dnn.DNN_TARGET_CUDA,
            ),
            'cuda_fp16': (
                cv2.dnn.DNN_BACKEND_CUDA,
                cv2.dnn.DNN_TARGET_CUDA_FP16,
            ),
        }
        if target not in targets:
            raise ValueError(
                "dnn_target must be one of 'cpu', 'cuda', or 'cuda_fp16'"
            )
        backend, device = targets[target]
        self._net.setPreferableBackend(backend)
        self._net.setPreferableTarget(device)

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
