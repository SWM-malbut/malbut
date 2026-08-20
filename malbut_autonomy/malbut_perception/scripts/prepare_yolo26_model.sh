#!/usr/bin/env bash
set -euo pipefail

# Export the official YOLO26n detection and human-pose weights to portable
# FP32 ONNX models. General detection remains the primary person/pet stage;
# pose is a person-gated secondary stage and is not a fall classifier.

cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_perception"
export_env="$cache_root/yolo26-export-env"
runtime_root="$cache_root/yolo26-runtime"
runtime_site="$runtime_root/site-packages"
model_path="$cache_root/yolo26n.onnx"
pose_model_path="$cache_root/yolo26n-pose.onnx"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(cd -- "$script_dir/../../.." && pwd -P)"
weights_url="https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt"
weights_sha256="9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"
pose_weights_url="https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n-pose.pt"
pose_weights_sha256="eb3bb8268828aeaf515cec23a4bfafd793944a86fe9af94ba7823609c14522a9"

mkdir -p "$cache_root"
temporary_root="$(mktemp -d /tmp/malbut-yolo26.XXXXXX)"
trap 'rm -rf "$temporary_root"' EXIT

weights="$temporary_root/yolo26n.pt"
pose_weights="$temporary_root/yolo26n-pose.pt"
curl --fail --location --retry 3 --output "$weights" "$weights_url"
curl --fail --location --retry 3 --output "$pose_weights" "$pose_weights_url"
echo "$weights_sha256  $weights" | sha256sum --check
echo "$pose_weights_sha256  $pose_weights" | sha256sum --check

if [[ ! -x "$export_env/bin/python" || ! -x "$export_env/bin/pip" ]]; then
  # `python -m venv` can leave an executable Python symlink behind when the
  # distro's ensurepip package is missing. Treat that partial directory as
  # invalid so the next run repairs it after python3-venv is installed.
  rm -rf -- "$export_env"
  if ! python3 -m venv "$export_env"; then
    echo "Install python3-venv, then run this script again." >&2
    exit 1
  fi
  "$export_env/bin/pip" install --upgrade pip
fi

if ! "$export_env/bin/python" - <<'PY'
import numpy
import onnxruntime
import onnxslim
import torch
import torchvision
import ultralytics
PY
then
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

# Keep inference dependencies separate from the Ultralytics export
# environment.  The exporter installs opencv-python, whose bundled Qt plugin
# path can break RViz when the whole environment is added to PYTHONPATH.
if ! PYTHONPATH="$runtime_site" python3 - <<'PY'
import numpy
import onnxruntime
PY
then
  runtime_stage="$temporary_root/runtime-site"
  "$export_env/bin/pip" install \
    --target "$runtime_stage" \
    'numpy==1.26.4' \
    'onnxruntime==1.23.2'
  rm -rf -- "$runtime_root"
  mkdir -p -- "$runtime_root"
  mv -- "$runtime_stage" "$runtime_site"
fi

"$export_env/bin/python" - "$weights" "$pose_weights" <<'PY'
from pathlib import Path
import sys

from ultralytics import YOLO


for weights_arg in sys.argv[1:]:
    weights = Path(weights_arg)
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
pose_exported="$temporary_root/yolo26n-pose.onnx"
"$export_env/bin/python" - "$exported" "$pose_exported" <<'PY'
from pathlib import Path
import sys

import numpy as np
import onnxruntime as ort


for model_arg, expected_shape in zip(
    sys.argv[1:], ((1, 300, 6), (1, 300, 57))
):
    path = Path(model_arg)
    session = ort.InferenceSession(
        str(path), providers=['CPUExecutionProvider']
    )
    input_name = session.get_inputs()[0].name
    output = session.run(
        None,
        {input_name: np.zeros((1, 3, 640, 640), dtype=np.float32)},
    )[0]
    if output.shape != expected_shape:
        raise RuntimeError(
            f'unexpected {path.name} output shape: {output.shape}'
        )
    if not np.all(np.isfinite(output)):
        raise RuntimeError(f'{path.name} output contains non-finite values')
    print(
        f'Validated {path.name} with ONNX Runtime {ort.__version__}: '
        f'output={output.shape}'
    )
PY

install -m 0644 "$exported" "$model_path"
install -m 0644 "$pose_exported" "$pose_model_path"
PYTHONPATH="$runtime_site:$repository_root/homecam_agent/homecam_detector${PYTHONPATH:+:$PYTHONPATH}" \
  python3 - "$model_path" "$pose_model_path" <<'PY'
import sys

import numpy as np

from homecam_detector.yolo import YoloOnnxDetector
from homecam_detector.pose import PersonPoseEstimator


detector = YoloOnnxDetector(sys.argv[1])
result = detector.detect(np.zeros((400, 640, 3), dtype=np.uint8))
if not isinstance(result, dict):
    raise RuntimeError('homecam detector returned an invalid result')
pose_estimator = PersonPoseEstimator(sys.argv[2])
pose = pose_estimator.estimate(np.zeros((400, 640, 3), dtype=np.uint8))
if pose is not None:
    raise RuntimeError('blank frame unexpectedly returned a person pose')
print('Validated detection and pose with the homecam runtime loaders')
PY
sha256sum "$model_path" "$pose_model_path"
echo "Prepared detection model: $model_path"
echo "Prepared person pose model: $pose_model_path"
