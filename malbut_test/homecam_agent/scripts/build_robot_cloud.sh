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
# Unlike generic health-only builds, robot cloud builds require real media I/O.
for build_tool in cmake git m4 make cc c++ pkg-config colcon; do
  if ! command -v "$build_tool" >/dev/null 2>&1; then
    echo "Missing cloud media build tool: $build_tool" >&2
    echo "Install dependencies first: bash $script_dir/install_dependencies.sh" >&2
    exit 1
  fi
done
if ! PATH=/usr/bin:/bin pkg-config --exists gstreamer-1.0 gstreamer-app-1.0 libcurl openssl; then
  echo 'Cloud media build requires libgstreamer1.0-dev, libgstreamer-plugins-base1.0-dev, libcurl4-openssl-dev and libssl-dev.' >&2
  echo "Resolve dependency installation errors first: bash $script_dir/install_dependencies.sh" >&2
  exit 1
fi
sdk_root="${KVS_WEBRTC_SDK_ROOT:-$workspace_dir/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1}"
# First run clones the pinned SDK; subsequent runs reuse its incremental build.
bash "$script_dir/build_kvs_webrtc_sdk.sh" "$sdk_root"
colcon_executable="$(command -v colcon)"
cd -- "$workspace_dir"
PATH=/usr/bin:/bin "$colcon_executable" --log-base log/malbut_test build \
  --base-paths "$source_root/homecam_agent/homecam_media_agent" \
  --build-base build/malbut_test --install-base install/malbut_test \
  --symlink-install --packages-select homecam_media_agent --cmake-force-configure \
  --cmake-args -DBUILD_TESTING=OFF -DHOMECAM_ENABLE_KVS=ON -DHOMECAM_ENABLE_GSTREAMER=ON \
  -DHOMECAM_ENABLE_CURL=ON "-DKVS_WEBRTC_SDK_ROOT=$sdk_root" \
  "-DHOMECAM_KVS_CA_CERT_PATH=$sdk_root/certs/cert.pem" "$@"
