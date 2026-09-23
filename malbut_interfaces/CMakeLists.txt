cmake_minimum_required(VERSION 3.8)
project(malbut_interfaces)

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/GetWeather.action"
  "action/ExecuteMission.action"
  "action/AutoSlam.action"
  "action/FollowPerson.action"
  "action/Patrol.action"
  "action/Relocalize.action"
  "msg/FallControlHeartbeat.msg"
  "msg/FallRuntimeStatus.msg"
  "msg/FallSettingsReport.msg"
  "msg/FallSettingsSnapshot.msg"
  "msg/LidarCluster.msg"
  "msg/LidarClusterArray.msg"
  "msg/MissionStatus.msg"
  "msg/SensorProcessingTrace.msg"
  "msg/SpeechPlaybackStatus.msg"
  "msg/SpeechRequest.msg"
  "msg/SpeechTranscript.msg"
  "msg/SystemState.msg"
  "msg/TrackingCommandTrace.msg"
  "msg/WeatherForecast.msg"
  "msg/WeatherState.msg"
  "srv/ClassifySpeechAddressee.srv"
  "srv/ControlSpeechPlayback.srv"
  "srv/ApplyFallSettings.srv"
  DEPENDENCIES builtin_interfaces geometry_msgs std_msgs
)

install(
  DIRECTORY capabilities
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/test/test_fall_interfaces.py")
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_fall_interfaces test/test_fall_interfaces.py
    APPEND_ENV PYTHONPATH=${CMAKE_CURRENT_BINARY_DIR}/rosidl_generator_py
    TIMEOUT 60
  )
endif()

ament_export_dependencies(rosidl_default_runtime)
ament_package()
