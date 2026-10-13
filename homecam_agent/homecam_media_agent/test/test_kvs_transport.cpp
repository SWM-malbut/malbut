#include <gtest/gtest.h>

#include <cstdint>
#include <string>

#include "homecam_media_agent/kvs_transport.hpp"
#include "homecam_media_agent/build_features.hpp"

using homecam_media_agent::AudioCaptureTimeline;
using homecam_media_agent::EncodedFrame;
using homecam_media_agent::KvsTimestampNormalizer;
using homecam_media_agent::AdaptivePlayout;
using homecam_media_agent::BufferedPlayout;
using homecam_media_agent::PlayoutSchedule;
using homecam_media_agent::SessionMode;
using homecam_media_agent::SessionRefreshDecision;
using homecam_media_agent::SessionLease;
using homecam_media_agent::SharedMediaTimeline;
using homecam_media_agent::decide_session_refresh;
using homecam_media_agent::make_kvs_transport;
using homecam_media_agent::session_lease_expired;
using homecam_media_agent::storage_session_hard_expired;
using homecam_media_agent::storage_session_refresh_due;
using homecam_media_agent::use_explicit_signaling_channel_arn;

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

TEST(SessionRefreshPolicy, RenewsConnectedP2pCredentialsBeforeExpiryWithoutReconnect)
{
  constexpr std::int64_t now = 1'000'000;
  constexpr std::int64_t routine_margin = 300'000;

  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now + routine_margin + 1,
      routine_margin),
    SessionRefreshDecision::kNotDue);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now + routine_margin,
      routine_margin),
    SessionRefreshDecision::kRefreshCredentials);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, false, now, now + routine_margin,
      routine_margin),
    SessionRefreshDecision::kRefreshNow);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now + 60'000,
      routine_margin),
    SessionRefreshDecision::kRefreshCredentials);
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kPeerToPeer, true, now, now,
      routine_margin),
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
      SessionMode::kStorage, false, now, now, 300'000),
    SessionRefreshDecision::kFailClosed);
}

TEST(SessionRefreshPolicy, RenewalCannotChangeScopeReviveExpiredLeaseOrShortenDeadline)
{
  SessionLease current;
  current.channel_arn = "arn:aws:kinesisvideo:ap-northeast-2:123456789012:channel/device/1";
  current.region = "ap-northeast-2";
  current.credentials = {"test-id", "test-secret", "test-token", 900'000};
  auto renewed = current;
  renewed.credentials.expires_at_unix_ms = 1'500'000;
  const auto allowed = [&](const SessionLease & candidate, std::int64_t now = 600'000) {
      return homecam_media_agent::can_renew_session_credentials(current, candidate, now);
    };
  EXPECT_TRUE(allowed(renewed));
  EXPECT_FALSE(allowed(current));
  EXPECT_FALSE(allowed(renewed, 900'000));
  auto wrong = renewed;
  wrong.channel_arn += "-other-device";
  EXPECT_FALSE(allowed(wrong));
  wrong = renewed;
  wrong.region = "us-east-1";
  EXPECT_FALSE(allowed(wrong));
  wrong = renewed;
  wrong.mode = SessionMode::kStorage;
  EXPECT_FALSE(allowed(wrong));
  wrong = renewed;
  wrong.credentials.session_token.clear();
  EXPECT_FALSE(allowed(wrong));
  wrong = renewed;
  wrong.credentials.expires_at_unix_ms = 800'000;
  EXPECT_FALSE(allowed(wrong));
}

TEST(SessionRefreshPolicy, StorageRenewalIsNeverDeferredForP2p)
{
  constexpr std::int64_t now = 1'000'000;
  EXPECT_EQ(
    decide_session_refresh(
      SessionMode::kStorage, true, now, now + 300'000, 300'000),
    SessionRefreshDecision::kRefreshNow);
}

TEST(SessionRefreshPolicy, StorageLifetimeUsesMonotonicSoftAndHardBoundaries)
{
  constexpr std::int64_t refresh_age = 50LL * 60LL * 1000LL;
  constexpr std::int64_t hard_age = 55LL * 60LL * 1000LL;
  EXPECT_FALSE(storage_session_refresh_due(-1, refresh_age));
  EXPECT_FALSE(storage_session_refresh_due(refresh_age - 1, refresh_age));
  EXPECT_TRUE(storage_session_refresh_due(refresh_age, refresh_age));
  EXPECT_FALSE(storage_session_hard_expired(hard_age - 1, hard_age));
  EXPECT_TRUE(storage_session_hard_expired(hard_age, hard_age));
}

TEST(KvsTransport, StorageSessionLetsSdkDiscoverMediaStorageConfiguration)
{
  EXPECT_TRUE(use_explicit_signaling_channel_arn(SessionMode::kPeerToPeer));
  EXPECT_FALSE(use_explicit_signaling_channel_arn(SessionMode::kStorage));
}

TEST(KvsTransport, FailsClosedWithoutAReviewedAdapter)
{
  auto transport = make_kvs_transport();
  std::string renewal_error;
  EXPECT_FALSE(transport->renew_credentials(SessionLease{}, &renewal_error));
  EXPECT_FALSE(renewal_error.empty());
  EXPECT_FALSE(transport->peer_connected());
  EXPECT_FALSE(transport->media_flowing());
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

TEST(KvsTransport, StampsAudioBurstsAtTheirCaptureCadence)
{
  AudioCaptureTimeline timeline;
  // Three 20 ms frames reach the appsink together; each keeps its capture time.
  EXPECT_EQ(timeline.stamp(0, 1'000'000'000), 1'000'000'000);
  EXPECT_EQ(timeline.stamp(20'000'000, 1'060'000'000), 1'020'000'000);
  EXPECT_EQ(timeline.stamp(40'000'000, 1'060'000'100), 1'040'000'000);
  EXPECT_EQ(timeline.stamp(60'000'000, 1'060'000'200), 1'060'000'000);
  EXPECT_EQ(timeline.stamp(80'000'000, 1'081'000'000), 1'080'000'000);
}

TEST(KvsTransport, ReanchorsAudioAfterARestartOrAStall)
{
  AudioCaptureTimeline timeline;
  timeline.stamp(5'000'000'000, 1'000'000'000);
  // The 말벗 마이크 switch restarts the capture pipeline at PTS zero.
  EXPECT_EQ(timeline.stamp(0, 2'000'000'000), 2'000'000'000);
  EXPECT_EQ(timeline.stamp(20'000'000, 2'030'000'000), 2'020'000'000);
  // The device stalled for a second while its clock advanced 20 ms.
  EXPECT_EQ(timeline.stamp(40'000'000, 3'040'000'000), 3'040'000'000);
  // Without a capture clock the arrival time is used, still increasing.
  EXPECT_EQ(timeline.stamp(-1, 3'000'000'000), 3'040'000'001);
}

TEST(KvsTransport, PlaysGuardianFramesOnTheirTimelineAfterADelay)
{
  PlayoutSchedule schedule(120'000'000);
  // The first frame plays 120 ms later; a burst of three keeps 20 ms spacing.
  EXPECT_EQ(schedule.schedule(5'000'000'000, 0), 120'000'000);
  EXPECT_EQ(schedule.schedule(5'020'000'000, 60'000'000), 140'000'000);
  EXPECT_EQ(schedule.schedule(5'040'000'000, 60'000'100), 160'000'000);
  EXPECT_EQ(schedule.schedule(5'060'000'000, 60'000'200), 180'000'000);
  // A frame arriving after its slot starts a new anchor rather than being dropped.
  EXPECT_EQ(schedule.schedule(5'080'000'000, 250'000'000), 370'000'000);
  // A sender jump far ahead, then a new talk on an unrelated timeline, start over.
  EXPECT_EQ(schedule.schedule(15'000'000'000, 300'000'000), 420'000'000);
  EXPECT_EQ(schedule.schedule(100, 400'000'000), 520'000'000);
  schedule.reset();
  EXPECT_EQ(schedule.schedule(200, 0), 120'000'000);
}

TEST(KvsTransport, BufferedPlayoutDropsALateFrameInsteadOfMovingTheTimeline)
{
  // 2026-10-13: on PlayoutSchedule one late frame moved everything 120 ms on
  // (the gap above, 180 ms -> 370 ms); here it costs only its own 20 ms.
  BufferedPlayout playout(300'000'000);
  auto plan = playout.schedule(5'000'000'000, 0);
  EXPECT_EQ(plan.playout_ns, 300'000'000);
  EXPECT_FALSE(plan.reanchored);
  EXPECT_EQ(playout.schedule(5'020'000'000, 100'000'000).playout_ns, 320'000'000);
  plan = playout.schedule(5'040'000'000, 350'000'000);  // Due at 340 ms: 10 ms late.
  EXPECT_TRUE(plan.dropped);
  EXPECT_EQ(plan.late_ns, 10'000'000);
  // The next frame, on time again, keeps its slot on the same timeline.
  plan = playout.schedule(5'060'000'000, 355'000'000);
  EXPECT_FALSE(plan.dropped);
  EXPECT_EQ(plan.playout_ns, 360'000'000);
}

TEST(KvsTransport, BufferedPlayoutMovesOnlyForARunOfLateFramesAJumpOrANewDelay)
{
  BufferedPlayout playout(300'000'000, 3);
  playout.schedule(0, 0);  // Anchored: frame 0 at 300 ms.
  // The network got slower: three frames in a row late, then a new anchor.
  EXPECT_TRUE(playout.schedule(20'000'000, 400'000'000).dropped);
  EXPECT_TRUE(playout.schedule(40'000'000, 420'000'000).dropped);
  auto plan = playout.schedule(60'000'000, 440'000'000);
  EXPECT_TRUE(plan.reanchored);
  EXPECT_EQ(plan.playout_ns, 740'000'000);
  // A jump far ahead (a pause in talking) and a new stream also start over.
  EXPECT_TRUE(playout.schedule(10'000'000'000, 760'000'000).reanchored);
  EXPECT_TRUE(playout.schedule(100, 800'000'000).reanchored);
  // A new delay applies at once, on a new anchor.
  playout.set_delay(500'000'000);
  plan = playout.schedule(120, 900'000'000);
  EXPECT_TRUE(plan.reanchored);
  EXPECT_EQ(plan.playout_ns, 1'400'000'000);
  playout.reset();
  plan = playout.schedule(5, 0);
  EXPECT_FALSE(plan.reanchored);
  EXPECT_EQ(plan.playout_ns, 500'000'000);
}

namespace
{
constexpr std::int64_t kMs = 1'000'000;
}  // namespace

TEST(KvsTransport, AdaptivePlayoutStartsSafeThenFollowsTheMeasuredJitter)
{
  // 2026-10-13: a jitter buffer like voice apps use; the delay follows the jitter.
  AdaptivePlayout playout;
  auto plan = playout.schedule(0, 40 * kMs, 80);
  EXPECT_EQ(plan.playout_ns, 190 * kMs);  // Arrival + 150 ms before anything is known.
  EXPECT_FALSE(plan.reanchored);
  // Two seconds of frames arriving 0 or 40 ms late: the 95th percentile is 40 ms.
  for (int i = 1; i <= 100; ++i) {
    plan = playout.schedule(i * 20 * kMs, i * 20 * kMs + 40 * kMs + (i % 2) * 40 * kMs, 80);
  }
  EXPECT_EQ(plan.target_ns, 70 * kMs);  // 40 ms of jitter plus a 30 ms margin.
  EXPECT_FALSE(plan.dropped);
}

TEST(KvsTransport, AdaptivePlayoutShortensTheDelayOnQuietFramesOnly)
{
  AdaptivePlayout playout;
  std::int64_t delay = 0;
  int skipped = 0;
  for (int i = 0; i < 200; ++i) {
    // Steady arrival; every third frame is quiet (fewer bytes).
    const auto plan = playout.schedule(i * 20 * kMs, i * 20 * kMs + 40 * kMs, i % 3 ? 90 : 60);
    if (plan.skipped) {
      ++skipped;
      EXPECT_EQ(i % 3, 0) << "only a quiet frame is skipped";
    }
    delay = plan.delay_ns;
  }
  EXPECT_EQ(skipped, 4);
  EXPECT_EQ(delay, 70 * kMs);  // 150 ms down to within 10 ms of the 60 ms floor, 20 ms at a time.
}

TEST(KvsTransport, AdaptivePlayoutDropsOneLateFrameAndMovesOnlyWhenLateAgain)
{
  AdaptivePlayout playout;
  playout.schedule(0, 40 * kMs, 80);  // Plays at 190 ms; the timeline is frame + 190 ms.
  auto plan = playout.schedule(20 * kMs, 260 * kMs, 80);  // Due at 210 ms.
  EXPECT_TRUE(plan.dropped);
  EXPECT_FALSE(plan.reanchored);
  plan = playout.schedule(40 * kMs, 265 * kMs, 80);  // Due at 230 ms: late again.
  EXPECT_TRUE(plan.reanchored);
  EXPECT_GT(plan.playout_ns, 265 * kMs);
}

TEST(KvsTransport, AdaptivePlayoutReportsLostFramesStillAheadForConcealment)
{
  AdaptivePlayout playout;
  playout.schedule(0, 40 * kMs, 80);   // Plays 190-210 ms.
  playout.schedule(20 * kMs, 60 * kMs, 80);  // Plays 210-230 ms.
  // Frames 40 and 60 ms were lost; 80 ms arrives in time for its slot (270 ms).
  auto plan = playout.schedule(80 * kMs, 100 * kMs, 80);
  EXPECT_EQ(plan.playout_ns, 270 * kMs);
  EXPECT_EQ(plan.gap_start_ns, 230 * kMs);  // Conceal 230-270 ms before it.
  // A long pause is silence, not loss: no concealment, the delay settles.
  plan = playout.schedule(2'000 * kMs, 2'020 * kMs, 80);
  EXPECT_EQ(plan.gap_start_ns, -1);
}

TEST(KvsTransport, AdaptivePlayoutNeverDelaysPastItsMaximum)
{
  AdaptivePlayout playout(60 * kMs, 600 * kMs);
  playout.schedule(0, 40 * kMs, 80);
  // The network stalls for 2 s: frames come late in a burst.
  playout.schedule(20 * kMs, 2'100 * kMs, 80);
  const auto plan = playout.schedule(40 * kMs, 2'100 * kMs, 80);
  EXPECT_TRUE(plan.reanchored);
  EXPECT_LE(plan.delay_ns, 600 * kMs);
  // A new stream on an unrelated timeline starts over.
  EXPECT_TRUE(playout.schedule(5, 3'000 * kMs, 80).reanchored);
}

