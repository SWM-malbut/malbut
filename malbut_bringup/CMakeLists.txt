cmake_minimum_required(VERSION 3.8)
project(malbut_bringup)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "Build type" FORCE)
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(depth_image_proc REQUIRED)
find_package(image_geometry REQUIRED)
find_package(nav2_costmap_2d REQUIRED)
find_package(nav2_util REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core imgproc)
find_package(pluginlib REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_ros REQUIRED)

ament_python_install_package(${PROJECT_NAME})
install(PROGRAMS
  scripts/wait_for_robot scripts/pose_memory scripts/robot_web_panel scripts/robot_cloud_sync
  DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME}
  PATTERN "__pycache__" EXCLUDE)
install(FILES README.md README_WEB.md DESTINATION share/${PROJECT_NAME})
install(FILES depth_costmap/README.md DESTINATION share/${PROJECT_NAME}/depth_costmap)

# This is an in-process Nav2 plugin, not another ROS node/package.
add_library(malbut_bringup_depth_costmap SHARED
  depth_costmap/src/depth_projector.cpp depth_costmap/src/depth_voxel_layer.cpp)
target_include_directories(malbut_bringup_depth_costmap PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/depth_costmap/include>)
ament_target_dependencies(malbut_bringup_depth_costmap
  depth_image_proc image_geometry nav2_costmap_2d nav2_util pluginlib
  rclcpp sensor_msgs tf2 tf2_ros)
target_link_libraries(malbut_bringup_depth_costmap opencv_core opencv_imgproc)
target_compile_options(malbut_bringup_depth_costmap PRIVATE -Wall -Wextra -Wpedantic)
pluginlib_export_plugin_description_file(nav2_costmap_2d depth_costmap/depth_voxel_layer.xml)
install(TARGETS malbut_bringup_depth_costmap
  ARCHIVE DESTINATION lib LIBRARY DESTINATION lib RUNTIME DESTINATION bin)

if(BUILD_TESTING AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/test")
  find_package(ament_cmake_pytest REQUIRED)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(geometry_msgs REQUIRED)
  ament_add_pytest_test(bringup_contracts test
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    APPEND_ENV PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}
    TIMEOUT 120)
  ament_add_gtest(test_depth_projector test/test_depth_projector.cpp)
  target_link_libraries(test_depth_projector malbut_bringup_depth_costmap)
  ament_target_dependencies(test_depth_projector sensor_msgs image_geometry)
  ament_add_gtest(test_depth_voxel_layer test/test_depth_voxel_layer.cpp TIMEOUT 60)
  target_link_libraries(test_depth_voxel_layer malbut_bringup_depth_costmap)
  ament_target_dependencies(test_depth_voxel_layer
    nav2_util nav2_costmap_2d pluginlib rclcpp tf2_ros geometry_msgs sensor_msgs)
  set_tests_properties(test_depth_voxel_layer PROPERTIES
    ENVIRONMENT "ROS_LOCALHOST_ONLY=1;ROS_DOMAIN_ID=195")
endif()

ament_package()
