#!/usr/bin/env bash
set -euo pipefail

# Export the official OSNet x0.25 MSMT17 person Re-ID checkpoint to a small
# FP32 ONNX model. The export environment stays outside the ROS installation,
# and the result is validated with the system OpenCV before installation.

cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_perception"
export_env="$cache_root/osnet-export-env"
model_path="$cache_root/osnet_x0_25_msmt17.onnx"
source_commit="f8cd150fdf77e8d9e1ed143b7f308c2c609ded50"
weight_id="1Kkx2zW89jq_NETu4u42CFZTMVD5Hwm6e"
weight_sha256="cf55163d78fc44c62c82f85ab62d39f10438679b5abe8c698ae08cfa84aa6e18"

mkdir -p "$cache_root"
temporary_root="$(mktemp -d /tmp/malbut-osnet.XXXXXX)"
trap 'rm -rf "$temporary_root"' EXIT

git clone --quiet https://github.com/KaiyangZhou/deep-person-reid.git \
  "$temporary_root/deep-person-reid"
git -C "$temporary_root/deep-person-reid" checkout --quiet "$source_commit"
actual_commit="$(git -C "$temporary_root/deep-person-reid" rev-parse HEAD)"
if [[ "$actual_commit" != "$source_commit" ]]; then
  echo "Unexpected Torchreid commit: $actual_commit" >&2
  exit 1
fi

weights="$temporary_root/osnet_x0_25_msmt17.pth"
curl --fail --location --retry 3 --output "$weights" \
  "https://drive.usercontent.google.com/download?id=$weight_id&export=download&confirm=t"
echo "$weight_sha256  $weights" | sha256sum --check

if [[ ! -x "$export_env/bin/python" ]]; then
  python3 -m venv "$export_env"
  "$export_env/bin/pip" install --upgrade pip
  "$export_env/bin/pip" install \
    --index-url https://download.pytorch.org/whl/cpu \
    'torch==2.5.1'
  "$export_env/bin/pip" install 'onnx==1.16.2'
fi

exported="$temporary_root/osnet_x0_25_msmt17.onnx"
"$export_env/bin/python" - \
  "$temporary_root/deep-person-reid/torchreid/models/osnet.py" \
  "$weights" "$exported" <<'PY'
import importlib.util
from pathlib import Path
import sys

import onnx
import torch

source_path, weight_path, output_path = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('official_osnet', source_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

state = torch.load(weight_path, map_location='cpu', weights_only=False)
num_classes = int(state['classifier.weight'].shape[0])
model = module.osnet_x0_25(num_classes=num_classes, pretrained=False)
model.load_state_dict(state)
model.eval()
sample = torch.zeros(1, 3, 256, 128)
torch.onnx.export(
    model,
    sample,
    output_path,
    input_names=['images'],
    output_names=['embeddings'],
    opset_version=12,
    do_constant_folding=True,
)
onnx.checker.check_model(onnx.load(output_path))
PY

python3 - "$exported" <<'PY'
import sys

import cv2
import numpy as np

network = cv2.dnn.readNetFromONNX(sys.argv[1])
network.setInput(np.zeros((1, 3, 256, 128), dtype=np.float32))
output = network.forward()
if output.shape != (1, 512) or not np.all(np.isfinite(output)):
    raise RuntimeError(f'unexpected OSNet output: {output.shape}')
print(f'Validated with OpenCV {cv2.__version__}: output={output.shape}')
PY

install -m 0644 "$exported" "$model_path"
sha256sum "$model_path"
echo "Prepared model: $model_path"
