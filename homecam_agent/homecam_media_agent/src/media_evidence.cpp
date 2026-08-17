#include "homecam_media_agent/media_evidence.hpp"

#include <sys/random.h>
#include <time.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <iomanip>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <utility>

#include "homecam_media_agent/config.hpp"

namespace homecam_media_agent
{

namespace
{

constexpr std::uint64_t kMaximumU64 =
  std::numeric_limits<std::uint64_t>::max();

std::uint64_t checked_add(
  const std::uint64_t left,
  const std::uint64_t right,
  const char * const label)
{
  if (right > kMaximumU64 - left) {
    throw std::overflow_error(label);
  }
  return left + right;
}

bool lower_hex(const char value)
{
  return (value >= '0' && value <= '9') ||
         (value >= 'a' && value <= 'f');
}

bool canonical_uuid_shape(const std::string & value)
{
  if (value.size() != 36U || value[8] != '-' || value[13] != '-' ||
    value[18] != '-' || value[23] != '-')
  {
    return false;
  }
  for (std::size_t index = 0; index < value.size(); ++index) {
    if (index == 8U || index == 13U || index == 18U || index == 23U) {
      continue;
    }
    if (!lower_hex(value[index])) {
      return false;
    }
  }
  return true;
}

bool canonical_uuid_v4(const std::string & value)
{
  return canonical_uuid_shape(value) && value[14] == '4' &&
         (value[19] == '8' || value[19] == '9' ||
         value[19] == 'a' || value[19] == 'b');
}

std::size_t bytes_per_pixel(const std::string & encoding)
{
  if (encoding == "rgb8" || encoding == "bgr8") {
    return 3U;
  }
  if (encoding == "rgba8" || encoding == "bgra8") {
    return 4U;
  }
  return 0U;
}

void validate_receipt(
  const std::uint64_t value,
  const char * const label)
{
  if (value == 0U) {
    throw std::invalid_argument(label);
  }
}

}  // namespace

MediaEvidenceState::MediaEvidenceState(MediaEvidenceConfig config)
: config_(std::move(config))
{
  if (!is_valid_device_id(config_.device_id)) {
    throw std::invalid_argument("media evidence device_id is invalid");
  }
  if (config_.evidence_ttl_ns == 0U ||
    config_.evidence_ttl_ns > kMaximumMediaEvidenceLifetimeNs ||
    config_.frame_timeout_ns == 0U ||
    config_.evidence_ttl_ns > config_.frame_timeout_ns)
  {
    throw std::invalid_argument("media evidence freshness is invalid");
  }
}

std::uint64_t MediaEvidenceState::issue_heartbeat_request()
{
  issued_heartbeat_request_ =
    next_media_evidence_sequence(issued_heartbeat_request_);
  return issued_heartbeat_request_;
}

bool MediaEvidenceState::heartbeat_request_can_apply(
  const std::uint64_t request_sequence) const
{
  return request_sequence != 0U &&
         request_sequence <= issued_heartbeat_request_ &&
         request_sequence >= minimum_heartbeat_request_ &&
         request_sequence > applied_heartbeat_request_;
}

std::uint64_t MediaEvidenceState::apply_device_bound_heartbeat(
  const std::uint64_t request_sequence,
  const bool camera_enabled,
  const std::uint64_t received_boottime_ns)
{
  validate_receipt(
    received_boottime_ns,
    "control-plane receipt is invalid");
  if (!heartbeat_request_can_apply(request_sequence)) {
    return control_plane_generation_;
  }
  if (received_boottime_ns < control_plane_received_boottime_ns_) {
    throw std::invalid_argument("control-plane receipt regressed");
  }
  const std::uint64_t next_generation =
    next_media_evidence_sequence(control_plane_generation_);
  applied_heartbeat_request_ = request_sequence;
  control_plane_generation_ = next_generation;
  control_plane_received_boottime_ns_ = received_boottime_ns;
  backend_device_bound_ = true;
  authoritative_camera_enabled_ = camera_enabled;
  video_pipeline_state_ = VideoPipelineEvidence::kUnknown;
  video_pipeline_generation_ = 0U;
  last_valid_frame_boottime_ns_ = 0U;
  frame_generation_ = 0U;
  return control_plane_generation_;
}

void MediaEvidenceState::invalidate_for_session_conflict()
{
  backend_device_bound_ = false;
  video_pipeline_state_ = VideoPipelineEvidence::kUnknown;
  video_pipeline_generation_ = 0U;
  last_valid_frame_boottime_ns_ = 0U;
  frame_generation_ = 0U;
  if (issued_heartbeat_request_ == kMaximumU64) {
    applied_heartbeat_request_ = kMaximumU64;
    minimum_heartbeat_request_ = kMaximumU64;
    throw std::overflow_error("media evidence sequence exhausted");
  }
  minimum_heartbeat_request_ = issued_heartbeat_request_ + 1U;
}

std::uint64_t MediaEvidenceState::control_plane_generation() const
{
  return control_plane_generation_;
}

bool MediaEvidenceState::authoritative_camera_enabled() const
{
  return authoritative_camera_enabled_;
}

bool MediaEvidenceState::backend_device_bound() const
{
  return backend_device_bound_;
}

void MediaEvidenceState::set_video_pipeline_state(
  const VideoPipelineEvidence state,
  const std::uint64_t control_plane_generation)
{
  if (control_plane_generation > control_plane_generation_) {
    throw std::invalid_argument("pipeline generation is invalid");
  }
  video_pipeline_state_ = state;
  video_pipeline_generation_ = control_plane_generation;
  if (state != VideoPipelineEvidence::kRunning) {
    last_valid_frame_boottime_ns_ = 0U;
    frame_generation_ = 0U;
  }
}

void MediaEvidenceState::record_valid_frame(
  const std::uint64_t control_plane_generation,
  const std::uint64_t received_boottime_ns)
{
  validate_receipt(received_boottime_ns, "frame receipt is invalid");
  if (!backend_device_bound_ ||
    control_plane_generation == 0U ||
    control_plane_generation != control_plane_generation_ ||
    video_pipeline_state_ != VideoPipelineEvidence::kRunning ||
    video_pipeline_generation_ != control_plane_generation ||
    received_boottime_ns < control_plane_received_boottime_ns_ ||
    received_boottime_ns < last_valid_frame_boottime_ns_)
  {
    throw std::invalid_argument("frame evidence is not current");
  }
  last_valid_frame_boottime_ns_ = received_boottime_ns;
  frame_generation_ = control_plane_generation;
}

MediaEvidenceSnapshot MediaEvidenceState::snapshot(
  const std::uint64_t observed_boottime_ns,
  const bool applied_camera_enabled,
  const bool media_io_closed,
  const EvidenceTruth transport_running,
  const ActiveSessionEvidence & active_session) const
{
  validate_receipt(observed_boottime_ns, "evidence observation is invalid");
  if (transport_running != EvidenceTruth::kUnknown &&
    transport_running != EvidenceTruth::kFalse &&
    transport_running != EvidenceTruth::kTrue)
  {
    throw std::invalid_argument("transport evidence is invalid");
  }
  if (active_session.session_id.empty()) {
    if (active_session.control_plane_generation != 0U) {
      throw std::invalid_argument("empty session evidence is invalid");
    }
  } else if (!canonical_uuid_v4(active_session.session_id) ||
    active_session.control_plane_generation != control_plane_generation_)
  {
    throw std::invalid_argument("active session evidence is invalid");
  }

  MediaEvidenceSnapshot result;
  result.observed_boottime_ns = observed_boottime_ns;
  result.valid_until_boottime_ns = checked_add(
    observed_boottime_ns,
    config_.evidence_ttl_ns,
    "media evidence validity overflow");

  const bool control_current =
    backend_device_bound_ &&
    control_plane_generation_ != 0U &&
    control_plane_received_boottime_ns_ != 0U &&
    control_plane_received_boottime_ns_ <= observed_boottime_ns &&
    observed_boottime_ns - control_plane_received_boottime_ns_ <
    config_.evidence_ttl_ns;
  if (!control_current) {
    return result;
  }
  result.control_plane_generation = control_plane_generation_;
  result.backend_device_bound = true;
  result.valid_until_boottime_ns = std::min(
    result.valid_until_boottime_ns,
    checked_add(
      control_plane_received_boottime_ns_,
      config_.evidence_ttl_ns,
      "control-plane validity overflow"));

  const bool applied_matches_authority =
    applied_camera_enabled == authoritative_camera_enabled_;
  if (!applied_matches_authority) {
    return result;
  }

  if (authoritative_camera_enabled_) {
    result.privacy_mode = EvidenceTruth::kFalse;
    const bool pipeline_current =
      video_pipeline_generation_ == control_plane_generation_;
    if (!config_.gstreamer_available) {
      result.camera_available = EvidenceTruth::kFalse;
    } else if (
      pipeline_current &&
      video_pipeline_state_ == VideoPipelineEvidence::kFailed)
    {
      result.camera_available = EvidenceTruth::kFalse;
    } else if (
      pipeline_current &&
      video_pipeline_state_ == VideoPipelineEvidence::kRunning &&
      frame_generation_ == control_plane_generation_ &&
      last_valid_frame_boottime_ns_ != 0U &&
      last_valid_frame_boottime_ns_ <= observed_boottime_ns &&
      observed_boottime_ns - last_valid_frame_boottime_ns_ <
      config_.frame_timeout_ns)
    {
      result.camera_available = EvidenceTruth::kTrue;
      result.last_valid_frame_boottime_ns =
        last_valid_frame_boottime_ns_;
      result.frame_generation = frame_generation_;
      result.valid_until_boottime_ns = std::min(
        result.valid_until_boottime_ns,
        checked_add(
          last_valid_frame_boottime_ns_,
          config_.frame_timeout_ns,
          "frame validity overflow"));
    }
  } else {
    result.camera_available = EvidenceTruth::kFalse;
    const bool software_gate_applied =
      media_io_closed &&
      video_pipeline_generation_ == control_plane_generation_ &&
      video_pipeline_state_ == VideoPipelineEvidence::kStopped &&
      transport_running == EvidenceTruth::kFalse;
    if (software_gate_applied) {
      result.privacy_mode = EvidenceTruth::kTrue;
    }
  }

  if (
    result.privacy_mode == EvidenceTruth::kFalse &&
    !active_session.session_id.empty())
  {
    result.active_session_id = active_session.session_id;
    result.active_session_generation =
      active_session.control_plane_generation;
  }
  return result;
}

bool valid_evidence_image_shape(
  const std::string & encoding,
  const std::uint32_t width,
  const std::uint32_t height,
  const std::uint32_t step,
  const std::size_t data_size)
{
  const std::size_t pixel_bytes = bytes_per_pixel(encoding);
  if (pixel_bytes == 0U || width == 0U || height == 0U ||
    width > static_cast<std::uint32_t>(std::numeric_limits<int>::max()) ||
    height > static_cast<std::uint32_t>(std::numeric_limits<int>::max()))
  {
    return false;
  }
  const std::size_t row_bytes = static_cast<std::size_t>(width) * pixel_bytes;
  if (step < row_bytes) {
    return false;
  }
  const std::size_t required =
    static_cast<std::size_t>(step) * static_cast<std::size_t>(height);
  return data_size == required;
}

std::uint64_t next_media_evidence_sequence(const std::uint64_t current)
{
  if (current == kMaximumU64) {
    throw std::overflow_error("media evidence sequence exhausted");
  }
  return current + 1U;
}

std::uint64_t strict_boottime_ns()
{
  timespec now{};
  if (clock_gettime(CLOCK_BOOTTIME, &now) != 0 || now.tv_sec < 0 ||
    now.tv_nsec < 0 || now.tv_nsec >= 1'000'000'000L)
  {
    throw std::runtime_error("CLOCK_BOOTTIME is unavailable");
  }
  const auto seconds = static_cast<std::uint64_t>(now.tv_sec);
  if (seconds > kMaximumU64 / 1'000'000'000ULL) {
    throw std::overflow_error("CLOCK_BOOTTIME overflow");
  }
  const std::uint64_t result = checked_add(
    seconds * 1'000'000'000ULL,
    static_cast<std::uint64_t>(now.tv_nsec),
    "CLOCK_BOOTTIME overflow");
  validate_receipt(result, "CLOCK_BOOTTIME is invalid");
  return result;
}

std::string generate_source_instance_id()
{
  std::array<unsigned char, 16> bytes{};
  std::size_t offset = 0U;
  while (offset < bytes.size()) {
    const ssize_t received = getrandom(
      bytes.data() + offset,
      bytes.size() - offset,
      0U);
    if (received < 0 && errno == EINTR) {
      continue;
    }
    if (received <= 0) {
      throw std::runtime_error("secure instance identity is unavailable");
    }
    offset += static_cast<std::size_t>(received);
  }
  bytes[6] = static_cast<unsigned char>((bytes[6] & 0x0fU) | 0x40U);
  bytes[8] = static_cast<unsigned char>((bytes[8] & 0x3fU) | 0x80U);

  std::ostringstream output;
  output << std::hex << std::nouppercase << std::setfill('0');
  for (std::size_t index = 0U; index < bytes.size(); ++index) {
    if (index == 4U || index == 6U || index == 8U || index == 10U) {
      output << '-';
    }
    output << std::setw(2) << static_cast<unsigned int>(bytes[index]);
  }
  const std::string result = output.str();
  if (!canonical_uuid_v4(result)) {
    throw std::runtime_error("secure instance identity is invalid");
  }
  return result;
}

bool is_canonical_uuid(const std::string & value)
{
  return canonical_uuid_shape(value);
}

bool same_material_evidence(
  const MediaEvidenceSnapshot & left,
  const MediaEvidenceSnapshot & right)
{
  return left.control_plane_generation == right.control_plane_generation &&
         left.camera_available == right.camera_available &&
         left.privacy_mode == right.privacy_mode &&
         left.last_valid_frame_boottime_ns ==
         right.last_valid_frame_boottime_ns &&
         left.frame_generation == right.frame_generation &&
         left.active_session_id == right.active_session_id &&
         left.active_session_generation == right.active_session_generation &&
         left.backend_device_bound == right.backend_device_bound;
}

MediaEvidencePublication MediaEvidencePublicationCache::observe(
  const MediaEvidenceSnapshot & candidate)
{
  if (snapshot_.has_value() &&
    same_material_evidence(*snapshot_, candidate))
  {
    return MediaEvidencePublication{sequence_, *snapshot_, false};
  }
  const std::uint64_t next = next_media_evidence_sequence(sequence_);
  snapshot_ = candidate;
  sequence_ = next;
  return MediaEvidencePublication{sequence_, *snapshot_, true};
}

bool bound_physical_authority(
  const bool configured,
  const bool backend_device_bound)
{
  return configured && backend_device_bound;
}

}  // namespace homecam_media_agent
