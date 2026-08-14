#!/usr/bin/env bash
set -euo pipefail

# Build a reproducible FP32 YOLOv5n ONNX model without changing the ROS Python
# environment. Ubuntu 22.04's OpenCV 4.5.4 cannot load the FP16 release asset
# or the current YOLOv8/YOLO11 exports, so the final load and forward pass are
# checked with the system python3 before the model enters the cache.

cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_perception"
export_env="$cache_root/yolov5-export-env"
model_path="$cache_root/yolov5n.onnx"
yolov5_tag="v7.0"
yolov5_commit="915bbf294bb74c859f0b41f1c23bc395014ea679"

mkdir -p "$cache_root"
temporary_root="$(mktemp -d /tmp/malbut-yolov5.XXXXXX)"
trap 'rm -rf "$temporary_root"' EXIT

git clone --quiet --depth 1 --branch "$yolov5_tag" \
  https://github.com/ultralytics/yolov5.git "$temporary_root/yolov5"
actual_commit="$(git -C "$temporary_root/yolov5" rev-parse HEAD)"
if [[ "$actual_commit" != "$yolov5_commit" ]]; then
  echo "Unexpected YOLOv5 commit: $actual_commit" >&2
  exit 1
fi

curl --fail --location --retry 3 \
  --output "$temporary_root/yolov5/yolov5n.pt" \
  https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.pt

if [[ ! -x "$export_env/bin/python" ]]; then
  python3 -m venv "$export_env"
  "$export_env/bin/pip" install --upgrade pip
  "$export_env/bin/pip" install \
    --index-url https://download.pytorch.org/whl/cpu \
    'torch==2.5.1' 'torchvision==0.20.1'
  "$export_env/bin/pip" install \
    -r "$temporary_root/yolov5/requirements.txt" \
    'onnx==1.16.2' 'onnxsim==0.4.36'
fi

(
  cd "$temporary_root/yolov5"
  TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 "$export_env/bin/python" export.py \
    --weights yolov5n.pt \
    --include onnx \
    --opset 12 \
    --device cpu \
    --imgsz 640 \
    --simplify
)

python3 - "$temporary_root/yolov5/yolov5n.onnx" <<'PY'
import sys

import cv2
import numpy as np

network = cv2.dnn.readNetFromONNX(sys.argv[1])
network.setInput(np.zeros((1, 3, 640, 640), dtype=np.float32))
output = network.forward()
if output.ndim != 3 or output.shape[-1] != 85:
    raise RuntimeError(f'unexpected YOLOv5 output shape: {output.shape}')
print(f'Validated with OpenCV {cv2.__version__}: output={output.shape}')
PY

install -m 0644 "$temporary_root/yolov5/yolov5n.onnx" "$model_path"
sha256sum "$model_path"
echo "Prepared model: $model_path"
