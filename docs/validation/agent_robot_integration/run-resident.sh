#!/usr/bin/env bash
set -e
source /opt/ros/humble/setup.bash
source /home/ubuntu/ros2_ws/install/setup.bash
source /home/ubuntu/agent-integration-ws/install/malbut_test/local_setup.bash
source /home/ubuntu/ros2_ws/.typerc >/dev/null
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export HOMECAM_BACKEND_URL='https://d22pvju736a5yt.cloudfront.net'
export HOMECAM_DEVICE_TOKEN_FILE='/home/ubuntu/.config/malbut/device-token'
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  read -rs -p 'OpenAI API key: ' OPENAI_API_KEY
  printf '\n'
fi
export OPENAI_API_KEY
test -n "$OPENAI_API_KEY"
exec ros2 launch malbut_bringup cloud.launch.py
