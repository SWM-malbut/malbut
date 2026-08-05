#include "homecam_media_agent/config.hpp"

#include <arpa/inet.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <fstream>
#include <regex>
#include <string_view>

namespace homecam_media_agent
{

namespace
{

std::string ascii_lower(std::string value)
{
  std::transform(
    value.begin(), value.end(), value.begin(),
    [](const unsigned char character) {
      return static_cast<char>(std::tolower(character));
    });
  return value;
}

bool valid_port(const std::string_view port)
{
  if (port.empty()) {
    return false;
  }
  unsigned int value = 0;
  for (const unsigned char character : port) {
    if (!std::isdigit(character)) {
      return false;
    }
    value = value * 10U + static_cast<unsigned int>(character - '0');
    if (value > 65535U) {
      return false;
    }
  }
  return value > 0U;
}

}  // namespace

bool is_allowed_backend_url(const std::string & value)
{
  if (value.empty() ||
    std::any_of(
      value.begin(), value.end(),
      [](const unsigned char character) {
        return std::isspace(character) != 0 || character == '\\';
      }))
  {
    return false;
  }

  const auto separator = value.find("://");
  if (separator == std::string::npos) {
    return false;
  }
  const std::string scheme = ascii_lower(value.substr(0, separator));
  if (scheme != "https" && scheme != "http") {
    return false;
  }

  const auto authority_start = separator + 3;
  const auto authority_end = value.find_first_of("/?#", authority_start);
  const std::string authority = value.substr(
    authority_start,
    authority_end == std::string::npos ?
    std::string::npos : authority_end - authority_start);
  if (authority.empty() || authority.find('@') != std::string::npos) {
    return false;
  }

  std::string hostname;
  std::string_view port;
  if (authority.front() == '[') {
    const auto close = authority.find(']');
    if (close == std::string::npos) {
      return false;
    }
    hostname = authority.substr(1, close - 1);
    in6_addr ipv6_address{};
    if (inet_pton(AF_INET6, hostname.c_str(), &ipv6_address) != 1) {
      return false;
    }
    const std::string_view remainder(authority.data() + close + 1, authority.size() - close - 1);
    if (!remainder.empty()) {
      if (remainder.front() != ':') {
        return false;
      }
      port = remainder.substr(1);
    }
  } else {
    if (authority.find('[') != std::string::npos ||
      authority.find(']') != std::string::npos)
    {
      return false;
    }
    const auto colon = authority.rfind(':');
    if (colon == std::string::npos) {
      hostname = authority;
    } else {
      if (authority.find(':') != colon) {
        return false;
      }
      hostname = authority.substr(0, colon);
      port = std::string_view(authority).substr(colon + 1);
    }
  }

  if (hostname.empty() || (!port.empty() && !valid_port(port))) {
    return false;
  }
  // A trailing colon creates an empty port and must not be accepted.
  if (!authority.empty() && authority.back() == ':') {
    return false;
  }

  if (scheme == "https") {
    return true;
  }
  hostname = ascii_lower(hostname);
  return hostname == "localhost" ||
         hostname == "127.0.0.1" ||
         hostname == "::1";
}

bool is_valid_device_id(const std::string & value)
{
  static const std::regex device_id_pattern(
    R"(^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$)",
    std::regex::ECMAScript);
  return std::regex_match(value, device_id_pattern);
}

std::string load_device_token(
  const std::string & token_file, const std::string & environment_fallback)
{
  if (token_file.empty()) {
    return is_valid_device_token(environment_fallback) ?
           environment_fallback : "";
  }
  std::ifstream input(token_file, std::ios::binary);
  if (!input) {
    return "";
  }
  std::array<char, 4097> buffer{};
  input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
  const auto bytes_read = static_cast<std::size_t>(input.gcount());
  if (bytes_read > 4096U) {
    return "";
  }
  std::string token(buffer.data(), bytes_read);
  while (!token.empty() && (token.back() == '\n' || token.back() == '\r')) {
    token.pop_back();
  }
  return is_valid_device_token(token) ? token : "";
}

bool is_valid_device_token(const std::string & value)
{
  static const std::regex token_pattern(
    R"(^hc1\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\.[0-9a-fA-F]{64}$)",
    std::regex::ECMAScript);
  return std::regex_match(value, token_pattern);
}

bool detector_monitoring_enabled(
  const MediaConfig & config,
  const bool desired_state_confirmed)
{
  return desired_state_confirmed &&
         config.monitoring_enabled &&
         config.camera_enabled;
}

bool media_generation_allows_io(
  const std::uint64_t active_generation,
  const std::uint64_t desired_generation,
  const bool camera_enabled)
{
  return camera_enabled &&
         active_generation == desired_generation;
}

std::vector<std::string> validate_config(const MediaConfig & config)
{
  std::vector<std::string> errors;
  if (config.image_topic.empty() || config.image_topic.front() != '/') {
    errors.emplace_back("image_topic must be an absolute ROS topic");
  }
  if (!config.camera_info_topic.empty() && config.camera_info_topic.front() != '/') {
    errors.emplace_back("camera_info_topic must be empty or an absolute ROS topic");
  }
  if (!config.odom_topic.empty() && config.odom_topic.front() != '/') {
    errors.emplace_back("odom_topic must be empty or an absolute ROS topic");
  }
  if (config.encoder != "x264" && config.encoder != "nvv4l2h264enc") {
    errors.emplace_back("encoder must be x264 or nvv4l2h264enc");
  }
  if (
    config.source_profile != "sim" &&
    config.source_profile != "aurora" &&
    config.source_profile != "unknown")
  {
    errors.emplace_back("source_profile must be sim, aurora, or unknown");
  }
  if (config.fps < 1 || config.fps > 60) {
    errors.emplace_back("fps must be between 1 and 60");
  }
  if (config.bitrate_kbps < 64 || config.bitrate_kbps > 20000) {
    errors.emplace_back("bitrate_kbps must be between 64 and 20000");
  }
  if (config.heartbeat_interval_ms < 1000) {
    errors.emplace_back("heartbeat_interval_ms must be at least 1000");
  }
  if (config.frame_timeout_ms < 250) {
    errors.emplace_back("frame_timeout_ms must be at least 250");
  }
  if (!config.backend_url.empty() && !is_allowed_backend_url(config.backend_url)) {
    errors.emplace_back(
      "backend_url must use HTTPS; plaintext HTTP is accepted only for the "
      "exact localhost, 127.0.0.1, or [::1] hostname");
  }
  if (!config.backend_url.empty() && !is_valid_device_id(config.device_id)) {
    errors.emplace_back(
      "device_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127} when "
      "backend_url is configured");
  }
  return errors;
}

std::string trim_trailing_slashes(const std::string & value)
{
  const auto end = value.find_last_not_of('/');
  if (end == std::string::npos) {
    return value;
  }
  return value.substr(0, end + 1);
}

}  // namespace homecam_media_agent
