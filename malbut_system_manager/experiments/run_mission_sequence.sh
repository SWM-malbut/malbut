#!/usr/bin/env bash
# Run only this experiment's simulation and processes; never kill other sessions.
set -eo pipefail

experiment_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
experiment_workspace="${MALBUT_WORKSPACE:-$(cd -- "$experiment_script_dir/../../../.." && pwd)}"
if [[ ! -f "$experiment_workspace/install/local_setup.bash" ]]; then
  echo "Build the ROS workspace first: $experiment_workspace" >&2
  exit 2
fi
source /opt/ros/humble/setup.bash
source "$experiment_workspace/install/local_setup.bash"
set -u

# A separate local ROS domain and Gazebo partition prevent cross-run traffic.
export ROS_DOMAIN_ID="${MALBUT_EXPERIMENT_DOMAIN:-86}"
export ROS_LOCALHOST_ONLY=1
export IGN_PARTITION="malbut-manager-experiment-$$"
export GZ_PARTITION="$IGN_PARTITION"
exec 9>"/tmp/malbut-manager-experiment-$(id -u).lock"
if ! flock -n 9; then
  echo 'Another manager experiment is still running. Stop it first.' >&2
  exit 2
fi

mkdir -p "$experiment_workspace/log/manager_experiment"
experiment_output="$(mktemp -d "$experiment_workspace/log/manager_experiment/run-XXXXXX")"
git -C "$experiment_script_dir/../.." rev-parse HEAD \
  >"$experiment_output/git_revision.txt"
git -C "$experiment_script_dir/../.." status --short --branch \
  >"$experiment_output/git_status.txt"
experiment_gui="${MALBUT_EXPERIMENT_GUI:-false}"
if [[ "$experiment_gui" != true && "$experiment_gui" != false ]]; then
  echo 'MALBUT_EXPERIMENT_GUI must be true or false.' >&2
  exit 2
fi
experiment_headless=true
[[ "$experiment_gui" == true ]] && experiment_headless=false
experiment_pids=()

cleanup() {
  local saved_status=$?
  trap - EXIT INT TERM
  # Every PID below was started by this script in its own process group.
  for signal in INT TERM KILL; do
    for process_id in "${experiment_pids[@]}"; do
      if kill -0 -- "-$process_id" 2>/dev/null; then
        kill -s "$signal" -- "-$process_id" 2>/dev/null || true
      fi
    done
    [[ "$signal" == KILL ]] && break
    for ((attempt=0; attempt<30; attempt++)); do
      local any_alive=false
      for process_id in "${experiment_pids[@]}"; do
        kill -0 -- "-$process_id" 2>/dev/null && any_alive=true
      done
      [[ "$any_alive" == false ]] && break
      sleep 0.2
    done
  done
  for process_id in "${experiment_pids[@]}"; do
    wait "$process_id" 2>/dev/null || true
  done
  echo "Experiment logs: $experiment_output"
  exit "$saved_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Starting isolated Small House experiment (ROS domain $ROS_DOMAIN_ID)."
echo "Experiment logs: $experiment_output"
setsid ros2 launch malbut_gazebo target_tracking_demo.launch.py \
  "gui:=$experiment_gui" "headless:=$experiment_headless" \
  "rviz:=$experiment_gui" "image_view:=$experiment_gui" \
  actor_spawn_delay:=15.0 >"$experiment_output/simulation.log" 2>&1 &
experiment_pids+=("$!")
setsid ros2 launch malbut_patrol patrol.launch.py \
  use_sim_time:=true camera_optical_frame:=camera_depth_optical_frame \
  >"$experiment_output/patrol.log" 2>&1 &
experiment_pids+=("$!")
setsid ros2 run malbut_system_manager system_manager \
  --ros-args -p use_sim_time:=true \
  >"$experiment_output/manager.log" 2>&1 &
experiment_pids+=("$!")

setsid python3 -u "$experiment_script_dir/mission_sequence.py" \
  --output-dir "$experiment_output" "$@" \
  >"$experiment_output/sequence.log" 2>&1 &
experiment_sequence_pid=$!
experiment_pids+=("$experiment_sequence_pid")
experiment_status=0
wait "$experiment_sequence_pid" || experiment_status=$?
cat "$experiment_output/sequence.log"
exit "$experiment_status"
