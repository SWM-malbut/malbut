#!/usr/bin/env bash
set -euo pipefail

selector="$(dirname "$0")/detect-ci-modules.sh"
scenarios=(
  "homecam_web/app/page.tsx|true,false,false,false"
  "homecam_web/infra/cdk/lib/stack.ts|true,true,false,false"
  "homecam_agent/homecam_media_agent/src/node.cpp|false,false,false,true"
  "malbut_gazebo/launch/navigation.launch.py|false,false,true,true"
  "malbut_autonomy/malbut_patrol/patrol.py|false,false,true,false"
  "malbut_scenarios/malbut_scenarios/text_agent_server.py|false,false,true,false"
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
