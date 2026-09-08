#!/usr/bin/env bash
set -euo pipefail

selector="$(dirname "$0")/detect-ci-modules.sh"
scenarios=(
  "homecam_web/app/page.tsx|true,false,false,false"
  "homecam_web/infra/cdk/lib/stack.ts|true,true,false,false"
  "homecam_agent/homecam_media_agent/src/node.cpp|false,false,false,true"
  "malbut_gazebo/launch/navigation.launch.py|false,false,true,true"
  "malbut_autonomy/malbut_patrol/patrol.py|false,false,true,false"
  "malbut_tracking/src/lidar_foreground_preprocessor.cpp|false,false,true,false"
  "malbut_reid/malbut_reid/person_reidentifier_node.py|false,false,true,false"
  "malbut_yolo/scripts/prepare_yolo26_model.sh|false,false,true,true"
  "malbut_gazebo_plugins/src/actor_pose_system.cpp|false,false,true,true"
  "perception.repos|false,false,true,true"
  "malbut_interfaces/action/FollowPerson.action|false,false,true,false"
  "malbut_scenarios/malbut_scenarios/text_agent_server.py|false,false,true,false"
  "malbut_system_manager/malbut_system_manager/system_manager_node.py|false,false,true,false"
  "malbut_stt/malbut_stt/node.py|false,false,true,false"
  "malbut_tts/malbut_tts/receiver.py|false,false,true,false"
  "README.md|false,false,false,false"
  ".github/workflows/ci.yml|true,true,true,true"
)

for scenario in "${scenarios[@]}"; do
  path="${scenario%%|*}"
  expected="${scenario#*|}"
  actual="$($selector --paths "$path" | cut -d= -f2 | paste -sd, -)"
  if [ "$actual" != "$expected" ]; then
    printf '%s: expected %s, got %s\n' "$path" "$expected" "$actual" >&2
    exit 1
  fi
done
