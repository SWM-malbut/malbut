#include <gtest/gtest.h>

#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>

#include "homecam_media_agent/media_evidence.hpp"

namespace
{

using homecam_media_agent::ActiveSessionEvidence;
using homecam_media_agent::EvidenceTruth;
using homecam_media_agent::MediaEvidenceConfig;
using homecam_media_agent::MediaEvidencePublicationCache;
using homecam_media_agent::MediaEvidenceSnapshot;
using homecam_media_agent::MediaEvidenceState;
using homecam_media_agent::VideoPipelineEvidence;
using homecam_media_agent::bound_physical_authority;
using homecam_media_agent::generate_source_instance_id;
using homecam_media_agent::is_canonical_uuid;
using homecam_media_agent::next_media_evidence_sequence;
using homecam_media_agent::same_material_evidence;
using homecam_media_agent::strict_boottime_ns;
using homecam_media_agent::valid_evidence_image_shape;

constexpr char kSessionId[] = "123e4567-e89b-42d3-a456-426614174000";

MediaEvidenceConfig config(
  const bool physical_authority = false,
  const bool gstreamer_available = true)
{
  MediaEvidenceConfig result;
  result.device_id = "jetson-homecam";
  result.physical_authority = physical_authority;
  result.gstreamer_available = gstreamer_available;
  result.evidence_ttl_ns = 100U;
  result.frame_timeout_ns = 200U;
  return result;
}

std::uint64_t bind_camera(
  MediaEvidenceState * const state,
  const bool enabled,
  const std::uint64_t receipt = 100U)
{
  const std::uint64_t request = state->issue_heartbeat_request();
  return state->apply_device_bound_heartbeat(request, enabled, receipt);
}

TEST(MediaEvidence, InitialStateIsUnboundAndUnknown)
{
  MediaEvidenceState state(config());
  const auto snapshot = state.snapshot(
    10U, true, false, EvidenceTruth::kUnknown, {});
  EXPECT_EQ(snapshot.control_plane_generation, 0U);
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kUnknown);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kUnknown);
  EXPECT_EQ(snapshot.frame_generation, 0U);
  EXPECT_TRUE(snapshot.active_session_id.empty());
  EXPECT_FALSE(snapshot.backend_device_bound);
  EXPECT_FALSE(bound_physical_authority(true, false));
  EXPECT_FALSE(bound_physical_authority(false, true));
  EXPECT_TRUE(bound_physical_authority(true, true));
}

TEST(MediaEvidence, FreshBoundFrameProvesCameraAndSoftwareGateOpen)
{
  MediaEvidenceState state(config(true));
  const std::uint64_t generation = bind_camera(&state, true);
  state.set_video_pipeline_state(
    VideoPipelineEvidence::kRunning, generation);
  state.record_valid_frame(generation, 120U);

  const auto snapshot = state.snapshot(
    130U, true, false, EvidenceTruth::kTrue,
    ActiveSessionEvidence{kSessionId, generation});
  EXPECT_EQ(snapshot.control_plane_generation, 1U);
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kTrue);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kFalse);
  EXPECT_EQ(snapshot.last_valid_frame_boottime_ns, 120U);
  EXPECT_EQ(snapshot.frame_generation, generation);
  EXPECT_EQ(snapshot.active_session_id, kSessionId);
  EXPECT_EQ(snapshot.active_session_generation, generation);
  EXPECT_TRUE(snapshot.backend_device_bound);
  EXPECT_EQ(snapshot.valid_until_boottime_ns, 200U);
}

TEST(MediaEvidence, ExpiredControlReturnsCanonicalUnboundShape)
{
  MediaEvidenceState state(config(true));
  const std::uint64_t generation = bind_camera(&state, true);
  state.set_video_pipeline_state(
    VideoPipelineEvidence::kRunning, generation);
  state.record_valid_frame(generation, 120U);

  const auto snapshot = state.snapshot(
    200U, true, false, EvidenceTruth::kTrue, {});
  EXPECT_EQ(snapshot.control_plane_generation, 0U);
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kUnknown);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kUnknown);
  EXPECT_EQ(snapshot.last_valid_frame_boottime_ns, 0U);
  EXPECT_EQ(snapshot.frame_generation, 0U);
  EXPECT_FALSE(snapshot.backend_device_bound);
}

TEST(MediaEvidence, CameraOffNeedsAppliedPipelineAndTransportForPrivacy)
{
  MediaEvidenceState state(config(true));
  const std::uint64_t generation = bind_camera(&state, false);
  state.set_video_pipeline_state(
    VideoPipelineEvidence::kStopped, generation);

  auto snapshot = state.snapshot(
    110U, false, true, EvidenceTruth::kUnknown, {});
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kFalse);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kUnknown);

  snapshot = state.snapshot(
    110U, false, true, EvidenceTruth::kFalse, {});
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kFalse);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kTrue);
  EXPECT_TRUE(snapshot.active_session_id.empty());
}

TEST(MediaEvidence, MissingGstreamerCanNeverClaimCameraTrue)
{
  MediaEvidenceState state(config(false, false));
  bind_camera(&state, true);
  const auto snapshot = state.snapshot(
    110U, true, false, EvidenceTruth::kFalse, {});
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kFalse);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kFalse);
}

TEST(MediaEvidence, AppliedMismatchAndPipelineFailureFailClosed)
{
  MediaEvidenceState state(config());
  const std::uint64_t generation = bind_camera(&state, true);
  auto snapshot = state.snapshot(
    110U, false, true, EvidenceTruth::kFalse, {});
  EXPECT_TRUE(snapshot.backend_device_bound);
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kUnknown);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kUnknown);

  state.set_video_pipeline_state(
    VideoPipelineEvidence::kFailed, generation);
  snapshot = state.snapshot(
    110U, true, false, EvidenceTruth::kFalse, {});
  EXPECT_EQ(snapshot.camera_available, EvidenceTruth::kFalse);
  EXPECT_EQ(snapshot.privacy_mode, EvidenceTruth::kFalse);
}

TEST(MediaEvidence, SessionResponseFencesEveryEarlierHeartbeat)
{
  MediaEvidenceState state(config());
  const std::uint64_t earlier = state.issue_heartbeat_request();
  state.invalidate_for_session_conflict();
  EXPECT_FALSE(state.heartbeat_request_can_apply(earlier));
  EXPECT_EQ(
    state.apply_device_bound_heartbeat(earlier, true, 100U), 0U);
  EXPECT_FALSE(state.backend_device_bound());

  const std::uint64_t newer = state.issue_heartbeat_request();
  EXPECT_TRUE(state.heartbeat_request_can_apply(newer));
  EXPECT_EQ(
    state.apply_device_bound_heartbeat(newer, true, 101U), 1U);
  EXPECT_TRUE(state.backend_device_bound());
}

TEST(MediaEvidence, LaterHeartbeatWinsAndReceiptsCannotRegress)
{
  MediaEvidenceState state(config());
  const std::uint64_t first = state.issue_heartbeat_request();
  const std::uint64_t second = state.issue_heartbeat_request();
  EXPECT_EQ(state.apply_device_bound_heartbeat(second, true, 100U), 1U);
  EXPECT_FALSE(state.heartbeat_request_can_apply(first));
  EXPECT_EQ(state.apply_device_bound_heartbeat(first, false, 101U), 1U);

  const std::uint64_t third = state.issue_heartbeat_request();
  EXPECT_THROW(
    state.apply_device_bound_heartbeat(third, true, 99U),
    std::invalid_argument);
}

TEST(MediaEvidence, FrameMustBelongToCurrentRunningGeneration)
{
  MediaEvidenceState state(config());
  const std::uint64_t generation = bind_camera(&state, true);
  EXPECT_THROW(
    state.record_valid_frame(generation, 110U), std::invalid_argument);
  state.set_video_pipeline_state(
    VideoPipelineEvidence::kRunning, generation);
  EXPECT_THROW(
    state.record_valid_frame(generation + 1U, 110U),
    std::invalid_argument);
  state.record_valid_frame(generation, 110U);
  EXPECT_THROW(
    state.record_valid_frame(generation, 109U), std::invalid_argument);
}

TEST(MediaEvidence, StrictFrameShapeRejectsPaddingAndTrailingBytes)
{
  EXPECT_TRUE(valid_evidence_image_shape("rgb8", 2U, 2U, 6U, 12U));
  EXPECT_TRUE(valid_evidence_image_shape("bgra8", 2U, 2U, 8U, 16U));
  EXPECT_FALSE(valid_evidence_image_shape("mono8", 2U, 2U, 2U, 4U));
  EXPECT_FALSE(valid_evidence_image_shape("rgb8", 2U, 2U, 5U, 10U));
  EXPECT_FALSE(valid_evidence_image_shape("rgb8", 2U, 2U, 6U, 13U));
  EXPECT_FALSE(valid_evidence_image_shape("rgb8", 0U, 2U, 6U, 12U));
}

TEST(MediaEvidence, ReadTimeDoesNotChangeMaterialEvidence)
{
  MediaEvidenceState state(config());
  const std::uint64_t generation = bind_camera(&state, true);
  state.set_video_pipeline_state(
    VideoPipelineEvidence::kRunning, generation);
  state.record_valid_frame(generation, 120U);
  const auto first = state.snapshot(
    125U, true, false, EvidenceTruth::kFalse, {});
  const auto second = state.snapshot(
    130U, true, false, EvidenceTruth::kFalse, {});
  EXPECT_TRUE(same_material_evidence(first, second));
  EXPECT_NE(first.observed_boottime_ns, second.observed_boottime_ns);
  // Validity remains bounded by the unchanged control-plane receipt.
  EXPECT_EQ(first.valid_until_boottime_ns, second.valid_until_boottime_ns);
}

TEST(MediaEvidence, MaterialEventChangesEvidence)
{
  MediaEvidenceState state(config());
  const std::uint64_t generation = bind_camera(&state, true);
  state.set_video_pipeline_state(
    VideoPipelineEvidence::kRunning, generation);
  auto first = state.snapshot(
    110U, true, false, EvidenceTruth::kFalse, {});
  state.record_valid_frame(generation, 111U);
  auto second = state.snapshot(
    112U, true, false, EvidenceTruth::kFalse, {});
  EXPECT_FALSE(same_material_evidence(first, second));
}

TEST(MediaEvidence, PublicationCacheReplaysExactBodyWithoutExtendingTtl)
{
  MediaEvidencePublicationCache cache;
  MediaEvidenceSnapshot first;
  first.observed_boottime_ns = 100U;
  first.valid_until_boottime_ns = 200U;
  const auto published = cache.observe(first);
  EXPECT_TRUE(published.material_changed);
  EXPECT_EQ(published.sequence, 1U);

  MediaEvidenceSnapshot same_material = first;
  same_material.observed_boottime_ns = 150U;
  same_material.valid_until_boottime_ns = 250U;
  const auto replay = cache.observe(same_material);
  EXPECT_FALSE(replay.material_changed);
  EXPECT_EQ(replay.sequence, 1U);
  EXPECT_EQ(replay.snapshot.observed_boottime_ns, 100U);
  EXPECT_EQ(replay.snapshot.valid_until_boottime_ns, 200U);

  same_material.backend_device_bound = true;
  same_material.control_plane_generation = 1U;
  const auto changed = cache.observe(same_material);
  EXPECT_TRUE(changed.material_changed);
  EXPECT_EQ(changed.sequence, 2U);
  EXPECT_EQ(changed.snapshot.observed_boottime_ns, 150U);
}

TEST(MediaEvidence, SequenceAndValidityNeverWrapUint64)
{
  EXPECT_EQ(next_media_evidence_sequence(0U), 1U);
  EXPECT_THROW(
    next_media_evidence_sequence(std::numeric_limits<std::uint64_t>::max()),
    std::overflow_error);

  MediaEvidenceState state(config());
  EXPECT_THROW(
    state.snapshot(
      std::numeric_limits<std::uint64_t>::max(), true, false,
      EvidenceTruth::kUnknown, {}),
    std::overflow_error);
}

TEST(MediaEvidence, SessionShapeAndConfigurationAreStrict)
{
  auto invalid = config();
  invalid.device_id = "bad device";
  EXPECT_THROW(MediaEvidenceState state(invalid), std::invalid_argument);
  invalid = config();
  invalid.evidence_ttl_ns = 201U;
  invalid.frame_timeout_ns = 200U;
  EXPECT_THROW(MediaEvidenceState state(invalid), std::invalid_argument);

  MediaEvidenceState state(config());
  bind_camera(&state, true);
  EXPECT_THROW(
    state.snapshot(
      110U, true, false, EvidenceTruth::kFalse,
      ActiveSessionEvidence{"not-a-uuid", 1U}),
    std::invalid_argument);
  EXPECT_THROW(
    state.snapshot(
      110U, true, false, EvidenceTruth::kFalse,
      ActiveSessionEvidence{"", 1U}),
    std::invalid_argument);
}

TEST(MediaEvidence, ProductionClockAndInstanceIdentityAreAvailable)
{
  EXPECT_GT(strict_boottime_ns(), 0U);
  const std::string instance_id = generate_source_instance_id();
  EXPECT_TRUE(is_canonical_uuid(instance_id));
  EXPECT_EQ(instance_id[14], '4');
  EXPECT_NE(instance_id, generate_source_instance_id());
}

}  // namespace
