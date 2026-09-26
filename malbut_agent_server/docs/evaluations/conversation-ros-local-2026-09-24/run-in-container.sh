#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/humble/setup.bash
export PYTHONDONTWRITEBYTECODE=1
export ROS_LOCALHOST_ONLY=1
export ROS_DOMAIN_ID=199
export ROS_LOG_DIR=/tmp/ros-logs
mkdir -p /tmp/ros-workspace
cd /tmp/ros-workspace
python3 - <<'PY' | tee /results/dependencies.txt
import importlib.util,sys
print(sys.version)
for name in ('rclpy','sensor_msgs','std_msgs','action_msgs','rosidl_runtime_py','colcon_core','pytest','yaml','numpy','cv2','cv_bridge','tiktoken','PIL','aiohttp'):
    try: spec=importlib.util.find_spec(name)
    except (ImportError,ValueError): spec=None
    print(name,bool(spec),getattr(spec,'origin',None))
PY
colcon --log-base /tmp/ros-workspace/log build --base-paths /source/malbut_interfaces --packages-select malbut_interfaces --build-base /tmp/ros-workspace/build --install-base /tmp/ros-workspace/install --event-handlers console_direct+ 2>&1 | tee /results/build.log
source /tmp/ros-workspace/install/setup.bash
export PYTHONPATH="/source/malbut_agent_server:/source/malbut_agent_server/test:/source/malbut_system_manager:/source/malbut_tts:/source/malbut_stt:${PYTHONPATH}"
python3 - <<'PY' | tee /results/interface-check.txt
from malbut_interfaces.action import ExecuteMission, FollowPerson, GetWeather
from malbut_interfaces.msg import SpeechRequest, SpeechTranscript
from malbut_interfaces.srv import ClassifySpeechAddressee
for interface in (ExecuteMission,FollowPerson,GetWeather,SpeechRequest,SpeechTranscript,ClassifySpeechAddressee):
    print(interface.__module__,interface.__name__)
PY
set +e
python3 -m pytest -q -rs -p no:cacheprovider --junitxml=/results/pytest.xml \
  /source/malbut_agent_server/test/test_node_communication_ros.py \
  /source/malbut_agent_server/test/test_ros_memory_communication.py \
  /source/malbut_agent_server/test/test_ros_weather_communication.py \
  /source/malbut_agent_server/test/test_speech_addressee_service_ros.py \
  /source/malbut_agent_server/test/test_fall_runtime.py::test_real_ros_callbacks_on_fake_node_keep_images_independent_of_pose \
  2>&1 | tee /results/pytest.log
result=${PIPESTATUS[0]}
printf '%s\n' "$result" > /results/pytest-exit-code.txt
exit "$result"
