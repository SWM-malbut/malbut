#!/usr/bin/env bash
# Offline only: no ROS, model downloads, credentials or provider requests.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$repo_root"
export PYTHONPATH="$repo_root/malbut_agent_server:$repo_root/homecam_agent/homecam_detector"
python -m pytest -q -rs \
  malbut_agent_server/test/test_cloud_fall_monitor.py \
  malbut_agent_server/test/test_fall*.py \
  malbut_agent_server/test/test_ollama_cloud_fall.py \
  malbut_agent_server/test/test_vlm*.py \
  homecam_agent/test \
  --ignore=homecam_agent/test/test_robot_launch.py
