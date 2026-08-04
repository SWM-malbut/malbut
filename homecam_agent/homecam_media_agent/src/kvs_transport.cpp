#include "homecam_media_agent/kvs_transport.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iomanip>
#include <limits>
#include <mutex>
#include <optional>
#include <sstream>
#include <string_view>
#include <thread>
#include <utility>

#include "homecam_media_agent/build_features.hpp"

#if HOMECAM_HAVE_KVS
extern "C"
{
#include "Samples.h"
}
#endif

namespace homecam_media_agent
{

std::uint64_t kvs_timestamp_from_nanoseconds(const std::int64_t nanoseconds)
{
  if (nanoseconds <= 0) {
    return 0;
  }
  return static_cast<std::uint64_t>(nanoseconds) / 100U;
}

KvsTimestampNormalizer::KvsTimestampNormalizer(
  const std::int64_t default_step_nanoseconds)
: default_step_(
    std::max<std::uint64_t>(
      kvs_timestamp_from_nanoseconds(default_step_nanoseconds), 1U)),
  estimated_step_(default_step_)
{
}

std::uint64_t KvsTimestampNormalizer::normalize(
  const std::int64_t source_nanoseconds)
{
  const std::uint64_t source =
    kvs_timestamp_from_nanoseconds(source_nanoseconds);
  if (!seen_) {
    seen_ = true;
    previous_source_ = source;
    previous_output_ = source;
    return previous_output_;
  }

  std::uint64_t step = estimated_step_;
  if (source > previous_source_) {
    const std::uint64_t delta = source - previous_source_;
    constexpr std::uint64_t maximum_estimated_step = 10'000'000;
    if (delta <= maximum_estimated_step) {
      estimated_step_ = std::max<std::uint64_t>(delta, 1U);
    }
    step = delta;
  }
  if (step > std::numeric_limits<std::uint64_t>::max() - previous_output_) {
    previous_output_ = std::numeric_limits<std::uint64_t>::max();
  } else {
    previous_output_ += step;
  }
  previous_source_ = source;
  return previous_output_;
}

void KvsTimestampNormalizer::reset()
{
  estimated_step_ = default_step_;
  previous_source_ = 0;
  previous_output_ = 0;
  seen_ = false;
}

std::int64_t SharedMediaTimeline::stamp(
  const std::int64_t pipeline_timestamp_ns,
  const std::int64_t steady_capture_time_ns)
{
  // Separate GstPipeline objects restart their PTS independently. Retaining
  // this argument documents that the mismatch is intentional, not omitted.
  (void)pipeline_timestamp_ns;
  std::int64_t candidate = std::max<std::int64_t>(
    steady_capture_time_ns, 1);
  std::int64_t previous = last_timestamp_ns_.load();
  for (;; ) {
    if (candidate <= previous) {
      if (previous == std::numeric_limits<std::int64_t>::max()) {
        return previous;
      }
      candidate = previous + 1;
    }
    if (last_timestamp_ns_.compare_exchange_weak(previous, candidate)) {
      return candidate;
    }
  }
}

bool SessionLease::valid_at(const std::int64_t now_unix_ms) const
{
  return !channel_arn.empty() &&
         !region.empty() &&
         !credentials.access_key_id.empty() &&
         !credentials.secret_access_key.empty() &&
         !credentials.session_token.empty() &&
         credentials.expires_at_unix_ms > now_unix_ms;
}

bool SessionLease::refresh_due(const std::int64_t now_unix_ms) const
{
  if (!valid_at(now_unix_ms)) {
    return true;
  }
  return credentials.expires_at_unix_ms - now_unix_ms <= refresh_margin_ms;
}

bool session_lease_expired(
  const std::int64_t now_unix_ms,
  const std::int64_t expires_at_unix_ms)
{
  return expires_at_unix_ms != 0 && expires_at_unix_ms <= now_unix_ms;
}

SessionRefreshDecision decide_session_refresh(
  const SessionMode mode,
  const bool peer_connected,
  const std::int64_t now_unix_ms,
  const std::int64_t expires_at_unix_ms,
  const std::int64_t refresh_margin_ms,
  const std::int64_t connected_peer_safety_margin_ms)
{
  if (session_lease_expired(now_unix_ms, expires_at_unix_ms)) {
    return SessionRefreshDecision::kFailClosed;
  }
  const std::int64_t remaining_ms = expires_at_unix_ms - now_unix_ms;
  const std::int64_t routine_margin_ms =
    std::max<std::int64_t>(refresh_margin_ms, 0);
  if (remaining_ms > routine_margin_ms) {
    return SessionRefreshDecision::kNotDue;
  }
  if (mode != SessionMode::kPeerToPeer || !peer_connected) {
    return SessionRefreshDecision::kRefreshNow;
  }
  const std::int64_t safety_margin_ms = std::clamp<std::int64_t>(
    connected_peer_safety_margin_ms, 0, routine_margin_ms);
  if (remaining_ms <= safety_margin_ms) {
    return SessionRefreshDecision::kForceBeforeExpiry;
  }
  return SessionRefreshDecision::kDeferForConnectedPeer;
}

namespace
{

#if HOMECAM_HAVE_KVS

constexpr char kMasterClientId[] = "homecam-master";

std::int64_t unix_now_ms()
{
  return std::chrono::duration_cast<std::chrono::milliseconds>(
    std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string sdk_error(const char * const operation, const STATUS status)
{
  std::ostringstream output;
  output << operation << " failed with KVS status 0x" <<
    std::hex << std::setw(8) << std::setfill('0') << status;
  return output.str();
}

bool set_error(std::string * const error, const std::string & message)
{
  if (error != nullptr) {
    *error = message;
  }
  return false;
}

std::optional<std::string> channel_name_from_arn(const std::string & arn)
{
  constexpr std::string_view marker = ":channel/";
  const std::size_t start = arn.find(marker);
  if (start == std::string::npos) {
    return std::nullopt;
  }
  const std::size_t name_start = start + marker.size();
  const std::size_t name_end = arn.find('/', name_start);
  if (name_end == std::string::npos || name_end == name_start) {
    return std::nullopt;
  }
  return arn.substr(name_start, name_end - name_start);
}

void secure_clear(std::string * const value)
{
  std::fill(value->begin(), value->end(), '\0');
  value->clear();
}

std::mutex & process_environment_mutex()
{
  static std::mutex mutex;
  return mutex;
}

class ScopedEnvironmentValue
{
public:
  ScopedEnvironmentValue(const char * const name, const char * const value)
  : name_(name)
  {
    const char * const previous = std::getenv(name);
    if (previous != nullptr) {
      previous_ = previous;
    }
    setenv(name, value, 1);
  }

  ~ScopedEnvironmentValue()
  {
    if (previous_.has_value()) {
      setenv(name_.c_str(), previous_->c_str(), 1);
    } else {
      unsetenv(name_.c_str());
    }
  }

  ScopedEnvironmentValue(const ScopedEnvironmentValue &) = delete;
  ScopedEnvironmentValue & operator=(const ScopedEnvironmentValue &) = delete;

private:
  std::string name_;
  std::optional<std::string> previous_;
};

class AwsKvsTransport final : public KvsTransport
{
public:
  ~AwsKvsTransport() override
  {
    stop();
  }

  bool implemented() const override
  {
    return true;
  }

  bool running() const override
  {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return configuration_ != nullptr &&
           !cleanup_thread_exited_.load() &&
           lease_.valid_at(unix_now_ms()) &&
           media_connected_locked();
  }

  bool peer_connected() const override
  {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return configuration_ != nullptr &&
           !cleanup_thread_exited_.load() &&
           media_connected_locked();
  }

  bool restart_required() const override
  {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return configuration_ != nullptr &&
           cleanup_thread_exited_.load();
  }

  std::string status() const override
  {
    std::lock_guard<std::mutex> lock(state_mutex_);
    if (configuration_ != nullptr &&
      !lease_.valid_at(unix_now_ms()))
    {
      return "temporary device session lease expired";
    }
    if (configuration_ != nullptr && cleanup_thread_exited_.load()) {
      return sdk_error(
        "sessionCleanupWait", cleanup_thread_status_.load());
    }
    if (configuration_ != nullptr && media_connected_locked()) {
      return lease_.mode == SessionMode::kStorage ?
             "connected to KVS WebRTC storage session" :
             "connected to KVS WebRTC peer";
    }
    return status_;
  }

  bool start(const SessionLease & lease, std::string * const error) override
  {
    stop();
    const std::int64_t now = unix_now_ms();
    if (!lease.valid_at(now)) {
      return set_error(error, "temporary device session lease is invalid or expired");
    }
    if (lease.refresh_margin_ms < 0) {
      return set_error(error, "session refresh margin must not be negative");
    }
    const auto channel_name = channel_name_from_arn(lease.channel_arn);
    if (!channel_name.has_value()) {
      return set_error(error, "channel ARN is not a KVS signaling channel ARN");
    }
    if (lease.credentials.expires_at_unix_ms >
      static_cast<std::int64_t>(
        std::numeric_limits<UINT64>::max() /
        HUNDREDS_OF_NANOS_IN_A_MILLISECOND))
    {
      return set_error(error, "temporary credential expiration overflows SDK time");
    }

    std::lock_guard<std::mutex> lock(state_mutex_);
    lease_ = lease;
    channel_name_ = *channel_name;
    channel_arn_ = lease.channel_arn;
    region_ = lease.region;
    status_ = "initializing KVS WebRTC";
    video_timestamp_.reset();
    audio_timestamp_.reset();
    last_successful_write_ns_.store(0);
    cleanup_thread_exited_.store(false);
    cleanup_thread_status_.store(STATUS_SUCCESS);

    STATUS status = STATUS_SUCCESS;
    {
      // The upstream sample helper constructs its initial provider from the
      // process environment. Only non-secret placeholders are present under
      // a process-wide lock; the provider is immediately replaced with the
      // explicit short-lived SessionLease below.
      std::lock_guard<std::mutex> environment_lock(process_environment_mutex());
      ScopedEnvironmentValue access_key("AWS_ACCESS_KEY_ID", "unused");
      ScopedEnvironmentValue secret_key("AWS_SECRET_ACCESS_KEY", "unused");
      ScopedEnvironmentValue session_token("AWS_SESSION_TOKEN", "unused");
      ScopedEnvironmentValue region("AWS_DEFAULT_REGION", region_.c_str());
      status = createSampleConfiguration(
        channel_name_.data(), SIGNALING_CHANNEL_ROLE_TYPE_MASTER,
        TRUE, TRUE, LOG_LEVEL_WARN, &configuration_);
    }
    if (STATUS_FAILED(status)) {
      configuration_ = nullptr;
      clear_lease_locked();
      status_ = sdk_error("createSampleConfiguration", status);
      return set_error(error, status_);
    }

    status = initKvsWebRtc();
    if (STATUS_FAILED(status)) {
      const std::string detail = sdk_error("initKvsWebRtc", status);
      quiesce_configuration_locked();
      cleanup_locked();
      status_ = detail;
      return set_error(error, detail);
    }

    status = freeStaticCredentialProvider(&configuration_->pCredentialProvider);
    if (STATUS_SUCCEEDED(status)) {
      const UINT64 expiration =
        static_cast<UINT64>(lease.credentials.expires_at_unix_ms) *
        HUNDREDS_OF_NANOS_IN_A_MILLISECOND;
      status = createStaticCredentialProvider(
        const_cast<char *>(lease.credentials.access_key_id.c_str()), 0,
        const_cast<char *>(lease.credentials.secret_access_key.c_str()), 0,
        const_cast<char *>(lease.credentials.session_token.c_str()), 0,
        expiration, &configuration_->pCredentialProvider);
    }
    if (STATUS_FAILED(status)) {
      const std::string detail = sdk_error("createStaticCredentialProvider", status);
      quiesce_configuration_locked();
      cleanup_locked();
      status_ = detail;
      return set_error(error, detail);
    }

    configuration_->channelInfo.pChannelName = channel_name_.data();
    configuration_->channelInfo.pChannelArn = channel_arn_.data();
    configuration_->channelInfo.pRegion = region_.data();
    // The systemd sandbox exposes no writable working directory. Avoid the
    // upstream sample's default ./.SignalingCache_v1 file entirely.
    configuration_->channelInfo.cachingPolicy =
      SIGNALING_API_CALL_CACHE_TYPE_NONE;
    configuration_->channelInfo.useMediaStorage =
      lease.mode == SessionMode::kStorage ? TRUE : FALSE;
    configuration_->mediaType = SAMPLE_STREAMING_AUDIO_VIDEO;
    configuration_->audioCodec = RTC_CODEC_OPUS;
    configuration_->videoCodec =
      RTC_CODEC_H264_PROFILE_42E01F_LEVEL_ASYMMETRY_ALLOWED_PACKETIZATION_MODE;
    configuration_->addTransceiversCallback =
      addSendOnlyVideoSendrecvAudioTransceivers;
    configuration_->receiveAudioVideoSource = register_receive_audio;
    configuration_->customData = reinterpret_cast<UINT64>(this);
    configuration_->enableTwcc = FALSE;

    {
      std::lock_guard<std::mutex> callback_lock(callback_mutex_);
      callbacks_enabled_ = true;
    }
    status = initSignaling(configuration_, const_cast<char *>(kMasterClientId));
    if (STATUS_FAILED(status)) {
      const std::string detail = sdk_error("initSignaling", status);
      disable_callbacks_locked();
      quiesce_configuration_locked();
      cleanup_locked();
      wait_for_callbacks_locked();
      status_ = detail;
      return set_error(error, detail);
    }

    try {
      cleanup_thread_ = std::thread(
        [this, configuration = configuration_]() {
          const STATUS cleanup_status = sessionCleanupWait(configuration);
          cleanup_thread_status_.store(cleanup_status);
          cleanup_thread_exited_.store(true);
        });
    } catch (const std::exception & exception) {
      const std::string detail =
        std::string("cannot start KVS session cleanup thread: ") + exception.what();
      disable_callbacks_locked();
      quiesce_configuration_locked();
      cleanup_locked();
      wait_for_callbacks_locked();
      status_ = detail;
      return set_error(error, detail);
    }
    status_ = lease.mode == SessionMode::kStorage ?
      "KVS signaling connected; waiting for storage-session offer" :
      "KVS signaling connected; waiting for viewer offer";
    return true;
  }

  void stop() override
  {
    std::unique_lock<std::mutex> lock(state_mutex_);
    disable_callbacks_locked();
    quiesce_configuration_locked();
    cleanup_locked();
    clear_lease_locked();
    status_ = "KVS transport stopped";
    wait_for_callbacks_locked();
  }

  bool push_h264(
    const EncodedFrame & frame,
    std::string * const error) override
  {
    return push(frame, true, error);
  }

  bool push_opus(
    const EncodedFrame & frame,
    std::string * const error) override
  {
    return push(frame, false, error);
  }

  void set_ptt_audio_callback(PttAudioCallback callback) override
  {
    std::lock_guard<std::mutex> lock(callback_mutex_);
    callback_ = std::move(callback);
  }

private:
  static PVOID register_receive_audio(PVOID arguments)
  {
    auto * const session = static_cast<PSampleStreamingSession>(arguments);
    if (session == nullptr || session->pSampleConfiguration == nullptr ||
      session->pAudioRtcRtpTransceiver == nullptr)
    {
      return reinterpret_cast<PVOID>(
        static_cast<ULONG_PTR>(STATUS_NULL_ARG));
    }
    auto * const transport = reinterpret_cast<AwsKvsTransport *>(
      session->pSampleConfiguration->customData);
    if (transport == nullptr) {
      return reinterpret_cast<PVOID>(
        static_cast<ULONG_PTR>(STATUS_NULL_ARG));
    }
    const STATUS status = transceiverOnFrame(
      session->pAudioRtcRtpTransceiver,
      reinterpret_cast<UINT64>(transport),
      on_remote_audio_frame);
    return reinterpret_cast<PVOID>(static_cast<ULONG_PTR>(status));
  }

  static VOID on_remote_audio_frame(
    const UINT64 custom_data,
    PFrame frame)
  {
    auto * const transport =
      reinterpret_cast<AwsKvsTransport *>(custom_data);
    if (transport != nullptr && frame != nullptr) {
      transport->dispatch_remote_audio(*frame);
    }
  }

  void dispatch_remote_audio(const Frame & frame)
  {
    PttAudioCallback callback;
    {
      std::lock_guard<std::mutex> lock(callback_mutex_);
      if (!callbacks_enabled_ || !callback_) {
        return;
      }
      ++callbacks_in_flight_;
      callback = callback_;
    }

    try {
      EncodedFrame encoded;
      if (frame.frameData != nullptr && frame.size > 0U) {
        encoded.payload.assign(frame.frameData, frame.frameData + frame.size);
      }
      if (frame.presentationTs <=
        static_cast<UINT64>(
          std::numeric_limits<std::int64_t>::max() / 100))
      {
        encoded.presentation_time_ns =
          static_cast<std::int64_t>(frame.presentationTs * 100U);
      }
      callback(encoded);
    } catch (...) {
      // Exceptions must never cross the C SDK callback boundary.
    }

    {
      std::lock_guard<std::mutex> lock(callback_mutex_);
      --callbacks_in_flight_;
      if (callbacks_in_flight_ == 0U) {
        callback_condition_.notify_all();
      }
    }
  }

  bool push(
    const EncodedFrame & encoded,
    const bool video,
    std::string * const error)
  {
    if (encoded.payload.empty()) {
      return set_error(error, "encoded frame is empty");
    }
    if (encoded.payload.size() > std::numeric_limits<UINT32>::max()) {
      return set_error(error, "encoded frame exceeds the SDK size limit");
    }

    std::lock_guard<std::mutex> lock(state_mutex_);
    if (configuration_ == nullptr) {
      return set_error(error, "KVS transport is stopped");
    }
    if (cleanup_thread_exited_.load()) {
      status_ = sdk_error(
        "sessionCleanupWait", cleanup_thread_status_.load());
      return set_error(error, status_);
    }
    if (!lease_.valid_at(unix_now_ms())) {
      status_ = "temporary device session lease expired";
      return set_error(error, status_);
    }

    Frame frame{};
    frame.version = FRAME_CURRENT_VERSION;
    frame.flags =
      video && encoded.key_frame ? FRAME_FLAG_KEY_FRAME : FRAME_FLAG_NONE;
    frame.trackId = video ? DEFAULT_VIDEO_TRACK_ID : DEFAULT_AUDIO_TRACK_ID;
    frame.duration = 0;
    frame.size = static_cast<UINT32>(encoded.payload.size());
    frame.frameData =
      const_cast<PBYTE>(reinterpret_cast<const BYTE *>(encoded.payload.data()));
    frame.presentationTs = normalized_timestamp_locked(
      video, encoded.presentation_time_ns);
    frame.decodingTs = frame.presentationTs;

    UINT32 accepted = 0;
    UINT32 session_count = 0;
    STATUS last_status = STATUS_SUCCESS;
    MUTEX_LOCK(configuration_->streamingSessionListReadLock);
    session_count = configuration_->streamingSessionCount;
    for (UINT32 index = 0; index < session_count; ++index) {
      PSampleStreamingSession session =
        configuration_->sampleStreamingSessionList[index];
      if (session == nullptr ||
        ATOMIC_LOAD_BOOL(&session->terminateFlag))
      {
        continue;
      }
      PRtcRtpTransceiver transceiver =
        video ? session->pVideoRtcRtpTransceiver :
        session->pAudioRtcRtpTransceiver;
      if (transceiver == nullptr) {
        continue;
      }
      frame.index =
        static_cast<UINT32>(ATOMIC_INCREMENT(&session->frameIndex));
      last_status = writeFrame(transceiver, &frame);
      if (last_status == STATUS_SUCCESS) {
        ++accepted;
      }
    }
    MUTEX_UNLOCK(configuration_->streamingSessionListReadLock);

    if (accepted > 0U) {
      last_successful_write_ns_.store(steady_now_ns());
      return true;
    }
    if (session_count == 0U) {
      return set_error(error, "KVS signaling is ready but no peer is connected");
    }
    if (last_status == STATUS_SRTP_NOT_READY_YET) {
      return set_error(error, "KVS peer exists but SRTP is not ready");
    }
    return set_error(error, sdk_error("writeFrame", last_status));
  }

  UINT64 normalized_timestamp_locked(
    const bool video,
    const std::int64_t nanoseconds)
  {
    return video ?
           video_timestamp_.normalize(nanoseconds) :
           audio_timestamp_.normalize(nanoseconds);
  }

  void disable_callbacks_locked()
  {
    std::lock_guard<std::mutex> lock(callback_mutex_);
    callbacks_enabled_ = false;
  }

  void wait_for_callbacks_locked()
  {
    std::unique_lock<std::mutex> callback_lock(callback_mutex_);
    callback_condition_.wait(
      callback_lock,
      [this]() {return callbacks_in_flight_ == 0U;});
  }

  void cleanup_locked()
  {
    if (configuration_ == nullptr) {
      return;
    }
    if (IS_VALID_SIGNALING_CLIENT_HANDLE(
        configuration_->signalingClientHandle))
    {
      freeSignalingClient(&configuration_->signalingClientHandle);
    }
    freeSampleConfiguration(&configuration_);
    configuration_ = nullptr;
  }

  void quiesce_configuration_locked()
  {
    if (configuration_ != nullptr) {
      ATOMIC_STORE_BOOL(&configuration_->appTerminateFlag, TRUE);
      ATOMIC_STORE_BOOL(&configuration_->interrupted, TRUE);
      CVAR_BROADCAST(configuration_->cvar);
    }
    if (cleanup_thread_.joinable()) {
      cleanup_thread_.join();
    }
    join_media_sender_locked();
  }

  void join_media_sender_locked()
  {
    if (configuration_ != nullptr &&
      IS_VALID_TID_VALUE(configuration_->mediaSenderTid))
    {
      THREAD_JOIN(configuration_->mediaSenderTid, nullptr);
      configuration_->mediaSenderTid = INVALID_TID_VALUE;
    }
  }

  static std::int64_t steady_now_ns()
  {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
  }

  bool media_connected_locked() const
  {
    if (ATOMIC_LOAD_BOOL(&configuration_->connected)) {
      return true;
    }
    const std::int64_t last_write = last_successful_write_ns_.load();
    constexpr std::int64_t recent_write_window_ns = 5'000'000'000;
    return last_write > 0 &&
           steady_now_ns() - last_write <= recent_write_window_ns;
  }

  void clear_lease_locked()
  {
    secure_clear(&lease_.credentials.access_key_id);
    secure_clear(&lease_.credentials.secret_access_key);
    secure_clear(&lease_.credentials.session_token);
    lease_.credentials.expires_at_unix_ms = 0;
    secure_clear(&lease_.channel_arn);
    secure_clear(&lease_.region);
    secure_clear(&channel_arn_);
    secure_clear(&channel_name_);
    secure_clear(&region_);
  }

  mutable std::mutex state_mutex_;
  mutable std::mutex callback_mutex_;
  std::condition_variable callback_condition_;
  PttAudioCallback callback_;
  std::size_t callbacks_in_flight_{0};
  bool callbacks_enabled_{false};
  PSampleConfiguration configuration_{nullptr};
  std::thread cleanup_thread_;
  std::atomic<bool> cleanup_thread_exited_{false};
  std::atomic<STATUS> cleanup_thread_status_{STATUS_SUCCESS};
  SessionLease lease_;
  std::string channel_name_;
  std::string channel_arn_;
  std::string region_;
  std::string status_{"KVS transport has not started"};
  KvsTimestampNormalizer video_timestamp_{66'666'667};
  KvsTimestampNormalizer audio_timestamp_{20'000'000};
  std::atomic<std::int64_t> last_successful_write_ns_{0};
};

#else

class UnavailableKvsTransport final : public KvsTransport
{
public:
  bool implemented() const override
  {
    return false;
  }

  bool running() const override
  {
    return false;
  }

  bool peer_connected() const override
  {
    return false;
  }

  bool restart_required() const override
  {
    return false;
  }

  std::string status() const override
  {
    return
      "KVS transport disabled at build time: signaling, writeFrame, and "
      "remote PTT are fail-closed";
  }

  bool start(const SessionLease &, std::string * const error) override
  {
    return fail(error);
  }

  void stop() override
  {
  }

  bool push_h264(const EncodedFrame &, std::string * const error) override
  {
    return fail(error);
  }

  bool push_opus(const EncodedFrame &, std::string * const error) override
  {
    return fail(error);
  }

  void set_ptt_audio_callback(PttAudioCallback callback) override
  {
    callback_ = std::move(callback);
  }

private:
  bool fail(std::string * const error) const
  {
    if (error != nullptr) {
      *error = status();
    }
    return false;
  }

  PttAudioCallback callback_;
};

#endif

}  // namespace

std::unique_ptr<KvsTransport> make_kvs_transport()
{
#if HOMECAM_HAVE_KVS
  return std::make_unique<AwsKvsTransport>();
#else
  return std::make_unique<UnavailableKvsTransport>();
#endif
}

}  // namespace homecam_media_agent
