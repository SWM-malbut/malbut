#!/usr/bin/env bash
set -euo pipefail

# Explicit build of the cloud media runtime; never rebuild manufacturer packages.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_root="$(cd -- "$script_dir/../.." && pwd)"
source_parent="$(dirname -- "$source_root")"
if [[ "$(basename -- "$source_parent")" == src ]]; then
  workspace_dir="$(dirname -- "$source_parent")"
elif [[ "$(basename -- "$(dirname -- "$source_parent")")" == src ]]; then
  workspace_dir="$(dirname -- "$(dirname -- "$source_parent")")"
else
  echo 'Use <workspace>/src/malbut or <workspace>/src/malbut/malbut_test.' >&2
  exit 1
fi
if [[ "${ROS_DISTRO:-}" != humble ]]; then
  echo 'Source ROS 2 Humble and the manufacturer workspace first.' >&2
  exit 1
fi
sdk_root="${KVS_WEBRTC_SDK_ROOT:-$workspace_dir/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1}"
if [[ ! -f "$sdk_root/certs/cert.pem" || ! -d "$sdk_root/build" ]]; then
  echo "Build the pinned SDK first: bash $script_dir/build_kvs_webrtc_sdk.sh $sdk_root" >&2
  exit 1
fi
colcon_executable="$(command -v colcon)"
cd -- "$workspace_dir"
PATH=/usr/bin:/bin "$colcon_executable" --log-base log/malbut_test build \
  --base-paths "$source_root/homecam_agent/homecam_media_agent" \
  --build-base build/malbut_test --install-base install/malbut_test \
  --symlink-install --packages-select homecam_media_agent --cmake-force-configure \
  --cmake-args -DBUILD_TESTING=OFF -DHOMECAM_ENABLE_KVS=ON -DHOMECAM_ENABLE_GSTREAMER=ON \
  -DHOMECAM_ENABLE_CURL=ON "-DKVS_WEBRTC_SDK_ROOT=$sdk_root" \
  "-DHOMECAM_KVS_CA_CERT_PATH=$sdk_root/certs/cert.pem" "$@"
