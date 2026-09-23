#include <gtest/gtest.h>

#include <limits>
#include "homecam_media_agent/fall_settings_bridge.hpp"

using homecam_media_agent::DesiredDeviceSettings;
using homecam_media_agent::FallSettingsBridge;
using homecam_media_agent::parse_desired_settings;

namespace
{
nlohmann::json body()
{
  return {{"desiredState", {{"cameraEnabled", true}, {"microphoneEnabled", false},
    {"monitoringEnabled", false}}}, {"fallSettings", {{"settingsRevision", "1"},
    {"enabled", true}, {"cameraEnabled", true}, {"cloudConsent", true}}}};
}

DesiredDeviceSettings settings()
{
  DesiredDeviceSettings result;
  EXPECT_TRUE(parse_desired_settings(body().dump(), &result, nullptr));
  return result;
}

FallSettingsBridge::Report report()
{
  FallSettingsBridge::Report r;
  r.bridge_runtime_id = "bridge";
  r.manager_runtime_id = "manager";
  r.runtime_id = "vlm";
  r.sequence = 1;
  r.snapshot_sequence = 2;
  r.reported_at = 101;
  r.requested_revision = r.applied_revision = 1;
  r.applied = r.enabled = r.camera_enabled = r.cloud_consent = true;
  r.reason_code = "applied";
  return r;
}
}  // namespace

TEST(FallSettings, OldServerDoesNotEnableFallsOrChangeStoragePrivacyContract)
{
  auto b = body();
  b.erase("fallSettings");
  DesiredDeviceSettings desired;
  ASSERT_TRUE(parse_desired_settings(b.dump(), &desired, nullptr));
  EXPECT_EQ(desired.fall_reason, "server_settings_missing");
  EXPECT_FALSE(desired.fall);
  EXPECT_TRUE(desired.camera_enabled.value());
  EXPECT_FALSE(desired.monitoring_enabled.value());
  FallSettingsBridge bridge("bridge", "manager", "vlm", 100);
  const auto s = bridge.update(desired, "none", 101);
  EXPECT_EQ(s.check_state, "rejected");
  EXPECT_EQ(s.server_checked_at, -1);
  EXPECT_FALSE(bridge.report_payload(102));
}

TEST(FallSettings, ParseStringUint64WithoutJsonNumberPrecisionLoss)
{
  auto b = body();
  DesiredDeviceSettings desired;
  b["fallSettings"]["settingsRevision"] = "18446744073709551615";
  ASSERT_TRUE(parse_desired_settings(b.dump(), &desired, nullptr));
  ASSERT_TRUE(desired.fall);
  EXPECT_EQ(desired.fall->revision, std::numeric_limits<uint64_t>::max());
  for (const nlohmann::json & invalid : {nlohmann::json(1), nlohmann::json(true),
    nlohmann::json("0"), nlohmann::json("01"), nlohmann::json("-1"), nlohmann::json("1.0"),
    nlohmann::json("18446744073709551616"), nlohmann::json("")})
  {
    b["fallSettings"]["settingsRevision"] = invalid;
    ASSERT_TRUE(parse_desired_settings(b.dump(), &desired, nullptr));
    EXPECT_FALSE(desired.fall);
    EXPECT_EQ(desired.fall_reason, "server_invalid_settings");
  }
}

TEST(FallSettings, ValidateBooleansMissingExtraAndMismatchedCamera)
{
  DesiredDeviceSettings desired;
  for (const auto key : {"enabled", "cameraEnabled", "cloudConsent"}) {
    auto b = body();
    b["fallSettings"][key] = 1;
    ASSERT_TRUE(parse_desired_settings(b.dump(), &desired, nullptr));
    EXPECT_FALSE(desired.fall);
    b["fallSettings"].erase(key);
    ASSERT_TRUE(parse_desired_settings(b.dump(), &desired, nullptr));
    EXPECT_FALSE(desired.fall);
  }
  auto b = body();
  b["fallSettings"]["cameraEnabled"] = false;
  ASSERT_TRUE(parse_desired_settings(b.dump(), &desired, nullptr));
  EXPECT_FALSE(desired.fall);
  b = body();
  b["fallSettings"]["extra"] = true;
  ASSERT_TRUE(parse_desired_settings(b.dump(), &desired, nullptr));
  EXPECT_FALSE(desired.fall);
}

TEST(FallSettings, TransportFailureDoesNotRefreshProofAndRejectionClearsIt)
{
  FallSettingsBridge bridge("bridge", "manager", "vlm", 100);
  EXPECT_EQ(bridge.snapshot().check_state, "waiting");
  EXPECT_EQ(bridge.snapshot().settings_revision, 0U);
  auto desired = settings();
  EXPECT_EQ(bridge.update(desired, "none", 101).server_checked_at, 101);
  const auto failed = bridge.update({}, "server_timeout", 111);
  EXPECT_EQ(failed.server_checked_at, 101);
  EXPECT_EQ(failed.observed_at, 111);
  EXPECT_EQ(failed.settings_revision, 1U);
  EXPECT_TRUE(failed.enabled);
  EXPECT_EQ(failed.check_state, "unavailable");
  EXPECT_EQ(bridge.update({}, "server_auth_failed", 112).server_checked_at, -1);
  EXPECT_EQ(bridge.update({}, "server_transport_error", 113).server_checked_at, -1);
  EXPECT_EQ(bridge.update(desired, "none", 114).server_checked_at, 114);
}

TEST(FallSettings, SameRevisionConflictAndOlderSettingsAreRejected)
{
  FallSettingsBridge bridge("bridge", "manager", "vlm", 100);
  auto desired = settings();
  desired.fall->revision = 2;
  bridge.update(desired, "none", 101);
  desired.fall->enabled = false;
  EXPECT_EQ(bridge.update(desired, "none", 102).check_state, "rejected");
  EXPECT_TRUE(bridge.snapshot().enabled);
  desired.fall->revision = 1;
  EXPECT_EQ(bridge.update(desired, "none", 103).check_state, "rejected");
  EXPECT_EQ(bridge.snapshot().settings_revision, 2U);
}

TEST(FallSettings, ReportsMatchRealSnapshotAndKeepAgeAcrossHttpRetries)
{
  FallSettingsBridge bridge("bridge", "manager", "vlm", 100);
  bridge.update(settings(), "none", 100);
  ASSERT_TRUE(bridge.accept_report(report(), 102));
  EXPECT_FALSE(bridge.accept_report(report(), 103));
  auto payload = bridge.report_payload(104).value();
  EXPECT_EQ(payload["sequence"], "1");
  EXPECT_EQ(payload["snapshotSequence"], "2");
  EXPECT_EQ(payload["requestedRevision"], "1");
  EXPECT_EQ(payload["appliedRevision"], "1");
  EXPECT_EQ(payload["reportAgeS"], 3);
  EXPECT_EQ(bridge.report_payload(108)->at("reportAgeS"), 7);
  bridge.update({}, "server_settings_missing", 109);
  EXPECT_FALSE(bridge.report_payload(110));
}

TEST(FallSettings, RejectWrongPeerWrongSnapshotWrongFlagsAndFutureReport)
{
  FallSettingsBridge bridge("bridge", "manager", "vlm", 100);
  bridge.update(settings(), "none", 100);
  auto r = report();
  r.runtime_id = "old";
  EXPECT_FALSE(bridge.accept_report(r, 102));
  r = report(); r.manager_runtime_id = "old";
  EXPECT_FALSE(bridge.accept_report(r, 102));
  r = report(); r.snapshot_sequence = 99;
  EXPECT_FALSE(bridge.accept_report(r, 102));
  r = report(); r.requested_revision = 2;
  EXPECT_FALSE(bridge.accept_report(r, 102));
  r = report(); r.enabled = false;
  EXPECT_FALSE(bridge.accept_report(r, 102));
  r = report(); r.applied = false;
  EXPECT_FALSE(bridge.accept_report(r, 102));
  EXPECT_FALSE(bridge.accept_report(report(), 100));
}

TEST(FallSettings, NoReportAddedUntilActualServiceReplyArrives)
{
  FallSettingsBridge bridge("bridge", "manager", "vlm", 100);
  homecam_media_agent::HeartbeatStatus status;
  bridge.update(settings(), "none", 100);
  status.fall_settings_report = bridge.report_payload(101);
  EXPECT_FALSE(nlohmann::json::parse(homecam_media_agent::heartbeat_to_json(status))
    .contains("fallSettingsReport"));
  ASSERT_TRUE(bridge.accept_report(report(), 101));
  status.fall_settings_report = bridge.report_payload(102);
  const auto payload = nlohmann::json::parse(homecam_media_agent::heartbeat_to_json(status));
  EXPECT_TRUE(payload["fallSettingsReport"]["applied"].get<bool>());
}
