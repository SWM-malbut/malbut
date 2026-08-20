cmake_minimum_required(VERSION 3.8)
project(malbut_gazebo_plugins)

find_package(ament_cmake REQUIRED)
find_package(ignition-gazebo6 REQUIRED)

add_library(actor_control_system SHARED src/actor_control_system.cpp)
target_compile_features(actor_control_system PUBLIC cxx_std_17)
target_link_libraries(
  actor_control_system
  ignition-gazebo6::core
)

install(
  TARGETS actor_control_system
  LIBRARY DESTINATION lib
)

ament_package()
