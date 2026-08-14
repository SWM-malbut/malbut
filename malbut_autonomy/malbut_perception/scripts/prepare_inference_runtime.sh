#!/usr/bin/env bash
set -euo pipefail

# Install only the ONNX execution runtime used by the ROS node. Model export
# dependencies remain isolated in the preparation scripts' cache environments.

architecture="$(uname -m)"
python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

if [[ "$architecture" == "x86_64" ]]; then
  runtime="onnxruntime==1.23.2"
elif [[ "$architecture" == "aarch64" ]]; then
  if [[ "$python_version" != "3.10" ]]; then
    echo "JetPack 6 deployment requires Python 3.10; found $python_version" >&2
    exit 1
  fi
  if [[ ! -r /etc/nv_tegra_release ]] || \
    ! grep -q '# R36' /etc/nv_tegra_release; then
    echo "Expected JetPack 6 / L4T R36 on the ROSOrin Orin NX." >&2
    echo "Check /etc/nv_tegra_release before selecting another wheel." >&2
    exit 1
  fi
  runtime="https://github.com/ultralytics/assets/releases/download/v0.0.0/onnxruntime_gpu-1.23.0-cp310-cp310-linux_aarch64.whl"
else
  echo "Unsupported architecture: $architecture" >&2
  exit 1
fi

python3 -m pip install --user --upgrade 'numpy==1.23.5' "$runtime"

python3 - "$architecture" <<'PY'
import sys

import numpy as np
import onnxruntime as ort


architecture = sys.argv[1]
providers = ort.get_available_providers()
print(f'ONNX Runtime {ort.__version__}: {providers}')
print(f'NumPy {np.__version__}')
if architecture == 'aarch64' and not {
    'TensorrtExecutionProvider',
    'CUDAExecutionProvider',
}.intersection(providers):
    raise RuntimeError('Jetson runtime exposes no NVIDIA GPU provider')
PY
