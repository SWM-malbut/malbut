cmake_minimum_required(VERSION 3.8)
project(homecam_media_agent)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

option(HOMECAM_ENABLE_GSTREAMER "Enable the in-process GStreamer appsrc pipeline" ON)
option(HOMECAM_ENABLE_CURL "Enable HTTPS device heartbeats through libcurl" ON)
option(HOMECAM_ENABLE_KVS "Enable the Amazon KVS WebRTC C SDK transport" OFF)
set(KVS_WEBRTC_SDK_ROOT "" CACHE PATH
  "Pinned amazon-kinesis-video-streams-webrtc-sdk-c source tree with a completed build")
set(HOMECAM_KVS_CA_CERT_PATH "" CACHE FILEPATH
  "CA bundle passed to the upstream KVS sample support code")

find_package(ament_cmake REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)

set(HOMECAM_OPTIONAL_LIBRARIES "")
set(HOMECAM_KVS_COMMON_SOURCE "")
set(HOMECAM_KVS_RUNTIME_PATHS "")

if(HOMECAM_ENABLE_GSTREAMER)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(GSTREAMER QUIET IMPORTED_TARGET
      gstreamer-1.0
      gstreamer-app-1.0)
  endif()
  if(GSTREAMER_FOUND)
    message(STATUS "homecam_media_agent: GStreamer support enabled")
    list(APPEND HOMECAM_OPTIONAL_LIBRARIES PkgConfig::GSTREAMER)
    set(HOMECAM_HAVE_GSTREAMER 1)
  else()
    message(WARNING
      "homecam_media_agent: GStreamer development packages not found; "
      "building health-only mode")
  endif()
endif()

if(HOMECAM_ENABLE_CURL)
  find_package(CURL QUIET)
  if(CURL_FOUND)
    message(STATUS "homecam_media_agent: libcurl heartbeat support enabled")
    list(APPEND HOMECAM_OPTIONAL_LIBRARIES CURL::libcurl)
    set(HOMECAM_HAVE_CURL 1)
  else()
    message(WARNING
      "homecam_media_agent: libcurl development package not found; "
      "heartbeats will be logged locally only")
  endif()
endif()

if(HOMECAM_ENABLE_KVS)
  if(NOT HOMECAM_KVS_CA_CERT_PATH)
    set(
      HOMECAM_KVS_CA_CERT_PATH
      "${KVS_WEBRTC_SDK_ROOT}/certs/cert.pem")
  endif()
  find_path(KVS_WEBRTC_INCLUDE_DIR
    NAMES com/amazonaws/kinesis/video/webrtcclient/Include.h
    HINTS
      "${KVS_WEBRTC_SDK_ROOT}/src/include"
      "${KVS_WEBRTC_SDK_ROOT}/include")
  find_path(KVS_WEBRTC_SAMPLE_INCLUDE_DIR
    NAMES Samples.h
    HINTS "${KVS_WEBRTC_SDK_ROOT}/samples/common"
    NO_DEFAULT_PATH)
  find_path(KVS_WEBRTC_DEPENDENCY_INCLUDE_DIR
    NAMES com/amazonaws/kinesis/video/client/Include.h
    HINTS "${KVS_WEBRTC_SDK_ROOT}/open-source/include"
    NO_DEFAULT_PATH)
  find_file(KVS_WEBRTC_SAMPLE_COMMON_SOURCE
    NAMES Common.c
    HINTS "${KVS_WEBRTC_SDK_ROOT}/samples/common"
    NO_DEFAULT_PATH)
  set(KVS_WEBRTC_LIBRARY_HINTS
    "${KVS_WEBRTC_SDK_ROOT}/build"
    "${KVS_WEBRTC_SDK_ROOT}/lib"
    "${KVS_WEBRTC_SDK_ROOT}/lib64"
    "${KVS_WEBRTC_SDK_ROOT}/open-source/lib"
    "${KVS_WEBRTC_SDK_ROOT}/open-source/lib64")
  find_library(KVS_WEBRTC_CLIENT_LIBRARY
    NAMES kvsWebrtcClient
    HINTS ${KVS_WEBRTC_LIBRARY_HINTS}
    NO_DEFAULT_PATH)
  find_library(KVS_WEBRTC_SIGNALING_LIBRARY
    NAMES kvsWebrtcSignalingClient
    HINTS ${KVS_WEBRTC_LIBRARY_HINTS}
    NO_DEFAULT_PATH)
  find_library(KVS_COMMON_LWS_LIBRARY
    NAMES kvsCommonLws
    HINTS ${KVS_WEBRTC_LIBRARY_HINTS}
    NO_DEFAULT_PATH)
  find_library(KVS_PIC_UTILS_LIBRARY
    NAMES kvspicUtils
    HINTS ${KVS_WEBRTC_LIBRARY_HINTS}
    NO_DEFAULT_PATH)
  find_library(KVS_WEBSOCKETS_LIBRARY
    NAMES websockets
    HINTS ${KVS_WEBRTC_LIBRARY_HINTS}
    NO_DEFAULT_PATH)
  if(NOT KVS_WEBRTC_INCLUDE_DIR OR
    NOT KVS_WEBRTC_SAMPLE_INCLUDE_DIR OR
    NOT KVS_WEBRTC_DEPENDENCY_INCLUDE_DIR OR
    NOT KVS_WEBRTC_SAMPLE_COMMON_SOURCE OR
    NOT KVS_WEBRTC_CLIENT_LIBRARY OR
    NOT KVS_WEBRTC_SIGNALING_LIBRARY OR
    NOT KVS_COMMON_LWS_LIBRARY OR
    NOT KVS_PIC_UTILS_LIBRARY OR
    NOT KVS_WEBSOCKETS_LIBRARY)
    message(FATAL_ERROR
      "HOMECAM_ENABLE_KVS=ON requires the complete pinned AWS KVS WebRTC "
      "SDK source/build tree, including samples/common/Common.c and the "
      "kvsWebrtcClient, kvsWebrtcSignalingClient, kvsCommonLws, kvspicUtils, "
      "and websockets libraries. Run scripts/build_kvs_webrtc_sdk.sh and set "
      "KVS_WEBRTC_SDK_ROOT to that source tree.")
  endif()
  if(NOT EXISTS "${HOMECAM_KVS_CA_CERT_PATH}")
    message(FATAL_ERROR
      "HOMECAM_KVS_CA_CERT_PATH does not exist: ${HOMECAM_KVS_CA_CERT_PATH}")
  endif()
  set(HOMECAM_HAVE_KVS 1)
  set(HOMECAM_KVS_RUNTIME_PATHS
    "${KVS_WEBRTC_SDK_ROOT}/build"
    "${KVS_WEBRTC_SDK_ROOT}/open-source/lib")
  if(EXISTS "${KVS_WEBRTC_SDK_ROOT}/open-source/lib64")
    list(APPEND HOMECAM_KVS_RUNTIME_PATHS
      "${KVS_WEBRTC_SDK_ROOT}/open-source/lib64")
  endif()
  set(HOMECAM_KVS_COMMON_SOURCE "${KVS_WEBRTC_SAMPLE_COMMON_SOURCE}")
  set_source_files_properties(
    "${KVS_WEBRTC_SAMPLE_COMMON_SOURCE}"
    PROPERTIES
      COMPILE_DEFINITIONS
        "CMAKE_DETECTED_CACERT_PATH;KVS_CA_CERT_PATH=\"${HOMECAM_KVS_CA_CERT_PATH}\";deinitKvsWebRtc=homecam_defer_deinit_kvs_webrtc")
  if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    set_property(
      SOURCE "${KVS_WEBRTC_SAMPLE_COMMON_SOURCE}"
      APPEND PROPERTY COMPILE_OPTIONS
        "-Wno-pedantic" "-Wno-implicit-fallthrough")
  endif()
  list(APPEND HOMECAM_OPTIONAL_LIBRARIES
    "${KVS_WEBRTC_CLIENT_LIBRARY}"
    "${KVS_WEBRTC_SIGNALING_LIBRARY}"
    "${KVS_COMMON_LWS_LIBRARY}"
    "${KVS_PIC_UTILS_LIBRARY}"
    "${KVS_WEBSOCKETS_LIBRARY}")
endif()

configure_file(
  include/homecam_media_agent/build_features.hpp.in
  ${CMAKE_CURRENT_BINARY_DIR}/generated/homecam_media_agent/build_features.hpp
  @ONLY)

add_library(homecam_media_core
  src/config.cpp
  src/pipeline_builder.cpp
  src/http_runtime.cpp
  src/heartbeat_client.cpp
  src/session_client.cpp
  src/kvs_transport.cpp
  ${HOMECAM_KVS_COMMON_SOURCE})
target_include_directories(homecam_media_core
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/generated>
    $<INSTALL_INTERFACE:include>)
target_link_libraries(
  homecam_media_core nlohmann_json::nlohmann_json ${HOMECAM_OPTIONAL_LIBRARIES})
if(KVS_WEBRTC_INCLUDE_DIR)
  target_include_directories(homecam_media_core PUBLIC "${KVS_WEBRTC_INCLUDE_DIR}")
endif()
if(KVS_WEBRTC_SAMPLE_INCLUDE_DIR)
  target_include_directories(
    homecam_media_core PRIVATE
      "${KVS_WEBRTC_SAMPLE_INCLUDE_DIR}"
      "${KVS_WEBRTC_DEPENDENCY_INCLUDE_DIR}")
endif()
ament_target_dependencies(homecam_media_core rclcpp)

add_executable(homecam_media_agent_node src/media_agent_node.cpp)
target_link_libraries(
  homecam_media_agent_node homecam_media_core ${HOMECAM_OPTIONAL_LIBRARIES})
target_include_directories(homecam_media_agent_node
  PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)
ament_target_dependencies(
  homecam_media_agent_node nav_msgs rclcpp sensor_msgs std_msgs)
if(HOMECAM_HAVE_KVS)
  # SDK-enabled deployment keeps the pinned SDK tree at this exact path.
  # Embed it so systemd does not depend on a shell LD_LIBRARY_PATH.
  set_target_properties(
    homecam_media_core homecam_media_agent_node
    PROPERTIES
      BUILD_RPATH "${HOMECAM_KVS_RUNTIME_PATHS}"
      INSTALL_RPATH "${HOMECAM_KVS_RUNTIME_PATHS}")
endif()

install(
  TARGETS homecam_media_core homecam_media_agent_node
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(
  DIRECTORY include/
  DESTINATION include
  FILES_MATCHING PATTERN "*.hpp")
install(
  FILES
    ${CMAKE_CURRENT_BINARY_DIR}/generated/homecam_media_agent/build_features.hpp
  DESTINATION include/homecam_media_agent)
install(
  DIRECTORY config launch
  DESTINATION share/${PROJECT_NAME}
  PATTERN "__pycache__" EXCLUDE
  PATTERN "*.pyc" EXCLUDE)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  ament_add_gtest(test_media_config test/test_config.cpp)
  target_link_libraries(test_media_config homecam_media_core)
  ament_add_gtest(test_pipeline_builder test/test_pipeline_builder.cpp)
  target_link_libraries(test_pipeline_builder homecam_media_core)
  ament_add_gtest(test_kvs_transport test/test_kvs_transport.cpp)
  target_link_libraries(test_kvs_transport homecam_media_core)
  ament_add_gtest(test_session_client test/test_session_client.cpp)
  target_link_libraries(test_session_client homecam_media_core)
endif()

ament_export_include_directories(include)
ament_export_libraries(homecam_media_core)
ament_package()
