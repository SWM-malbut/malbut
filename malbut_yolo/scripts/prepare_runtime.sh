#!/usr/bin/env bash
set -euo pipefail

# Installs only into this runtime venv. Never install a desktop CUDA wheel
# over the JetPack PyTorch build or change /opt/ros/humble.
runtime_dir="${MALBUT_YOLO_RUNTIME:-${XDG_CACHE_HOME:-$HOME/.cache}/malbut_yolo/runtime}"
runtime_python="$runtime_dir/bin/python"
architecture="$(uname -m)"

if [[ ! -x "$runtime_python" ]] || ! "$runtime_python" -m pip --version >/dev/null 2>&1; then
  python3 -m venv --system-site-packages "$runtime_dir" || {
    echo 'Install python3-venv, then rerun this script.' >&2
    exit 1
  }
fi

if [[ "$architecture" == x86_64 ]]; then
  "$runtime_python" -m pip install \
    'torch==2.7.1' 'torchvision==0.22.1' \
    --index-url https://download.pytorch.org/whl/cu128
elif [[ "$architecture" == aarch64 && -f /etc/nv_tegra_release ]]; then
  "$runtime_python" -c 'import torch, torchvision; assert torch.cuda.is_available()' || {
    echo 'Install NVIDIA PyTorch/torchvision matching THIS JetPack first.' >&2
    echo 'https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform/' >&2
    echo 'Alternatively install the NVIDIA wheel inside the runtime venv.' >&2
    exit 1
  }
else
  echo "Unsupported GPU platform: $architecture" >&2
  exit 1
fi

# Freeze the already-selected Torch builds, especially NVIDIA Jetson wheels.
# Humble cv_bridge requires the NumPy 1.x ABI.
"$runtime_python" -m pip install \
  --constraint <("$runtime_python" -c \
    'import torch, torchvision; print("torch=="+torch.__version__); print("torchvision=="+torchvision.__version__)') \
  'numpy==1.26.4' 'opencv-python==4.11.0.86' 'ultralytics==8.4.6' \
  'lap>=0.5.12' pyyaml

"$runtime_python" - <<'PY'
import torch
import torchvision
from ultralytics import YOLO

assert torch.cuda.is_available(), 'CUDA unavailable; no silent CPU fallback'
value = torch.ones(1, device='cuda') + 1
torch.cuda.synchronize()
print(f'CUDA verified: {torch.cuda.get_device_name(0)}; Torch {torch.__version__}')
PY

model_dir="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_perception"
mkdir -p "$model_dir"
model_path="$model_dir/yolo26n.pt"
if [[ ! -f "$model_path" ]]; then
  curl --fail --location --retry 3 \
    https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt \
    --output "$model_path"
fi
echo "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef  $model_path" | sha256sum --check
