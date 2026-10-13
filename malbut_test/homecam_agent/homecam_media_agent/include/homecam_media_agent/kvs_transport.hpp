#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
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
  kRefreshCredentials,
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

// KVS WebRTC C SDK v1.19.1 skips DescribeMediaStorageConfiguration when a
// channel ARN is supplied. Storage sessions must therefore resolve the
// signaling channel by name so the SDK can discover that storage is enabled.
bool use_explicit_signaling_channel_arn(SessionMode mode);

// Renew credentials before expiry without replacing a connected P2P peer.
// Failed renewals do not extend the old deadline: expiry still fails closed.
SessionRefreshDecision decide_session_refresh(
  SessionMode mode,
  bool peer_connected,
  std::int64_t now_unix_ms,
  std::int64_t expires_at_unix_ms,
  std::int64_t refresh_margin_ms);

bool can_renew_session_credentials(
  const SessionLease & current, const SessionLease & renewed, std::int64_t now_unix_ms);

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

// ALSA hands Opus frames over in bursts, so their arrival times bunch up and
// then gap. RTP timestamps built from arrival made the browser drop and conceal
// live audio (it crackled while recordings were clean; 2026-10-09). Audio
// frames follow their capture clock instead: anchored to the steady clock at
// the first frame, and again only after a restart, a stall or drift.
class AudioCaptureTimeline
{
public:
  // A negative pipeline timestamp means the capture clock is unknown.
  std::int64_t stamp(
    std::int64_t pipeline_timestamp_ns,
    std::int64_t steady_capture_time_ns);

private:
  std::mutex mutex_;
  bool anchored_{false};
  std::int64_t pipeline_anchor_ns_{0};
  std::int64_t steady_anchor_ns_{0};
  std::int64_t last_timestamp_ns_{0};
};

// The guardian's voice used to reach the speaker as soon as each Opus frame
// arrived, so the network's bunching emptied and overflowed it (crackles).
// Frames are now played on the sender's timeline, delay_ns after the first:
// a frame keeps its place relative to that anchor, and a frame that would be
// late or far ahead (a new talk, a lost stretch) starts a new anchor.
class PlayoutSchedule
{
public:
  explicit PlayoutSchedule(std::int64_t delay_ns);
  // Return the running time at which the frame should be heard.
  std::int64_t schedule(std::int64_t frame_ns, std::int64_t running_now_ns);
  void reset();

private:
  std::int64_t delay_ns_;
  bool anchored_{false};
  std::int64_t frame_anchor_ns_{0};
  std::int64_t running_anchor_ns_{0};
  std::int64_t last_ns_{-1};
};

// The guardian's voice through a small jitter buffer (2026-10-13). Played on
// PlayoutSchedule, every frame arriving after its slot moved the whole timeline
// delay_ns ahead, leaving that much silence: the "툭툭" heard from 10-09 on.
// Here frames keep the sender's spacing delay_ns behind the first; a late frame
// is dropped (a 20 ms hole), and only a run of late frames, a jump far ahead or
// a new stream moves the timeline.
class BufferedPlayout
{
public:
  struct Plan
  {
    std::int64_t playout_ns{-1};  // Running time to hear it; -1 when dropped.
    bool dropped{false};
    bool reanchored{false};       // The timeline moved (not the first anchor).
    std::int64_t late_ns{0};      // How late a late frame was.
  };

  explicit BufferedPlayout(std::int64_t delay_ns, int late_run_limit = 5);
  Plan schedule(std::int64_t frame_ns, std::int64_t running_now_ns);
  // A new delay applies from the next frame, on a new anchor.
  void set_delay(std::int64_t delay_ns);
  std::int64_t delay_ns() const {return delay_ns_;}
  void reset();

private:
  Plan anchor(std::int64_t frame_ns, std::int64_t running_now_ns, Plan plan);

  std::int64_t delay_ns_;
  int late_run_limit_;
  bool started_{false};
  bool anchored_{false};
  std::int64_t frame_anchor_ns_{0};
  std::int64_t running_anchor_ns_{0};
  std::int64_t last_ns_{-1};
  int late_run_{0};
};

// The guardian's voice through an adaptive jitter buffer, the way voice apps
// (WebRTC NetEQ) do it (2026-10-13). The delay follows the measured arrival
// jitter: the 95th percentile over the last 3 s plus a margin, 60-600 ms. It
// changes only where no one hears it: in a pause (no frames for a while) or on
// a quiet frame, 20 ms at a time, by adding or skipping silence. A late frame is
// dropped; frames lost in transit are reported as a gap so the decoder can
// conceal them (PLC, FEC) before their slot comes.
class AdaptivePlayout
{
public:
  struct Plan
  {
    std::int64_t playout_ns{-1};    // Running time to hear it; -1 when not played.
    bool dropped{false};            // Too late for its slot.
    bool skipped{false};            // A quiet frame skipped to shorten the delay.
    bool reanchored{false};         // The timeline moved because frames were late.
    std::int64_t gap_start_ns{-1};  // Frames lost before this one: conceal from here.
    std::int64_t late_ns{0};
    std::int64_t delay_ns{0};       // The delay now, beyond the fastest arrival.
    std::int64_t target_ns{0};      // The delay the jitter asks for.
  };

  AdaptivePlayout(
    std::int64_t min_delay_ns = 60'000'000,
    std::int64_t max_delay_ns = 600'000'000);
  Plan schedule(std::int64_t frame_ns, std::int64_t running_now_ns, std::size_t payload_bytes);
  void reset();

private:
  std::int64_t target_delay_ns(std::int64_t base_ns) const;
  bool quiet(std::size_t payload_bytes) const;

  std::int64_t min_delay_ns_;
  std::int64_t max_delay_ns_;
  std::deque<std::int64_t> offsets_;  // Arrival minus frame time, last 10 s.
  std::deque<std::size_t> sizes_;
  bool started_{false};
  bool anchored_{false};
  std::int64_t shift_ns_{0};          // Playout = frame time + shift.
  std::int64_t last_frame_ns_{0};
  std::int64_t last_end_ns_{-1};      // Where the last played frame ends.
  int late_run_{0};
  int frames_since_skip_{0};
};

using PttAudioCallback = std::function<void (const EncodedFrame &)>;

class KvsTransport
{
public:
  virtual ~KvsTransport() = default;
  virtual bool implemented() const = 0;
  virtual bool running() const = 0;
  virtual bool media_flowing() const = 0;
  virtual bool peer_connected() const = 0;
  virtual bool restart_required() const = 0;
  virtual std::string status() const = 0;
  virtual bool start(const SessionLease & lease, std::string * error) = 0;
  // Same-channel P2P renewal only; failure leaves the old deadline unchanged.
  virtual bool renew_credentials(const SessionLease & lease, std::string * error) = 0;
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
