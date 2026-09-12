#!/usr/bin/env bash
set -euo pipefail

# Run with bash even when the robot's interactive shell is Zsh.
robot_source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
parent_dir="$(dirname -- "$robot_source_dir")"
if [[ "$(basename -- "$parent_dir")" == src ]]; then
  source_dir="$parent_dir"
elif [[ "$(basename -- "$(dirname -- "$parent_dir")")" == src ]]; then
  source_dir="$(dirname -- "$parent_dir")"
else
  echo 'Use <workspace>/src/malbut or <workspace>/src/malbut/malbut_test.' >&2
  exit 1
fi
workspace_dir="$(dirname -- "$source_dir")"
if [[ "${ROS_DISTRO:-}" != humble ]]; then
  echo 'Source ROS 2 Humble and the manufacturer workspace first.' >&2
  exit 1
fi

# Explicit roots bypass only this copy's COLCON_IGNORE, without discovering
# the original/simulation packages or rebuilding manufacturer source packages.
package_paths=(
  "$robot_source_dir/malbut_bringup"
  "$robot_source_dir/malbut_interfaces"
  "$robot_source_dir/malbut_system_manager"
  "$robot_source_dir/malbut_yolo"
  "$robot_source_dir/malbut_reid"
  "$robot_source_dir/malbut_tracking"
  "$robot_source_dir/malbut_patrol"
  "$robot_source_dir/malbut_autoslam"
  "$robot_source_dir/malbut_yolo/vendor/yolo_ros/yolo_ros"
  "$robot_source_dir/malbut_yolo/vendor/yolo_ros/yolo_msgs"
)
for package_path in "${package_paths[@]}"; do
  if [[ ! -f "$package_path/package.xml" ]]; then
    echo "Missing bundled package: $package_path (check the Malbut source copy)." >&2
    exit 1
  fi
done
colcon_executable="$(command -v colcon)"
cd -- "$workspace_dir"
# Keep factory build/install hooks intact and avoid upstream's optional uv sync.
PATH=/usr/bin:/bin "$colcon_executable" --log-base log/malbut_test build \
  --base-paths "${package_paths[@]}" \
  --build-base build/malbut_test --install-base install/malbut_test \
  --symlink-install --packages-up-to malbut_bringup "$@"
echo "Built robot copy. In Zsh: source $workspace_dir/install/malbut_test/local_setup.zsh"
