cmake_minimum_required(VERSION 3.8)
project(malbut_bringup)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "Build type" FORCE)
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The Nav2 depth costmap plugin is kept for later use but not built: its depth
# input delayed TF on the robot, so both costmaps are LiDAR-only for now.
# See depth_costmap/README.md before building it with -DMALBUT_DEPTH_COSTMAP=ON.
option(MALBUT_DEPTH_COSTMAP "Build the unused Nav2 depth costmap plugin" OFF)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(slam_toolbox REQUIRED)
find_package(Boost REQUIRED COMPONENTS thread serialization)
find_package(nav2_costmap_2d REQUIRED)
find_package(nav2_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(pluginlib REQUIRED)

add_library(malbut_keepout_escape SHARED
  keepout/src/exit_corridor.cpp keepout/src/keepout_escape.cpp)
target_include_directories(malbut_keepout_escape PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/keepout/include>)
ament_target_dependencies(malbut_keepout_escape
  nav2_costmap_2d nav2_msgs geometry_msgs rclcpp pluginlib tf2_geometry_msgs)
target_compile_options(malbut_keepout_escape PRIVATE -Wall -Wextra -Wpedantic)
pluginlib_export_plugin_description_file(nav2_costmap_2d keepout/keepout_escape.xml)
install(TARGETS malbut_keepout_escape LIBRARY DESTINATION lib)

# Native servers/plugins are unchanged; these small entrypoints fix Humble's
# worker lifetime on process exit, without adding another resident process.
add_executable(component_container_isolated src/component_container_isolated.cpp)
ament_target_dependencies(component_container_isolated rclcpp rclcpp_components)
target_link_libraries(component_container_isolated ${CMAKE_DL_LIBS})
add_executable(sync_slam_toolbox src/sync_slam_toolbox.cpp)
ament_target_dependencies(sync_slam_toolbox rclcpp tf2_ros message_filters
  nav_msgs sensor_msgs std_srvs visualization_msgs interactive_markers
  pluginlib tf2_geometry_msgs tf2_sensor_msgs)
target_include_directories(sync_slam_toolbox PRIVATE ${slam_toolbox_INCLUDE_DIRS})
# slam_toolbox's exported target is its RViz plugin, not the mapping library.
# Link only the installed mapper libraries used by this native entrypoint.
find_library(MALBUT_SLAM_SYNC_LIBRARY sync_slam_toolbox
  HINTS "${slam_toolbox_DIR}/../../../lib" REQUIRED)
find_library(MALBUT_SLAM_COMMON_LIBRARY toolbox_common
  HINTS "${slam_toolbox_DIR}/../../../lib" REQUIRED)
find_library(MALBUT_KARTO_LIBRARY kartoSlamToolbox
  HINTS "${slam_toolbox_DIR}/../../../lib" REQUIRED)
target_link_libraries(sync_slam_toolbox ${MALBUT_SLAM_SYNC_LIBRARY}
  ${MALBUT_SLAM_COMMON_LIBRARY} ${MALBUT_KARTO_LIBRARY} Boost::thread Boost::serialization)
install(TARGETS component_container_isolated sync_slam_toolbox
  DESTINATION lib/${PROJECT_NAME})

ament_python_install_package(${PROJECT_NAME})
install(PROGRAMS
  scripts/wait_for_robot scripts/zone_filter scripts/robot_web_panel scripts/robot_cloud_sync
  scripts/speech_preflight
  scripts/runtime_control
  DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME}
  PATTERN "__pycache__" EXCLUDE)
install(FILES README.md README_OPERATIONS.md README_WEB.md README_SPEECH.md
  DESTINATION share/${PROJECT_NAME})

if(MALBUT_DEPTH_COSTMAP)
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
endif()

if(BUILD_TESTING AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/test")
  find_package(ament_cmake_gtest REQUIRED)
  find_package(nav2_util REQUIRED)
  find_package(dwb_core REQUIRED)
  find_package(nav2_navfn_planner REQUIRED)
  ament_add_gtest(test_keepout_escape test/test_keepout_escape.cpp TIMEOUT 90)
  target_link_libraries(test_keepout_escape malbut_keepout_escape)
  ament_target_dependencies(test_keepout_escape nav2_util nav2_costmap_2d nav2_msgs
    rclcpp pluginlib tf2_ros geometry_msgs dwb_core nav2_navfn_planner)
  target_compile_definitions(test_keepout_escape PRIVATE
    NAV2_TEST_PARAMS="${CMAKE_CURRENT_SOURCE_DIR}/config/nav2_params.yaml")
  set_tests_properties(test_keepout_escape PROPERTIES
    ENVIRONMENT "ROS_LOCALHOST_ONLY=1;ROS_DOMAIN_ID=196")
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(bringup_contracts test
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    APPEND_ENV PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}
    TIMEOUT 120)
  if(MALBUT_DEPTH_COSTMAP)
    find_package(ament_cmake_gtest REQUIRED)
    find_package(geometry_msgs REQUIRED)
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
endif()

ament_package()
