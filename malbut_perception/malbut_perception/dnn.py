"""Shared OpenCV DNN execution-target selection."""

from typing import List

import cv2


SUPPORTED_DNN_TARGETS = ('auto', 'cpu', 'cuda', 'cuda_fp16')


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


def resolve_dnn_target(requested: str) -> str:
    """Resolve auto to CUDA when usable and otherwise to CPU."""
    if requested not in SUPPORTED_DNN_TARGETS:
        choices = ', '.join(SUPPORTED_DNN_TARGETS)
        raise ValueError(f'dnn_target must be one of {choices}')
    if requested != 'auto':
        return requested
    targets = available_cuda_targets()
    if cv2.dnn.DNN_TARGET_CUDA in targets:
        return 'cuda'
    return 'cpu'


def configure_network_target(network, requested: str) -> str:
    """Configure an OpenCV DNN network and return the resolved target."""
    resolved = resolve_dnn_target(requested)
    if resolved == 'cpu':
        backend = cv2.dnn.DNN_BACKEND_OPENCV
        target = cv2.dnn.DNN_TARGET_CPU
    else:
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
    network.setPreferableBackend(backend)
    network.setPreferableTarget(target)
    return resolved
