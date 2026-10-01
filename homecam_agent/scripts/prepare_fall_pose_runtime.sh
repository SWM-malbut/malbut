#!/usr/bin/env bash
set -euo pipefail

# Run explicitly on the target; never replace Jetson's Torch/CUDA or system pip.
cuda_wheel=''
if [[ "${1:-}" == '--help' && $# == 1 ]]; then
  echo 'Usage: prepare_fall_pose_runtime.sh [--cuda-wheel /absolute/path/to/compatible.whl]'
  echo 'Default: prepare/reuse runtime; never overwrite an existing GPU runtime with CPU ORT.'
  echo 'CUDA wheel: prepare a separate runtime-cuda; select its Python in Bringup and preflight.'
  exit 0
elif [[ $# != 0 ]]; then
  if [[ $# != 2 || "$1" != '--cuda-wheel' || "$2" != /*.whl || ! -f "$2" ]]; then
    echo 'Expected --cuda-wheel /absolute/path/to/compatible.whl (local file only).' >&2
    exit 1
  fi
  cuda_wheel="$2"
fi
if [[ "${ROS_DISTRO:-}" != humble ]]; then
  echo 'Source ROS 2 Humble first.' >&2
  exit 1
fi
/usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3, 10), "ROS Humble target requires Python 3.10"'
pose_runtime="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_fall_pose/runtime"
if [[ -n "$cuda_wheel" ]]; then
  # Keep the previous CPU environment intact. The operator must supply a wheel
  # compatible with this target's architecture/Python/JetPack/CUDA/cuDNN.
  pose_runtime="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_fall_pose/runtime-cuda"
fi
if [[ -e "$pose_runtime" && ! -f "$pose_runtime/pyvenv.cfg" ]]; then
  echo "Refusing to replace a non-venv directory: $pose_runtime" >&2
  exit 1
fi
/usr/bin/python3 -m venv --system-site-packages "$pose_runtime"
export PYTHONNOUSERSITE=1
# NumPy 2 wheels break the Ubuntu 22.04 cv_bridge/OpenCV binary interface.
if [[ -n "$cuda_wheel" ]]; then
  # CPU/GPU wheels share the onnxruntime module; refuse a mixed local install.
  "$pose_runtime/bin/python" - <<'PY'
import importlib.metadata
from pathlib import Path
import sys
try:
    dist = importlib.metadata.distribution('onnxruntime')
except importlib.metadata.PackageNotFoundError:
    pass
else:
    if Path(dist.locate_file('')).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError('runtime-cuda contains CPU onnxruntime; do not mix CPU/GPU wheels')
PY
  "$pose_runtime/bin/python" -m pip --isolated install 'numpy<2' "$cuda_wheel"
elif "$pose_runtime/bin/python" -c 'import importlib.metadata as m; m.version("onnxruntime-gpu")' >/dev/null 2>&1 || \
     "$pose_runtime/bin/python" -c 'import importlib.metadata as m; m.version("onnxruntime")' >/dev/null 2>&1; then
  # Check distribution metadata, not import success: even a broken GPU import
  # must not cause a CPU wheel to overwrite the shared onnxruntime module.
  "$pose_runtime/bin/python" -m pip --isolated install 'numpy<2'
else
  "$pose_runtime/bin/python" -m pip --isolated install 'numpy<2' 'onnxruntime>=1.17,<2'
fi
"$pose_runtime/bin/python" - "$cuda_wheel" <<'PY'
import sys
import rclpy, cv_bridge, cv2, numpy, onnxruntime
providers = onnxruntime.get_available_providers()
print(f'Fall pose imports OK; ONNX Runtime {onnxruntime.__version__}; providers={providers}')
if sys.argv[1] and 'CUDAExecutionProvider' not in providers:
    raise RuntimeError('The supplied wheel does not expose CUDAExecutionProvider')
if 'CUDAExecutionProvider' not in providers:
    print('GPU acceleration is NOT installed; auto will use CPU with limited threads.')
else:
    print('CUDA provider is present; model initialization still requires a --probe CUDA check.')
PY
echo "Fall pose Python: $pose_runtime/bin/python"
if [[ -n "$cuda_wheel" ]]; then
  printf 'Select for Bringup/preflight: export MALBUT_FALL_POSE_PYTHON=%q\n' "$pose_runtime/bin/python"
fi
echo 'Place the reviewed YOLO26s pose ONNX model in ~/.cache/malbut_perception/yolo26s-pose.onnx.'
echo 'No model is downloaded and no camera or Cloud request is started by this script.'
