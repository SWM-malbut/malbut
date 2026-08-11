"""Tests for deterministic OpenCV DNN target selection."""

import cv2
import pytest

import malbut_perception.dnn as dnn


class _FakeNetwork:
    def __init__(self):
        self.backend = None
        self.target = None

    def setPreferableBackend(self, backend):
        self.backend = backend

    def setPreferableTarget(self, target):
        self.target = target


def test_auto_prefers_cuda_over_opencl(monkeypatch):
    monkeypatch.setattr(
        dnn,
        'available_cuda_targets',
        lambda: [cv2.dnn.DNN_TARGET_CUDA],
    )
    monkeypatch.setattr(
        dnn,
        'available_opencl_targets',
        lambda: [cv2.dnn.DNN_TARGET_OPENCL],
    )
    assert dnn.resolve_dnn_target('auto') == 'cuda'


def test_auto_uses_gpu_opencl_when_cuda_dnn_is_unavailable(monkeypatch):
    monkeypatch.setattr(dnn, 'available_cuda_targets', lambda: [])
    monkeypatch.setattr(
        dnn,
        'available_opencl_targets',
        lambda: [cv2.dnn.DNN_TARGET_OPENCL],
    )
    assert dnn.resolve_dnn_target('auto') == 'opencl'


def test_auto_falls_back_to_cpu_without_a_gpu_runtime(monkeypatch):
    monkeypatch.setattr(dnn, 'available_cuda_targets', lambda: [])
    monkeypatch.setattr(dnn, 'available_opencl_targets', lambda: [])
    assert dnn.resolve_dnn_target('auto') == 'cpu'


def test_opencl_configures_the_opencv_gpu_target(monkeypatch):
    monkeypatch.setattr(
        dnn,
        'available_opencl_targets',
        lambda: [cv2.dnn.DNN_TARGET_OPENCL],
    )
    network = _FakeNetwork()
    assert dnn.configure_network_target(network, 'opencl') == 'opencl'
    assert network.backend == cv2.dnn.DNN_BACKEND_OPENCV
    assert network.target == cv2.dnn.DNN_TARGET_OPENCL


def test_explicit_unavailable_opencl_target_fails(monkeypatch):
    monkeypatch.setattr(dnn, 'available_opencl_targets', lambda: [])
    with pytest.raises(RuntimeError, match='OpenCL DNN target'):
        dnn.configure_network_target(_FakeNetwork(), 'opencl')


def test_unknown_target_is_rejected():
    with pytest.raises(ValueError, match='dnn_target must be one of'):
        dnn.resolve_dnn_target('gpu')
