#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <nlohmann/json.hpp>

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
  bool p2p_healthy{false};
  bool storage_healthy{false};
  std::string stream_mode{"idle"};
  std::string source_profile{"unknown"};
  std::string image_topic;
  std::uint64_t frames_received{0};
  std::optional<nlohmann::json> fall_settings_report;
  std::optional<nlohmann::json> talk_report;
  std::optional<nlohmann::json> media_settings_report;
};

struct FallServerSettings
{
  std::uint64_t revision{0};
  bool enabled{false};
  bool camera_enabled{false};
  bool cloud_consent{false};
};

struct DesiredDeviceSettings
{
  std::optional<std::uint64_t> media_settings_revision;
  std::optional<bool> camera_enabled;
  std::optional<bool> microphone_enabled;
  std::optional<bool> monitoring_enabled;
  std::optional<FallServerSettings> fall;
  std::string fall_reason{"server_settings_missing"};
  std::string talk_lease_id;
  std::int64_t talk_remaining_ms{0};
};

// One web channel (2026-10-12): the resident robot_cloud_sync carries this request
// in its robot state upload and hands the web's heartbeat answer back over ROS.
// The answer is the same body POST /api/device/v1/heartbeat returned.
struct HeartbeatRelayReply
{
  std::uint64_t sequence{0};
  bool success{false};
  DesiredDeviceSettings desired;
  std::string failure_code{"server_transport_error"};
  std::string error;
  double requested_at{0};
  double observed_at{0};
};

std::string heartbeat_to_json(const HeartbeatStatus & status);
std::string heartbeat_relay_request(const HeartbeatStatus & status, std::uint64_t sequence);
bool parse_heartbeat_relay_reply(
  const std::string & message,
  HeartbeatRelayReply * reply,
  std::string * error);
bool media_settings_apply_ready(
  const HeartbeatStatus & status, bool video_pipeline_ready, bool audio_pipeline_ready);
bool parse_desired_settings(
  const std::string & response_body,
  DesiredDeviceSettings * desired,
  std::string * error);

}  // namespace homecam_media_agent
