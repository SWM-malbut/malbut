#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly script_dir="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
  pwd -P
)"
readonly repo_root="$(
  cd -- "$script_dir/.." >/dev/null 2>&1
  pwd -P
)"
# shellcheck source=scripts/lib/portable_runtime.sh
source "$script_dir/lib/portable_runtime.sh"

config_path="$(homecam_default_config_path)"
reuse_gazebo=false
check_only=false
gazebo_pid=""
robot_pid=""
homecam_pid=""
runtime_control_file=""
runtime_supervisor_file=""
runtime_supervisor_refreshed_at=0
simulation_bootstrap_pose=()

refresh_runtime_supervisor() {
  # 요청 파일을 실제로 소비하는 감독자가 살아 있음을 남긴다. 이 표식이
  # 없으면 cloud_robot_sync 가 모드 전환 요청을 수락하지 않는다.
  [[ -n "$runtime_supervisor_file" ]] || return 0
  local temporary="$runtime_supervisor_file.$$.tmp"
  printf '%s %s\n' "$$" "$(date +%s)" > "$temporary" 2>/dev/null || return 0
  chmod 600 -- "$temporary" 2>/dev/null || true
  mv -f -- "$temporary" "$runtime_supervisor_file" 2>/dev/null || true
  runtime_supervisor_refreshed_at=$SECONDS
}

usage() {
  cat <<'EOF'
Usage:
  run_gazebo_homecam.sh [options]

Options:
  --config PATH     Device configuration file
  --reuse-gazebo    Use an already-running Gazebo/ROS camera
  --check-only      Validate dependencies and camera frames, then exit
  -h, --help        Show this help

By default the script starts the managed Malbut device stack. It creates a map
only when the device has no saved revision, otherwise it reuses the device's
persistent map. It then discovers RGB and CameraInfo topics, verifies a frame,
and starts the KVS homecam agent. Ctrl+C stops only processes started here.
EOF
}

while (($# > 0)); do
  case "$1" in
    --config)
      (($# >= 2)) || { usage >&2; exit 2; }
      config_path="$2"
      shift 2
      ;;
    --reuse-gazebo)
      reuse_gazebo=true
      shift
      ;;
    --check-only)
      check_only=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

config_path="$(realpath -m -- "$config_path")"
if [[ -f "$config_path" ]]; then
  homecam_load_config "$config_path"
elif ! "$check_only"; then
  homecam_die \
    "device configuration is missing: $config_path (run configure_sim_device.sh)"
  exit 1
fi

: "${HOMECAM_WORLD:=small_house}"
: "${HOMECAM_START_GAZEBO:=true}"
: "${HOMECAM_GAZEBO_GUI:=false}"
: "${HOMECAM_GAZEBO_HEADLESS:=true}"
: "${HOMECAM_MAP_STORE:=}"
: "${HOMECAM_MAP_WEB_HOST:=127.0.0.1}"
: "${HOMECAM_MAP_WEB_PORT:=8765}"
: "${HOMECAM_MAP_RVIZ:=true}"
: "${HOMECAM_CLOUD_MAP_ENABLED:=true}"
: "${HOMECAM_IMAGE_TOPIC:=}"
: "${HOMECAM_CAMERA_INFO_TOPIC:=}"
: "${HOMECAM_ODOM_TOPIC:=/odom}"
: "${HOMECAM_NAVIGATION_STATUS_TOPIC:=/navigate_to_pose/_action/status}"
: "${HOMECAM_AUDIO_SOURCE:=default}"
: "${HOMECAM_AUDIO_SINK:=default}"
: "${HOMECAM_MICROPHONE_ENABLED:=false}"
: "${HOMECAM_MODEL_PATH:=}"
: "${HOMECAM_POSE_MODEL_PATH:=}"
: "${HOMECAM_MONITORING_ENABLED:=false}"
: "${HOMECAM_EVENT_CLIPS_ENABLED:=true}"
: "${HOMECAM_FORCE_MAPPING:=false}"
: "${HOMECAM_TOPIC_TIMEOUT_SECONDS:=90}"

if [[ -z "$HOMECAM_MODEL_PATH" ]]; then
  homecam_model_cache_root="${XDG_CACHE_HOME:-${HOME}/.cache}/malbut_perception"
  homecam_cached_model="$homecam_model_cache_root/yolo26n.onnx"
  if [[ -f "$homecam_cached_model" ]]; then
    HOMECAM_MODEL_PATH="$homecam_cached_model"
    homecam_log "using cached YOLO model: $HOMECAM_MODEL_PATH"
  else
    homecam_warn \
      "YOLO model not found; person and pet labels are disabled. Run "\
      "malbut_autonomy/malbut_perception/scripts/prepare_yolo26_model.sh once."
  fi
fi

if [[ -z "$HOMECAM_POSE_MODEL_PATH" ]]; then
  homecam_pose_cache_root="${XDG_CACHE_HOME:-${HOME}/.cache}/malbut_perception"
  homecam_cached_pose_model="$homecam_pose_cache_root/yolo26n-pose.onnx"
  if [[ -f "$homecam_cached_pose_model" ]]; then
    HOMECAM_POSE_MODEL_PATH="$homecam_cached_pose_model"
    homecam_log "using cached person pose model: $HOMECAM_POSE_MODEL_PATH"
  else
    homecam_warn \
      "YOLO pose model not found; secondary person pose is disabled. Run "\
      "malbut_autonomy/malbut_perception/scripts/prepare_yolo26_model.sh once."
  fi
fi

if [[ -n "$HOMECAM_MODEL_PATH" || -n "$HOMECAM_POSE_MODEL_PATH" ]]; then
  homecam_onnx_runtime_site="${HOMECAM_ONNX_RUNTIME_SITE_PACKAGES:-${XDG_CACHE_HOME:-${HOME}/.cache}/malbut_perception/yolo26-runtime/site-packages}"
  if [[ -d "$homecam_onnx_runtime_site/onnxruntime" ]]; then
    export PYTHONPATH="$homecam_onnx_runtime_site${PYTHONPATH:+:$PYTHONPATH}"
    homecam_log "using ONNX Runtime from the local model environment"
  fi
fi

if ! "$check_only"; then
  homecam_validate_device_config "$config_path"
fi
homecam_validate_boolean HOMECAM_START_GAZEBO "$HOMECAM_START_GAZEBO"
homecam_validate_boolean HOMECAM_GAZEBO_GUI "$HOMECAM_GAZEBO_GUI"
homecam_validate_boolean HOMECAM_GAZEBO_HEADLESS "$HOMECAM_GAZEBO_HEADLESS"
homecam_validate_boolean HOMECAM_MAP_RVIZ "$HOMECAM_MAP_RVIZ"
homecam_validate_boolean \
  HOMECAM_CLOUD_MAP_ENABLED "$HOMECAM_CLOUD_MAP_ENABLED"
homecam_validate_boolean \
  HOMECAM_MONITORING_ENABLED "$HOMECAM_MONITORING_ENABLED"
homecam_validate_boolean \
  HOMECAM_EVENT_CLIPS_ENABLED "$HOMECAM_EVENT_CLIPS_ENABLED"
homecam_validate_boolean HOMECAM_FORCE_MAPPING "$HOMECAM_FORCE_MAPPING"
homecam_validate_boolean \
  HOMECAM_MICROPHONE_ENABLED "$HOMECAM_MICROPHONE_ENABLED"
[[ "$HOMECAM_TOPIC_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] &&
  ((10#$HOMECAM_TOPIC_TIMEOUT_SECONDS >= 5)) || {
  homecam_die "HOMECAM_TOPIC_TIMEOUT_SECONDS must be an integer >= 5"
  exit 1
}
HOMECAM_TOPIC_TIMEOUT_SECONDS=$((10#$HOMECAM_TOPIC_TIMEOUT_SECONDS))
[[ "$HOMECAM_MAP_WEB_PORT" =~ ^[0-9]+$ ]] &&
  ((10#$HOMECAM_MAP_WEB_PORT >= 1)) &&
  ((10#$HOMECAM_MAP_WEB_PORT <= 65535)) || {
  homecam_die "HOMECAM_MAP_WEB_PORT must be an integer from 1 to 65535"
  exit 1
}
HOMECAM_MAP_WEB_PORT=$((10#$HOMECAM_MAP_WEB_PORT))
case "$HOMECAM_MAP_WEB_HOST" in
  127.0.0.1|localhost|::1) ;;
  *)
    homecam_die "HOMECAM_MAP_WEB_HOST must be a loopback host"
    exit 1
    ;;
esac
if [[ -z "$HOMECAM_MAP_STORE" ]]; then
  map_data_base="${XDG_DATA_HOME:-${HOME:?HOME is required}/.local/share}"
  map_device_id="${HOMECAM_DEVICE_ID:-gazebo-homecam}"
  HOMECAM_MAP_STORE="$map_data_base/malbut/devices/$map_device_id/maps"
fi
HOMECAM_MAP_STORE="$(realpath -m -- "$HOMECAM_MAP_STORE")"
if homecam_is_true "$HOMECAM_GAZEBO_HEADLESS"; then
  HOMECAM_GAZEBO_GUI=false
fi
if "$reuse_gazebo"; then
  HOMECAM_START_GAZEBO=false
fi

homecam_source_runtime "$repo_root"
homecam_validate_detector_runtime \
  "$HOMECAM_MODEL_PATH" "$HOMECAM_POSE_MODEL_PATH"
homecam_prepare_media_runtime "$HOMECAM_WORKSPACE"
if ! "$check_only" && homecam_is_true "$HOMECAM_START_GAZEBO"; then
  saved_pose_line="$(
    homecam_simulation_bootstrap_pose \
      "$HOMECAM_MAP_STORE" 2>/dev/null || true
  )"
  if [[ -n "$saved_pose_line" ]]; then
    pose_source=""
    pose_x=""
    pose_y=""
    pose_yaw=""
    read -r pose_source pose_x pose_y pose_yaw <<< "$saved_pose_line"
    simulation_bootstrap_pose=("$pose_x" "$pose_y" "$pose_yaw")
    if [[ -z "$pose_source" || -z "$pose_x" || -z "$pose_y" || -z "$pose_yaw" ]]; then
      homecam_die "saved map pose is malformed"
      exit 1
    fi
    if [[ "$pose_source" == "checkpoint" ]]; then
      homecam_log "restoring simulator spawn from the last verified pose"
    else
      homecam_log "no pose checkpoint; using the map creation pose"
    fi
  fi
fi
command -v setsid >/dev/null 2>&1 || {
  homecam_die "setsid is required to manage the Gazebo and homecam process groups"
  exit 1
}

if ! "$check_only"; then
  command -v flock >/dev/null 2>&1 || {
    homecam_die "flock is required to prevent duplicate homecam sessions"
    exit 1
  }
  runtime_base="${XDG_RUNTIME_DIR:-/tmp/homecam-runtime-$EUID}"
  if [[ ! -d "$runtime_base" ]]; then
    mkdir -p -- "$runtime_base"
    chmod 700 -- "$runtime_base"
  fi
  runtime_owner="$(stat -c '%u' -- "$runtime_base")"
  runtime_mode="$(stat -c '%a' -- "$runtime_base")"
  if [[ "$runtime_owner" != "$EUID" ]] ||
    (((8#$runtime_mode) & 077))
  then
    homecam_die "runtime lock directory is not private: $runtime_base"
    exit 1
  fi
  exec {homecam_lock_fd}> \
    "$runtime_base/sim-${ROS_DOMAIN_ID:-0}.lock"
  if ! flock -n "$homecam_lock_fd"; then
    homecam_die \
      "another Gazebo homecam launcher is already active in ROS domain ${ROS_DOMAIN_ID:-0}"
    exit 1
  fi
  runtime_control_file="$runtime_base/sim-${ROS_DOMAIN_ID:-0}.mode-request"
  rm -f -- "$runtime_control_file"
  runtime_supervisor_file="$runtime_control_file.supervisor"
  refresh_runtime_supervisor
fi

cleanup_process_group() {
  local child_pid="$1"
  local signal_name="$2"
  [[ -n "$child_pid" ]] || return 0
  if kill -0 -- "-$child_pid" >/dev/null 2>&1; then
    kill "-$signal_name" -- "-$child_pid" >/dev/null 2>&1 || true
  fi
}

process_group_alive() {
  local child_pid="$1"
  [[ -n "$child_pid" ]] || return 1
  if kill -0 -- "-$child_pid" >/dev/null 2>&1; then
    return 0
  fi
  # setsid 가 새 프로세스 그룹을 만들기 전까지는 자식이 아직 이 셸의
  # 그룹에 속한다. 그 짧은 구간에 그룹만 보고 죽었다고 판정하면 감독
  # 루프가 첫 회차에 wait 로 빠지고, 대상이 멀쩡히 살아 있으므로 영영
  # 돌아오지 않는다. 그러면 런타임 모드 요청을 아무도 소비하지 못한다.
  kill -0 -- "$child_pid" >/dev/null 2>&1
}

cleanup() {
  local exit_status=$?
  trap - EXIT INT TERM
  [[ -z "$runtime_supervisor_file" ]] || rm -f -- "$runtime_supervisor_file"
  cleanup_process_group "$homecam_pid" INT
  cleanup_process_group "$robot_pid" INT
  cleanup_process_group "$gazebo_pid" INT
  local deadline=$((SECONDS + 8))
  while ((SECONDS < deadline)); do
    local alive=false
    if process_group_alive "$homecam_pid"; then
      alive=true
    fi
    if process_group_alive "$gazebo_pid"; then
      alive=true
    fi
    if process_group_alive "$robot_pid"; then
      alive=true
    fi
    "$alive" || break
    sleep 0.2
  done
  cleanup_process_group "$homecam_pid" TERM
  cleanup_process_group "$robot_pid" TERM
  cleanup_process_group "$gazebo_pid" TERM
  deadline=$((SECONDS + 3))
  while ((SECONDS < deadline)); do
    if ! process_group_alive "$homecam_pid" &&
      ! process_group_alive "$robot_pid" &&
      ! process_group_alive "$gazebo_pid"
    then
      break
    fi
    sleep 0.2
  done
  cleanup_process_group "$homecam_pid" KILL
  cleanup_process_group "$robot_pid" KILL
  cleanup_process_group "$gazebo_pid" KILL
  [[ -z "$homecam_pid" ]] || wait "$homecam_pid" 2>/dev/null || true
  [[ -z "$robot_pid" ]] || wait "$robot_pid" 2>/dev/null || true
  [[ -z "$gazebo_pid" ]] || wait "$gazebo_pid" 2>/dev/null || true
  exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stop_robot_stack() {
  [[ -n "$robot_pid" ]] || return 0
  cleanup_process_group "$robot_pid" INT
  local deadline=$((SECONDS + 8))
  while ((SECONDS < deadline)) && process_group_alive "$robot_pid"; do
    sleep 0.2
  done
  cleanup_process_group "$robot_pid" TERM
  deadline=$((SECONDS + 3))
  while ((SECONDS < deadline)) && process_group_alive "$robot_pid"; do
    sleep 0.2
  done
  cleanup_process_group "$robot_pid" KILL
  wait "$robot_pid" 2>/dev/null || true
  robot_pid=""
}

start_robot_stack() {
  local force_mapping="$1"
  local auto_start="$2"
  local trust_bootstrap_pose="${3:-false}"
  local trust_localization_handoff="${4:-false}"
  robot_command=(
    ros2 launch malbut_gazebo managed_home.launch.py
    "world_name:=$HOMECAM_WORLD"
    "rviz:=$HOMECAM_MAP_RVIZ"
    "simulation:=false"
    "use_sim_time:=true"
    "map_store:=$HOMECAM_MAP_STORE"
    "web_host:=$HOMECAM_MAP_WEB_HOST"
    "web_port:=$HOMECAM_MAP_WEB_PORT"
    "cloud_sync:=$HOMECAM_CLOUD_MAP_ENABLED"
    "cloud_backend_url:=$HOMECAM_BACKEND_URL"
    "cloud_device_id:=$HOMECAM_DEVICE_ID"
    "cloud_token_file:=$HOMECAM_DEVICE_TOKEN_FILE"
    "cloud_local_url:=$homecam_map_local_url"
    "runtime_request_file:=$runtime_control_file"
    "force_mapping:=$force_mapping"
    "auto_start:=$auto_start"
    "trusted_localization_handoff:=$trust_localization_handoff"
  )
  if "$trust_bootstrap_pose" &&
    ((${#simulation_bootstrap_pose[@]} == 3))
  then
    robot_command+=("trusted_initial_pose:=true")
    homecam_append_pose_arguments \
      robot_command "${simulation_bootstrap_pose[@]}"
  fi
  homecam_log \
    "starting robot runtime: force_mapping=$force_mapping auto_start=$auto_start"
  setsid "${robot_command[@]}" &
  robot_pid=$!
}

if ! "$check_only" &&
  ros2 node list 2>/dev/null | grep -Fxq '/homecam_media_agent'
then
  homecam_die "a homecam media agent is already running"
  exit 1
fi

if homecam_is_true "$HOMECAM_START_GAZEBO"; then
  existing_topics="$(ros2 topic list -t 2>/dev/null || true)"
  if homecam_discover_image_topic \
    "$existing_topics" "$HOMECAM_IMAGE_TOPIC" >/dev/null 2>&1
  then
    homecam_die \
      "a ROS camera is already running; use --reuse-gazebo or stop it first"
    exit 1
  fi
  if "$check_only"; then
    gazebo_command=(
      ros2 launch malbut_gazebo worlds.launch.py
      "world_name:=$HOMECAM_WORLD"
      "gui:=$HOMECAM_GAZEBO_GUI"
      "headless:=$HOMECAM_GAZEBO_HEADLESS"
      "spawn_robot:=true"
      "bridge:=true"
    )
    homecam_log "starting camera check world: $HOMECAM_WORLD"
  else
    if [[ "$HOMECAM_MAP_WEB_HOST" == *:* ]]; then
      homecam_map_local_url="http://[$HOMECAM_MAP_WEB_HOST]:$HOMECAM_MAP_WEB_PORT"
    else
      homecam_map_local_url="http://$HOMECAM_MAP_WEB_HOST:$HOMECAM_MAP_WEB_PORT"
    fi
    gazebo_command=(
      ros2 launch malbut_gazebo worlds.launch.py
      "world_name:=$HOMECAM_WORLD"
      "gui:=$HOMECAM_GAZEBO_GUI"
      "headless:=$HOMECAM_GAZEBO_HEADLESS"
      "rviz:=false"
      "lidar_enabled:=true"
      "spawn_robot:=true"
      "bridge:=true"
    )
    homecam_log "starting managed device world: $HOMECAM_WORLD"
    homecam_log "persistent map store: $HOMECAM_MAP_STORE"
    homecam_log \
      "local map UI: http://$HOMECAM_MAP_WEB_HOST:$HOMECAM_MAP_WEB_PORT/"
  fi
  if ((${#simulation_bootstrap_pose[@]} == 3)); then
    homecam_append_pose_arguments \
      gazebo_command "${simulation_bootstrap_pose[@]}"
  fi
  setsid "${gazebo_command[@]}" &
  gazebo_pid=$!
  if ! "$check_only"; then
    start_robot_stack "$HOMECAM_FORCE_MAPPING" false true false
  fi
else
  homecam_log "using the already-running Gazebo/ROS graph"
fi

deadline=$((SECONDS + HOMECAM_TOPIC_TIMEOUT_SECONDS))
topic_snapshot=""
image_topic=""
while ((SECONDS < deadline)); do
  if [[ -n "$gazebo_pid" ]] && ! kill -0 "$gazebo_pid" 2>/dev/null; then
    wait "$gazebo_pid" || true
    homecam_die "Gazebo exited before publishing an RGB image"
    exit 1
  fi
  if [[ -n "$robot_pid" ]] && ! kill -0 "$robot_pid" 2>/dev/null; then
    wait "$robot_pid" || true
    homecam_die "robot map/navigation stack exited before camera startup"
    exit 1
  fi
  topic_snapshot="$(ros2 topic list -t 2>/dev/null || true)"
  image_topic="$(
    homecam_discover_image_topic \
      "$topic_snapshot" "$HOMECAM_IMAGE_TOPIC" 2>/dev/null || true
  )"
  [[ -z "$image_topic" ]] || break
  sleep 1
done
if [[ -z "$image_topic" ]]; then
  if [[ -n "$HOMECAM_IMAGE_TOPIC" ]]; then
    homecam_die \
      "configured image topic was not published: $HOMECAM_IMAGE_TOPIC"
  else
    homecam_die "no sensor_msgs/msg/Image RGB topic was discovered"
  fi
  exit 1
fi

camera_info_topic="$(
  homecam_discover_camera_info_topic \
    "$topic_snapshot" "$image_topic" "$HOMECAM_CAMERA_INFO_TOPIC" \
    2>/dev/null || true
)"
if [[ -n "$HOMECAM_CAMERA_INFO_TOPIC" && -z "$camera_info_topic" ]]; then
  homecam_die \
    "configured CameraInfo topic was not published: $HOMECAM_CAMERA_INFO_TOPIC"
  exit 1
fi
if [[ -z "$camera_info_topic" ]]; then
  homecam_warn "CameraInfo was not found; streaming will continue without it"
fi

homecam_log "RGB topic: $image_topic"
homecam_log "CameraInfo topic: ${camera_info_topic:-none}"
homecam_log "odometry topic: ${HOMECAM_ODOM_TOPIC:-disabled}"
homecam_log "waiting for one RGB frame"
encoding_output="$(
  timeout 12 ros2 topic echo --once "$image_topic" --field encoding \
    2>/dev/null || true
)"
image_encoding="$(homecam_select_image_encoding "$encoding_output")"
case "$image_encoding" in
  rgb8|bgr8|rgba8|bgra8) ;;
  "")
  homecam_die "topic exists but no RGB frame arrived within 12 seconds"
  exit 1
    ;;
  *)
    homecam_die \
      "unsupported ROS image encoding on $image_topic: $image_encoding"
    exit 1
    ;;
esac
homecam_log "RGB encoding: $image_encoding"

if "$check_only"; then
  homecam_log "portable simulation check passed"
  exit 0
fi

homecam_command=(
  ros2 launch homecam_media_agent homecam_sim.launch.py
  "backend_url:=$HOMECAM_BACKEND_URL"
  "device_id:=$HOMECAM_DEVICE_ID"
  "monitoring_enabled:=$HOMECAM_MONITORING_ENABLED"
  "event_clips_enabled:=$HOMECAM_EVENT_CLIPS_ENABLED"
  "image_topic:=$image_topic"
  "odom_topic:=$HOMECAM_ODOM_TOPIC"
  "navigation_status_topic:=$HOMECAM_NAVIGATION_STATUS_TOPIC"
  "audio_source:=$HOMECAM_AUDIO_SOURCE"
  "audio_sink:=$HOMECAM_AUDIO_SINK"
  "microphone_enabled:=$HOMECAM_MICROPHONE_ENABLED"
)
if [[ -n "$camera_info_topic" ]]; then
  homecam_command+=("camera_info_topic:=$camera_info_topic")
fi
if [[ -n "$HOMECAM_MODEL_PATH" ]]; then
  homecam_command+=("model_path:=$HOMECAM_MODEL_PATH")
fi
if [[ -n "$HOMECAM_POSE_MODEL_PATH" ]]; then
  homecam_command+=("pose_model_path:=$HOMECAM_POSE_MODEL_PATH")
fi
homecam_log \
  "starting stream for device $HOMECAM_DEVICE_ID (token is not printed)"
setsid "${homecam_command[@]}" &
homecam_pid=$!

if [[ -z "$gazebo_pid" ]]; then
  wait "$homecam_pid"
  exit $?
fi

while true; do
  if ! process_group_alive "$homecam_pid"; then
    wait "$homecam_pid" 2>/dev/null || true
    homecam_die "homecam agent stopped; shutting down the device runtime"
    exit 1
  fi
  if ! process_group_alive "$gazebo_pid"; then
    wait "$gazebo_pid" 2>/dev/null || true
    homecam_die "Gazebo stopped; shutting down the device runtime"
    exit 1
  fi
  if [[ -n "$robot_pid" ]] && ! process_group_alive "$robot_pid"; then
    wait "$robot_pid" 2>/dev/null || true
    homecam_die "robot map/navigation stack stopped unexpectedly"
    exit 1
  fi
  if [[ -s "$runtime_control_file" ]]; then
    requested_mode=""
    not_before=""
    read -r requested_mode not_before < "$runtime_control_file" || true
    if [[ ! "$not_before" =~ ^[0-9]+$ ]]; then
      rm -f -- "$runtime_control_file"
      homecam_warn "ignored an invalid robot runtime request"
    elif ((10#$not_before <= $(date +%s))); then
      rm -f -- "$runtime_control_file"
      case "$requested_mode" in
        mapping)
          homecam_log "switching the robot runtime to mapping"
          stop_robot_stack
          start_robot_stack true true false false
          ;;
        navigation)
          homecam_log "switching the robot runtime to saved-map navigation"
          stop_robot_stack
          # Gazebo and its odometry bridge remain alive across this managed
          # SLAM-to-navigation transition, so the handoff is trusted here.
          start_robot_stack false false false true
          ;;
        *)
          homecam_warn "ignored an invalid robot runtime request"
          ;;
      esac
    fi
  fi
  if ((SECONDS - runtime_supervisor_refreshed_at >= 5)); then
    refresh_runtime_supervisor
  fi
  sleep 0.5
done
