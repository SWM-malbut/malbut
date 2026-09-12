cmake_minimum_required(VERSION 3.8)
project(malbut_tracking)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(laser_geometry REQUIRED)
find_package(malbut_interfaces REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core imgproc)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_ros REQUIRED)

ament_python_install_package(${PROJECT_NAME})

add_executable(lidar_foreground_preprocessor
  src/lidar_foreground_preprocessor.cpp
)
ament_target_dependencies(lidar_foreground_preprocessor
  laser_geometry
  malbut_interfaces
  nav_msgs
  rclcpp
  sensor_msgs
  tf2
  tf2_ros
)
target_link_libraries(lidar_foreground_preprocessor ${OpenCV_LIBS})
target_compile_options(lidar_foreground_preprocessor PRIVATE
  -Wall -Wextra -Wpedantic
)

install(TARGETS lidar_foreground_preprocessor
  DESTINATION lib/${PROJECT_NAME}
)
install(PROGRAMS
  scripts/person_follower
  scripts/person_localizer
  DESTINATION lib/${PROJECT_NAME}
)
install(DIRECTORY config launch
  DESTINATION share/${PROJECT_NAME}
  PATTERN "__pycache__" EXCLUDE
)
install(FILES README.md DESTINATION share/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(tracking_contracts test
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    APPEND_ENV PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}
    TIMEOUT 120
  )
endif()

ament_package()
