cmake_minimum_required(VERSION 3.8)
project(malbut_depth_costmap)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "Build type" FORCE)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(ament_cmake REQUIRED)
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

add_library(${PROJECT_NAME} SHARED src/depth_projector.cpp src/depth_voxel_layer.cpp)
target_include_directories(${PROJECT_NAME} PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(${PROJECT_NAME}
  depth_image_proc image_geometry nav2_costmap_2d nav2_util pluginlib rclcpp sensor_msgs tf2 tf2_ros)
target_link_libraries(${PROJECT_NAME} opencv_core opencv_imgproc)
target_compile_options(${PROJECT_NAME} PRIVATE -Wall -Wextra -Wpedantic)
pluginlib_export_plugin_description_file(nav2_costmap_2d depth_voxel_layer.xml)

install(TARGETS ${PROJECT_NAME} EXPORT export_${PROJECT_NAME}
  ARCHIVE DESTINATION lib LIBRARY DESTINATION lib RUNTIME DESTINATION bin)
install(DIRECTORY include/ DESTINATION include)
install(FILES README.md DESTINATION share/${PROJECT_NAME})
ament_export_targets(export_${PROJECT_NAME} HAS_LIBRARY_TARGET)
ament_export_dependencies(
  depth_image_proc image_geometry nav2_costmap_2d nav2_util pluginlib rclcpp sensor_msgs tf2 tf2_ros)

if(BUILD_TESTING AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/test/test_depth_projector.cpp")
  find_package(ament_cmake_gtest REQUIRED)
  find_package(geometry_msgs REQUIRED)
  ament_add_gtest(test_depth_projector test/test_depth_projector.cpp)
  target_link_libraries(test_depth_projector ${PROJECT_NAME})
  ament_target_dependencies(test_depth_projector sensor_msgs image_geometry)
  ament_add_gtest(test_depth_voxel_layer test/test_depth_voxel_layer.cpp TIMEOUT 60)
  target_link_libraries(test_depth_voxel_layer ${PROJECT_NAME})
  ament_target_dependencies(test_depth_voxel_layer
    nav2_util nav2_costmap_2d pluginlib rclcpp tf2_ros geometry_msgs sensor_msgs)
  set_tests_properties(test_depth_voxel_layer PROPERTIES
    ENVIRONMENT "ROS_LOCALHOST_ONLY=1;ROS_DOMAIN_ID=195")
endif()

ament_package()
