#!/usr/bin/env bash
set -Eeuo pipefail

readonly script_dir="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
  pwd -P
)"
readonly repo_root="$(
  cd -- "$script_dir/.." >/dev/null 2>&1
  pwd -P
)"
readonly malbut_repository="https://github.com/SWM-malbut/malbut.git"
readonly malbut_commit="607ee4a9461f4c54b71b39b4e70f4077ed4c1b5c"
readonly sdk_directory_name="amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1"
# shellcheck source=scripts/lib/portable_runtime.sh
source "$script_dir/lib/portable_runtime.sh"

workspace=""
skip_apt=false
skip_tests=false
bootstrap_malbut=false
require_pinned_malbut=false

usage() {
  cat <<'EOF'
Usage:
  setup_portable_sim.sh [options]

Options:
  --workspace PATH          Colcon workspace (auto-detected by default)
  --skip-apt                Do not install apt/rosdep dependencies
  --skip-tests              Build without running package tests
  --bootstrap-malbut        Clone the validated malbut revision when absent
  --require-pinned-malbut   Require the validated, clean malbut revision
  -h, --help                Show this help

The normal merged layout is:
  <workspace>/src/malbut/homecam_agent

The repository-root workspace layout is also supported:
  <workspace>/homecam_agent

The standalone layout is also supported:
  <workspace>/src/malbut
  <workspace>/src/homecam_agent

The default path never clones, checks out, or modifies malbut. It installs
homecam dependencies, builds the pinned AWS KVS SDK, and builds/tests the
current malbut simulation and homecam packages. --bootstrap-malbut is only for
a new standalone workspace.
EOF
}

while (($# > 0)); do
  case "$1" in
    --workspace)
      (($# >= 2)) || { usage >&2; exit 2; }
      workspace="$2"
      shift 2
      ;;
    --skip-apt)
      skip_apt=true
      shift
      ;;
    --skip-tests)
      skip_tests=true
      shift
      ;;
    --bootstrap-malbut)
      bootstrap_malbut=true
      shift
      ;;
    --require-pinned-malbut)
      require_pinned_malbut=true
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

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 22.04 ]]; then
    printf \
      'This portable simulation profile requires Ubuntu 22.04 (found %s %s).\n' \
      "${ID:-unknown}" "${VERSION_ID:-unknown}" >&2
    exit 1
  fi
fi
if [[ "$(dpkg --print-architecture 2>/dev/null || true)" != amd64 ]]; then
  printf 'This portable Gazebo profile is validated only on Ubuntu amd64.\n' >&2
  exit 1
fi

ros_setup="/opt/ros/humble/setup.bash"
if [[ ! -r "$ros_setup" ]]; then
  cat >&2 <<'EOF'
ROS 2 Humble is not installed.
This script expects the same Ubuntu 22.04 / ROS 2 Humble / Gazebo Fortress
environment already used by the malbut project.
EOF
  exit 1
fi

if [[ -z "$workspace" ]]; then
  workspace="$(homecam_workspace_from_repo "$repo_root")" || {
    printf \
      'Cannot infer the colcon workspace from %s; pass --workspace PATH.\n' \
      "$repo_root" >&2
    exit 1
  }
else
  workspace="$(realpath -m -- "$workspace")"
fi
standalone_repo_root="$(realpath -m -- "$workspace/src/homecam_agent")"
embedded_repo_root="$(realpath -m -- "$workspace/src/malbut/homecam_agent")"
workspace_root_repo="$(realpath -m -- "$workspace/homecam_agent")"
if [[ "$repo_root" != "$standalone_repo_root" &&
  "$repo_root" != "$embedded_repo_root" &&
  "$repo_root" != "$workspace_root_repo" ]]
then
  printf \
    'homecam_agent must be at %s, %s, or %s (current: %s).\n' \
    "$standalone_repo_root" "$embedded_repo_root" \
    "$workspace_root_repo" "$repo_root" >&2
  exit 1
fi

if [[ "$repo_root" == "$workspace_root_repo" ]]; then
  malbut_source="$workspace"
else
  malbut_source="$workspace/src/malbut"
fi
if ! "$skip_apt"; then
  "$script_dir/install_dependencies.sh"
fi
for required_command in git rosdep colcon cmake; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    printf \
      'Required command is missing: %s (rerun without --skip-apt).\n' \
      "$required_command" >&2
    exit 1
  fi
done

if [[ ! -e "$malbut_source" ]]; then
  if "$bootstrap_malbut"; then
    mkdir -p -- "$workspace/src"
    git clone "$malbut_repository" "$malbut_source"
    git -C "$malbut_source" checkout --detach "$malbut_commit"
  else
    cat >&2 <<EOF
The existing malbut source was not found:
  $malbut_source

Place homecam_agent next to the team's SWM-malbut/malbut checkout. For a new
standalone workspace only, rerun with --bootstrap-malbut.
EOF
    exit 1
  fi
fi

required_malbut_files=(
  "$malbut_source/malbut_gazebo/package.xml"
  "$malbut_source/malbut_gazebo/launch/managed_home.launch.py"
  "$malbut_source/malbut_gazebo/launch/worlds.launch.py"
  "$malbut_source/malbut_gazebo/worlds/small_house.sdf"
)
for required_malbut_file in "${required_malbut_files[@]}"; do
  if [[ ! -f "$required_malbut_file" ]]; then
    printf 'Required malbut simulation file is missing: %s\n' \
      "$required_malbut_file" >&2
    exit 1
  fi
done

actual_malbut_commit="not available (non-Git source)"
malbut_is_clean=false
if git -C "$malbut_source" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  actual_malbut_commit="$(git -C "$malbut_source" rev-parse HEAD)"
  if [[ -z "$(git -C "$malbut_source" status --porcelain --untracked-files=all)" ]]
  then
    malbut_is_clean=true
  fi
fi
if "$require_pinned_malbut"; then
  if [[ "$actual_malbut_commit" != "$malbut_commit" ]] ||
    ! "$malbut_is_clean"
  then
    cat >&2 <<EOF
The existing malbut checkout does not match the strict reproducible profile.
  expected commit: $malbut_commit
  actual commit:   $actual_malbut_commit
  clean checkout:  $malbut_is_clean

No malbut files were changed. Remove --require-pinned-malbut to use the
team's existing checkout and validate its simulation contract instead.
EOF
    exit 1
  fi
fi
printf 'Using the existing malbut source without modifying it: %s\n' \
  "$malbut_source"
printf 'malbut revision: %s\n' "$actual_malbut_commit"

homecam_source_setup_file "$ros_setup"
if [[ -r "$workspace/install/setup.bash" ]]; then
  homecam_source_setup_file "$workspace/install/setup.bash"
fi

if ! "$skip_apt"; then
  if [[ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]]; then
    if [[ "$EUID" -eq 0 ]]; then
      rosdep init
    else
      sudo rosdep init
    fi
  fi
  rosdep update
  rosdep install \
    --from-paths "$repo_root" \
    --ignore-src \
    --rosdistro humble \
    -r -y
fi

sdk_root="$workspace/.deps/$sdk_directory_name"
"$script_dir/build_kvs_webrtc_sdk.sh" "$sdk_root"

cd "$workspace"
# Always rebuild the current malbut source. An older install overlay may contain
# malbut_gazebo while still missing launch/world files added by the latest pull.
colcon build \
  --symlink-install \
  --packages-up-to \
    malbut_gazebo \
    homecam_detector \
    homecam_media_agent \
  --cmake-force-configure \
  --cmake-args \
    -DHOMECAM_ENABLE_KVS=ON \
    "-DKVS_WEBRTC_SDK_ROOT=$sdk_root" \
    "-DHOMECAM_KVS_CA_CERT_PATH=$sdk_root/certs/cert.pem"

homecam_source_setup_file "$workspace/install/setup.bash"

bash "$repo_root/test/test_portable_runtime.sh"
homecam_prepare_media_runtime "$workspace"

if ! "$skip_tests"; then
  malbut_contract_tests=()
  for malbut_test_name in \
    test_simulation_contract.py \
    test_slam_contract.py \
    test_world_assets.py \
    test_world_catalog.py
  do
    malbut_test_path="$malbut_source/malbut_gazebo/test/$malbut_test_name"
    if [[ -f "$malbut_test_path" ]]; then
      malbut_contract_tests+=("$malbut_test_path")
    fi
  done
  if ((${#malbut_contract_tests[@]} > 0)); then
    python3 -m pytest -q "${malbut_contract_tests[@]}"
  else
    printf \
      'No malbut contract tests found; required launch/world files were checked.\n'
  fi
  colcon test \
    --packages-select homecam_detector homecam_media_agent \
    --return-code-on-test-failure
fi

cat <<EOF

Portable Gazebo + homecam build is ready.
Workspace: $workspace
Existing malbut revision: $actual_malbut_commit
KVS SDK: $sdk_root

Next:
  $script_dir/configure_sim_device.sh
  $script_dir/run_gazebo_homecam.sh
EOF
