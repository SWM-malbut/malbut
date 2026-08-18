"""Tests for portable ONNX execution-provider selection."""

from pathlib import Path

import pytest

from malbut_perception.onnx_runtime import resolve_onnxruntime_providers


def test_auto_prefers_tensorrt_fp16_on_jetson(tmp_path):
    providers, resolved = resolve_onnxruntime_providers(
        [
            'TensorrtExecutionProvider',
            'CUDAExecutionProvider',
            'CPUExecutionProvider',
        ],
        'auto',
        tmp_path,
    )
    assert providers[0][0] == 'TensorrtExecutionProvider'
    assert providers[0][1]['trt_fp16_enable'] is True
    assert providers[0][1]['trt_engine_cache_path'] == str(tmp_path)
    assert resolved == 'onnxruntime-tensorrt-fp16'


def test_auto_uses_cuda_when_tensorrt_is_unavailable(tmp_path):
    providers, resolved = resolve_onnxruntime_providers(
        ['CUDAExecutionProvider', 'CPUExecutionProvider'],
        'auto',
        tmp_path,
    )
    assert providers == ['CUDAExecutionProvider', 'CPUExecutionProvider']
    assert resolved == 'onnxruntime-cuda'


def test_desktop_auto_uses_cuda_even_when_tensorrt_is_compiled(tmp_path):
    providers, resolved = resolve_onnxruntime_providers(
        [
            'TensorrtExecutionProvider',
            'CUDAExecutionProvider',
            'CPUExecutionProvider',
        ],
        'auto',
        tmp_path,
        prefer_tensorrt=False,
    )
    assert providers == ['CUDAExecutionProvider', 'CPUExecutionProvider']
    assert resolved == 'onnxruntime-cuda'


def test_auto_uses_cpu_on_development_machine(tmp_path):
    providers, resolved = resolve_onnxruntime_providers(
        ['CPUExecutionProvider'], 'auto', tmp_path
    )
    assert providers == ['CPUExecutionProvider']
    assert resolved == 'onnxruntime-cpu'


def test_explicit_cuda_does_not_silently_use_cpu(tmp_path):
    with pytest.raises(RuntimeError, match='CUDA provider'):
        resolve_onnxruntime_providers(
            ['CPUExecutionProvider'], 'cuda', tmp_path
        )


def test_opencl_requires_opencv_backend(tmp_path):
    with pytest.raises(ValueError, match='inference_backend=opencv'):
        resolve_onnxruntime_providers(
            ['CPUExecutionProvider'], 'opencl', Path(tmp_path)
        )
