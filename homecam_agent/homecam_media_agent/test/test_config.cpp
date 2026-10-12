#include <gtest/gtest.h>

#include <cstdio>
#include <fstream>
#include "homecam_media_agent/config.hpp"
#include "homecam_media_agent/heartbeat_client.hpp"

using homecam_media_agent::MediaConfig;
using homecam_media_agent::detector_monitoring_enabled;
using homecam_media_agent::media_generation_allows_io;
using homecam_media_agent::trim_trailing_slashes;
using homecam_media_agent::validate_config;
using homecam_media_agent::HeartbeatStatus;
using homecam_media_agent::heartbeat_relay_request;
using homecam_media_agent::heartbeat_to_json;
using homecam_media_agent::parse_heartbeat_relay_reply;
using homecam_media_agent::is_allowed_backend_url;
using homecam_media_agent::is_valid_device_id;
using homecam_media_agent::is_valid_device_token;
using homecam_media_agent::load_device_token;
using homecam_media_agent::parse_desired_settings;

TEST(Heartbeat, TalkLeaseRequiresBoundedServerAuthority)
{
  const std::string id = "123e4567-e89b-42d3-a456-426614174000";
  nlohmann::json response = {{"desiredState", {
        {"cameraEnabled", true}, {"microphoneEnabled", true},
        {"monitoringEnabled", false}}}};
  homecam_media_agent::DesiredDeviceSettings desired;
  std::string error;
  response["talkLease"] = {{"leaseId", id}, {"remainingMs", 15000}};
  ASSERT_TRUE(parse_desired_settings(response.dump(), &desired, &error));
  EXPECT_EQ(desired.talk_lease_id, id);
  EXPECT_EQ(desired.talk_remaining_ms, 15000);
  // Old servers, video-only viewers and malformed leases all revoke playback.
  for (const auto & invalid : std::vector<nlohmann::json>{
      nullptr, true, {{"leaseId", id}},
      {{"leaseId", "invalid"}, {"remainingMs", 1000}},
      {{"leaseId", id}, {"remainingMs", 0}},
      {{"leaseId", id}, {"remainingMs", -1}},
      {{"leaseId", id}, {"remainingMs", 15001}},
      {{"leaseId", id}, {"remainingMs", 1.5}},
      {{"leaseId", id}, {"remainingMs", true}},
      {{"leaseId", id}, {"remainingMs", 1000}, {"extra", true}}})
  {
    response["talkLease"] = invalid;
    ASSERT_TRUE(parse_desired_settings(response.dump(), &desired, &error));
    EXPECT_TRUE(desired.talk_lease_id.empty());
    EXPECT_EQ(desired.talk_remaining_ms, 0);
  }
  response.erase("talkLease");
  ASSERT_TRUE(parse_desired_settings(response.dump(), &desired, &error));
  EXPECT_TRUE(desired.talk_lease_id.empty());
  HeartbeatStatus status;
  status.talk_report = {{"leaseId", id}, {"ready", false}};
  EXPECT_EQ(nlohmann::json::parse(heartbeat_to_json(status))["talkReport"],
    *status.talk_report);
}

TEST(MediaConfig, AcceptsSafeSimulationDefaults)
{
  MediaConfig config;
  EXPECT_EQ(config.heartbeat_interval_ms, 1000);
  config.device_id = "gazebo-poc";
  config.backend_url = "https://homecam.example.test";
  EXPECT_TRUE(validate_config(config).empty());
}

TEST(MediaConfig, CameraPrivacyAlwaysDisablesDetectorMonitoring)
{
  MediaConfig config;
  config.monitoring_enabled = true;
  config.camera_enabled = true;
  EXPECT_TRUE(detector_monitoring_enabled(config));
  EXPECT_FALSE(detector_monitoring_enabled(config, false));
  config.camera_enabled = false;
  EXPECT_FALSE(detector_monitoring_enabled(config));
  config.camera_enabled = true;
  config.monitoring_enabled = false;
  EXPECT_FALSE(detector_monitoring_enabled(config));
}

TEST(MediaConfig, StaleSessionGenerationCannotSendOrReceiveMedia)
{
  EXPECT_TRUE(media_generation_allows_io(7U, 7U, true));
  // Generation zero is the closed gate while a POST response's microphone
  // privacy state is waiting to be applied by the main executor.
  EXPECT_FALSE(media_generation_allows_io(0U, 7U, true));
  EXPECT_FALSE(media_generation_allows_io(6U, 7U, true));
  EXPECT_FALSE(media_generation_allows_io(7U, 7U, false));
}

TEST(MediaConfig, RejectsUnsafeOrInvalidValues)
{
  MediaConfig config;
  config.image_topic = "relative/image";
  config.encoder = "unknown";
  config.fps = 0;
  config.bitrate_kbps = 12;
  config.backend_url = "http://public.example.test";
  config.device_id.clear();
  const auto errors = validate_config(config);
  EXPECT_GE(errors.size(), 5U);
}

TEST(MediaConfig, AllowsLocalHttpForDevelopment)
{
  MediaConfig config;
  config.backend_url = "http://127.0.0.1:3000/";
  config.device_id = "local-device";
  EXPECT_TRUE(validate_config(config).empty());
  EXPECT_EQ(trim_trailing_slashes(config.backend_url), "http://127.0.0.1:3000");
}

TEST(MediaConfig, PlaintextHttpAllowsOnlyExactLoopbackHosts)
{
  EXPECT_TRUE(is_allowed_backend_url("http://localhost"));
  EXPECT_TRUE(is_allowed_backend_url("http://LOCALHOST:3000/api"));
  EXPECT_TRUE(is_allowed_backend_url("http://127.0.0.1:8080"));
  EXPECT_TRUE(is_allowed_backend_url("http://[::1]:3000"));
  EXPECT_FALSE(is_allowed_backend_url("http://localhost.evil"));
  EXPECT_FALSE(is_allowed_backend_url("http://localhost@evil.example"));
  EXPECT_FALSE(is_allowed_backend_url("http://127.0.0.1.evil"));
  EXPECT_FALSE(is_allowed_backend_url("http://[::1].evil"));
  EXPECT_FALSE(is_allowed_backend_url("http://[::1]evil"));
  EXPECT_FALSE(is_allowed_backend_url("http://localhost:"));
  EXPECT_FALSE(is_allowed_backend_url("http://localhost:70000"));
  EXPECT_FALSE(is_allowed_backend_url("http://192.168.0.10"));
  EXPECT_FALSE(is_allowed_backend_url("http://example.com"));
}

TEST(MediaConfig, HttpsRequiresAValidAuthority)
{
  EXPECT_TRUE(is_allowed_backend_url("https://homecam.example.test"));
  EXPECT_TRUE(is_allowed_backend_url("HTTPS://homecam.example.test:443/api"));
  EXPECT_FALSE(is_allowed_backend_url("https://"));
  EXPECT_FALSE(is_allowed_backend_url("https://user@homecam.example.test"));
  EXPECT_FALSE(is_allowed_backend_url("ftp://homecam.example.test"));
}

TEST(MediaConfig, BackendDeviceIdMatchesBrokerContract)
{
  EXPECT_TRUE(is_valid_device_id("gazebo-homecam:sim"));
  EXPECT_TRUE(is_valid_device_id("A"));
  EXPECT_FALSE(is_valid_device_id(".leading-dot"));
  EXPECT_FALSE(is_valid_device_id("contains space"));
  EXPECT_FALSE(is_valid_device_id(std::string(129, 'a')));

  MediaConfig config;
  config.backend_url = "https://homecam.example.test";
  config.device_id = ".invalid";
  EXPECT_FALSE(validate_config(config).empty());
}

TEST(MediaConfig, CredentialFileTakesPrecedenceAndMissingFileFailsClosed)
{
  const std::string valid_token =
    "hc1.123e4567-e89b-42d3-a456-426614174000." + std::string(64, 'a');
  const std::string token_path =
    std::string(::testing::TempDir()) + "homecam-device-token-test";
  {
    std::ofstream token_file(token_path, std::ios::binary);
    token_file << valid_token << '\n';
  }
  EXPECT_EQ(
    load_device_token(token_path, "must-not-win"), valid_token);
  {
    std::ofstream token_file(token_path, std::ios::binary | std::ios::trunc);
    token_file << std::string(4097, 'a');
  }
  EXPECT_TRUE(load_device_token(token_path, "must-not-win").empty());
  std::remove(token_path.c_str());
  EXPECT_TRUE(load_device_token(token_path, "must-not-win").empty());
  EXPECT_EQ(load_device_token("", valid_token), valid_token);
}

TEST(MediaConfig, CredentialRejectsHeaderInjectionAndWrongTokenShape)
{
  const std::string valid_token =
    "hc1.123e4567-e89b-42d3-a456-426614174000." + std::string(64, 'a');
  EXPECT_TRUE(is_valid_device_token(valid_token));
  EXPECT_FALSE(is_valid_device_token(valid_token + "\r\nInjected: value"));
  EXPECT_FALSE(
    is_valid_device_token(
      "hc1.123e4567-e89b-12d3-a456-426614174000." + std::string(64, 'a')));
  EXPECT_FALSE(
    is_valid_device_token(
      "hc1.123e4567-e89b-42d3-a456-426614174000.not-hex"));
}

TEST(HeartbeatContract, UsesCamelCaseAndParsesDesiredState)
{
  HeartbeatStatus status;
  status.source_profile = "sim";
  status.image_topic = "/camera";
  status.stream_mode = "idle";
  const auto json = heartbeat_to_json(status);
  EXPECT_EQ(
    json,
    R"({"sourceProfile":"sim","imageTopic":"/camera","streamMode":"idle","mediaHealthy":false,"p2pHealthy":false,"storageHealthy":false})");

  homecam_media_agent::DesiredDeviceSettings desired;
  std::string error;
  ASSERT_TRUE(
    parse_desired_settings(
      R"({"reportedState":{"cameraEnabled":true},"desiredState":{"cameraEnabled":false,"microphoneEnabled":true,"monitoringEnabled":true}})",
      &desired, &error));
  ASSERT_TRUE(desired.camera_enabled.has_value());
  EXPECT_FALSE(*desired.camera_enabled);
  ASSERT_TRUE(desired.microphone_enabled.has_value());
  EXPECT_TRUE(*desired.microphone_enabled);
  ASSERT_TRUE(desired.monitoring_enabled.has_value());
  EXPECT_TRUE(*desired.monitoring_enabled);
}

TEST(HeartbeatRelay, RequestCarriesTheSameBodyWithASequence)
{
  homecam_media_agent::HeartbeatStatus status;
  status.stream_mode = "p2p";
  const auto request = nlohmann::json::parse(heartbeat_relay_request(status, 7U));
  EXPECT_EQ(request.size(), 2U);
  EXPECT_EQ(request["sequence"], 7U);
  EXPECT_EQ(request["body"], nlohmann::json::parse(heartbeat_to_json(status)));
}

TEST(HeartbeatRelay, AnswerIsReadLikeTheHttpsResponse)
{
  const nlohmann::json body = {
    {"desiredState", {{"cameraEnabled", false}, {"microphoneEnabled", true},
      {"monitoringEnabled", true}}},
    {"talkLease", {{"leaseId", "8d9b0a52-6f6e-4c5a-9a7f-0d7c2f9c5b11"}, {"remainingMs", 9000}}}};
  const nlohmann::json reply = {{"sequence", 3U}, {"requestedAt", 10.0}, {"observedAt", 10.4},
    {"ok", true}, {"body", body}};
  homecam_media_agent::HeartbeatRelayReply parsed;
  std::string error;
  ASSERT_TRUE(parse_heartbeat_relay_reply(reply.dump(), &parsed, &error)) << error;
  EXPECT_TRUE(parsed.success);
  EXPECT_EQ(parsed.failure_code, "none");
  EXPECT_EQ(parsed.sequence, 3U);
  EXPECT_DOUBLE_EQ(parsed.requested_at, 10.0);
  EXPECT_DOUBLE_EQ(parsed.observed_at, 10.4);
  ASSERT_TRUE(parsed.desired.camera_enabled.has_value());
  EXPECT_FALSE(*parsed.desired.camera_enabled);
  EXPECT_EQ(parsed.desired.talk_remaining_ms, 9000);
  // A body the HTTPS path would reject fails the same way, as a received answer.
  auto bad = reply;
  bad["body"] = {{"desiredState", {{"cameraEnabled", "no"}}}};
  ASSERT_TRUE(parse_heartbeat_relay_reply(bad.dump(), &parsed, &error));
  EXPECT_FALSE(parsed.success);
  EXPECT_EQ(parsed.failure_code, "server_invalid_settings");
  // Past 64 KiB is the HTTPS overflow answer; far past it is not read at all.
  bad["body"] = {{"padding", std::string(64U * 1024U, 'a')}};
  ASSERT_TRUE(parse_heartbeat_relay_reply(bad.dump(), &parsed, &error));
  EXPECT_FALSE(parsed.success);
  EXPECT_EQ(parsed.failure_code, "server_invalid_settings");
  bad["body"] = {{"padding", std::string(66U * 1024U, 'a')}};
  EXPECT_FALSE(parse_heartbeat_relay_reply(bad.dump(), &parsed, &error));
}

TEST(HeartbeatRelay, FailuresKeepTheHttpsFailureCodes)
{
  homecam_media_agent::HeartbeatRelayReply parsed;
  std::string error;
  for (const std::string code : {"server_timeout", "server_transport_error", "server_auth_failed",
      "server_invalid_settings"})
  {
    const nlohmann::json reply = {{"sequence", 1U}, {"requestedAt", 1.0}, {"observedAt", 2.0},
      {"ok", false}, {"failureCode", code}};
    ASSERT_TRUE(parse_heartbeat_relay_reply(reply.dump(), &parsed, &error)) << code;
    EXPECT_FALSE(parsed.success);
    EXPECT_EQ(parsed.failure_code, code);
  }
  for (const std::string text : {
      R"({"sequence":1,"requestedAt":1,"observedAt":2,"ok":false,"failureCode":"none"})",
      R"({"sequence":0,"requestedAt":1,"observedAt":2,"ok":false,"failureCode":"server_timeout"})",
      R"({"sequence":1,"requestedAt":3,"observedAt":2,"ok":false,"failureCode":"server_timeout"})",
      R"({"sequence":1,"requestedAt":1,"observedAt":2,"ok":true,"body":[]})",
      R"({"sequence":1,"requestedAt":1,"observedAt":2,"ok":"yes","failureCode":"server_timeout"})",
      R"({"sequence":1,"requestedAt":1,"observedAt":2,"ok":true,"body":{},"extra":1})",
      R"([])", R"(not json)"})
  {
    EXPECT_FALSE(parse_heartbeat_relay_reply(text, &parsed, &error)) << text;
  }
  EXPECT_FALSE(parse_heartbeat_relay_reply("{}", nullptr, &error));
}

TEST(HeartbeatContract, MediaReceiptRevisionIsOptionalAndNeverUsesAnInvalidOrOldValue)
{
  homecam_media_agent::DesiredDeviceSettings desired;
  std::string error;
  auto body = nlohmann::json::parse(
    R"({"desiredState":{"cameraEnabled":true,"microphoneEnabled":false,"monitoringEnabled":false}})");
  body["mediaSettingsRevision"] = "18446744073709551615";
  ASSERT_TRUE(parse_desired_settings(body.dump(), &desired, &error));
  ASSERT_TRUE(desired.media_settings_revision);
  EXPECT_EQ(*desired.media_settings_revision, std::numeric_limits<std::uint64_t>::max());
  for (const auto & invalid : {"0", "01", "-1", "18446744073709551616"}) {
    body["mediaSettingsRevision"] = invalid;
    ASSERT_TRUE(parse_desired_settings(body.dump(), &desired, &error));
    EXPECT_FALSE(desired.media_settings_revision);
  }
  body.erase("mediaSettingsRevision");
  ASSERT_TRUE(parse_desired_settings(body.dump(), &desired, &error));
  EXPECT_FALSE(desired.media_settings_revision);
}

TEST(HeartbeatContract, AppliedMediaReceiptRequiresLocalPipelinesAndRecordingHealth)
{
  HeartbeatStatus status;
  status.camera_enabled = true;
  status.camera_healthy = true;
  EXPECT_FALSE(homecam_media_agent::media_settings_apply_ready(status, false, true));
  EXPECT_FALSE(homecam_media_agent::media_settings_apply_ready(status, true, false));
  EXPECT_TRUE(homecam_media_agent::media_settings_apply_ready(status, true, true));
  status.monitoring_enabled = true;
  EXPECT_FALSE(homecam_media_agent::media_settings_apply_ready(status, true, true));
  status.storage_healthy = true;
  EXPECT_TRUE(homecam_media_agent::media_settings_apply_ready(status, true, true));
  status.camera_enabled = false;
  EXPECT_FALSE(homecam_media_agent::media_settings_apply_ready(status, true, false));
  EXPECT_TRUE(homecam_media_agent::media_settings_apply_ready(status, false, false));
  status.media_settings_report = {{"runtimeId", "media-1"}, {"requestedRevision", "2"}};
  EXPECT_EQ(nlohmann::json::parse(heartbeat_to_json(status))["mediaSettingsReport"], *status.media_settings_report);
}

TEST(HeartbeatContract, RejectsMalformedWrongTypeAndDuplicateDesiredState)
{
  homecam_media_agent::DesiredDeviceSettings desired;
  std::string error;
  EXPECT_FALSE(parse_desired_settings("{", &desired, &error));
  EXPECT_FALSE(
    parse_desired_settings(
      R"({"desiredState":{"cameraEnabled":"false","microphoneEnabled":true,"monitoringEnabled":true}})",
      &desired, &error));
  EXPECT_FALSE(
    parse_desired_settings(
      R"({"desiredState":{"cameraEnabled":false,"cameraEnabled":true,"microphoneEnabled":true,"monitoringEnabled":true}})",
      &desired, &error));
  EXPECT_FALSE(
    parse_desired_settings(
      R"({"desiredState":{"cameraEnabled":false,"microphoneEnabled":true,"monitoringEnabled":true,"unexpected":false}})",
      &desired, &error));
}
