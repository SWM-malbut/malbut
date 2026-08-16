#!/usr/bin/env bash
set -euo pipefail

# Export the official YOLO26n end-to-end weights to a portable FP32 ONNX
# model. The one-to-one head returns final detections without external NMS.
# Runtime acceleration is selected independently by ONNX Runtime.

cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_perception"
export_env="$cache_root/yolo26-export-env"
model_path="$cache_root/yolo26n.onnx"
weights_url="https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt"
weights_sha256="9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"

mkdir -p "$cache_root"
temporary_root="$(mktemp -d /tmp/malbut-yolo26.XXXXXX)"
trap 'rm -rf "$temporary_root"' EXIT

weights="$temporary_root/yolo26n.pt"
curl --fail --location --retry 3 --output "$weights" "$weights_url"
echo "$weights_sha256  $weights" | sha256sum --check

if [[ ! -x "$export_env/bin/python" ]]; then
  if ! python3 -m venv "$export_env"; then
    echo "Install python3-venv, then run this script again." >&2
    exit 1
  fi
  "$export_env/bin/pip" install --upgrade pip
  "$export_env/bin/pip" install \
    --index-url https://download.pytorch.org/whl/cpu \
    'torch==2.5.1' 'torchvision==0.20.1'
  "$export_env/bin/pip" install \
    'numpy==1.26.4' \
    'ultralytics==8.4.55' \
    'onnx==1.20.1' \
    'onnxslim==0.1.82' \
    'onnxruntime==1.23.2'
fi

"$export_env/bin/python" - "$weights" <<'PY'
from pathlib import Path
import sys

from ultralytics import YOLO


weights = Path(sys.argv[1])
exported = YOLO(str(weights)).export(
    format='onnx',
    imgsz=640,
    opset=12,
    simplify=True,
    dynamic=False,
    end2end=True,
    device='cpu',
)
print(f'Exported model: {exported}')
PY

exported="$temporary_root/yolo26n.onnx"
"$export_env/bin/python" - "$exported" <<'PY'
from pathlib import Path
import sys

import numpy as np
import onnxruntime as ort


path = Path(sys.argv[1])
session = ort.InferenceSession(
    str(path), providers=['CPUExecutionProvider']
)
input_name = session.get_inputs()[0].name
output = session.run(
    None,
    {input_name: np.zeros((1, 3, 640, 640), dtype=np.float32)},
)[0]
if output.shape != (1, 300, 6):
    raise RuntimeError(f'unexpected YOLO26 output shape: {output.shape}')
if not np.all(np.isfinite(output)):
    raise RuntimeError('YOLO26 output contains non-finite values')
print(f'Validated with ONNX Runtime {ort.__version__}: output={output.shape}')
PY

install -m 0644 "$exported" "$model_path"
sha256sum "$model_path"
echo "Prepared model: $model_path"
