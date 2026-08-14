"""OSNet person Re-ID inference through OpenCV DNN."""

from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from malbut_perception.detector.base import ImageDetection
from malbut_perception.dnn import configure_network_target

from .base import AppearanceFeature, PersonAppearanceEncoder, normalized_feature
from .crop import person_crop


class OsNetPersonEncoder(PersonAppearanceEncoder):
    """Extract lightweight OSNet embeddings from person image crops."""

    def __init__(
        self,
        model_path: str,
        dnn_target: str = 'auto',
        minimum_width: int = 16,
        minimum_height: int = 32,
        network: Optional[object] = None,
    ) -> None:
        """Load an OSNet x0.25-compatible ONNX feature extractor."""
        if network is None:
            path = Path(model_path).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f'OSNet model not found: {path}')
            try:
                network = cv2.dnn.readNetFromONNX(str(path))
            except cv2.error as error:
                raise RuntimeError(
                    f'cannot load OSNet ONNX model: {error}'
                ) from error
        self._network = network
        self._resolved_target = configure_network_target(
            self._network, dnn_target
        )
        self._minimum_width = minimum_width
        self._minimum_height = minimum_height

    @property
    def resolved_target(self) -> str:
        """Return the actual OpenCV execution target."""
        return self._resolved_target

    @staticmethod
    def _blob(crop: np.ndarray) -> np.ndarray:
        resized = cv2.resize(crop, (128, 256), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
        rgb *= 1.0 / 255.0
        rgb -= np.array([0.485, 0.456, 0.406], dtype=np.float32)
        rgb /= np.array([0.229, 0.224, 0.225], dtype=np.float32)
        return np.transpose(rgb, (2, 0, 1))[None, ...]

    def encode(
        self,
        bgr_image: np.ndarray,
        detections: List[ImageDetection],
    ) -> List[AppearanceFeature]:
        """Return a normalized 512-dimensional descriptor per valid crop."""
        features: List[AppearanceFeature] = []
        for detection in detections:
            crop = person_crop(
                bgr_image,
                detection.bbox,
                self._minimum_width,
                self._minimum_height,
            )
            if crop is None:
                features.append(None)
                continue
            try:
                self._network.setInput(self._blob(crop))
                output = self._network.forward()
            except cv2.error as error:
                raise RuntimeError(f'OSNet inference failed: {error}') from error
            features.append(normalized_feature(output))
        return features
