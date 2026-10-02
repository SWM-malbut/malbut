#!/usr/bin/env bash
set -euo pipefail

# Run explicitly on the target; never replace Jetson's Torch/CUDA or system pip.
cuda_wheel=''
if [[ "${1:-}" == '--help' && $# == 1 ]]; then
  echo 'Usage: prepare_fall_pose_runtime.sh [--gpu | --cuda-wheel /absolute/path/to/compatible.whl]'
  echo 'Always uses malbut_fall_pose/runtime; --gpu installs ORT only, not CUDA/cuDNN/Torch.'
  echo 'Without options: preserve existing ORT, or prepare a CPU-only runtime.'
  exit 0
elif [[ "${1:-}" == '--gpu' && $# == 1 ]]; then
  case "$(uname -m)" in
    x86_64) cuda_wheel='onnxruntime-gpu==1.23.2' ;;
    aarch64)
      if [[ ! -r /etc/nv_tegra_release ]] || ! grep -q '# R36' /etc/nv_tegra_release; then
        echo 'Automatic GPU wheel requires JetPack 6 / L4T R36; use --cuda-wheel for other targets.' >&2
        exit 1
      fi
      # Same JetPack 6 / Python 3.10 wheel as the existing ReID installer.
      cuda_wheel='https://github.com/ultralytics/assets/releases/download/v0.0.0/onnxruntime_gpu-1.23.0-cp310-cp310-linux_aarch64.whl'
      ;;
    *) echo 'Unsupported GPU wheel architecture; supply --cuda-wheel explicitly.' >&2; exit 1 ;;
  esac
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
if [[ -e "$pose_runtime" && ! -f "$pose_runtime/pyvenv.cfg" ]]; then
  echo "Refusing to replace a non-venv directory: $pose_runtime" >&2
  exit 1
fi
if [[ ! -x "$pose_runtime/bin/python" ]] || \
  ! "$pose_runtime/bin/python" -m pip --version >/dev/null 2>&1; then
  /usr/bin/python3 -m venv --system-site-packages "$pose_runtime"
fi
export PYTHONNOUSERSITE=1
"$pose_runtime/bin/python" -c '
import pathlib, sys
assert sys.version_info[:2] == (3, 10), "Fall pose requires Python 3.10"
assert sys.prefix != sys.base_prefix, "Expected a dedicated virtualenv"
assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve(), "Wrong runtime"
' "$pose_runtime"
# NumPy 2 wheels break the Ubuntu 22.04 cv_bridge/OpenCV binary interface.
if [[ -n "$cuda_wheel" ]]; then
  # Migrate only a CPU wheel owned by this venv. Never uninstall user/system ORT.
  replaced_cpu=0
  if "$pose_runtime/bin/python" - <<'PY'
import importlib.metadata
from pathlib import Path
import sys
try:
    dist = importlib.metadata.distribution('onnxruntime')
except importlib.metadata.PackageNotFoundError:
    sys.exit(1)
sys.exit(0 if Path(dist.locate_file('')).resolve().is_relative_to(Path(sys.prefix).resolve()) else 1)
PY
  then
    "$pose_runtime/bin/python" -m pip --isolated uninstall -y onnxruntime
    replaced_cpu=1
  fi
  # Reuse a matching venv-local wheel on repeat builds, including offline builds.
  if [[ "$replaced_cpu" == 1 || "${1:-}" == '--cuda-wheel' ]] || \
    ! "$pose_runtime/bin/python" - "$cuda_wheel" <<'PY'
import importlib.metadata
from pathlib import Path
import sys
try:
    dist = importlib.metadata.distribution('onnxruntime-gpu')
except importlib.metadata.PackageNotFoundError:
    sys.exit(1)
expected = '1.23.0' if sys.argv[1].startswith('https:') else '1.23.2'
local = Path(dist.locate_file('')).resolve().is_relative_to(Path(sys.prefix).resolve())
sys.exit(0 if local and dist.version == expected else 1)
PY
  then
    "$pose_runtime/bin/python" -m pip --isolated install --no-cache-dir \
      --force-reinstall --no-deps "$cuda_wheel"
  fi
  # Resolve small Python dependencies, without the cuda/cudnn extras or Torch.
  "$pose_runtime/bin/python" -m pip --isolated install --no-cache-dir \
    'numpy==1.23.5' onnxruntime-gpu
  # ORT searches adjacent NVIDIA wheel libraries, not every Python site directory.
  # Reuse those libraries if already installed; Jetson system CUDA needs no link.
  "$pose_runtime/bin/python" - <<'PY'
from pathlib import Path
import site
import sysconfig
source = Path(site.getusersitepackages()) / 'nvidia'
target = Path(sysconfig.get_path('purelib')) / 'nvidia'
if source.is_dir() and not target.exists() and not target.is_symlink():
    target.symlink_to(source, target_is_directory=True)
    print(f'Reusing existing NVIDIA libraries: {source}')
PY
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
from pathlib import Path
import rclpy, cv_bridge, cv2, numpy, onnxruntime
providers = onnxruntime.get_available_providers()
print(f'Fall pose imports OK; ONNX Runtime {onnxruntime.__version__}; providers={providers}')
if sys.argv[1] and 'CUDAExecutionProvider' not in providers:
    raise RuntimeError('The supplied wheel does not expose CUDAExecutionProvider')
if sys.argv[1] and not Path(onnxruntime.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise RuntimeError('GPU ONNX Runtime must be installed inside the dedicated fall runtime')
if 'CUDAExecutionProvider' not in providers:
    print('GPU acceleration is NOT installed; auto will use CPU with limited threads.')
else:
    print('CUDA provider is present; model initialization still requires a --probe CUDA check.')
PY
echo "Fall pose Python: $pose_runtime/bin/python"
echo 'Bringup and preflight use this runtime by default; explicit Python overrides still take priority.'
echo 'Place the reviewed YOLO26s pose ONNX model in ~/.cache/malbut_perception/yolo26s-pose.onnx.'
echo 'No model is downloaded and no camera or Cloud request is started by this script.'
