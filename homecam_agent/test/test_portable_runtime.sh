#!/usr/bin/env bash
set -euo pipefail

readonly test_dir="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
  pwd -P
)"
readonly repo_root="$(
  cd -- "$test_dir/.." >/dev/null 2>&1
  pwd -P
)"
# shellcheck source=scripts/lib/portable_runtime.sh
source "$repo_root/scripts/lib/portable_runtime.sh"

snapshot=$'/camera/color/camera_info [sensor_msgs/msg/CameraInfo]\n/camera/color/image_raw [sensor_msgs/msg/Image]\n/camera/depth/image_raw [sensor_msgs/msg/Image]\n/odom [nav_msgs/msg/Odometry]'
image_topic="$(homecam_discover_image_topic "$snapshot" "")"
[[ "$image_topic" == /camera/color/image_raw ]]
camera_info_topic="$(
  homecam_discover_camera_info_topic "$snapshot" "$image_topic" ""
)"
[[ "$camera_info_topic" == /camera/color/camera_info ]]

legacy_snapshot=$'/depth_cam/depth_cam [sensor_msgs/msg/Image]\n/depth_cam/rgb/camera_info [sensor_msgs/msg/CameraInfo]'
legacy_image="$(homecam_discover_image_topic "$legacy_snapshot" "")"
[[ "$legacy_image" == /depth_cam/depth_cam ]]
legacy_info="$(
  homecam_discover_camera_info_topic "$legacy_snapshot" "$legacy_image" ""
)"
[[ "$legacy_info" == /depth_cam/rgb/camera_info ]]

if homecam_discover_image_topic "$snapshot" /missing/image >/dev/null; then
  printf 'explicit missing image topic should fail\n' >&2
  exit 1
fi

temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "$temporary_dir"' EXIT
config_path="$temporary_dir/sim.env"
printf 'HOMECAM_WORLD=small_house\nHOMECAM_START_GAZEBO=true\n' > "$config_path"
chmod 600 "$config_path"
unset HOMECAM_WORLD HOMECAM_START_GAZEBO
homecam_load_config "$config_path"
[[ "$HOMECAM_WORLD" == small_house ]]
[[ "$HOMECAM_START_GAZEBO" == true ]]
homecam_config_key_allowed HOMECAM_MAP_STORE
homecam_config_key_allowed HOMECAM_MAP_WEB_HOST
homecam_config_key_allowed HOMECAM_MAP_WEB_PORT
homecam_config_key_allowed HOMECAM_MAP_RVIZ
if homecam_config_key_allowed HOMECAM_MAP_DELETE_ON_START; then
  printf 'destructive map configuration key should be rejected\n' >&2
  exit 1
fi

homecam_validate_backend_url https://example.com
homecam_validate_backend_url http://localhost:3000
homecam_validate_backend_url 'http://[::1]:3000'
if homecam_validate_backend_url https://; then
  printf 'URL without an authority should fail\n' >&2
  exit 1
fi
if homecam_validate_backend_url https://user@example.com; then
  printf 'URL with userinfo should fail\n' >&2
  exit 1
fi
if homecam_validate_backend_url http://192.168.0.2:3000; then
  printf 'non-loopback plaintext URL should fail\n' >&2
  exit 1
fi
homecam_validate_backend_url HTTPS://example.com

[[ $((10#08)) -eq 8 ]]

standalone_workspace="$temporary_dir/standalone_workspace"
mkdir -p "$standalone_workspace/src/homecam_agent"
[[ "$(
  homecam_workspace_from_repo "$standalone_workspace/src/homecam_agent"
)" == "$standalone_workspace" ]]

embedded_workspace="$temporary_dir/embedded_workspace"
mkdir -p "$embedded_workspace/src/malbut/homecam_agent"
[[ "$(
  homecam_workspace_from_repo \
    "$embedded_workspace/src/malbut/homecam_agent"
)" == "$embedded_workspace" ]]

repository_root_workspace="$temporary_dir/repository_root_workspace"
mkdir -p \
  "$repository_root_workspace/homecam_agent/homecam_detector" \
  "$repository_root_workspace/homecam_agent/homecam_media_agent" \
  "$repository_root_workspace/malbut_gazebo"
: > "$repository_root_workspace/homecam_agent/homecam_detector/package.xml"
: > "$repository_root_workspace/homecam_agent/homecam_media_agent/package.xml"
: > "$repository_root_workspace/malbut_gazebo/package.xml"
[[ "$(
  homecam_workspace_from_repo "$repository_root_workspace/homecam_agent"
)" == "$repository_root_workspace" ]]

chmod 644 "$config_path"
unset HOMECAM_WORLD HOMECAM_START_GAZEBO
if homecam_load_config "$config_path" 2>/dev/null; then
  printf 'group-readable config should fail\n' >&2
  exit 1
fi

printf 'portable runtime helper tests passed\n'
