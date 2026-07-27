#include <gtest/gtest.h>

#include <cstdio>
#include <fstream>
#include "homecam_media_agent/config.hpp"
#include "homecam_media_agent/heartbeat_client.hpp"

using homecam_media_agent::MediaConfig;
using homecam_media_agent::append_heartbeat_response_chunk;
using homecam_media_agent::detector_monitoring_enabled;
using homecam_media_agent::media_generation_allows_io;
using homecam_media_agent::trim_trailing_slashes;
using homecam_media_agent::validate_config;
using homecam_media_agent::HeartbeatStatus;
using homecam_media_agent::heartbeat_to_json;
using homecam_media_agent::is_allowed_backend_url;
using homecam_media_agent::is_valid_device_id;
using homecam_media_agent::is_valid_device_token;
using homecam_media_agent::load_device_token;
using homecam_media_agent::parse_desired_settings;

TEST(MediaConfig, AcceptsSafeSimulationDefaults)
{
  MediaConfig config;
  EXPECT_EQ(config.heartbeat_interval_ms, 2000);
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
    R"({"sourceProfile":"sim","imageTopic":"/camera","streamMode":"idle","mediaHealthy":false,"detectorHealthy":false})");

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

TEST(HeartbeatContract, RejectsResponsePast64KiBWithoutGrowingTheBuffer)
{
  std::string response(64U * 1024U - 1U, 'a');
  ASSERT_TRUE(append_heartbeat_response_chunk(&response, "b", 1U));
  EXPECT_EQ(response.size(), 64U * 1024U);
  EXPECT_FALSE(append_heartbeat_response_chunk(&response, "c", 1U));
  EXPECT_EQ(response.size(), 64U * 1024U);
  EXPECT_FALSE(append_heartbeat_response_chunk(nullptr, "d", 1U));
  EXPECT_FALSE(append_heartbeat_response_chunk(&response, nullptr, 1U));
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
