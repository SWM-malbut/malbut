"""Shared OpenCV DNN execution-target selection."""

from typing import List

import cv2


SUPPORTED_DNN_TARGETS = (
    'auto',
    'cpu',
    'cuda',
    'cuda_fp16',
    'opencl',
    'opencl_fp16',
)


def available_cuda_targets() -> List[int]:
    """Return CUDA DNN targets exposed by the installed OpenCV build."""
    try:
        if cv2.cuda.getCudaEnabledDeviceCount() <= 0:
            return []
        return list(
            cv2.dnn.getAvailableTargets(cv2.dnn.DNN_BACKEND_CUDA)
        )
    except (AttributeError, cv2.error):
        return []


def available_opencl_targets() -> List[int]:
    """Return OpenCL DNN targets backed by an available GPU device."""
    try:
        cv2.ocl.setUseOpenCL(True)
        if not cv2.ocl.haveOpenCL() or not cv2.ocl.useOpenCL():
            return []
        device = cv2.ocl.Device_getDefault()
        if not device.available() or not device.compilerAvailable():
            return []
        if not device.type() & cv2.ocl.DEVICE_TYPE_GPU:
            return []
        available = cv2.dnn.getAvailableTargets(
            cv2.dnn.DNN_BACKEND_OPENCV
        )
        return [
            target
            for target in available
            if target in {
                cv2.dnn.DNN_TARGET_OPENCL,
                cv2.dnn.DNN_TARGET_OPENCL_FP16,
            }
        ]
    except (AttributeError, cv2.error):
        return []


def resolve_dnn_target(requested: str) -> str:
    """Resolve auto to CUDA, GPU OpenCL, or CPU in priority order."""
    if requested not in SUPPORTED_DNN_TARGETS:
        choices = ', '.join(SUPPORTED_DNN_TARGETS)
        raise ValueError(f'dnn_target must be one of {choices}')
    if requested != 'auto':
        return requested
    targets = available_cuda_targets()
    if cv2.dnn.DNN_TARGET_CUDA in targets:
        return 'cuda'
    targets = available_opencl_targets()
    if cv2.dnn.DNN_TARGET_OPENCL in targets:
        return 'opencl'
    return 'cpu'


def configure_network_target(network, requested: str) -> str:
    """Configure an OpenCV DNN network and return the resolved target."""
    resolved = resolve_dnn_target(requested)
    if resolved == 'cpu':
        backend = cv2.dnn.DNN_BACKEND_OPENCV
        target = cv2.dnn.DNN_TARGET_CPU
    elif resolved in {'cuda', 'cuda_fp16'}:
        available = available_cuda_targets()
        required = (
            cv2.dnn.DNN_TARGET_CUDA_FP16
            if resolved == 'cuda_fp16'
            else cv2.dnn.DNN_TARGET_CUDA
        )
        if required not in available:
            raise RuntimeError(
                f'OpenCV CUDA DNN target {resolved!r} is unavailable'
            )
        backend = cv2.dnn.DNN_BACKEND_CUDA
        target = required
    else:
        available = available_opencl_targets()
        required = (
            cv2.dnn.DNN_TARGET_OPENCL_FP16
            if resolved == 'opencl_fp16'
            else cv2.dnn.DNN_TARGET_OPENCL
        )
        if required not in available:
            raise RuntimeError(
                f'OpenCV OpenCL DNN target {resolved!r} is unavailable'
            )
        backend = cv2.dnn.DNN_BACKEND_OPENCV
        target = required
    network.setPreferableBackend(backend)
    network.setPreferableTarget(target)
    return resolved
