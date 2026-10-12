#include "homecam_media_agent/heartbeat_client.hpp"
#include <charconv>

#include <cmath>
#include <limits>
#include <regex>
#include <sstream>
#include <unordered_map>
#include <unordered_set>
#include <utility>

#include "nlohmann/json.hpp"

namespace homecam_media_agent
{

namespace
{

constexpr std::size_t kMaximumResponseBytes = 64U * 1024U;

std::string json_escape(const std::string & value)
{
  std::ostringstream output;
  for (const unsigned char character : value) {
    switch (character) {
      case '"': output << "\\\""; break;
      case '\\': output << "\\\\"; break;
      case '\b': output << "\\b"; break;
      case '\f': output << "\\f"; break;
      case '\n': output << "\\n"; break;
      case '\r': output << "\\r"; break;
      case '\t': output << "\\t"; break;
      default:
        if (character < 0x20) {
          output << "\\u00";
          constexpr char hex[] = "0123456789abcdef";
          output << hex[(character >> 4) & 0x0f] << hex[character & 0x0f];
        } else {
          output << character;
        }
    }
  }
  return output.str();
}


}  // namespace

std::string heartbeat_to_json(const HeartbeatStatus & status)
{
  std::ostringstream json;
  json << "{"
       << "\"sourceProfile\":\"" << json_escape(status.source_profile) << "\","
       << "\"imageTopic\":\"" << json_escape(status.image_topic) << "\","
       << "\"streamMode\":\"" << json_escape(status.stream_mode) << "\","
       << "\"mediaHealthy\":" << (status.media_healthy ? "true" : "false") << ","
       << "\"p2pHealthy\":" << (status.p2p_healthy ? "true" : "false") << ","
       << "\"storageHealthy\":" << (status.storage_healthy ? "true" : "false");
  if (status.fall_settings_report) {
    json << ",\"fallSettingsReport\":" << status.fall_settings_report->dump();
  }
  if (status.talk_report) {
    json << ",\"talkReport\":" << status.talk_report->dump();
  }
  if (status.media_settings_report) {
    json << ",\"mediaSettingsReport\":" << status.media_settings_report->dump();
  }
  json << "}";
  return json.str();
}

bool media_settings_apply_ready(
  const HeartbeatStatus & status,
  const bool video_pipeline_ready, const bool audio_pipeline_ready)
{
  if (!status.camera_enabled) {return !video_pipeline_ready;}
  return video_pipeline_ready && audio_pipeline_ready && status.camera_healthy &&
         (!status.monitoring_enabled || status.storage_healthy);
}

bool parse_desired_settings(
  const std::string & response_body,
  DesiredDeviceSettings * const desired,
  std::string * const error)
{
  bool duplicate_key = false;
  std::unordered_map<int, std::unordered_set<std::string>> keys_by_depth;
  const auto callback =
    [&duplicate_key, &keys_by_depth](
    const int depth, const nlohmann::json::parse_event_t event,
    nlohmann::json & parsed) {
      if (event == nlohmann::json::parse_event_t::object_start) {
        keys_by_depth[depth + 1].clear();
      } else if (event == nlohmann::json::parse_event_t::key) {
        const auto inserted =
          keys_by_depth[depth].insert(parsed.get<std::string>()).second;
        duplicate_key = duplicate_key || !inserted;
      } else if (event == nlohmann::json::parse_event_t::object_end) {
        keys_by_depth.erase(depth + 1);
      }
      return true;
    };

  const auto root = nlohmann::json::parse(
    response_body, callback, false, true);
  if (root.is_discarded() || duplicate_key || !root.is_object()) {
    if (error != nullptr) {
      *error = duplicate_key ? "duplicate JSON key" : "malformed JSON object";
    }
    return false;
  }
  const auto desired_iterator = root.find("desiredState");
  if (desired_iterator == root.end() || !desired_iterator->is_object()) {
    if (error != nullptr) {
      *error = "desiredState must be an object";
    }
    return false;
  }
  if (desired_iterator->size() != 3U) {
    if (error != nullptr) {
      *error = "desiredState contains an unsupported or missing field";
    }
    return false;
  }

  constexpr const char * keys[] = {
    "cameraEnabled", "microphoneEnabled", "monitoringEnabled"};
  for (const char * key : keys) {
    const auto value = desired_iterator->find(key);
    if (value == desired_iterator->end() || !value->is_boolean()) {
      if (error != nullptr) {
        *error = std::string("desiredState.") + key + " must be a boolean";
      }
      return false;
    }
  }

  if (desired != nullptr) {
    desired->media_settings_revision.reset();
    const auto media_revision = root.find("mediaSettingsRevision");
    if (media_revision != root.end() && media_revision->is_string()) {
      const auto text = media_revision->get<std::string>();
      std::uint64_t revision = 0;
      const auto parsed = std::from_chars(text.data(), text.data() + text.size(), revision);
      if (!text.empty() && text[0] != '0' && parsed.ec == std::errc() &&
        parsed.ptr == text.data() + text.size() && revision > 0)
      {
        desired->media_settings_revision = revision;
      }
    }
    desired->fall.reset();
    desired->fall_reason = "server_settings_missing";
    desired->camera_enabled =
      desired_iterator->at("cameraEnabled").get<bool>();
    desired->microphone_enabled =
      desired_iterator->at("microphoneEnabled").get<bool>();
    desired->monitoring_enabled =
      desired_iterator->at("monitoringEnabled").get<bool>();
    desired->talk_lease_id.clear();
    desired->talk_remaining_ms = 0;
    const auto talk = root.find("talkLease");
    // Missing/invalid talk state must never permit remote speaker playback.
    if (talk != root.end() && talk->is_object() && talk->size() == 2U &&
      talk->contains("leaseId") && (*talk)["leaseId"].is_string() &&
      talk->contains("remainingMs") && (*talk)["remainingMs"].is_number_integer())
    {
      static const std::regex uuid(
        "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        std::regex::icase);
      const auto id = (*talk)["leaseId"].get<std::string>();
      const auto & remaining = (*talk)["remainingMs"];
      if (std::regex_match(id, uuid) && remaining > 0 && remaining <= 15000) {
        desired->talk_lease_id = id;
        desired->talk_remaining_ms = remaining.get<std::int64_t>();
      }
    }
    const auto fall = root.find("fallSettings");
    if (fall != root.end()) {
      desired->fall_reason = "server_invalid_settings";
      if (fall->is_object() && fall->size() == 4U &&
        fall->contains("settingsRevision") && (*fall)["settingsRevision"].is_string() &&
        fall->contains("enabled") && (*fall)["enabled"].is_boolean() &&
        fall->contains("cameraEnabled") && (*fall)["cameraEnabled"].is_boolean() &&
        fall->contains("cloudConsent") && (*fall)["cloudConsent"].is_boolean())
      {
        const auto text = (*fall)["settingsRevision"].get<std::string>();
        std::uint64_t revision = 0;
        const auto parsed = std::from_chars(text.data(), text.data() + text.size(), revision);
        if (!text.empty() && text.size() <= 20U && text[0] != '0' &&
          parsed.ec == std::errc() && parsed.ptr == text.data() + text.size() && revision > 0 &&
          (*fall)["cameraEnabled"].get<bool>() == *desired->camera_enabled)
        {
          desired->fall = FallServerSettings{revision, (*fall)["enabled"].get<bool>(),
            (*fall)["cameraEnabled"].get<bool>(), (*fall)["cloudConsent"].get<bool>()};
          desired->fall_reason = "none";
        }
      }
    }
  }
  return true;
}

std::string heartbeat_relay_request(const HeartbeatStatus & status, const std::uint64_t sequence)
{
  return "{\"sequence\":" + std::to_string(sequence) + ",\"body\":" + heartbeat_to_json(status) + "}";
}

bool parse_heartbeat_relay_reply(
  const std::string & message,
  HeartbeatRelayReply * const reply,
  std::string * const error)
{
  const auto fail = [error](const std::string & text) {
      if (error != nullptr) {*error = text;}
      return false;
    };
  if (reply == nullptr) {return fail("heartbeat reply output is required");}
  // The relayed body keeps the 64 KiB limit the HTTPS heartbeat had.
  if (message.size() > kMaximumResponseBytes + 1024U) {
    return fail("heartbeat reply exceeds its size limit");
  }
  const auto root = nlohmann::json::parse(message, nullptr, false);
  if (root.is_discarded() || !root.is_object() || root.size() != 5U) {
    return fail("heartbeat reply must be an object with five fields");
  }
  const auto time = [&root](const char * key) {
      const auto value = root.find(key);
      return value != root.end() && value->is_number() &&
             std::isfinite(value->get<double>()) && value->get<double>() >= 0.0;
    };
  const auto sequence = root.find("sequence");
  const auto ok = root.find("ok");
  if (sequence == root.end() || !sequence->is_number_unsigned() ||
    sequence->get<std::uint64_t>() == 0U || !time("requestedAt") || !time("observedAt") ||
    ok == root.end() || !ok->is_boolean() ||
    root["observedAt"].get<double>() < root["requestedAt"].get<double>())
  {
    return fail("heartbeat reply has a missing or invalid field");
  }
  HeartbeatRelayReply parsed;
  parsed.sequence = sequence->get<std::uint64_t>();
  parsed.requested_at = root["requestedAt"].get<double>();
  parsed.observed_at = root["observedAt"].get<double>();
  if (ok->get<bool>()) {
    const auto body = root.find("body");
    if (body == root.end() || !body->is_object()) {
      return fail("heartbeat reply body must be an object");
    }
    std::string parse_error;
    const std::string text = body->dump();
    // Same outcome as an oversized or malformed HTTPS heartbeat response.
    if (text.size() > kMaximumResponseBytes) {
      parsed.failure_code = "server_invalid_settings";
      parsed.error = "backend response exceeded 64 KiB";
    } else if (!parse_desired_settings(text, &parsed.desired, &parse_error)) {
      parsed.failure_code = "server_invalid_settings";
      parsed.error = "invalid heartbeat response: " + parse_error;
    } else {
      parsed.success = true;
      parsed.failure_code = "none";
    }
  } else {
    static const std::unordered_set<std::string> failures{
      "server_timeout", "server_transport_error", "server_auth_failed", "server_invalid_settings"};
    const auto code = root.find("failureCode");
    if (code == root.end() || !code->is_string() || !failures.count(code->get<std::string>())) {
      return fail("heartbeat reply failure code is invalid");
    }
    parsed.failure_code = code->get<std::string>();
    parsed.error = "robot_cloud_sync reported " + parsed.failure_code;
  }
  *reply = std::move(parsed);
  return true;
}

}  // namespace homecam_media_agent
