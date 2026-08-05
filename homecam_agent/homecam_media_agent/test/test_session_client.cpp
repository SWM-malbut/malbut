#include <gtest/gtest.h>

#include <cstdint>
#include <string>

#include "homecam_media_agent/session_client.hpp"

using homecam_media_agent::DeviceSessionResult;
using homecam_media_agent::SessionCloseDisposition;
using homecam_media_agent::SessionMode;
using homecam_media_agent::classify_session_close_response;
using homecam_media_agent::is_valid_session_id;
using homecam_media_agent::parse_device_session_response;
using homecam_media_agent::parse_device_session_close_response;
using homecam_media_agent::session_close_request_json;
using homecam_media_agent::session_create_requires_fail_closed;

namespace
{

constexpr std::int64_t kNow = 1'785'064'200'000;
constexpr char kSessionId[] = "123e4567-e89b-42d3-a456-426614174000";

std::string valid_response(const std::string & mode = "storage")
{
  const bool storage = mode == "storage";
  return
    "{"
    "\"deviceId\":\"robot-01\","
    "\"mode\":\"" + mode + "\","
    "\"session\":{"
    "\"id\":\"" + std::string(kSessionId) + "\","
    "\"roomCode\":\"123456\","
    "\"mode\":\"" + mode + "\","
    "\"startedAt\":\"2026-07-26T11:00:00.000Z\","
    "\"expiresAt\":\"2026-07-26T11:20:00.000Z\""
    "},"
    "\"kvs\":{"
    "\"role\":\"MASTER\","
    "\"region\":\"ap-northeast-2\","
    "\"channelArn\":\"arn:aws:kinesisvideo:ap-northeast-2:123456789012:"
    "channel/homecam/1234567890123\","
    "\"streamArn\":" +
    std::string(
    storage ?
    "\"arn:aws:kinesisvideo:ap-northeast-2:123456789012:stream/homecam/1\"" :
    "null") + ","
    "\"channelMode\":\"" + mode + "\","
    "\"credentials\":{"
    "\"accessKeyId\":\"temporary-id\","
    "\"secretAccessKey\":\"temporary-secret\","
    "\"sessionToken\":\"temporary-token\","
    "\"expiresAt\":\"2026-07-26T11:25:00.000Z\""
    "}"
    "},"
    "\"desiredState\":{"
    "\"monitoringEnabled\":" + std::string(storage ? "true" : "false") + ","
    "\"cameraEnabled\":true,"
    "\"microphoneEnabled\":true"
    "}"
    "}";
}

}  // namespace

TEST(DeviceSessionResponse, ParsesStrictStorageLease)
{
  DeviceSessionResult result;
  std::string error;
  ASSERT_TRUE(
    parse_device_session_response(
      valid_response(), "robot-01", kNow, &result, &error)) << error;
  EXPECT_EQ(result.session_id, kSessionId);
  EXPECT_EQ(result.lease.mode, SessionMode::kStorage);
  EXPECT_EQ(result.lease.region, "ap-northeast-2");
  EXPECT_EQ(
    result.lease.credentials.expires_at_unix_ms,
    1'785'064'800'000);
  ASSERT_TRUE(result.desired.monitoring_enabled.has_value());
  EXPECT_TRUE(*result.desired.monitoring_enabled);
}

TEST(DeviceSessionResponse, ParsesPeerToPeerLease)
{
  DeviceSessionResult result;
  std::string error;
  ASSERT_TRUE(
    parse_device_session_response(
      valid_response("p2p"), "robot-01", kNow, &result, &error)) << error;
  EXPECT_EQ(result.lease.mode, SessionMode::kPeerToPeer);
}

TEST(DeviceSessionResponse, RejectsDuplicateCredentialKey)
{
  std::string response = valid_response();
  const std::string needle = "\"accessKeyId\":\"temporary-id\",";
  response.replace(
    response.find(needle), needle.size(),
    needle + "\"accessKeyId\":\"attacker\",");
  std::string error;
  EXPECT_FALSE(
    parse_device_session_response(
      response, "robot-01", kNow, nullptr, &error));
  EXPECT_EQ(error, "duplicate JSON key");
}

TEST(DeviceSessionResponse, RejectsWrongDeviceAndModeMismatch)
{
  std::string error;
  EXPECT_FALSE(
    parse_device_session_response(
      valid_response(), "another-device", kNow, nullptr, &error));

  std::string response = valid_response();
  const std::string needle = "\"channelMode\":\"storage\"";
  response.replace(response.find(needle), needle.size(), "\"channelMode\":\"p2p\"");
  EXPECT_FALSE(
    parse_device_session_response(
      response, "robot-01", kNow, nullptr, &error));
}

TEST(DeviceSessionResponse, RejectsMalformedOrRegionMismatchedChannelArn)
{
  std::string error;
  std::string response = valid_response();
  const std::string region =
    "arn:aws:kinesisvideo:ap-northeast-2:123456789012:";
  response.replace(
    response.find(region), region.size(),
    "arn:aws:kinesisvideo:us-east-1:123456789012:");
  EXPECT_FALSE(
    parse_device_session_response(
      response, "robot-01", kNow, nullptr, &error));

  response = valid_response();
  const std::string partition = "arn:aws:kinesisvideo:";
  response.replace(
    response.find(partition), partition.size(),
    "arn:aws-attacker:kinesisvideo:");
  EXPECT_FALSE(
    parse_device_session_response(
      response, "robot-01", kNow, nullptr, &error));
}

TEST(DeviceSessionResponse, RejectsExpiredOrMalformedTimestamp)
{
  std::string response = valid_response();
  const std::string expiration = "2026-07-26T11:25:00.000Z";
  response.replace(
    response.rfind(expiration), expiration.size(),
    "2026-07-26T11:09:59.999Z");
  std::string error;
  EXPECT_FALSE(
    parse_device_session_response(
      response, "robot-01", kNow, nullptr, &error));

  response = valid_response();
  response.replace(
    response.rfind(expiration), expiration.size(),
    "2026-02-30T11:25:00.000Z");
  EXPECT_FALSE(
    parse_device_session_response(
      response, "robot-01", kNow, nullptr, &error));
}

TEST(DeviceSessionResponse, CloseIsIdempotentWhenNothingWasActive)
{
  bool ended = true;
  std::string error;
  ASSERT_TRUE(
    parse_device_session_close_response(
      R"({"ended":false})", &ended, &error)) << error;
  EXPECT_FALSE(ended);
  EXPECT_FALSE(
    parse_device_session_close_response(
      R"({"ended":"false"})", nullptr, &error));
  EXPECT_FALSE(
    parse_device_session_close_response(
      R"({"ended":false,"extra":true})", nullptr, &error));
}

TEST(DeviceSessionResponse, CloseTargetsOnlyTheCurrentUuidV4)
{
  EXPECT_TRUE(is_valid_session_id(kSessionId));
  EXPECT_FALSE(is_valid_session_id("session-01"));
  EXPECT_FALSE(
    is_valid_session_id(
      "123e4567-e89b-12d3-a456-426614174000"));
  EXPECT_FALSE(
    is_valid_session_id(
      "123e4567-e89b-42d3-c456-426614174000"));
  EXPECT_EQ(
    session_close_request_json(kSessionId),
    std::string("{\"sessionId\":\"") + kSessionId + "\"}");
  EXPECT_TRUE(session_close_request_json("session-01").empty());
}

TEST(DeviceSessionResponse, CloseRetriesOnlyNetworkClassHttpFailures)
{
  const auto ended =
    classify_session_close_response(200, R"({"ended":true})");
  EXPECT_EQ(ended.disposition, SessionCloseDisposition::kTerminal);
  EXPECT_TRUE(ended.ended);

  const auto already_ended =
    classify_session_close_response(200, R"({"ended":false})");
  EXPECT_EQ(
    already_ended.disposition, SessionCloseDisposition::kTerminal);
  EXPECT_FALSE(already_ended.ended);

  EXPECT_EQ(
    classify_session_close_response(503, "{}").disposition,
    SessionCloseDisposition::kRetryableFailure);
  EXPECT_EQ(
    classify_session_close_response(400, "{}").disposition,
    SessionCloseDisposition::kPermanentFailure);
  EXPECT_EQ(
    classify_session_close_response(401, "{}").disposition,
    SessionCloseDisposition::kPermanentFailure);
  EXPECT_EQ(
    classify_session_close_response(
      200, R"({"ended":"wrong"})").disposition,
    SessionCloseDisposition::kPermanentFailure);
}

TEST(DeviceSessionSecurity, AuthorizationRejectionRequiresFailClosedTransport)
{
  EXPECT_TRUE(session_create_requires_fail_closed(401));
  EXPECT_TRUE(session_create_requires_fail_closed(403));
  EXPECT_FALSE(session_create_requires_fail_closed(0));
  EXPECT_FALSE(session_create_requires_fail_closed(400));
  EXPECT_FALSE(session_create_requires_fail_closed(429));
  EXPECT_FALSE(session_create_requires_fail_closed(500));
}
