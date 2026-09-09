#!/usr/bin/env bash
set -eo pipefail
cd "/tmp/malbut-swm25-163-full-ros-dhy4hb7h"
source /opt/ros/humble/setup.bash
unset OPENAI_API_KEY PICOVOICE_ACCESS_KEY
export ROS_DOMAIN_ID=195
export ROS_LOCALHOST_ONLY=1
export RCUTILS_COLORIZED_OUTPUT=0
export PYTHONUNBUFFERED=1
python3 - <<'INFO' > environment.json
import importlib.metadata,json,platform,sys,os
packages={}
for name in ['pytest','pydantic','PyYAML','openai','httpx','httpx2','pvporcupine','pvrecorder','webrtcvad']:
 try: packages[name]=importlib.metadata.version(name)
 except importlib.metadata.PackageNotFoundError: packages[name]=None
print(json.dumps({'platform':platform.platform(),'architecture':platform.machine(),'python':sys.version,'ROS_DISTRO':os.getenv('ROS_DISTRO'),'ROS_DOMAIN_ID':os.getenv('ROS_DOMAIN_ID'),'ROS_LOCALHOST_ONLY':os.getenv('ROS_LOCALHOST_ONLY'),'packages':packages},indent=2))
INFO
set +e
timeout 300s colcon build --packages-select malbut_interfaces malbut_agent_server malbut_stt malbut_tts malbut_system_manager > build.log 2>&1
build_status=$?
set -e
printf '%s\n' "$build_status" > build.exit
 tail -25 build.log
if [ "$build_status" -ne 0 ]; then exit "$build_status"; fi
source install/setup.bash
for package in malbut_agent_server malbut_stt malbut_tts malbut_system_manager; do
  set +e
  (cd "src/$package" && timeout 300s env PYTHONPATH=".:${PYTHONPATH}" python3 -m pytest -q -rs test --junitxml="/tmp/malbut-swm25-163-full-ros-dhy4hb7h/${package}.xml") > "${package}.log" 2>&1
  test_status=$?
  set -e
  printf '%s\n' "$test_status" > "${package}.exit"
  printf '\n%s exit=%s\n' "$package" "$test_status"
  tail -22 "${package}.log"
done
