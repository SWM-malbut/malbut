#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace homecam_media_agent
{

enum class SessionMode
{
  kPeerToPeer,
  kStorage
};

struct SessionCredentials
{
  std::string access_key_id;
  std::string secret_access_key;
  std::string session_token;
  std::int64_t expires_at_unix_ms{0};
};

struct SessionLease
{
  std::string channel_arn;
  std::string region;
  SessionMode mode{SessionMode::kPeerToPeer};
  SessionCredentials credentials;
  std::int64_t refresh_margin_ms{300000};

  bool valid_at(std::int64_t now_unix_ms) const;
  bool refresh_due(std::int64_t now_unix_ms) const;
};

enum class SessionRefreshDecision
{
  kNotDue,
  kRefreshNow,
  kDeferForConnectedPeer,
  kForceBeforeExpiry,
  kFailClosed
};

bool session_lease_expired(
  std::int64_t now_unix_ms,
  std::int64_t expires_at_unix_ms);

bool storage_session_refresh_due(
  std::int64_t active_age_ms,
  std::int64_t refresh_age_ms);

bool storage_session_hard_expired(
  std::int64_t active_age_ms,
  std::int64_t hard_age_ms);

// Routine P2P credential renewal must not tear down a healthy peer. Defer it
// until that peer disconnects, but retain a bounded safety window for a final
// fail-closed replacement before the current lease expires.
SessionRefreshDecision decide_session_refresh(
  SessionMode mode,
  bool peer_connected,
  std::int64_t now_unix_ms,
  std::int64_t expires_at_unix_ms,
  std::int64_t refresh_margin_ms,
  std::int64_t connected_peer_safety_margin_ms);

struct EncodedFrame
{
  std::vector<std::uint8_t> payload;
  std::int64_t presentation_time_ns{0};
  bool key_frame{false};
};

// AWS KVS Frame timestamps use absolute/relative 100 ns units.
std::uint64_t kvs_timestamp_from_nanoseconds(std::int64_t nanoseconds);

class KvsTimestampNormalizer
{
public:
  explicit KvsTimestampNormalizer(std::int64_t default_step_nanoseconds);
  std::uint64_t normalize(std::int64_t source_nanoseconds);
  void reset();

private:
  std::uint64_t default_step_{1};
  std::uint64_t estimated_step_{1};
  std::uint64_t previous_source_{0};
  std::uint64_t previous_output_{0};
  bool seen_{false};
};

// Audio and video are produced by independent GStreamer pipelines with
// unrelated base times. This clock deliberately ignores their local PTS and
// stamps both tracks in one steady-clock domain.
class SharedMediaTimeline
{
public:
  std::int64_t stamp(
    std::int64_t pipeline_timestamp_ns,
    std::int64_t steady_capture_time_ns);

private:
  std::atomic<std::int64_t> last_timestamp_ns_{0};
};

using PttAudioCallback = std::function<void (const EncodedFrame &)>;

class KvsTransport
{
public:
  virtual ~KvsTransport() = default;
  virtual bool implemented() const = 0;
  virtual bool running() const = 0;
  virtual bool peer_connected() const = 0;
  virtual bool restart_required() const = 0;
  virtual std::string status() const = 0;
  virtual bool start(const SessionLease & lease, std::string * error) = 0;
  // stop() must not return while a registered PTT callback is still running.
  virtual void stop() = 0;
  virtual bool push_h264(const EncodedFrame & frame, std::string * error) = 0;
  virtual bool push_opus(const EncodedFrame & frame, std::string * error) = 0;
  virtual void set_ptt_audio_callback(PttAudioCallback callback) = 0;
};

// Returns the real AWS adapter in an SDK-enabled build and a fail-closed
// implementation otherwise.
std::unique_ptr<KvsTransport> make_kvs_transport();

}  // namespace homecam_media_agent
