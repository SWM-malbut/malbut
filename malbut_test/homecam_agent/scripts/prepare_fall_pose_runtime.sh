#!/usr/bin/env bash
set -euo pipefail

# Run explicitly on the target; never replace Jetson's Torch/CUDA or system pip.
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
/usr/bin/python3 -m venv --system-site-packages "$pose_runtime"
# NumPy 2 wheels break the Ubuntu 22.04 cv_bridge/OpenCV binary interface.
"$pose_runtime/bin/python" -m pip --isolated install 'numpy<2' 'onnxruntime>=1.17,<2'
"$pose_runtime/bin/python" -c 'import rclpy, cv_bridge, cv2, numpy, onnxruntime; print("Fall pose imports OK; CPU backend")'
echo "Fall pose Python: $pose_runtime/bin/python"
echo 'Place the reviewed YOLO26s pose ONNX model in ~/.cache/malbut_perception/yolo26s-pose.onnx.'
echo 'No model is downloaded and no camera or Cloud request is started by this script.'
