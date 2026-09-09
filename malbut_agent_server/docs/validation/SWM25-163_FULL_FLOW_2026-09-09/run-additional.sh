#!/usr/bin/env bash
set -eo pipefail
cd "/tmp/malbut-swm25-163-full-ros-dhy4hb7h"
source /opt/ros/humble/setup.bash
source install/setup.bash
unset OPENAI_API_KEY PICOVOICE_ACCESS_KEY
export ROS_DOMAIN_ID=195
export ROS_LOCALHOST_ONLY=1
export RCUTILS_COLORIZED_OUTPUT=0
export PYTHONUNBUFFERED=1
cd src/malbut_agent_server
set +e
timeout 300s env PYTHONPATH=".:${PYTHONPATH}" python3 -m pytest -q -rs test/test_ros_memory_communication.py --junitxml="/tmp/malbut-swm25-163-full-ros-dhy4hb7h/cross_flow_focused.xml" > "/tmp/malbut-swm25-163-full-ros-dhy4hb7h/cross_flow_focused.log" 2>&1
focused_status=$?
set -e
printf '%s\n' "$focused_status" > "/tmp/malbut-swm25-163-full-ros-dhy4hb7h/cross_flow_focused.exit"
cat "/tmp/malbut-swm25-163-full-ros-dhy4hb7h/cross_flow_focused.log"
if [ "$focused_status" -ne 0 ]; then exit "$focused_status"; fi
set +e
timeout 300s env PYTHONPATH=".:${PYTHONPATH}" python3 -m pytest -q -rs test --junitxml="/tmp/malbut-swm25-163-full-ros-dhy4hb7h/malbut_agent_server_final.xml" > "/tmp/malbut-swm25-163-full-ros-dhy4hb7h/malbut_agent_server_final.log" 2>&1
full_status=$?
set -e
printf '%s\n' "$full_status" > "/tmp/malbut-swm25-163-full-ros-dhy4hb7h/malbut_agent_server_final.exit"
cat "/tmp/malbut-swm25-163-full-ros-dhy4hb7h/malbut_agent_server_final.log"
exit "$full_status"
