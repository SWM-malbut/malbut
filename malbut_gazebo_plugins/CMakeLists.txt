cmake_minimum_required(VERSION 3.8)
project(malbut_gazebo_plugins)

find_package(ament_cmake REQUIRED)
find_package(ignition-common4 REQUIRED COMPONENTS graphics)
find_package(ignition-gazebo6 REQUIRED)
find_package(ignition-msgs8 REQUIRED)

add_library(actor_control_system SHARED src/actor_control_system.cpp)
target_compile_features(actor_control_system PUBLIC cxx_std_17)
target_link_libraries(
  actor_control_system
  ignition-gazebo6::core
)

add_library(
  benchmark_ground_truth_system SHARED
  src/benchmark_ground_truth_system.cpp
)
target_compile_features(benchmark_ground_truth_system PUBLIC cxx_std_17)
target_link_libraries(
  benchmark_ground_truth_system
  ignition-common4::graphics
  ignition-gazebo6::core
  ignition-msgs8::core
)

install(
  TARGETS
    actor_control_system
    benchmark_ground_truth_system
  LIBRARY DESTINATION lib
)

ament_package()
