"""Tests for sensor-image person appearance encoders."""

import cv2
import numpy as np

from malbut_perception.detector.base import BoundingBox, ImageDetection
from malbut_perception.dnn import resolve_dnn_target
from malbut_perception.reid.histogram_encoder import HistogramPersonEncoder
from malbut_perception.reid.osnet_encoder import OsNetPersonEncoder


class _FakeNetwork:
    def __init__(self):
        self.input = None

    def setPreferableBackend(self, _backend):
        pass

    def setPreferableTarget(self, _target):
        pass

    def setInput(self, value):
        self.input = value

    def forward(self):
        return np.arange(1, 513, dtype=np.float32)[None, :]


def _detection() -> ImageDetection:
    return ImageDetection(BoundingBox(10.0, 5.0, 90.0, 195.0), 0.9)


def test_osnet_encoder_preprocesses_crop_and_normalizes_output():
    network = _FakeNetwork()
    encoder = OsNetPersonEncoder('', dnn_target='cpu', network=network)
    image = np.full((200, 100, 3), (30, 80, 160), dtype=np.uint8)
    feature = encoder.encode(image, [_detection()])[0]
    assert network.input.shape == (1, 3, 256, 128)
    assert feature.shape == (512,)
    assert np.isclose(np.linalg.norm(feature), 1.0)


def test_histogram_encoder_is_stable_for_same_person_crop():
    encoder = HistogramPersonEncoder()
    image = np.zeros((200, 100, 3), dtype=np.uint8)
    image[5:100, 10:90] = (0, 0, 220)
    image[100:195, 10:90] = (220, 0, 0)
    first = encoder.encode(image, [_detection()])[0]
    second = encoder.encode(image.copy(), [_detection()])[0]
    assert np.isclose(float(np.dot(first, second)), 1.0)


def test_too_small_crop_has_no_appearance_feature():
    encoder = HistogramPersonEncoder(minimum_width=20, minimum_height=40)
    image = np.zeros((30, 30, 3), dtype=np.uint8)
    detection = ImageDetection(BoundingBox(1.0, 1.0, 10.0, 20.0), 0.9)
    assert encoder.encode(image, [detection]) == [None]


def test_dnn_cpu_target_is_always_explicitly_available():
    assert resolve_dnn_target('cpu') == 'cpu'
    assert cv2.dnn.DNN_TARGET_CPU == 0
