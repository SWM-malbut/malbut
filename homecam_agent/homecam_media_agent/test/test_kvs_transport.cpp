#include <gtest/gtest.h>

#include <cstdint>
#include <string>

#include "homecam_media_agent/kvs_transport.hpp"
#include "homecam_media_agent/build_features.hpp"

using homecam_media_agent::EncodedFrame;
using homecam_media_agent::KvsTimestampNormalizer;
using homecam_media_agent::SessionMode;
using homecam_media_agent::SessionRefreshDecision;
using homecam_media_agent::SessionLease;
using homecam_media_agent::SharedMediaTimeline;
using homecam_media_agent::decide_session_refresh;
using homecam_media_agent::make_kvs_transport;
using homecam_media_agent::session_lease_expired;

TEST(SessionLease, RefreshesBeforeShortLivedCredentialsExpire)
{
  SessionLease lease;
  lease.channel_arn = "arn:aws:kinesisvideo:region:account:channel/device/1";
  lease.region = "ap-northeast-2";
  lease.credentials.access_key_id = "short-lived-id";
  lease.credentials.secret_access_key = "short-lived-secret";
  lease.credentials.session_token = "short-lived-token";
  lease.credentials.expires_at_unix_ms = 1'000'000;
  lease.refresh_margin_ms = 300'000;

  EXPECT_TRUE(lease.valid_at(100'000));
  EXPECT_FALSE(lease.refresh_due(600'000));
  EXPECT_TRUE(lease.refresh_due(700'000));
  EXPECT_FALSE(lease.valid_at(1'000'000));
}

TEST(SessionRefreshPolicy, PreservesConnectedP2pUntilSafetyBoundary)
{
  constexpr std::int64_t now = 1'000'000;
  constexpr std::int64_t routine_margin = 300'000;
  constexpr std::int64_t safety_margin = 60'000;

  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now + routine_margin + 1,
      routine_margin, safety_margin),
    SessionRefreshDecision::kNotDue);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now + routine_margin,
      routine_margin, safety_margin),
    SessionRefreshDecision::kDeferForConnectedPeer);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, false, now, now + routine_margin,
      routine_margin, safety_margin),
    SessionRefreshDecision::kRefreshNow);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now + safety_margin,
      routine_margin, safety_margin),
    SessionRefreshDecision::kForceBeforeExpiry);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now,
      routine_margin, safety_margin),
    SessionRefreshDecision::kFailClosed);
}

TEST(SessionRefreshPolicy, FailsClosedWhenLeaseReachesExpiry)
{
  constexpr std::int64_t now = 1'000'000;

  EXPECT_FALSE(session_lease_expired(now, 0));
  EXPECT_FALSE(session_lease_expired(now, now + 1));
  EXPECT_TRUE(session_lease_expired(now, now));
  EXPECT_TRUE(session_lease_expired(now, now - 1));
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kStorage, false, now, now, 300'000, 60'000),
    SessionRefreshDecision::kFailClosed);
}

TEST(SessionRefreshPolicy, StorageRenewalIsNeverDeferredForP2p)
{
  constexpr std::int64_t now = 1'000'000;
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kStorage, true, now, now + 300'000, 300'000, 60'000),
    SessionRefreshDecision::kRefreshNow);
}

TEST(KvsTransport, FailsClosedWithoutAReviewedAdapter)
{
  auto transport = make_kvs_transport();
  EXPECT_FALSE(transport->peer_connected());
  EXPECT_FALSE(transport->restart_required());
#if HOMECAM_HAVE_KVS
  EXPECT_TRUE(transport->implemented());
  std::string error;
  EXPECT_FALSE(transport->start(SessionLease{}, &error));
  EXPECT_NE(error.find("invalid or expired"), std::string::npos);
#else
  EXPECT_FALSE(transport->implemented());
  std::string error;
  EXPECT_FALSE(transport->start(SessionLease{}, &error));
  EXPECT_NE(error.find("disabled at build time"), std::string::npos);
  error.clear();
  EXPECT_FALSE(transport->push_h264(EncodedFrame{}, &error));
  EXPECT_FALSE(error.empty());
#endif
}

TEST(KvsTransport, ConvertsNanosecondsToSdkHundredNanoseconds)
{
  EXPECT_EQ(homecam_media_agent::kvs_timestamp_from_nanoseconds(-1), 0U);
  EXPECT_EQ(homecam_media_agent::kvs_timestamp_from_nanoseconds(99), 0U);
  EXPECT_EQ(homecam_media_agent::kvs_timestamp_from_nanoseconds(100), 1U);
  EXPECT_EQ(
    homecam_media_agent::kvs_timestamp_from_nanoseconds(20'000'000),
    200'000U);
}

TEST(KvsTransport, RebasesTimestampAfterPipelineRestart)
{
  KvsTimestampNormalizer audio(20'000'000);
  EXPECT_EQ(audio.normalize(0), 0U);
  EXPECT_EQ(audio.normalize(20'000'000), 200'000U);
  EXPECT_EQ(audio.normalize(40'000'000), 400'000U);
  // A restarted GStreamer pipeline returns to PTS zero. Output must continue
  // at the learned 20 ms cadence, rather than advancing by only 100 ns.
  EXPECT_EQ(audio.normalize(0), 600'000U);
  EXPECT_EQ(audio.normalize(20'000'000), 800'000U);
}

TEST(KvsTransport, AlignsIndependentPipelinesOnOneSteadyTimeline)
{
  SharedMediaTimeline timeline;
  const auto audio = timeline.stamp(8'000'000'000, 1'000'000'000);
  const auto video = timeline.stamp(0, 1'010'000'000);
  // The audio pipeline restarts to PTS zero, but the shared capture timeline
  // remains aligned and monotonic across both tracks.
  const auto restarted_audio = timeline.stamp(0, 1'020'000'000);
  EXPECT_EQ(audio, 1'000'000'000);
  EXPECT_EQ(video, 1'010'000'000);
  EXPECT_EQ(restarted_audio, 1'020'000'000);
  EXPECT_LT(video, restarted_audio);
}
