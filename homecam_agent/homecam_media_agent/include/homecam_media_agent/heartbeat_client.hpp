#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

namespace homecam_media_agent
{

struct HeartbeatStatus
{
  std::string device_id;
  bool online{true};
  bool camera_enabled{true};
  bool microphone_enabled{true};
  bool monitoring_enabled{false};
  bool camera_healthy{false};
  bool media_healthy{false};
  bool detector_healthy{false};
  std::string stream_mode{"idle"};
  std::string source_profile{"unknown"};
  std::string image_topic;
  std::uint64_t frames_received{0};
};

struct DesiredDeviceSettings
{
  std::optional<bool> camera_enabled;
  std::optional<bool> microphone_enabled;
  std::optional<bool> monitoring_enabled;
};

class HeartbeatClient
{
public:
  HeartbeatClient(std::string backend_url, std::string bearer_token);
  bool available() const;
  bool post(
    const HeartbeatStatus & status,
    DesiredDeviceSettings * desired,
    std::string * error) const;

private:
  std::string backend_url_;
  std::string bearer_token_;
};

std::string heartbeat_to_json(const HeartbeatStatus & status);
bool append_heartbeat_response_chunk(
  std::string * response_body,
  const char * data,
  std::size_t bytes);
bool parse_desired_settings(
  const std::string & response_body,
  const std::string & expected_device_id,
  DesiredDeviceSettings * desired,
  std::string * error);

}  // namespace homecam_media_agent
