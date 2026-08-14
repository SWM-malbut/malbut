"""Portable ONNX inference with NVIDIA and CPU execution providers."""

import importlib
import os
from pathlib import Path
from typing import List, Sequence, Tuple, Union

import cv2
import numpy as np

from .dnn import configure_network_target


SUPPORTED_INFERENCE_BACKENDS = ('auto', 'onnxruntime', 'opencv')
Provider = Union[str, Tuple[str, dict]]


def _runtime_cache_root() -> Path:
    configured = os.environ.get('XDG_CACHE_HOME')
    root = Path(configured).expanduser() if configured else Path.home() / '.cache'
    return root / 'malbut_perception' / 'tensorrt'


def resolve_onnxruntime_providers(
    available: Sequence[str],
    requested_target: str,
    cache_path: Path,
) -> Tuple[List[Provider], str]:
    """Choose the fastest provider set without silently ignoring requests."""
    known_targets = {
        'auto',
        'cpu',
        'cuda',
        'cuda_fp16',
        'opencl',
        'opencl_fp16',
    }
    if requested_target not in known_targets:
        choices = ', '.join(sorted(known_targets))
        raise ValueError(f'dnn_target must be one of {choices}')
    if requested_target in {'opencl', 'opencl_fp16'}:
        raise ValueError('OpenCL targets require inference_backend=opencv')

    providers = set(available)
    cpu = 'CPUExecutionProvider'
    cuda = 'CUDAExecutionProvider'
    tensorrt = 'TensorrtExecutionProvider'

    if requested_target == 'cpu':
        if cpu not in providers:
            raise RuntimeError('ONNX Runtime CPU provider is unavailable')
        return [cpu], 'onnxruntime-cpu'

    if requested_target == 'cuda':
        if cuda not in providers:
            raise RuntimeError('ONNX Runtime CUDA provider is unavailable')
        selected: List[Provider] = [cuda]
        if cpu in providers:
            selected.append(cpu)
        return selected, 'onnxruntime-cuda'

    if requested_target == 'cuda_fp16':
        if tensorrt not in providers:
            raise RuntimeError(
                'ONNX Runtime TensorRT provider is required for cuda_fp16'
            )
        cache_path.mkdir(parents=True, exist_ok=True)
        selected = [
            (
                tensorrt,
                {
                    'device_id': 0,
                    'trt_fp16_enable': True,
                    'trt_engine_cache_enable': True,
                    'trt_engine_cache_path': str(cache_path),
                },
            )
        ]
        if cuda in providers:
            selected.append(cuda)
        if cpu in providers:
            selected.append(cpu)
        return selected, 'onnxruntime-tensorrt-fp16'

    if tensorrt in providers:
        cache_path.mkdir(parents=True, exist_ok=True)
        selected = [
            (
                tensorrt,
                {
                    'device_id': 0,
                    'trt_fp16_enable': True,
                    'trt_engine_cache_enable': True,
                    'trt_engine_cache_path': str(cache_path),
                },
            )
        ]
        if cuda in providers:
            selected.append(cuda)
        if cpu in providers:
            selected.append(cpu)
        return selected, 'onnxruntime-tensorrt-fp16'
    if cuda in providers:
        selected = [cuda]
        if cpu in providers:
            selected.append(cpu)
        return selected, 'onnxruntime-cuda'
    if cpu in providers:
        return [cpu], 'onnxruntime-cpu'
    raise RuntimeError('ONNX Runtime exposes no usable execution provider')


class OnnxRuntimeNetwork:
    """Expose an OpenCV-like interface around an ONNX Runtime session."""

    def __init__(self, model_path: Path, requested_target: str) -> None:
        """Load one-input ONNX inference using the selected provider set."""
        try:
            runtime = importlib.import_module('onnxruntime')
        except ImportError as error:
            raise RuntimeError(
                'onnxruntime is not installed; run '
                'scripts/prepare_inference_runtime.sh'
            ) from error
        providers, resolved_target = resolve_onnxruntime_providers(
            runtime.get_available_providers(),
            requested_target,
            _runtime_cache_root(),
        )
        try:
            session = runtime.InferenceSession(
                str(model_path), providers=providers
            )
        except Exception as error:
            raise RuntimeError(
                f'cannot load ONNX Runtime model {model_path}: {error}'
            ) from error
        inputs = session.get_inputs()
        if len(inputs) != 1:
            raise RuntimeError(
                f'expected one ONNX input, received {len(inputs)}'
            )
        self._session = session
        self._input_name = inputs[0].name
        self._input = None
        self.resolved_target = resolved_target

    def setInput(self, value: np.ndarray) -> None:
        """Store a contiguous float32 tensor for the next inference."""
        self._input = np.ascontiguousarray(value, dtype=np.float32)

    def forward(self) -> np.ndarray:
        """Run inference and return the model's first output tensor."""
        if self._input is None:
            raise RuntimeError('ONNX input was not set before forward')
        try:
            outputs = self._session.run(
                None, {self._input_name: self._input}
            )
        except Exception as error:
            raise RuntimeError(f'ONNX Runtime inference failed: {error}') from error
        if not outputs:
            raise RuntimeError('ONNX Runtime returned no outputs')
        return np.asarray(outputs[0])


def load_onnx_network(
    model_path: Path,
    inference_backend: str,
    requested_target: str,
):
    """Load an ONNX model and return its network and resolved target."""
    if inference_backend not in SUPPORTED_INFERENCE_BACKENDS:
        choices = ', '.join(SUPPORTED_INFERENCE_BACKENDS)
        raise ValueError(f'inference_backend must be one of {choices}')

    backend = inference_backend
    if backend == 'auto':
        try:
            importlib.import_module('onnxruntime')
            backend = 'onnxruntime'
        except ImportError:
            backend = 'opencv'

    if backend == 'onnxruntime':
        network = OnnxRuntimeNetwork(model_path, requested_target)
        return network, network.resolved_target

    try:
        network = cv2.dnn.readNetFromONNX(str(model_path))
    except cv2.error as error:
        raise RuntimeError(f'cannot load ONNX model: {error}') from error
    return network, configure_network_target(network, requested_target)
