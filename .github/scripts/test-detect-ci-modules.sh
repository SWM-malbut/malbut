#!/usr/bin/env bash
set -euo pipefail

selector="$(cd "$(dirname "$0")" && pwd)/detect-ci-modules.sh"
scenarios=(
  "homecam_web/app/page.tsx|true,false,false,false,false"
  "homecam_web/infra/cdk/lib/stack.ts|true,true,false,false,false"
  "homecam_agent/homecam_media_agent/src/node.cpp|false,false,false,false,true"
  "malbut_agent_server/malbut_agent_server/memory.py|false,false,true,false,false"
  "malbut_description/urdf/robot.xacro|false,false,true,false,false"
  "malbut_gazebo/launch/navigation.launch.py|false,false,true,true,false"
  "malbut_autonomy/malbut_patrol/patrol.py|false,false,true,false,false"
  "malbut_tracking/src/lidar_foreground_preprocessor.cpp|false,false,true,false,false"
  "malbut_reid/malbut_reid/person_reidentifier_node.py|false,false,true,false,false"
  "malbut_yolo/scripts/prepare_yolo26_model.sh|false,false,true,false,false"
  "malbut_gazebo_plugins/src/actor_pose_system.cpp|false,false,true,false,false"
  "malbut_yolo/vendor/yolo_ros/yolo_ros/package.xml|false,false,true,false,false"
  "malbut_interfaces/action/FollowPerson.action|false,false,true,true,false"
  "malbut_scenarios/malbut_scenarios/text_agent_server.py|false,false,true,true,false"
  "malbut_system_manager/malbut_system_manager/system_manager_node.py|false,false,true,false,false"
  "malbut_autoslam/malbut_autoslam/autoslam_node.py|false,false,true,false,false"
  "malbut_bringup/launch/robot.launch.py|false,false,true,false,false"
  "malbut_stt/malbut_stt/node.py|false,false,true,false,false"
  "malbut_tts/malbut_tts/receiver.py|false,false,true,false,false"
  "README.md|false,false,false,false,false"
  ".github/workflows/ci.yml|true,true,true,true,true"
  ".github/scripts/detect-ci-modules.sh|true,true,true,true,true"
  ".github/scripts/test-detect-ci-modules.sh|true,true,true,true,true"
)

assert_selection()
{
  local label="$1" expected="$2" actual
  shift 2
  actual="$("$selector" "$@" | sed -n '1,5p' | cut -d= -f2 | paste -sd, -)"
  if [ "$actual" != "$expected" ]; then
    printf '%s: expected %s, got %s\n' "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

for scenario in "${scenarios[@]}"; do
  path="${scenario%%|*}"
  assert_selection "$path" "${scenario#*|}" --paths "$path"
done

assert_selection "No changed paths" false,false,false,false,false --paths
assert_selection "Agent and web" true,false,true,false,false --paths \
  malbut_agent_server/memory.py homecam_web/app/page.tsx
assert_selection "Agent then other ROS" false,false,true,false,false --paths \
  malbut_agent_server/memory.py malbut_stt/node.py
assert_selection "Other ROS then Agent" false,false,true,false,false --paths \
  malbut_stt/node.py malbut_agent_server/memory.py
assert_selection "Agent and shared CI" true,true,true,true,true --paths \
  .github/workflows/ci.yml malbut_agent_server/memory.py

# Exercise Git diff selection in an isolated repository, including a spaced path.
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
git -C "$fixture" init --quiet
git -C "$fixture" config user.name "CI module selector test"
git -C "$fixture" config user.email "ci-module-selector@example.invalid"
git -C "$fixture" config commit.gpgsign false
touch "$fixture/README.md"
git -C "$fixture" add .
git -C "$fixture" commit --quiet -m "Base fixture"
base_sha="$(git -C "$fixture" rev-parse HEAD)"
mkdir "$fixture/malbut_agent_server"
touch "$fixture/malbut_agent_server/memory source.py"
git -C "$fixture" add .
git -C "$fixture" commit --quiet -m "Agent fixture"
(
  cd "$fixture"
  assert_selection "Agent Git diff" false,false,true,false,false "$base_sha"
  assert_selection "No Git diff" false,false,false,false,false HEAD
  assert_selection "Missing base fallback" false,false,true,false,false
  assert_selection "Invalid base fallback" false,false,true,false,false invalid-base
  assert_selection "Zero base fallback" false,false,true,false,false 0000000000000000000000000000000000000000
)
mkdir "$fixture/malbut_stt"
touch "$fixture/malbut_stt/node.py"
git -C "$fixture" add .
git -C "$fixture" commit --quiet -m "Other ROS fixture"
(
  cd "$fixture"
  assert_selection "Mixed ROS Git diff" false,false,true,false,false "$base_sha"
)
before_delete_sha="$(git -C "$fixture" rev-parse HEAD)"
rm "$fixture/malbut_agent_server/memory source.py"
git -C "$fixture" add .
git -C "$fixture" commit --quiet -m "Delete Agent fixture"
(
  cd "$fixture"
  assert_selection "Deleted Agent Git diff" false,false,true,false,false "$before_delete_sha"
)
before_move_sha="$(git -C "$fixture" rev-parse HEAD)"
git -C "$fixture" mv malbut_stt/node.py malbut_agent_server/node.py
git -C "$fixture" commit --quiet -m "Move ROS source into Agent"
(
  cd "$fixture"
  assert_selection "Moved ROS source still needs full tests" false,false,true,false,false "$before_move_sha"
)

# Test/build selection are separate: dependency builds do not rerun every suite.
assert_value()
{
  local key="$1" expected="$2" actual
  shift 2
  actual="$("$selector" "$@" | sed -n "s/^$key=//p")"
  if [ "$actual" != "$expected" ]; then
    printf '%s: expected %s, got %s\n' "$key" "$expected" "$actual" >&2
    exit 1
  fi
}
assert_value ros_test_packages malbut_bringup --paths malbut_bringup/launch/robot.launch.py
assert_value agent false --paths malbut_bringup/config/nav2_params.yaml
assert_value ros_test_packages malbut_tracking --paths malbut_tracking/src/lidar_foreground_preprocessor.cpp
assert_value ros_test_packages malbut_bringup --paths malbut_test/malbut_bringup/config/nav2_params.yaml
assert_value ros_test_packages malbut_bringup --paths malbut_test/COLCON_IGNORE
assert_value ros_test_packages malbut_patrol --paths malbut_test/malbut_patrol/launch/patrol.launch.py
assert_value agent true --paths malbut_agent_server/malbut_agent_server/node.py
assert_value ros_test_packages malbut_yolo --paths malbut_yolo/vendor/yolo_ros/yolo_ros/yolo_node.py
assert_value ros_test_packages '' --paths malbut_bringup/README.md
assert_value assets false --paths malbut_gazebo/web/index.html
assert_value assets true --paths malbut_gazebo/models/humanoid_actor/model.sdf
assert_value ros_test_packages malbut_gazebo --paths homecam_agent/scripts/spawn_event_test_person.sh

builds="$("$selector" --paths malbut_bringup/launch/robot.launch.py | sed -n 's/^ros_packages=//p')"
for needed in malbut_interfaces malbut_tracking malbut_system_manager yolo_msgs; do
  [[ " $builds " == *" $needed "* ]]
done
for unrelated in malbut_gazebo malbut_scenarios malbut_agent_server; do
  [[ " $builds " != *" $unrelated "* ]]
done
tests="$("$selector" --paths malbut_interfaces/action/FollowPerson.action | sed -n 's/^ros_test_packages=//p')"
for consumer in malbut_system_manager malbut_tracking malbut_patrol; do
  [[ " $tests " == *" $consumer "* ]]
done
assert_selection "Manual full suite" true,true,true,true,true --all
printf '%s\n' 'CI selection checks passed.'
