#include "homecam_media_agent/session_client.hpp"

#include <algorithm>
#include <array>
#include <limits>
#include <optional>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>

#include "homecam_media_agent/build_features.hpp"
#include "homecam_media_agent/config.hpp"
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

bool fail(std::string * const error, const std::string & message)
{
  if (error != nullptr) {
    *error = message;
  }
  return false;
}

bool bounded_string(
  const nlohmann::json & object,
  const char * const key,
  const std::size_t maximum_length,
  std::string * const value)
{
  const auto iterator = object.find(key);
  if (iterator == object.end() || !iterator->is_string()) {
    return false;
  }
  const std::string parsed = iterator->get<std::string>();
  if (parsed.empty() || parsed.size() > maximum_length) {
    return false;
  }
  if (value != nullptr) {
    *value = parsed;
  }
  return true;
}

bool required_bool(
  const nlohmann::json & object,
  const char * const key,
  bool * const value)
{
  const auto iterator = object.find(key);
  if (iterator == object.end() || !iterator->is_boolean()) {
    return false;
  }
  if (value != nullptr) {
    *value = iterator->get<bool>();
  }
  return true;
}

bool all_ascii(
  const std::string_view value,
  bool (* predicate)(char))
{
  return !value.empty() &&
         std::all_of(value.begin(), value.end(), predicate);
}

bool valid_region(const std::string & region)
{
  if (region.size() < 9U || region.size() > 32U ||
    region.front() < 'a' || region.front() > 'z' ||
    region.back() < '0' || region.back() > '9')
  {
    return false;
  }
  int hyphens = 0;
  bool previous_hyphen = false;
  for (const char character : region) {
    if (character == '-') {
      if (previous_hyphen) {
        return false;
      }
      ++hyphens;
      previous_hyphen = true;
    } else if (!((character >= 'a' && character <= 'z') ||
      (character >= '0' && character <= '9')))
    {
      return false;
    } else {
      previous_hyphen = false;
    }
  }
  return hyphens >= 2;
}

bool valid_channel_arn_for_region(
  const std::string & arn,
  const std::string & expected_region)
{
  std::array<std::string_view, 6> fields;
  std::size_t start = 0;
  for (std::size_t index = 0; index < 5U; ++index) {
    const std::size_t separator = arn.find(':', start);
    if (separator == std::string::npos) {
      return false;
    }
    fields[index] = std::string_view(arn).substr(start, separator - start);
    start = separator + 1U;
  }
  fields[5] = std::string_view(arn).substr(start);
  const bool valid_partition =
    fields[1] == "aws" ||
    fields[1] == "aws-cn" ||
    fields[1] == "aws-us-gov" ||
    fields[1] == "aws-iso" ||
    fields[1] == "aws-iso-b";
  if (fields[0] != "arn" ||
    !valid_partition ||
    fields[2] != "kinesisvideo" ||
    fields[3] != expected_region ||
    fields[4].size() != 12U ||
    !all_ascii(
      fields[4], [](const char value) {
        return value >= '0' && value <= '9';
      }))
  {
    return false;
  }

  constexpr std::string_view channel_prefix = "channel/";
  if (fields[5].substr(0, channel_prefix.size()) != channel_prefix) {
    return false;
  }
  const std::string_view resource = fields[5].substr(channel_prefix.size());
  const std::size_t separator = resource.find('/');
  if (separator == std::string_view::npos ||
    resource.find('/', separator + 1U) != std::string_view::npos)
  {
    return false;
  }
  const std::string_view name = resource.substr(0, separator);
  const std::string_view creation_time = resource.substr(separator + 1U);
  return name.size() <= 256U &&
         all_ascii(
    name, [](const char value) {
      return (value >= 'A' && value <= 'Z') ||
      (value >= 'a' && value <= 'z') ||
      (value >= '0' && value <= '9') ||
      value == '_' || value == '.' || value == '-';
    }) &&
         creation_time.size() == 13U &&
         all_ascii(
    creation_time, [](const char value) {
      return value >= '0' && value <= '9';
    });
}

bool leap_year(const int year)
{
  return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
}

int days_in_month(const int year, const int month)
{
  constexpr std::array<int, 12> days{
    31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  if (month < 1 || month > 12) {
    return 0;
  }
  if (month == 2 && leap_year(year)) {
    return 29;
  }
  return days[static_cast<std::size_t>(month - 1)];
}

bool parse_decimal(
  const std::string_view input,
  const std::size_t offset,
  const std::size_t length,
  int * const result)
{
  if (offset + length > input.size()) {
    return false;
  }
  int value = 0;
  for (std::size_t index = offset; index < offset + length; ++index) {
    const char character = input[index];
    if (character < '0' || character > '9') {
      return false;
    }
    value = value * 10 + static_cast<int>(character - '0');
  }
  *result = value;
  return true;
}

// Howard Hinnant's civil-calendar conversion, adjusted to the Unix epoch.
std::int64_t days_from_civil(int year, const unsigned int month, const unsigned int day)
{
  year -= month <= 2U ? 1 : 0;
  const std::int64_t era = (year >= 0 ? year : year - 399) / 400;
  const unsigned int year_of_era =
    static_cast<unsigned int>(year - static_cast<int>(era * 400));
  const int adjusted_month =
    static_cast<int>(month) + (month > 2U ? -3 : 9);
  const unsigned int day_of_year =
    (153U * static_cast<unsigned int>(adjusted_month) + 2U) /
    5U + day - 1U;
  const unsigned int day_of_era =
    year_of_era * 365U + year_of_era / 4U - year_of_era / 100U + day_of_year;
  return era * 146097 + static_cast<std::int64_t>(day_of_era) - 719468;
}

std::optional<std::int64_t> parse_iso8601_milliseconds(const std::string & input)
{
  if (input.size() != 24U ||
    input[4] != '-' || input[7] != '-' || input[10] != 'T' ||
    input[13] != ':' || input[16] != ':' || input[19] != '.' ||
    input[23] != 'Z')
  {
    return std::nullopt;
  }

  int year = 0;
  int month = 0;
  int day = 0;
  int hour = 0;
  int minute = 0;
  int second = 0;
  int millisecond = 0;
  if (!parse_decimal(input, 0, 4, &year) ||
    !parse_decimal(input, 5, 2, &month) ||
    !parse_decimal(input, 8, 2, &day) ||
    !parse_decimal(input, 11, 2, &hour) ||
    !parse_decimal(input, 14, 2, &minute) ||
    !parse_decimal(input, 17, 2, &second) ||
    !parse_decimal(input, 20, 3, &millisecond))
  {
    return std::nullopt;
  }
  if (year < 1970 || month < 1 || month > 12 ||
    day < 1 || day > days_in_month(year, month) ||
    hour < 0 || hour > 23 || minute < 0 || minute > 59 ||
    second < 0 || second > 59)
  {
    return std::nullopt;
  }

  constexpr std::int64_t milliseconds_per_day = 86'400'000;
  const std::int64_t days = days_from_civil(
    year, static_cast<unsigned int>(month), static_cast<unsigned int>(day));
  return days * milliseconds_per_day +
         static_cast<std::int64_t>(hour) * 3'600'000 +
         static_cast<std::int64_t>(minute) * 60'000 +
         static_cast<std::int64_t>(second) * 1'000 +
         millisecond;
}

bool parse_json_without_duplicate_keys(
  const std::string & response_body,
  nlohmann::json * const parsed,
  std::string * const error)
{
  bool duplicate_key = false;
  std::unordered_map<int, std::unordered_set<std::string>> keys_by_depth;
  const auto callback =
    [&duplicate_key, &keys_by_depth](
    const int depth,
    const nlohmann::json::parse_event_t event,
    nlohmann::json & value) {
      if (event == nlohmann::json::parse_event_t::object_start) {
        keys_by_depth[depth + 1].clear();
      } else if (event == nlohmann::json::parse_event_t::key) {
        const bool inserted =
          keys_by_depth[depth].insert(value.get<std::string>()).second;
        duplicate_key = duplicate_key || !inserted;
      } else if (event == nlohmann::json::parse_event_t::object_end) {
        keys_by_depth.erase(depth + 1);
      }
      return true;
    };

  nlohmann::json root =
    nlohmann::json::parse(response_body, callback, false, true);
  if (root.is_discarded() || duplicate_key || !root.is_object()) {
    return fail(
      error, duplicate_key ? "duplicate JSON key" : "malformed JSON object");
  }
  *parsed = std::move(root);
  return true;
}

#if HOMECAM_HAVE_CURL
struct ResponseBuffer
{
  std::string body;
  bool overflow{false};
};

std::size_t collect_response(
  char * data,
  const std::size_t size,
  const std::size_t count,
  void * output)
{
  auto * const response = static_cast<ResponseBuffer *>(output);
  if (count != 0U && size > kMaximumResponseBytes / count) {
    response->overflow = true;
    return 0;
  }
  const std::size_t bytes = size * count;
  if (bytes > kMaximumResponseBytes ||
    response->body.size() > kMaximumResponseBytes - bytes)
  {
    response->overflow = true;
    return 0;
  }
  response->body.append(data, bytes);
  return bytes;
}

bool retryable_curl_error(const CURLcode result)
{
  switch (result) {
    case CURLE_COULDNT_RESOLVE_PROXY:
    case CURLE_COULDNT_RESOLVE_HOST:
    case CURLE_COULDNT_CONNECT:
    case CURLE_PARTIAL_FILE:
    case CURLE_WRITE_ERROR:
    case CURLE_READ_ERROR:
    case CURLE_OPERATION_TIMEDOUT:
    case CURLE_SEND_ERROR:
    case CURLE_RECV_ERROR:
    case CURLE_GOT_NOTHING:
      return true;
    default:
      return false;
  }
}

bool perform_request(
  const std::string & url,
  const std::string & bearer_token,
  const char * const method,
  const char * const body,
  const long timeout_ms,
  std::string * const response_body,
  long * const http_status,
  bool * const retryable,
  std::string * const error)
{
  if (retryable != nullptr) {
    *retryable = false;
  }
  if (!ensure_http_runtime(error)) {
    return false;
  }
  CURL * const curl = curl_easy_init();
  if (curl == nullptr) {
    return fail(error, "curl_easy_init failed");
  }

  ResponseBuffer response;
  const std::string authorization = "Authorization: Bearer " + bearer_token;
  curl_slist * headers = nullptr;
  headers = curl_slist_append(headers, "Accept: application/json");
  headers = curl_slist_append(headers, "Content-Type: application/json");
  headers = curl_slist_append(headers, authorization.c_str());

  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
  curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method);
  if (body != nullptr) {
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body);
    curl_easy_setopt(
      curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(std::char_traits<char>::length(body)));
  }
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 2000L);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeout_ms);
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
  curl_easy_setopt(curl, CURLOPT_PROTOCOLS, CURLPROTO_HTTP | CURLPROTO_HTTPS);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, collect_response);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);

  const CURLcode result = curl_easy_perform(curl);
  long received_status = 0;
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &received_status);
  curl_slist_free_all(headers);
  curl_easy_cleanup(curl);

  if (response.overflow) {
    return fail(error, "backend response exceeded 64 KiB");
  }
  if (result != CURLE_OK) {
    if (retryable != nullptr) {
      *retryable = retryable_curl_error(result);
    }
    return fail(error, curl_easy_strerror(result));
  }
  if (http_status != nullptr) {
    *http_status = received_status;
  }
  if (response_body != nullptr) {
    *response_body = std::move(response.body);
  }
  return true;
}
#endif

}  // namespace

bool is_valid_session_id(const std::string & session_id)
{
  if (session_id.size() != 36U ||
    session_id[8] != '-' ||
    session_id[13] != '-' ||
    session_id[18] != '-' ||
    session_id[23] != '-' ||
    session_id[14] != '4')
  {
    return false;
  }
  const char variant = session_id[19];
  if (variant != '8' && variant != '9' &&
    variant != 'a' && variant != 'b' &&
    variant != 'A' && variant != 'B')
  {
    return false;
  }
  for (std::size_t index = 0; index < session_id.size(); ++index) {
    if (index == 8U || index == 13U || index == 18U || index == 23U) {
      continue;
    }
    const char character = session_id[index];
    if (!((character >= '0' && character <= '9') ||
      (character >= 'a' && character <= 'f') ||
      (character >= 'A' && character <= 'F')))
    {
      return false;
    }
  }
  return true;
}

std::string session_close_request_json(const std::string & session_id)
{
  if (!is_valid_session_id(session_id)) {
    return {};
  }
  return nlohmann::json{{"sessionId", session_id}}.dump();
}

bool parse_device_session_response(
  const std::string & response_body,
  const std::string & expected_device_id,
  const std::int64_t now_unix_ms,
  DeviceSessionResult * const result,
  std::string * const error)
{
  nlohmann::json root;
  if (!parse_json_without_duplicate_keys(response_body, &root, error)) {
    return false;
  }

  std::string device_id;
  std::string mode;
  if (!bounded_string(root, "deviceId", 128U, &device_id) ||
    device_id != expected_device_id)
  {
    return fail(error, "deviceId does not match this device");
  }
  if (!bounded_string(root, "mode", 16U, &mode) ||
    (mode != "p2p" && mode != "storage"))
  {
    return fail(error, "mode must be p2p or storage");
  }

  const auto session_iterator = root.find("session");
  const auto kvs_iterator = root.find("kvs");
  const auto desired_iterator = root.find("desiredState");
  if (session_iterator == root.end() || !session_iterator->is_object() ||
    kvs_iterator == root.end() || !kvs_iterator->is_object() ||
    desired_iterator == root.end() || !desired_iterator->is_object())
  {
    return fail(error, "session, kvs, and desiredState must be objects");
  }
  const auto & session = *session_iterator;
  const auto & kvs = *kvs_iterator;
  const auto & desired = *desired_iterator;

  std::string session_id;
  std::string session_mode;
  std::string session_expires_at;
  if (!bounded_string(session, "id", 128U, &session_id) ||
    !is_valid_session_id(session_id) ||
    !bounded_string(session, "mode", 16U, &session_mode) ||
    session_mode != mode ||
    !bounded_string(session, "expiresAt", 32U, &session_expires_at))
  {
    return fail(error, "session fields are missing or inconsistent");
  }
  const auto session_expiration =
    parse_iso8601_milliseconds(session_expires_at);
  if (!session_expiration.has_value() || *session_expiration <= now_unix_ms) {
    return fail(error, "session.expiresAt is invalid or expired");
  }

  std::string role;
  std::string region;
  std::string channel_arn;
  std::string channel_mode;
  if (!bounded_string(kvs, "role", 16U, &role) || role != "MASTER" ||
    !bounded_string(kvs, "region", 64U, &region) ||
    !valid_region(region) ||
    !bounded_string(kvs, "channelArn", 1024U, &channel_arn) ||
    !valid_channel_arn_for_region(channel_arn, region) ||
    !bounded_string(kvs, "channelMode", 16U, &channel_mode) ||
    channel_mode != mode)
  {
    return fail(error, "KVS role, region, channel ARN, or mode is invalid");
  }
  const auto stream_arn_iterator = kvs.find("streamArn");
  if (stream_arn_iterator == kvs.end() ||
    (mode == "storage" &&
    (!stream_arn_iterator->is_string() ||
    stream_arn_iterator->get_ref<const std::string &>().empty())) ||
    (mode == "p2p" && !stream_arn_iterator->is_null()))
  {
    return fail(error, "KVS streamArn does not match the session mode");
  }

  const auto credentials_iterator = kvs.find("credentials");
  if (credentials_iterator == kvs.end() || !credentials_iterator->is_object()) {
    return fail(error, "kvs.credentials must be an object");
  }
  const auto & credentials = *credentials_iterator;
  SessionCredentials parsed_credentials;
  std::string credentials_expires_at;
  if (!bounded_string(
      credentials, "accessKeyId", 4096U,
      &parsed_credentials.access_key_id) ||
    !bounded_string(
      credentials, "secretAccessKey", 4096U,
      &parsed_credentials.secret_access_key) ||
    !bounded_string(
      credentials, "sessionToken", 16384U,
      &parsed_credentials.session_token) ||
    !bounded_string(
      credentials, "expiresAt", 32U,
      &credentials_expires_at))
  {
    return fail(error, "temporary KVS credentials are incomplete");
  }
  const auto credential_expiration =
    parse_iso8601_milliseconds(credentials_expires_at);
  if (!credential_expiration.has_value() ||
    *credential_expiration <= now_unix_ms)
  {
    return fail(error, "temporary KVS credentials are invalid or expired");
  }
  // The backend media-session lease can be shorter than the STS lease. Treat
  // the earlier boundary as authoritative so refresh/stop cannot outlive the
  // application session merely because the AWS credentials still work.
  parsed_credentials.expires_at_unix_ms =
    std::min(*credential_expiration, *session_expiration);

  bool camera_enabled = false;
  bool microphone_enabled = false;
  bool monitoring_enabled = false;
  if (!required_bool(desired, "cameraEnabled", &camera_enabled) ||
    !required_bool(desired, "microphoneEnabled", &microphone_enabled) ||
    !required_bool(desired, "monitoringEnabled", &monitoring_enabled) ||
    monitoring_enabled != (mode == "storage"))
  {
    return fail(error, "desiredState is invalid or inconsistent with mode");
  }

  if (result != nullptr) {
    result->session_id = session_id;
    result->lease.channel_arn = channel_arn;
    result->lease.region = region;
    result->lease.mode =
      mode == "storage" ? SessionMode::kStorage : SessionMode::kPeerToPeer;
    result->lease.credentials = std::move(parsed_credentials);
    result->desired.camera_enabled = camera_enabled;
    result->desired.microphone_enabled = microphone_enabled;
    result->desired.monitoring_enabled = monitoring_enabled;
  }
  return true;
}

bool parse_device_session_close_response(
  const std::string & response_body,
  bool * const ended,
  std::string * const error)
{
  nlohmann::json root;
  if (!parse_json_without_duplicate_keys(response_body, &root, error)) {
    return false;
  }
  const auto value = root.find("ended");
  if (root.size() != 1U || value == root.end() || !value->is_boolean()) {
    return fail(
      error, "session close response must contain only boolean ended");
  }
  if (ended != nullptr) {
    *ended = value->get<bool>();
  }
  // ended=false is an idempotent success: the backend had no active session.
  return true;
}

bool session_create_requires_fail_closed(const long http_status)
{
  return http_status == 401 || http_status == 403;
}

SessionCloseResult classify_session_close_response(
  const long http_status,
  const std::string & response_body)
{
  SessionCloseResult result;
  if (http_status == 200) {
    if (!parse_device_session_close_response(
        response_body, &result.ended, &result.error))
    {
      return result;
    }
    result.disposition = SessionCloseDisposition::kTerminal;
    return result;
  }
  result.disposition =
    http_status >= 500 && http_status <= 599 ?
    SessionCloseDisposition::kRetryableFailure :
    SessionCloseDisposition::kPermanentFailure;
  result.error = "backend returned HTTP " + std::to_string(http_status);
  return result;
}

DeviceSessionClient::DeviceSessionClient(
  std::string backend_url,
  std::string device_id,
  std::string bearer_token)
: backend_url_(std::move(backend_url)),
  device_id_(std::move(device_id)),
  bearer_token_(std::move(bearer_token))
{
}

bool DeviceSessionClient::available() const
{
#if HOMECAM_HAVE_CURL
  return is_allowed_backend_url(backend_url_) &&
         is_valid_device_id(device_id_) &&
         is_valid_device_token(bearer_token_);
#else
  return false;
#endif
}

bool DeviceSessionClient::create(
  const std::int64_t now_unix_ms,
  DeviceSessionResult * const result,
  std::string * const error,
  long * const http_status) const
{
  if (http_status != nullptr) {
    *http_status = 0;
  }
#if HOMECAM_HAVE_CURL
  if (!available()) {
    return fail(error, "device session client is not configured");
  }
  std::string response_body;
  long response_status = 0;
  if (!perform_request(
      backend_url_ + "/api/device/v1/session",
      bearer_token_, "POST", "{}", 15000L, &response_body,
      &response_status, nullptr, error))
  {
    return false;
  }
  if (http_status != nullptr) {
    *http_status = response_status;
  }
  if (response_status < 200 || response_status >= 300) {
    return fail(
      error, "backend returned HTTP " + std::to_string(response_status));
  }
  return parse_device_session_response(
    response_body, device_id_, now_unix_ms, result, error);
#else
  (void)now_unix_ms;
  (void)result;
  return fail(error, "homecam_media_agent was built without libcurl");
#endif
}

SessionCloseResult DeviceSessionClient::close(
  const std::string & session_id) const
{
  SessionCloseResult close_result;
#if HOMECAM_HAVE_CURL
  if (!available()) {
    close_result.error = "device session client is not configured";
    return close_result;
  }
  const std::string request_body = session_close_request_json(session_id);
  if (request_body.empty()) {
    close_result.error = "sessionId must be a UUIDv4";
    return close_result;
  }
  std::string response_body;
  long http_status = 0;
  bool retryable = false;
  std::string error;
  if (!perform_request(
      backend_url_ + "/api/device/v1/session",
      bearer_token_, "DELETE", request_body.c_str(), 2000L,
      &response_body, &http_status, &retryable, &error))
  {
    close_result.disposition = retryable ?
      SessionCloseDisposition::kRetryableFailure :
      SessionCloseDisposition::kPermanentFailure;
    close_result.error = error;
    return close_result;
  }
  return classify_session_close_response(http_status, response_body);
#else
  (void)session_id;
  close_result.error = "homecam_media_agent was built without libcurl";
  return close_result;
#endif
}

}  // namespace homecam_media_agent
