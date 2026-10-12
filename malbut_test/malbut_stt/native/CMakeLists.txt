cmake_minimum_required(VERSION 3.18)
project(malbut_whisper_bridge LANGUAGES C CXX)

set(WHISPER_CPP_SOURCE_DIR "" CACHE PATH "Existing local whisper.cpp checkout")
set(WHISPER_CPP_REVISION "da54572229bcf64ba367d96c7ef15770376c4280")
if(NOT EXISTS "${WHISPER_CPP_SOURCE_DIR}/include/whisper.h")
    message(FATAL_ERROR "Set WHISPER_CPP_SOURCE_DIR to the pinned local whisper.cpp checkout")
endif()
find_package(Git REQUIRED)
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${WHISPER_CPP_SOURCE_DIR}" rev-parse HEAD
    RESULT_VARIABLE revision_status OUTPUT_VARIABLE revision OUTPUT_STRIP_TRAILING_WHITESPACE)
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${WHISPER_CPP_SOURCE_DIR}" status --porcelain
    RESULT_VARIABLE status_status OUTPUT_VARIABLE changes OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT revision_status EQUAL 0 OR NOT status_status EQUAL 0
        OR NOT revision STREQUAL WHISPER_CPP_REVISION OR NOT changes STREQUAL "")
    message(FATAL_ERROR "whisper.cpp must be clean and pinned to ${WHISPER_CPP_REVISION}")
endif()

# Use only supplied sources; no model fetching, system install, or demo targets.
set(BUILD_SHARED_LIBS ON CACHE BOOL "Build shared inference libraries" FORCE)
set(WHISPER_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(WHISPER_CURL OFF CACHE BOOL "" FORCE)
add_subdirectory("${WHISPER_CPP_SOURCE_DIR}" "${CMAKE_BINARY_DIR}/whisper-cpp" EXCLUDE_FROM_ALL)

# The public API omits the selected sequence score and discarded windows. Add a
# read-only observer to a generated copy, retaining the clean pinned checkout.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(observed_source "${CMAKE_CURRENT_BINARY_DIR}/whisper-observed.cpp")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/instrument_whisper.py"
    "${WHISPER_CPP_SOURCE_DIR}/src/whisper.cpp")
execute_process(COMMAND "${Python3_EXECUTABLE}"
    "${CMAKE_CURRENT_SOURCE_DIR}/instrument_whisper.py"
    "${WHISPER_CPP_SOURCE_DIR}/src/whisper.cpp" "${observed_source}"
    RESULT_VARIABLE observation_status)
if(NOT observation_status EQUAL 0)
    message(FATAL_ERROR "Could not instrument the pinned Whisper decoder")
endif()
get_target_property(whisper_sources whisper SOURCES)
list(FIND whisper_sources "whisper.cpp" whisper_source_index)
if(whisper_source_index LESS 0)
    message(FATAL_ERROR "Pinned Whisper target no longer contains whisper.cpp")
endif()
list(REMOVE_AT whisper_sources ${whisper_source_index})
list(APPEND whisper_sources "${observed_source}" "${CMAKE_CURRENT_SOURCE_DIR}/whisper_trace.cpp")
set_property(TARGET whisper PROPERTY SOURCES "${whisper_sources}")
target_include_directories(whisper PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}"
    "${WHISPER_CPP_SOURCE_DIR}/src")

add_library(malbut_whisper SHARED whisper_cpp_bridge.cpp)
target_compile_features(malbut_whisper PRIVATE cxx_std_17)
target_link_libraries(malbut_whisper PRIVATE whisper)
set_target_properties(malbut_whisper PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    BUILD_RPATH "$<TARGET_FILE_DIR:whisper>")
