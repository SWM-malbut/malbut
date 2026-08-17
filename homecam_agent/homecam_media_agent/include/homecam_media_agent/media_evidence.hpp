#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

namespace homecam_media_agent
{

constexpr std::uint32_t kMediaEvidenceSchemaVersion = 1U;
constexpr std::uint64_t kMaximumMediaEvidenceLifetimeNs =
  5'000'000'000ULL;

enum class EvidenceTruth : std::uint8_t
{
  kUnknown = 0U,
  kFalse = 1U,
  kTrue = 2U,
};

enum class VideoPipelineEvidence
{
  kUnknown,
  kStopped,
  kRunning,
  kFailed,
};

struct MediaEvidenceConfig
{
  std::string device_id;
  bool physical_authority{false};
  bool gstreamer_available{false};
  std::uint64_t evidence_ttl_ns{2'000'000'000ULL};
  std::uint64_t frame_timeout_ns{2'000'000'000ULL};
};

struct ActiveSessionEvidence
{
  std::string session_id;
  std::uint64_t control_plane_generation{0U};
};

struct MediaEvidenceSnapshot
{
  std::uint64_t control_plane_generation{0U};
  std::uint64_t observed_boottime_ns{0U};
  std::uint64_t valid_until_boottime_ns{0U};
  EvidenceTruth camera_available{EvidenceTruth::kUnknown};
  EvidenceTruth privacy_mode{EvidenceTruth::kUnknown};
  std::uint64_t last_valid_frame_boottime_ns{0U};
  std::uint64_t frame_generation{0U};
  std::string active_session_id;
  std::uint64_t active_session_generation{0U};
  bool backend_device_bound{false};
};

struct MediaEvidencePublication
{
  std::uint64_t sequence{0U};
  MediaEvidenceSnapshot snapshot;
  bool material_changed{false};
};

class MediaEvidencePublicationCache
{
public:
  MediaEvidencePublication observe(const MediaEvidenceSnapshot & candidate);

private:
  std::uint64_t sequence_{0U};
  std::optional<MediaEvidenceSnapshot> snapshot_;
};

class MediaEvidenceState
{
public:
  explicit MediaEvidenceState(MediaEvidenceConfig config);

  std::uint64_t issue_heartbeat_request();
  bool heartbeat_request_can_apply(std::uint64_t request_sequence) const;
  std::uint64_t apply_device_bound_heartbeat(
    std::uint64_t request_sequence,
    bool camera_enabled,
    std::uint64_t received_boottime_ns);
  void invalidate_for_session_conflict();

  std::uint64_t control_plane_generation() const;
  bool authoritative_camera_enabled() const;
  bool backend_device_bound() const;

  void set_video_pipeline_state(
    VideoPipelineEvidence state,
    std::uint64_t control_plane_generation);
  void record_valid_frame(
    std::uint64_t control_plane_generation,
    std::uint64_t received_boottime_ns);

  MediaEvidenceSnapshot snapshot(
    std::uint64_t observed_boottime_ns,
    bool applied_camera_enabled,
    bool media_io_closed,
    EvidenceTruth transport_running,
    const ActiveSessionEvidence & active_session) const;

private:
  MediaEvidenceConfig config_;
  std::uint64_t issued_heartbeat_request_{0U};
  std::uint64_t minimum_heartbeat_request_{1U};
  std::uint64_t applied_heartbeat_request_{0U};
  std::uint64_t control_plane_generation_{0U};
  std::uint64_t control_plane_received_boottime_ns_{0U};
  bool backend_device_bound_{false};
  bool authoritative_camera_enabled_{false};
  VideoPipelineEvidence video_pipeline_state_{
    VideoPipelineEvidence::kUnknown};
  std::uint64_t video_pipeline_generation_{0U};
  std::uint64_t last_valid_frame_boottime_ns_{0U};
  std::uint64_t frame_generation_{0U};
};

bool valid_evidence_image_shape(
  const std::string & encoding,
  std::uint32_t width,
  std::uint32_t height,
  std::uint32_t step,
  std::size_t data_size);
std::uint64_t next_media_evidence_sequence(std::uint64_t current);
std::uint64_t strict_boottime_ns();
std::string generate_source_instance_id();
bool is_canonical_uuid(const std::string & value);
bool same_material_evidence(
  const MediaEvidenceSnapshot & left,
  const MediaEvidenceSnapshot & right);
bool bound_physical_authority(bool configured, bool backend_device_bound);

}  // namespace homecam_media_agent
