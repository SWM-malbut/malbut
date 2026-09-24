#!/usr/bin/env bash
set -eo pipefail
# Docker Desktop was started with: open -g -a Docker
# Local dependency snapshot (no pull or package installation):
# docker commit --pause=false malbut-rosorin-sim-20260918 malbut-conversation-ros-fall-local:20260924
repo_root=/Users/sinhyeonjae/Documents/ChatGPT/malbut
artifact_dir="$repo_root/malbut_agent_server/docs/evaluations/conversation-ros-local-2026-09-24/final"
docker run --name malbut-conversation-ros-final-20260924 --pull=never \
  --network none --cap-drop ALL --security-opt no-new-privileges \
  --read-only --tmpfs /tmp:exec,size=2147483648 --tmpfs /root:exec,size=268435456 \
  --mount "type=bind,src=$repo_root,dst=/source,readonly" \
  --mount "type=bind,src=$artifact_dir,dst=/results" \
  --env ROS_LOCALHOST_ONLY=1 --env ROS_DOMAIN_ID=199 \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint bash \
  malbut-conversation-ros-fall-local:20260924 /results/run-in-container.sh
