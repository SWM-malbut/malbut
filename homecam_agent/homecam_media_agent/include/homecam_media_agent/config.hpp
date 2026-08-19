#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace homecam_media_agent
{

struct MediaConfig
{
  std::string image_topic{"/depth_cam/depth_cam"};
  std::string camera_info_topic{"/depth_cam/rgb/camera_info"};
  std::string odom_topic{"/odom"};
  std::string audio_source{"default"};
  std::string audio_sink{"default"};
  std::string encoder{"x264"};
  std::string device_id;
  std::string backend_url;
  std::string source_profile{"unknown"};
  int fps{15};
  int bitrate_kbps{700};
  int heartbeat_interval_ms{1000};
  int frame_timeout_ms{2000};
  bool monitoring_enabled{false};
  bool camera_enabled{true};
  bool microphone_enabled{true};
};

std::vector<std::string> validate_config(const MediaConfig & config);
bool is_allowed_backend_url(const std::string & value);
bool is_valid_device_id(const std::string & value);
bool is_valid_device_token(const std::string & value);
bool detector_monitoring_enabled(
  const MediaConfig & config,
  bool desired_state_confirmed = true);
bool media_generation_allows_io(
  std::uint64_t active_generation,
  std::uint64_t desired_generation,
  bool camera_enabled);
std::string load_device_token(
  const std::string & token_file, const std::string & environment_fallback);
std::string trim_trailing_slashes(const std::string & value);

}  // namespace homecam_media_agent
