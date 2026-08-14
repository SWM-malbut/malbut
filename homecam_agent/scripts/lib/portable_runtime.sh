#!/usr/bin/env bash

# Shared helpers for the portable Gazebo + homecam scripts.
# This file is sourced by other scripts and intentionally does not change
# shell options on its own.

homecam_log() {
  printf '[homecam] %s\n' "$*"
}

homecam_warn() {
  printf '[homecam] warning: %s\n' "$*" >&2
}

homecam_die() {
  printf '[homecam] error: %s\n' "$*" >&2
  return 1
}

homecam_is_true() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

homecam_is_false() {
  case "${1,,}" in
    0|false|no|off) return 0 ;;
    *) return 1 ;;
  esac
}

homecam_validate_boolean() {
  local name="$1"
  local value="$2"
  if ! homecam_is_true "$value" && ! homecam_is_false "$value"; then
    homecam_die "$name must be true or false (got: $value)"
  fi
}

homecam_config_key_allowed() {
  case "$1" in
    HOMECAM_AUDIO_SINK|\
    HOMECAM_AUDIO_SOURCE|\
    HOMECAM_BACKEND_URL|\
    HOMECAM_CAMERA_INFO_TOPIC|\
    HOMECAM_CLOUD_MAP_ENABLED|\
    HOMECAM_DEVICE_ID|\
    HOMECAM_DEVICE_TOKEN_FILE|\
    HOMECAM_FORCE_MAPPING|\
    HOMECAM_GAZEBO_GUI|\
    HOMECAM_GAZEBO_HEADLESS|\
    HOMECAM_GST_PLUGIN_PATH|\
    HOMECAM_GST_REGISTRY|\
    HOMECAM_IMAGE_TOPIC|\
    HOMECAM_KVS_SDK_ROOT|\
    HOMECAM_MAP_RVIZ|\
    HOMECAM_MAP_STORE|\
    HOMECAM_MAP_WEB_HOST|\
    HOMECAM_MAP_WEB_PORT|\
    HOMECAM_MICROPHONE_ENABLED|\
    HOMECAM_MODEL_PATH|\
    HOMECAM_MONITORING_ENABLED|\
    HOMECAM_ODOM_TOPIC|\
    HOMECAM_ROS_SETUP|\
    HOMECAM_START_GAZEBO|\
    HOMECAM_TOPIC_TIMEOUT_SECONDS|\
    HOMECAM_WORKSPACE|\
    HOMECAM_WORLD|\
    ROS_DOMAIN_ID|\
    RMW_IMPLEMENTATION)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

homecam_load_config() {
  local config_path="$1"
  local line=""
  local line_number=0
  local key=""
  local value=""
  local -A seen=()

  homecam_validate_private_file "$config_path" "configuration file" ||
    return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    line_number=$((line_number + 1))
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ ! "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
      homecam_die \
        "invalid configuration at $config_path:$line_number (expected KEY=value)" ||
        return 1
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if ! homecam_config_key_allowed "$key"; then
      homecam_die \
        "unsupported key at $config_path:$line_number: $key" ||
        return 1
    fi
    if [[ -v "seen[$key]" ]]; then
      homecam_die "duplicate key at $config_path:$line_number: $key" ||
        return 1
    fi
    seen["$key"]=1

    # Environment and command-line overrides win over file values.
    if [[ ! -v "$key" ]]; then
      printf -v "$key" '%s' "$value"
      export "$key"
    else
      homecam_log "environment overrides protected config key: $key"
    fi
  done < "$config_path"
}

homecam_default_config_path() {
  local config_base="${XDG_CONFIG_HOME:-${HOME:?HOME is required}/.config}"
  printf '%s/homecam/sim.env\n' "$config_base"
}

homecam_resolve_config_relative_path() {
  local config_path="$1"
  local value="$2"
  if [[ -z "$value" || "$value" == /* ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  realpath -m -- "$(dirname -- "$config_path")/$value"
}

homecam_source_setup_file() {
  local setup_file="$1"
  local restore_nounset=false
  local source_status=0
  case "$-" in
    *u*) restore_nounset=true ;;
  esac
  set +u
  # shellcheck disable=SC1090
  if source "$setup_file"; then
    source_status=0
  else
    source_status=$?
  fi
  if "$restore_nounset"; then
    set -u
  fi
  return "$source_status"
}

homecam_validate_private_file() {
  local file_path="$1"
  local description="$2"
  local file_mode=""
  local file_permissions=0

  [[ ! -L "$file_path" ]] ||
    homecam_die "$description must not be a symbolic link: $file_path" ||
    return 1
  [[ -f "$file_path" && -r "$file_path" ]] ||
    homecam_die "$description is not a readable regular file: $file_path" ||
    return 1
  [[ "$(stat -c '%u' -- "$file_path")" == "$EUID" ]] ||
    homecam_die "$description must be owned by the current user: $file_path" ||
    return 1

  file_mode="$(stat -c '%a' -- "$file_path")"
  file_permissions=$((8#$file_mode))
  if ((file_permissions & 077)); then
    homecam_die \
      "$description permissions must deny group/other access (got: $file_mode)" ||
      return 1
  fi
}

homecam_validate_backend_url() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
if not value or any(character.isspace() or character == "\\" for character in value):
    raise SystemExit(1)
try:
    parsed = urlsplit(value)
    port = parsed.port
except ValueError:
    raise SystemExit(1)
if parsed.scheme.lower() not in {"http", "https"}:
    raise SystemExit(1)
if not parsed.netloc or parsed.hostname is None:
    raise SystemExit(1)
if parsed.username is not None or parsed.password is not None:
    raise SystemExit(1)
if port is not None and port == 0:
    raise SystemExit(1)
authority = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
if port is not None:
    authority += f":{port}"
if parsed.netloc.lower() != authority.lower():
    raise SystemExit(1)
if parsed.scheme.lower() == "http" and parsed.hostname.lower() not in {
    "localhost",
    "127.0.0.1",
    "::1",
}:
    raise SystemExit(1)
PY
}

homecam_validate_device_config() {
  local config_path="$1"

  [[ "${HOMECAM_DEVICE_ID:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] ||
    homecam_die \
      "HOMECAM_DEVICE_ID must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}" ||
    return 1

  homecam_validate_backend_url "${HOMECAM_BACKEND_URL:-}" ||
    homecam_die "HOMECAM_BACKEND_URL must be valid HTTPS (HTTP is loopback-only)" ||
    return 1

  HOMECAM_DEVICE_TOKEN_FILE="$(
    homecam_resolve_config_relative_path \
      "$config_path" "${HOMECAM_DEVICE_TOKEN_FILE:-}"
  )"
  export HOMECAM_DEVICE_TOKEN_FILE
  [[ -n "$HOMECAM_DEVICE_TOKEN_FILE" ]] ||
    homecam_die "HOMECAM_DEVICE_TOKEN_FILE is required" ||
    return 1
  homecam_validate_device_token_file "$HOMECAM_DEVICE_TOKEN_FILE"
}

homecam_validate_device_token_file() {
  local token_file="$1"
  local token=""

  homecam_validate_private_file "$token_file" "device token file" ||
    return 1
  token="$(<"$token_file")"
  if [[ ! "$token" =~ ^hc1\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\.[0-9a-fA-F]{64}$ ]]; then
    unset token
    homecam_die "device token file has an invalid token format" ||
      return 1
  fi
  unset token
}

homecam_workspace_from_repo() {
  local source_repo_root="$1"
  local current=""
  local candidate=""

  # Prefer the conventional colcon layouts first. A merged repository below
  # <workspace>/src/malbut also contains package.xml files, so checking for a
  # repository-root workspace before this pass would incorrectly return the
  # source checkout instead of the colcon workspace.
  current="$(realpath -m -- "$source_repo_root")"
  while [[ "$current" != / ]]; do
    if [[ "$(basename -- "$current")" == src ]]; then
      candidate="$(dirname -- "$current")"
      if [[ -d "$candidate/src" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
    current="$(dirname -- "$current")"
  done

  # The team repository is also commonly cloned directly as the colcon
  # workspace root (malbut/homecam_agent and malbut/malbut_gazebo live next to
  # build/, install/, and log/). Support that layout so launch helpers work
  # from a normal checkout without requiring HOMECAM_WORKSPACE on every run.
  current="$(realpath -m -- "$source_repo_root")"
  while [[ "$current" != / ]]; do
    if [[ -f "$current/homecam_agent/homecam_detector/package.xml" &&
      -f "$current/homecam_agent/homecam_media_agent/package.xml" &&
      -f "$current/malbut_gazebo/package.xml" ]]
    then
      printf '%s\n' "$current"
      return 0
    fi
    current="$(dirname -- "$current")"
  done
  return 1
}

homecam_source_runtime() {
  local source_repo_root="$1"
  local ros_setup="${HOMECAM_ROS_SETUP:-/opt/ros/humble/setup.bash}"
  local workspace="${HOMECAM_WORKSPACE:-}"
  local workspace_setup=""

  [[ -r "$ros_setup" ]] ||
    homecam_die "ROS 2 Humble setup is missing: $ros_setup" ||
    return 1
  homecam_source_setup_file "$ros_setup"

  if [[ -z "$workspace" ]]; then
    workspace="$(homecam_workspace_from_repo "$source_repo_root")" ||
      homecam_die \
        "cannot infer the colcon workspace; set HOMECAM_WORKSPACE" ||
      return 1
  fi
  workspace="$(realpath -m -- "$workspace")"
  workspace_setup="$workspace/install/setup.bash"
  [[ -r "$workspace_setup" ]] ||
    homecam_die \
      "workspace is not built: $workspace_setup (run setup_portable_sim.sh)" ||
    return 1
  homecam_source_setup_file "$workspace_setup"
  HOMECAM_WORKSPACE="$workspace"
  export HOMECAM_WORKSPACE

  for package_name in malbut_gazebo homecam_media_agent homecam_detector; do
    if ! ros2 pkg prefix "$package_name" >/dev/null 2>&1; then
      homecam_die \
        "ROS package is unavailable after sourcing the workspace: $package_name" ||
        return 1
    fi
  done
}

homecam_find_kvs_sdk_root() {
  local workspace="$1"
  local configured="${HOMECAM_KVS_SDK_ROOT:-}"
  local candidate=""
  local candidates=()

  if [[ -n "$configured" ]]; then
    candidates+=("$configured")
  fi
  candidates+=(
    "$workspace/.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1"
    "/opt/homecam/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1"
  )

  for candidate in "${candidates[@]}"; do
    candidate="$(realpath -m -- "$candidate")"
    actual_commit="$(
      git -C "$candidate" rev-parse HEAD 2>/dev/null || true
    )"
    if [[ -r "$candidate/certs/cert.pem" &&
      -d "$candidate/build" &&
      -d "$candidate/.git" &&
      "$actual_commit" == \
      "d7322f63af3c600ee7031b28436e3f8a12664272" ]] &&
      git -C "$candidate" diff --quiet &&
      git -C "$candidate" diff --cached --quiet
    then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

homecam_prepare_media_runtime() {
  local workspace="$1"
  local sdk_root=""
  local media_prefix=""
  local media_node=""
  local feature_header=""
  local feature_macro=""
  local plugin=""
  local required_plugins=(
    appsrc
    appsink
    queue
    videoconvert
    videoscale
    videorate
    x264enc
    h264parse
    audiotestsrc
    alsasrc
    audioconvert
    audioresample
    opusenc
    opusdec
    alsasink
  )

  if [[ -n "${HOMECAM_GST_PLUGIN_PATH:-}" ]]; then
    GST_PLUGIN_PATH="$HOMECAM_GST_PLUGIN_PATH"
    export GST_PLUGIN_PATH
  fi
  if [[ -n "${HOMECAM_GST_REGISTRY:-}" ]]; then
    GST_REGISTRY="$HOMECAM_GST_REGISTRY"
    export GST_REGISTRY
  fi

  command -v gst-inspect-1.0 >/dev/null 2>&1 ||
    homecam_die "gst-inspect-1.0 is missing; run setup_portable_sim.sh" ||
    return 1
  for plugin in "${required_plugins[@]}"; do
    if ! gst-inspect-1.0 "$plugin" >/dev/null 2>&1; then
      homecam_die "required GStreamer plugin is missing: $plugin" ||
        return 1
    fi
  done

  media_prefix="$(ros2 pkg prefix homecam_media_agent)"
  media_node="$media_prefix/lib/homecam_media_agent/homecam_media_agent_node"
  feature_header="$media_prefix/include/homecam_media_agent/build_features.hpp"
  [[ -x "$media_node" ]] ||
    homecam_die "homecam media executable is missing: $media_node" ||
    return 1
  [[ -r "$feature_header" ]] ||
    homecam_die "homecam build feature manifest is missing: $feature_header" ||
    return 1
  for feature_macro in \
    HOMECAM_HAVE_CURL \
    HOMECAM_HAVE_GSTREAMER \
    HOMECAM_HAVE_KVS
  do
    if ! grep -Eq \
      "^#define[[:space:]]+$feature_macro[[:space:]]+1$" \
      "$feature_header"
    then
      homecam_die \
        "$feature_macro is disabled in the installed binary; rebuild with setup_portable_sim.sh" ||
        return 1
    fi
  done
  if ldd "$media_node" 2>/dev/null | grep -Fq 'not found'; then
    homecam_die \
      "homecam media executable has unresolved shared libraries; rebuild on this PC" ||
      return 1
  fi

  sdk_root="$(homecam_find_kvs_sdk_root "$workspace")" ||
    homecam_die \
      "pinned AWS KVS SDK was not found; run setup_portable_sim.sh" ||
    return 1
  HOMECAM_KVS_SDK_ROOT="$sdk_root"
  if ! readelf -d "$media_node" 2>/dev/null |
    grep -Fq "$sdk_root/build"
  then
    homecam_die \
      "installed media binary was not linked against the selected pinned KVS SDK" ||
      return 1
  fi
  AWS_KVS_CACERT_PATH="$sdk_root/certs/cert.pem"
  export HOMECAM_KVS_SDK_ROOT AWS_KVS_CACERT_PATH
}

homecam_topic_has_type() {
  local snapshot="$1"
  local topic="$2"
  local message_type="$3"
  local line=""
  while IFS= read -r line; do
    if [[ "${line%% *}" == "$topic" && "$line" == *"$message_type"* ]]; then
      return 0
    fi
  done <<< "$snapshot"
  return 1
}

homecam_topics_of_type() {
  local snapshot="$1"
  local message_type="$2"
  local line=""
  while IFS= read -r line; do
    if [[ "$line" == *"$message_type"* ]]; then
      printf '%s\n' "${line%% *}"
    fi
  done <<< "$snapshot"
}

homecam_discover_image_topic() {
  local snapshot="$1"
  local explicit="${2:-}"
  local topic=""
  local known=(
    /camera/color/image_raw
    /depth_cam/depth_cam
    /camera/rgb/image_raw
    /rgb/image_raw
    /camera/image_raw
  )

  if [[ -n "$explicit" ]]; then
    homecam_topic_has_type "$snapshot" "$explicit" "sensor_msgs/msg/Image" ||
      return 1
    printf '%s\n' "$explicit"
    return 0
  fi

  for topic in "${known[@]}"; do
    if homecam_topic_has_type "$snapshot" "$topic" "sensor_msgs/msg/Image"; then
      printf '%s\n' "$topic"
      return 0
    fi
  done

  while IFS= read -r topic; do
    case "$topic" in
      */color/image_raw|*/rgb/image_raw)
        printf '%s\n' "$topic"
        return 0
        ;;
    esac
  done < <(homecam_topics_of_type "$snapshot" "sensor_msgs/msg/Image")

  while IFS= read -r topic; do
    case "$topic" in
      *depth*|*points*) continue ;;
      */image_raw)
        printf '%s\n' "$topic"
        return 0
        ;;
    esac
  done < <(homecam_topics_of_type "$snapshot" "sensor_msgs/msg/Image")
  return 1
}

homecam_discover_camera_info_topic() {
  local snapshot="$1"
  local image_topic="$2"
  local explicit="${3:-}"
  local topic=""
  local candidates=()

  if [[ -n "$explicit" ]]; then
    homecam_topic_has_type \
      "$snapshot" "$explicit" "sensor_msgs/msg/CameraInfo" ||
      return 1
    printf '%s\n' "$explicit"
    return 0
  fi

  case "$image_topic" in
    /camera/color/image_raw)
      candidates+=(/camera/color/camera_info)
      ;;
    /depth_cam/depth_cam)
      candidates+=(/depth_cam/rgb/camera_info)
      ;;
  esac
  case "$image_topic" in
    */image_raw)
      candidates+=("${image_topic%/image_raw}/camera_info")
      ;;
  esac

  for topic in "${candidates[@]}"; do
    if homecam_topic_has_type \
      "$snapshot" "$topic" "sensor_msgs/msg/CameraInfo"
    then
      printf '%s\n' "$topic"
      return 0
    fi
  done

  return 0
}
