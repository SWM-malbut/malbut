#include "homecam_media_agent/heartbeat_client.hpp"

#include <limits>
#include <sstream>
#include <unordered_map>
#include <unordered_set>
#include <utility>

#include "homecam_media_agent/config.hpp"
#include "homecam_media_agent/build_features.hpp"
#include "homecam_media_agent/http_runtime.hpp"
#include "nlohmann/json.hpp"

#if HOMECAM_HAVE_CURL
#include <curl/curl.h>
#endif

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

#if HOMECAM_HAVE_CURL
struct ResponseBuffer
{
  std::string body;
  bool overflow{false};
};

std::size_t collect_response(
  char * data, std::size_t size, std::size_t count, void * output)
{
  auto * const response = static_cast<ResponseBuffer *>(output);
  if (count != 0U && size > std::numeric_limits<std::size_t>::max() / count) {
    response->overflow = true;
    return 0U;
  }
  const std::size_t bytes = size * count;
  if (!append_heartbeat_response_chunk(&response->body, data, bytes)) {
    response->overflow = true;
    return 0U;
  }
  return bytes;
}
#endif

}  // namespace

bool append_heartbeat_response_chunk(
  std::string * const response_body,
  const char * const data,
  const std::size_t bytes)
{
  if (response_body == nullptr || (data == nullptr && bytes != 0U)) {
    return false;
  }
  if (bytes > kMaximumResponseBytes ||
    response_body->size() > kMaximumResponseBytes - bytes)
  {
    return false;
  }
  if (bytes == 0U) {
    return true;
  }
  response_body->append(data, bytes);
  return true;
}

std::string heartbeat_to_json(const HeartbeatStatus & status)
{
  std::ostringstream json;
  json << "{"
       << "\"sourceProfile\":\"" << json_escape(status.source_profile) << "\","
       << "\"imageTopic\":\"" << json_escape(status.image_topic) << "\","
       << "\"streamMode\":\"" << json_escape(status.stream_mode) << "\","
       << "\"mediaHealthy\":" << (status.media_healthy ? "true" : "false") << ","
       << "\"detectorHealthy\":" << (status.detector_healthy ? "true" : "false")
       << "}";
  return json.str();
}

bool parse_desired_settings(
  const std::string & response_body,
  const std::string & expected_device_id,
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
  const auto device_iterator = root.find("deviceId");
  if (
    !is_valid_device_id(expected_device_id) ||
    device_iterator == root.end() ||
    !device_iterator->is_string() ||
    device_iterator->get_ref<const std::string &>() != expected_device_id)
  {
    if (error != nullptr) {
      *error = "deviceId does not match this device";
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
    desired->camera_enabled =
      desired_iterator->at("cameraEnabled").get<bool>();
    desired->microphone_enabled =
      desired_iterator->at("microphoneEnabled").get<bool>();
    desired->monitoring_enabled =
      desired_iterator->at("monitoringEnabled").get<bool>();
  }
  return true;
}

HeartbeatClient::HeartbeatClient(
  std::string backend_url, std::string bearer_token)
: backend_url_(std::move(backend_url)),
  bearer_token_(std::move(bearer_token))
{
}

bool HeartbeatClient::available() const
{
#if HOMECAM_HAVE_CURL
  return is_allowed_backend_url(backend_url_) &&
         is_valid_device_token(bearer_token_);
#else
  return false;
#endif
}

bool HeartbeatClient::post(
  const HeartbeatStatus & status,
  DesiredDeviceSettings * const desired,
  std::string * const error) const
{
#if HOMECAM_HAVE_CURL
  if (!available()) {
    if (error != nullptr) {
      *error = "backend URL or HOMECAM_DEVICE_TOKEN is missing";
    }
    return false;
  }
  if (!ensure_http_runtime(error)) {
    return false;
  }

  CURL * curl = curl_easy_init();
  if (curl == nullptr) {
    if (error != nullptr) {
      *error = "curl_easy_init failed";
    }
    return false;
  }

  const std::string url = backend_url_ + "/api/device/v1/heartbeat";
  const std::string payload = heartbeat_to_json(status);
  ResponseBuffer response;
  const std::string authorization = "Authorization: Bearer " + bearer_token_;
  curl_slist * headers = nullptr;
  headers = curl_slist_append(headers, "Content-Type: application/json");
  headers = curl_slist_append(headers, authorization.c_str());

  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
  curl_easy_setopt(curl, CURLOPT_POST, 1L);
  curl_easy_setopt(curl, CURLOPT_POSTFIELDS, payload.c_str());
  curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(payload.size()));
  // Production backends may need a few seconds for serverless cold starts and
  // database work. Keep the heartbeat asynchronous, but do not cancel a valid
  // response before it can complete.
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 2000L);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10000L);
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_PROTOCOLS, CURLPROTO_HTTP | CURLPROTO_HTTPS);
  curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
  curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 0L);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, collect_response);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);

  const CURLcode result = curl_easy_perform(curl);
  long response_code = 0;
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response_code);
  curl_slist_free_all(headers);
  curl_easy_cleanup(curl);

  if (response.overflow) {
    if (error != nullptr) {
      *error = "backend response exceeded 64 KiB";
    }
    return false;
  }
  if (result != CURLE_OK) {
    if (error != nullptr) {
      *error = curl_easy_strerror(result);
    }
    return false;
  }
  if (response_code < 200 || response_code >= 300) {
    if (error != nullptr) {
      *error = "backend returned HTTP " + std::to_string(response_code);
    }
    return false;
  }
  DesiredDeviceSettings parsed_desired;
  std::string parse_error;
  if (!parse_desired_settings(
      response.body, status.device_id, &parsed_desired, &parse_error))
  {
    if (error != nullptr) {
      *error = "invalid heartbeat response: " + parse_error;
    }
    return false;
  }
  if (desired != nullptr) {
    *desired = parsed_desired;
  }
  return true;
#else
  (void)status;
  (void)desired;
  if (error != nullptr) {
    *error = "homecam_media_agent was built without libcurl";
  }
  return false;
#endif
}

}  // namespace homecam_media_agent
