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
homecam_config_key_allowed HOMECAM_EVENT_CLIPS_ENABLED
homecam_config_key_allowed HOMECAM_NAVIGATION_STATUS_TOPIC
homecam_config_key_allowed HOMECAM_POSE_MODEL_PATH
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

map_store="$temporary_dir/maps"
mkdir -p "$map_store"
printf '%s\n' \
  '{"initial_pose":{"x":-1.25,"y":2.5,"yaw":0.75}}' \
  > "$map_store/active.json"
[[ "$(homecam_saved_map_pose "$map_store")" == '-1.25 2.5 0.75' ]]
printf '%s\n' \
  '{"format":"malbut-pose-checkpoint/v1","map_id":"home","map_revision":"r1","pose":{"x":3.25,"y":-4.5,"yaw":1.5}}' \
  > "$map_store/last-localized-pose.json"
printf '%s\n' \
  '{"map_id":"home","map_revision":"r1","initial_pose":{"x":-1.25,"y":2.5,"yaw":0.75}}' \
  > "$map_store/active.json"
[[ "$(homecam_simulation_bootstrap_pose "$map_store")" == \
  'checkpoint 3.25 -4.5 1.5' ]]
printf '%s\n' \
  '{"map_id":"home","map_revision":"r2","initial_pose":{"x":-1.25,"y":2.5,"yaw":0.75}}' \
  > "$map_store/active.json"
[[ "$(homecam_simulation_bootstrap_pose "$map_store")" == \
  'map -1.25 2.5 0.75' ]]
printf '%s\n' \
  '{"initial_pose":{"x":"NaN","y":2.5,"yaw":0.75}}' \
  > "$map_store/active.json"
if homecam_saved_map_pose "$map_store" >/dev/null 2>&1; then
  printf 'non-finite saved pose should fail\n' >&2
  exit 1
fi
pose_command=(ros2 launch example)
homecam_append_pose_arguments pose_command -1.25 2.5 0.75
[[ "${pose_command[*]}" == \
  'ros2 launch example x:=-1.25 y:=2.5 yaw:=0.75' ]]

[[ $((10#08)) -eq 8 ]]

source_token="$temporary_dir/source.token"
printf 'hc1.123e4567-e89b-42d3-a456-426614174000.%064d' 0 > "$source_token"
chmod 600 "$source_token"
generated_config="$temporary_dir/generated/sim.env"
"$repo_root/scripts/configure_sim_device.sh" \
  --config "$generated_config" \
  --device-id gazebo-test \
  --backend-url https://example.com \
  --token-file "$source_token" >/dev/null
grep -Fqx 'HOMECAM_GAZEBO_GUI=false' "$generated_config"
grep -Fqx 'HOMECAM_GAZEBO_HEADLESS=true' "$generated_config"
grep -Fqx 'HOMECAM_FORCE_MAPPING=false' "$generated_config"
grep -Fqx 'HOMECAM_EVENT_CLIPS_ENABLED=true' "$generated_config"
grep -Fqx 'HOMECAM_POSE_MODEL_PATH=' "$generated_config"
grep -Fqx \
  'HOMECAM_NAVIGATION_STATUS_TOPIC=/navigate_to_pose/_action/status' \
  "$generated_config"

runner="$repo_root/scripts/run_gazebo_homecam.sh"
grep -Fq 'ros2 launch malbut_gazebo worlds.launch.py' "$runner"
grep -Fq '"simulation:=false"' "$runner"
grep -Fq '"runtime_request_file:=$runtime_control_file"' "$runner"
grep -Fq '"trusted_initial_pose:=true"' "$runner"
grep -Fq '"trusted_localization_handoff:=$trust_localization_handoff"' \
  "$runner"
grep -Fq 'start_robot_stack true true' "$runner"
grep -Fq \
  '"navigation_status_topic:=$HOMECAM_NAVIGATION_STATUS_TOPIC"' \
  "$runner"
grep -Fq '"pose_model_path:=$HOMECAM_POSE_MODEL_PATH"' "$runner"

event_person="$repo_root/scripts/spawn_event_test_person.sh"
grep -Fq -- '--world small_house' "$event_person"
grep -Fq -- '--x 2.5' "$event_person"
grep -Fq -- '--y -3.6' "$event_person"
if grep -Eq '^[[:space:]]*(exec[[:space:]]+)?ros2.*set_pose' "$event_person"; then
  printf 'event person fixture must not teleport the actor\n' >&2
  exit 1
fi

dependencies="$repo_root/scripts/install_dependencies.sh"
grep -Fq 'python3-venv' "$dependencies"
grep -Fq 'ros-humble-action-msgs' "$dependencies"

model_preparer="$repo_root/../malbut_autonomy/malbut_perception/scripts/prepare_yolo26_model.sh"
grep -Fq '! -x "$export_env/bin/pip"' "$model_preparer"
grep -Fq 'rm -rf -- "$export_env"' "$model_preparer"

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
