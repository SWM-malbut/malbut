#!/usr/bin/env bash
# Build media/detector packages without installing the navigation/speech stack.
set -eo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
workspace="${HOMECAM_CI_WORKSPACE:?Set an isolated CI workspace}"
if [[ "$workspace" != /* || "$workspace" == / ]]; then
  echo 'HOMECAM_CI_WORKSPACE must be a specific absolute directory' >&2
  exit 2
fi
source "$repo_root/homecam_agent/scripts/lib/portable_runtime.sh"
homecam_source_setup_file /opt/ros/humble/setup.bash
set -u
sdk_root="$workspace/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1"
bash "$repo_root/homecam_agent/scripts/build_kvs_webrtc_sdk.sh" "$sdk_root"
cd "$workspace"
colcon build --base-paths "$repo_root/homecam_agent" "$repo_root/malbut_interfaces" \
  --symlink-install --packages-select homecam_detector homecam_media_agent malbut_interfaces \
  --cmake-args -DHOMECAM_ENABLE_KVS=ON \
  "-DKVS_WEBRTC_SDK_ROOT=$sdk_root" \
  "-DHOMECAM_KVS_CA_CERT_PATH=$sdk_root/certs/cert.pem"
homecam_source_setup_file "$workspace/install/setup.bash"
homecam_prepare_media_runtime "$workspace"
bash "$repo_root/homecam_agent/test/test_portable_runtime.sh"
export PYTHONPATH="$repo_root/malbut_agent_server${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest -q "$repo_root/homecam_agent/test"
# Gazebo contracts run in the ROS job for simulation/bridge changes or full CI.
colcon test --base-paths "$repo_root/homecam_agent" \
  --packages-select homecam_detector homecam_media_agent \
  --return-code-on-test-failure
colcon test-result --verbose
