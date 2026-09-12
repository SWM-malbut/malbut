cmake_minimum_required(VERSION 3.8)
project(malbut_interfaces)

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/ExecuteMission.action"
  "action/AutoSlam.action"
  "action/FollowPerson.action"
  "action/Patrol.action"
  "msg/LidarCluster.msg"
  "msg/LidarClusterArray.msg"
  "msg/MissionStatus.msg"
  "msg/SensorProcessingTrace.msg"
  "msg/SpeechRequest.msg"
  "msg/SpeechTranscript.msg"
  "msg/SystemState.msg"
  "msg/TrackingCommandTrace.msg"
  DEPENDENCIES builtin_interfaces geometry_msgs std_msgs
)

install(
  DIRECTORY capabilities
  DESTINATION share/${PROJECT_NAME}
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
