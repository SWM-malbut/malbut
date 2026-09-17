#pragma once

#include <cstdint>
#include <string>

#include "homecam_media_agent/heartbeat_client.hpp"
#include "homecam_media_agent/kvs_transport.hpp"

namespace homecam_media_agent
{

struct DeviceSessionResult
{
  SessionLease lease;
  DesiredDeviceSettings desired;
  std::string session_id;
};

enum class SessionCloseDisposition
{
  kTerminal,
  kRetryableFailure,
  kPermanentFailure
};

struct SessionCloseResult
{
  SessionCloseDisposition disposition{
    SessionCloseDisposition::kPermanentFailure};
  bool ended{false};
  std::string error;

  bool terminal() const
  {
    return disposition == SessionCloseDisposition::kTerminal;
  }
};

// Parses the device-session contract without accepting duplicate JSON keys,
// mismatched modes, expired credentials, or a response for another device.
bool parse_device_session_response(
  const std::string & response_body,
  const std::string & expected_device_id,
  std::int64_t now_unix_ms,
  DeviceSessionResult * result,
  std::string * error);
bool parse_device_session_close_response(
  const std::string & response_body,
  bool * ended,
  std::string * error);
bool session_create_requires_fail_closed(long http_status);
SessionCloseResult classify_session_close_response(
  long http_status,
  const std::string & response_body);
bool is_valid_session_id(const std::string & session_id);
std::string session_close_request_json(const std::string & session_id);
std::string session_create_request_json(SessionMode mode);

class DeviceSessionClient
{
public:
  DeviceSessionClient(
    std::string backend_url,
    std::string device_id,
    std::string bearer_token);

  bool available() const;
  bool create(
    SessionMode mode,
    std::int64_t now_unix_ms,
    DeviceSessionResult * result,
    std::string * error,
    long * http_status = nullptr) const;
  SessionCloseResult close(const std::string & session_id) const;

private:
  std::string backend_url_;
  std::string device_id_;
  std::string bearer_token_;
};

}  // namespace homecam_media_agent
