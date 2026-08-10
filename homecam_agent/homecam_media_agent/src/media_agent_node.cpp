#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <future>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "homecam_media_agent/build_features.hpp"
#include "homecam_media_agent/config.hpp"
#include "homecam_media_agent/heartbeat_client.hpp"
#include "homecam_media_agent/kvs_transport.hpp"
#include "homecam_media_agent/pipeline_builder.hpp"
#include "homecam_media_agent/session_client.hpp"
#include "nav_msgs/msg/odometry.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/camera_info.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "std_msgs/msg/bool.hpp"

#if HOMECAM_HAVE_GSTREAMER
#include <gst/app/gstappsrc.h>
#include <gst/app/gstappsink.h>
#include <gst/gst.h>
#endif

namespace homecam_media_agent
{

using namespace std::chrono_literals;

class MediaAgentNode final : public rclcpp::Node
{
public:
  MediaAgentNode()
  : Node("homecam_media_agent")
  {
    declare_parameters();
    config_ = read_config();
    const auto errors = validate_config(config_);
    if (!errors.empty()) {
      std::string combined;
      for (const auto & error : errors) {
        combined += (combined.empty() ? "" : "; ") + error;
      }
      throw std::invalid_argument("invalid homecam media configuration: " + combined);
    }
    media_permitted_.store(config_.camera_enabled);

    const char * const token_file = std::getenv("HOMECAM_DEVICE_TOKEN_FILE");
    const char * const environment_token = std::getenv("HOMECAM_DEVICE_TOKEN");
    const std::string token = load_device_token(
      token_file == nullptr ? "" : token_file,
      environment_token == nullptr ? "" : environment_token);
    heartbeat_client_ = std::make_unique<HeartbeatClient>(
      trim_trailing_slashes(config_.backend_url), token);
    session_client_ = std::make_unique<DeviceSessionClient>(
      trim_trailing_slashes(config_.backend_url), config_.device_id, token);
    desired_state_confirmed_ = config_.backend_url.empty();
    transport_ = make_kvs_transport();
    transport_->set_ptt_audio_callback(
      [this](const EncodedFrame & frame) {
        if (
          shutting_down_.load() ||
          !media_generation_allows_io(
            active_transport_generation_.load(),
            session_generation_.load(),
            media_permitted_.load()))
        {
          return;
        }
#if HOMECAM_HAVE_GSTREAMER
        push_ptt_audio(frame);
#else
        (void)frame;
#endif
      });

    image_subscription_ = create_subscription<sensor_msgs::msg::Image>(
      config_.image_topic, rclcpp::SensorDataQoS(),
      std::bind(&MediaAgentNode::on_image, this, std::placeholders::_1));

    if (!config_.camera_info_topic.empty()) {
      camera_info_subscription_ = create_subscription<sensor_msgs::msg::CameraInfo>(
        config_.camera_info_topic, rclcpp::SensorDataQoS(),
        [this](sensor_msgs::msg::CameraInfo::ConstSharedPtr) {
          camera_info_received_.store(true);
        });
    }
    if (!config_.odom_topic.empty()) {
      odom_subscription_ = create_subscription<nav_msgs::msg::Odometry>(
        config_.odom_topic, rclcpp::SensorDataQoS(),
        [this](nav_msgs::msg::Odometry::ConstSharedPtr) {
          odom_received_.store(true);
        });
    }
    monitoring_publisher_ = create_publisher<std_msgs::msg::Bool>(
      "/homecam/monitoring_enabled",
      rclcpp::QoS(1).reliable().transient_local());
    detector_health_subscription_ = create_subscription<std_msgs::msg::Bool>(
      "/homecam/detector_healthy",
      rclcpp::QoS(1).reliable().transient_local(),
      [this](const std_msgs::msg::Bool::ConstSharedPtr message) {
        detector_reported_healthy_.store(message->data);
        detector_health_received_at_ns_.store(steady_now_ns());
      });
    publish_monitoring_state();

    heartbeat_timer_ = create_wall_timer(
      std::chrono::milliseconds(config_.heartbeat_interval_ms),
      std::bind(&MediaAgentNode::publish_heartbeat, this));

#if HOMECAM_HAVE_GSTREAMER
    gst_init(nullptr, nullptr);
    start_audio_capture();
    RCLCPP_INFO(
      get_logger(),
      "GStreamer support is available; the encoder starts after the first image");
#else
    RCLCPP_WARN(
      get_logger(),
      "GStreamer development libraries were absent at build time; "
      "running image-health tracking only");
#endif
    session_timer_ = create_wall_timer(
      250ms, std::bind(&MediaAgentNode::maintain_media_runtime, this));

#if HOMECAM_HAVE_KVS && HOMECAM_HAVE_GSTREAMER
    RCLCPP_INFO(
      get_logger(),
      "Amazon KVS WebRTC transport is enabled; the device session will start "
      "after the backend issues short-lived credentials.");
#elif HOMECAM_HAVE_KVS
    RCLCPP_WARN(
      get_logger(),
      "Amazon KVS WebRTC transport is enabled, but GStreamer is unavailable; "
      "no device media session will be opened.");
#elif HOMECAM_HAVE_GSTREAMER
    RCLCPP_WARN(
      get_logger(),
      "Amazon KVS WebRTC SDK support is disabled. Frames are encoded to a "
      "local fakesink; no remote media is sent.");
#else
    RCLCPP_WARN(
      get_logger(),
      "Remote media is disabled and GStreamer is unavailable; camera frames "
      "are health-checked but are not encoded or sent.");
#endif
    RCLCPP_INFO(get_logger(), "%s", transport_->status().c_str());

    if (!heartbeat_client_->available()) {
      RCLCPP_WARN(
        get_logger(),
        "Remote heartbeat disabled. Configure backend_url and the "
        "HOMECAM_DEVICE_TOKEN_FILE credential.");
    }

    RCLCPP_INFO(
      get_logger(),
      "Listening for camera frames on %s (no velocity command publisher is created)",
      config_.image_topic.c_str());
  }

  ~MediaAgentNode() override
  {
    shutting_down_.store(true);
    media_permitted_.store(false);
    session_generation_.fetch_add(1U);
    active_transport_generation_.store(0U);
    if (heartbeat_future_.valid()) {
      heartbeat_future_.wait();
    }
    if (session_future_.valid()) {
      session_future_.wait();
      collect_session_result();
    }
    {
      std::lock_guard<std::mutex> lock(transport_mutex_);
      transport_->set_ptt_audio_callback({});
      transport_->stop();
    }
    const std::string session_to_close =
      !cleanup_session_id_.empty() ?
      cleanup_session_id_ : active_session_id_;
    if (session_client_->available() &&
      is_valid_session_id(session_to_close))
    {
      const SessionCloseResult result =
        session_client_->close(session_to_close);
      if (!result.terminal()) {
        RCLCPP_WARN(
          get_logger(), "Could not close backend device session: %s",
          result.error.c_str());
      }
    }
#if HOMECAM_HAVE_GSTREAMER
    stop_pipeline();
    stop_audio_capture();
    stop_ptt_playback();
#endif
  }

private:
  void declare_parameters()
  {
    declare_parameter<std::string>("image_topic", "/depth_cam/depth_cam");
    declare_parameter<std::string>(
      "camera_info_topic", "/depth_cam/rgb/camera_info");
    declare_parameter<std::string>("odom_topic", "/odom");
    declare_parameter<std::string>("audio_source", "default");
    declare_parameter<std::string>("audio_sink", "default");
    declare_parameter<std::string>("encoder", "x264");
    declare_parameter<std::string>("device_id", "");
    declare_parameter<std::string>("backend_url", "");
    declare_parameter<std::string>("source_profile", "unknown");
    declare_parameter<int>("fps", 15);
    declare_parameter<int>("bitrate_kbps", 700);
    declare_parameter<int>("heartbeat_interval_ms", 2000);
    declare_parameter<int>("frame_timeout_ms", 2000);
    declare_parameter<bool>("monitoring_enabled", false);
    declare_parameter<bool>("camera_enabled", true);
    declare_parameter<bool>("microphone_enabled", true);
  }

  MediaConfig read_config() const
  {
    MediaConfig config;
    config.image_topic = get_parameter("image_topic").as_string();
    config.camera_info_topic = get_parameter("camera_info_topic").as_string();
    config.odom_topic = get_parameter("odom_topic").as_string();
    config.audio_source = get_parameter("audio_source").as_string();
    config.audio_sink = get_parameter("audio_sink").as_string();
    config.encoder = get_parameter("encoder").as_string();
    config.device_id = get_parameter("device_id").as_string();
    config.backend_url = get_parameter("backend_url").as_string();
    config.source_profile = get_parameter("source_profile").as_string();
    config.fps = static_cast<int>(get_parameter("fps").as_int());
    config.bitrate_kbps = static_cast<int>(get_parameter("bitrate_kbps").as_int());
    config.heartbeat_interval_ms =
      static_cast<int>(get_parameter("heartbeat_interval_ms").as_int());
    config.frame_timeout_ms =
      static_cast<int>(get_parameter("frame_timeout_ms").as_int());
    config.monitoring_enabled = get_parameter("monitoring_enabled").as_bool();
    config.camera_enabled = get_parameter("camera_enabled").as_bool();
    config.microphone_enabled = get_parameter("microphone_enabled").as_bool();
    return config;
  }

  void on_image(const sensor_msgs::msg::Image::ConstSharedPtr message)
  {
    frames_received_.fetch_add(1);
    last_frame_time_ = std::chrono::steady_clock::now();
    has_frame_.store(true);

    if (!config_.camera_enabled) {
      return;
    }

#if HOMECAM_HAVE_GSTREAMER
    const bool dimensions_fit =
      message->width <= static_cast<std::uint32_t>(
      std::numeric_limits<int>::max()) &&
      message->height <= static_cast<std::uint32_t>(
      std::numeric_limits<int>::max());
    if (
      pipeline_ != nullptr &&
      (!dimensions_fit ||
      !video_format_matches(
        format_, message->encoding, static_cast<int>(message->width),
        static_cast<int>(message->height))))
    {
      RCLCPP_WARN(
        get_logger(),
        "Camera format changed from %dx%d %s to %ux%u %s; "
        "restarting the video pipeline",
        format_.width, format_.height, format_.ros_encoding.c_str(),
        message->width, message->height, message->encoding.c_str());
      stop_pipeline();
    }
    if (pipeline_ == nullptr &&
      std::chrono::steady_clock::now() < next_video_retry_)
    {
      return;
    }
    if (pipeline_ == nullptr) {
      if (!start_pipeline(*message)) {
        return;
      }
    }
    push_image(*message);
#else
    (void)message;
#endif
  }

#if HOMECAM_HAVE_GSTREAMER
  bool start_pipeline(const sensor_msgs::msg::Image & message)
  {
    try {
      if (
        message.width > static_cast<std::uint32_t>(
          std::numeric_limits<int>::max()) ||
        message.height > static_cast<std::uint32_t>(
          std::numeric_limits<int>::max()))
      {
        throw std::invalid_argument("video dimensions exceed the supported range");
      }
      format_ = video_format_from_ros(
        message.encoding, static_cast<int>(message.width),
        static_cast<int>(message.height));
      constexpr bool use_fake_sink = HOMECAM_HAVE_KVS == 0;
      const std::string description =
        build_video_pipeline(config_, format_, use_fake_sink);
      GError * error = nullptr;
      pipeline_ = gst_parse_launch(description.c_str(), &error);
      if (pipeline_ == nullptr || error != nullptr) {
        const std::string detail =
          error == nullptr ? "unknown GStreamer parse error" : error->message;
        if (error != nullptr) {
          g_error_free(error);
        }
        RCLCPP_ERROR(get_logger(), "Cannot create video pipeline: %s", detail.c_str());
        stop_pipeline();
        schedule_video_retry();
        return false;
      }
      video_source_ = gst_bin_get_by_name(GST_BIN(pipeline_), "ros_video_source");
      if (video_source_ == nullptr) {
        RCLCPP_ERROR(get_logger(), "Video pipeline has no ros_video_source appsrc");
        stop_pipeline();
        schedule_video_retry();
        return false;
      }
#if HOMECAM_HAVE_KVS
      video_encoded_sink_ =
        gst_bin_get_by_name(GST_BIN(pipeline_), "kvs_video_sink");
      if (video_encoded_sink_ == nullptr) {
        RCLCPP_ERROR(get_logger(), "Video pipeline has no kvs_video_sink appsink");
        stop_pipeline();
        schedule_video_retry();
        return false;
      }
      g_signal_connect(
        video_encoded_sink_, "new-sample",
        G_CALLBACK(MediaAgentNode::on_video_sample), this);
#endif
      const auto state = gst_element_set_state(pipeline_, GST_STATE_PLAYING);
      if (state == GST_STATE_CHANGE_FAILURE) {
        RCLCPP_ERROR(get_logger(), "GStreamer video pipeline failed to start");
        stop_pipeline();
        schedule_video_retry();
        return false;
      }
      RCLCPP_INFO(
        get_logger(), "Video encoder started: %dx%d %s -> H.264, %d FPS, %d kbps",
        format_.width, format_.height, format_.gst_format.c_str(),
        config_.fps, config_.bitrate_kbps);
      return true;
    } catch (const std::exception & exception) {
      RCLCPP_ERROR(get_logger(), "Cannot configure video pipeline: %s", exception.what());
      schedule_video_retry();
      return false;
    }
  }

  void push_image(const sensor_msgs::msg::Image & message)
  {
    const std::size_t packed_row =
      static_cast<std::size_t>(message.width) * format_.bytes_per_pixel;
    const std::size_t packed_size =
      packed_row * static_cast<std::size_t>(message.height);
    const std::size_t required_size =
      static_cast<std::size_t>(message.step) *
      static_cast<std::size_t>(message.height);
    if (message.step < packed_row || message.data.size() < required_size) {
      RCLCPP_WARN_THROTTLE(
        get_logger(), *get_clock(), 5000,
        "Dropping malformed image: step=%u, data=%zu, expected at least %zu",
        message.step, message.data.size(), required_size);
      return;
    }

    GstBuffer * buffer = gst_buffer_new_allocate(nullptr, packed_size, nullptr);
    if (buffer == nullptr) {
      RCLCPP_ERROR_THROTTLE(
        get_logger(), *get_clock(), 5000, "Cannot allocate GStreamer frame buffer");
      return;
    }

    GstMapInfo map;
    if (!gst_buffer_map(buffer, &map, GST_MAP_WRITE)) {
      gst_buffer_unref(buffer);
      RCLCPP_ERROR_THROTTLE(
        get_logger(), *get_clock(), 5000, "Cannot map GStreamer frame buffer");
      return;
    }
    for (std::size_t row = 0; row < message.height; ++row) {
      std::memcpy(
        map.data + row * packed_row,
        message.data.data() + row * static_cast<std::size_t>(message.step),
        packed_row);
    }
    gst_buffer_unmap(buffer, &map);

    const auto flow = gst_app_src_push_buffer(GST_APP_SRC(video_source_), buffer);
    if (flow != GST_FLOW_OK) {
      RCLCPP_WARN_THROTTLE(
        get_logger(), *get_clock(), 5000,
        "GStreamer rejected a camera frame (flow=%d)", static_cast<int>(flow));
    }
  }

  static GstFlowReturn on_video_sample(GstAppSink * sink, gpointer user_data)
  {
    return static_cast<MediaAgentNode *>(user_data)->forward_encoded_sample(
      sink, true);
  }

  static GstFlowReturn on_audio_sample(GstAppSink * sink, gpointer user_data)
  {
    return static_cast<MediaAgentNode *>(user_data)->forward_encoded_sample(
      sink, false);
  }

  GstFlowReturn forward_encoded_sample(GstAppSink * sink, const bool is_video)
  {
    GstSample * sample = gst_app_sink_pull_sample(sink);
    if (sample == nullptr) {
      return GST_FLOW_EOS;
    }
    GstBuffer * buffer = gst_sample_get_buffer(sample);
    GstMapInfo map;
    if (buffer == nullptr || !gst_buffer_map(buffer, &map, GST_MAP_READ)) {
      gst_sample_unref(sample);
      return GST_FLOW_ERROR;
    }

    EncodedFrame frame;
    frame.payload.assign(map.data, map.data + map.size);
    std::int64_t pipeline_timestamp_ns = 0;
    if (GST_BUFFER_PTS_IS_VALID(buffer)) {
      pipeline_timestamp_ns =
        GST_BUFFER_PTS(buffer) >
        static_cast<GstClockTime>(std::numeric_limits<std::int64_t>::max()) ?
        std::numeric_limits<std::int64_t>::max() :
        static_cast<std::int64_t>(GST_BUFFER_PTS(buffer));
    }
    frame.presentation_time_ns = media_timeline_.stamp(
      pipeline_timestamp_ns, steady_now_ns());
    frame.key_frame =
      !GST_BUFFER_FLAG_IS_SET(buffer, GST_BUFFER_FLAG_DELTA_UNIT);
    gst_buffer_unmap(buffer, &map);
    gst_sample_unref(sample);

    if (shutting_down_.load()) {
      return GST_FLOW_FLUSHING;
    }
    if (!media_generation_allows_io(
        active_transport_generation_.load(),
        session_generation_.load(),
        media_permitted_.load()))
    {
      return GST_FLOW_OK;
    }
    std::string error;
    bool accepted = false;
    {
      std::unique_lock<std::mutex> lock(
        transport_mutex_, std::try_to_lock);
      if (!lock.owns_lock()) {
        return GST_FLOW_OK;
      }
      if (shutting_down_.load()) {
        return GST_FLOW_FLUSHING;
      }
      if (!media_generation_allows_io(
          active_transport_generation_.load(),
          session_generation_.load(),
          media_permitted_.load()))
      {
        return GST_FLOW_OK;
      }
      accepted = is_video ?
        transport_->push_h264(frame, &error) :
        transport_->push_opus(frame, &error);
    }
    if (!accepted) {
      if (
        error == "KVS signaling is ready but no peer is connected" ||
        error == "KVS peer exists but SRTP is not ready")
      {
        RCLCPP_DEBUG_THROTTLE(
          get_logger(), *get_clock(), 30000,
          "Encoded %s is waiting for a remote peer: %s",
          is_video ? "H.264" : "Opus", error.c_str());
      } else {
        RCLCPP_WARN_THROTTLE(
          get_logger(), *get_clock(), 10000,
          "Encoded %s was not accepted by the transport: %s",
          is_video ? "H.264" : "Opus", error.c_str());
      }
    }
    return GST_FLOW_OK;
  }

  bool start_audio_capture()
  {
    stop_audio_capture();
    constexpr bool use_fake_sink = HOMECAM_HAVE_KVS == 0;
    const std::string description = build_audio_capture_pipeline(
      config_, !config_.microphone_enabled, use_fake_sink);
    GError * error = nullptr;
    audio_capture_pipeline_ = gst_parse_launch(description.c_str(), &error);
    if (audio_capture_pipeline_ == nullptr || error != nullptr) {
      const std::string detail =
        error == nullptr ? "unknown GStreamer parse error" : error->message;
      if (error != nullptr) {
        g_error_free(error);
      }
      RCLCPP_ERROR(
        get_logger(), "Cannot create audio capture pipeline: %s", detail.c_str());
      stop_audio_capture();
      schedule_audio_retry();
      return false;
    }
#if HOMECAM_HAVE_KVS
    audio_encoded_sink_ =
      gst_bin_get_by_name(GST_BIN(audio_capture_pipeline_), "kvs_audio_sink");
    if (audio_encoded_sink_ == nullptr) {
      RCLCPP_ERROR(get_logger(), "Audio pipeline has no kvs_audio_sink appsink");
      stop_audio_capture();
      schedule_audio_retry();
      return false;
    }
    g_signal_connect(
      audio_encoded_sink_, "new-sample",
      G_CALLBACK(MediaAgentNode::on_audio_sample), this);
#endif
    const auto state =
      gst_element_set_state(audio_capture_pipeline_, GST_STATE_PLAYING);
    if (state == GST_STATE_CHANGE_FAILURE) {
      RCLCPP_ERROR(get_logger(), "GStreamer audio capture pipeline failed to start");
      stop_audio_capture();
      schedule_audio_retry();
      return false;
    }
    RCLCPP_INFO(
      get_logger(), "Audio capture started with %s Opus source",
      config_.microphone_enabled ? config_.audio_source.c_str() : "silent");
    return true;
  }

  void stop_audio_capture()
  {
#if HOMECAM_HAVE_KVS
    if (audio_encoded_sink_ != nullptr) {
      gst_object_unref(audio_encoded_sink_);
      audio_encoded_sink_ = nullptr;
    }
#endif
    if (audio_capture_pipeline_ != nullptr) {
      gst_element_set_state(audio_capture_pipeline_, GST_STATE_NULL);
      gst_object_unref(audio_capture_pipeline_);
      audio_capture_pipeline_ = nullptr;
    }
  }

  void push_ptt_audio(const EncodedFrame & frame)
  {
    std::lock_guard<std::mutex> lock(ptt_mutex_);
    if (shutting_down_.load()) {
      return;
    }
    if (ptt_playback_pipeline_ != nullptr &&
      !pipeline_bus_healthy(ptt_playback_pipeline_, "PTT playback"))
    {
      stop_ptt_playback_locked();
      next_ptt_retry_ = std::chrono::steady_clock::now() + 3s;
    }
    if (ptt_source_ == nullptr &&
      std::chrono::steady_clock::now() < next_ptt_retry_)
    {
      return;
    }
    if (ptt_source_ == nullptr && !start_ptt_playback_locked()) {
      return;
    }
    GstBuffer * buffer =
      gst_buffer_new_allocate(nullptr, frame.payload.size(), nullptr);
    if (buffer == nullptr) {
      return;
    }
    gst_buffer_fill(buffer, 0, frame.payload.data(), frame.payload.size());
    GST_BUFFER_PTS(buffer) =
      static_cast<GstClockTime>(std::max<std::int64_t>(
        frame.presentation_time_ns, 0));
    const auto flow = gst_app_src_push_buffer(GST_APP_SRC(ptt_source_), buffer);
    if (flow != GST_FLOW_OK) {
      RCLCPP_WARN_THROTTLE(
        get_logger(), *get_clock(), 5000,
        "PTT playback rejected Opus frame (flow=%d)", static_cast<int>(flow));
    }
  }

  bool start_ptt_playback_locked()
  {
    const std::string description = build_audio_playback_pipeline(config_);
    GError * error = nullptr;
    ptt_playback_pipeline_ = gst_parse_launch(description.c_str(), &error);
    if (ptt_playback_pipeline_ == nullptr || error != nullptr) {
      const std::string detail =
        error == nullptr ? "unknown GStreamer parse error" : error->message;
      if (error != nullptr) {
        g_error_free(error);
      }
      RCLCPP_ERROR(
        get_logger(), "Cannot create PTT playback pipeline: %s", detail.c_str());
      stop_ptt_playback_locked();
      next_ptt_retry_ = std::chrono::steady_clock::now() + 3s;
      return false;
    }
    ptt_source_ =
      gst_bin_get_by_name(GST_BIN(ptt_playback_pipeline_), "ptt_audio_source");
    if (ptt_source_ == nullptr) {
      RCLCPP_ERROR(get_logger(), "PTT pipeline has no ptt_audio_source appsrc");
      stop_ptt_playback_locked();
      next_ptt_retry_ = std::chrono::steady_clock::now() + 3s;
      return false;
    }
    const auto state =
      gst_element_set_state(ptt_playback_pipeline_, GST_STATE_PLAYING);
    if (state == GST_STATE_CHANGE_FAILURE) {
      RCLCPP_ERROR(get_logger(), "GStreamer PTT playback pipeline failed to start");
      stop_ptt_playback_locked();
      next_ptt_retry_ = std::chrono::steady_clock::now() + 3s;
      return false;
    }
    return true;
  }

  void stop_ptt_playback()
  {
    std::lock_guard<std::mutex> lock(ptt_mutex_);
    stop_ptt_playback_locked();
  }

  void stop_ptt_playback_locked()
  {
    if (ptt_source_ != nullptr) {
      gst_app_src_end_of_stream(GST_APP_SRC(ptt_source_));
      gst_object_unref(ptt_source_);
      ptt_source_ = nullptr;
    }
    if (ptt_playback_pipeline_ != nullptr) {
      gst_element_set_state(ptt_playback_pipeline_, GST_STATE_NULL);
      gst_object_unref(ptt_playback_pipeline_);
      ptt_playback_pipeline_ = nullptr;
    }
  }

  void stop_pipeline()
  {
#if HOMECAM_HAVE_KVS
    if (video_encoded_sink_ != nullptr) {
      gst_object_unref(video_encoded_sink_);
      video_encoded_sink_ = nullptr;
    }
#endif
    if (video_source_ != nullptr) {
      gst_app_src_end_of_stream(GST_APP_SRC(video_source_));
      gst_object_unref(video_source_);
      video_source_ = nullptr;
    }
    if (pipeline_ != nullptr) {
      gst_element_set_state(pipeline_, GST_STATE_NULL);
      gst_object_unref(pipeline_);
      pipeline_ = nullptr;
    }
  }

  void schedule_video_retry()
  {
    next_video_retry_ = std::chrono::steady_clock::now() + 3s;
  }

  void schedule_audio_retry()
  {
    next_audio_retry_ = std::chrono::steady_clock::now() + 3s;
  }

  bool pipeline_bus_healthy(GstElement * element, const char * const label)
  {
    if (element == nullptr) {
      return false;
    }
    GstBus * bus = gst_element_get_bus(element);
    if (bus == nullptr) {
      RCLCPP_ERROR(get_logger(), "%s pipeline has no GstBus", label);
      return false;
    }

    bool healthy = true;
    while (GstMessage * message = gst_bus_pop_filtered(
        bus, static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS)))
    {
      if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR) {
        GError * error = nullptr;
        gchar * debug = nullptr;
        gst_message_parse_error(message, &error, &debug);
        if (debug != nullptr) {
          RCLCPP_ERROR(
            get_logger(), "%s pipeline error: %s (%s)",
            label, error == nullptr ? "unknown error" : error->message, debug);
          g_free(debug);
        } else {
          RCLCPP_ERROR(
            get_logger(), "%s pipeline error: %s",
            label, error == nullptr ? "unknown error" : error->message);
        }
        if (error != nullptr) {
          g_error_free(error);
        }
      } else {
        RCLCPP_WARN(get_logger(), "%s pipeline reached EOS", label);
      }
      healthy = false;
      gst_message_unref(message);
    }
    gst_object_unref(bus);
    return healthy;
  }

  void maintain_gstreamer_pipelines()
  {
    const auto now = std::chrono::steady_clock::now();
    if (pipeline_ != nullptr && !pipeline_bus_healthy(pipeline_, "video")) {
      stop_pipeline();
      schedule_video_retry();
    }
    if (audio_capture_pipeline_ != nullptr &&
      !pipeline_bus_healthy(audio_capture_pipeline_, "audio capture"))
    {
      stop_audio_capture();
      schedule_audio_retry();
    }
    if (audio_capture_pipeline_ == nullptr &&
      !shutting_down_.load() && now >= next_audio_retry_)
    {
      start_audio_capture();
    }
  }
#endif

  bool camera_is_healthy() const
  {
    if (!config_.camera_enabled) {
      return true;
    }
    if (!has_frame_.load()) {
      return false;
    }
    const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - last_frame_time_);
    return age.count() <= config_.frame_timeout_ms;
  }

  static std::int64_t steady_now_ns()
  {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
  }

  bool detector_is_healthy() const
  {
    const auto received_at = detector_health_received_at_ns_.load();
    if (!detector_reported_healthy_.load() || received_at == 0) {
      return false;
    }
    constexpr std::int64_t timeout_ns = 3'000'000'000;
    return steady_now_ns() - received_at <= timeout_ns;
  }

  static std::int64_t unix_now_ms()
  {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count();
  }

  enum class SessionOperation
  {
    kStartOrRefresh,
    kStop
  };

  struct SessionOutcome
  {
    SessionOperation operation{SessionOperation::kStartOrRefresh};
    bool success{false};
    bool transport_replaced{false};
    bool connected_peer_preserved{false};
    bool session_created{false};
    bool desired_available{false};
    bool backend_session_state_known{false};
    bool backend_session_open{false};
    bool permanent_failure{false};
    bool fail_closed{false};
    std::uint64_t request_generation{0};
    DeviceSessionResult session;
    std::string cleanup_session_id;
    std::string closed_session_id;
    std::string error;
  };

  static void append_session_error(
    std::string * const error,
    const std::string & detail)
  {
    if (detail.empty()) {
      return;
    }
    if (!error->empty()) {
      *error += "; ";
    }
    *error += detail;
  }

  static void clear_session_credentials(
    SessionCredentials * const credentials)
  {
    std::fill(
      credentials->access_key_id.begin(),
      credentials->access_key_id.end(), '\0');
    std::fill(
      credentials->secret_access_key.begin(),
      credentials->secret_access_key.end(), '\0');
    std::fill(
      credentials->session_token.begin(),
      credentials->session_token.end(), '\0');
    credentials->access_key_id.clear();
    credentials->secret_access_key.clear();
    credentials->session_token.clear();
    credentials->expires_at_unix_ms = 0;
  }

  void clear_local_session_state()
  {
    active_transport_generation_.store(0U);
    active_stream_mode_ = "idle";
    active_session_expires_at_ms_ = 0;
    active_session_id_.clear();
    cleanup_session_id_.clear();
    backend_session_may_be_open_ = false;
  }

  void fail_closed_active_session(const std::string & reason)
  {
    session_generation_.fetch_add(1U);
    active_transport_generation_.store(0U);
    {
      std::lock_guard<std::mutex> lock(transport_mutex_);
      transport_->stop();
    }
    clear_local_session_state();
    RCLCPP_ERROR(
      get_logger(), "Device media session stopped fail-closed: %s",
      reason.c_str());
  }

  void schedule_session_retry(const std::string & error)
  {
    ++session_failure_count_;
    const int exponent = std::min(session_failure_count_ - 1, 5);
    const int delay_seconds = std::min(1 << exponent, 30);
    next_session_attempt_ =
      std::chrono::steady_clock::now() +
      std::chrono::seconds(delay_seconds);
    RCLCPP_WARN(
      get_logger(),
      "Device media session failed; retrying in %d s: %s",
      delay_seconds, error.c_str());
  }

  void collect_session_result()
  {
    if (!session_future_.valid() ||
      session_future_.wait_for(0ms) != std::future_status::ready)
    {
      return;
    }

    SessionOutcome outcome;
    try {
      outcome = session_future_.get();
    } catch (const std::exception & exception) {
      outcome.success = false;
      outcome.error =
        std::string("device session worker exception: ") + exception.what();
    }

    if (outcome.fail_closed) {
      clear_session_credentials(&outcome.session.lease.credentials);
      clear_local_session_state();
    }

    if (
      outcome.operation == SessionOperation::kStartOrRefresh &&
      outcome.success &&
      !media_generation_allows_io(
        outcome.request_generation,
        session_generation_.load(),
        media_permitted_.load()))
    {
      {
        std::lock_guard<std::mutex> lock(transport_mutex_);
        transport_->stop();
      }
      active_transport_generation_.store(0U);
      outcome.success = false;
      outcome.transport_replaced = true;
      outcome.cleanup_session_id = outcome.session.session_id;
      outcome.backend_session_state_known = true;
      outcome.backend_session_open = true;
      append_session_error(
        &outcome.error,
        "discarded a stale media session after desired-state change");
    }

    if (outcome.session_created && outcome.backend_session_open) {
      active_session_id_ = outcome.session.session_id;
    }
    if (!outcome.cleanup_session_id.empty()) {
      cleanup_session_id_ = outcome.cleanup_session_id;
    }
    if (outcome.permanent_failure) {
      session_permanent_failure_ = true;
    }
    if (outcome.backend_session_state_known) {
      backend_session_may_be_open_ = outcome.backend_session_open;
    }
    if (
      outcome.session_created &&
      outcome.backend_session_state_known &&
      !outcome.backend_session_open)
    {
      // A successful POST replaced any older backend session. If the new
      // session was then closed, no previously remembered ID remains current.
      active_session_id_.clear();
      cleanup_session_id_.clear();
    }
    if (!outcome.backend_session_open &&
      !outcome.closed_session_id.empty())
    {
      if (active_session_id_ == outcome.closed_session_id) {
        active_session_id_.clear();
      }
      if (cleanup_session_id_ == outcome.closed_session_id) {
        cleanup_session_id_.clear();
      }
    }

    if (outcome.operation == SessionOperation::kStop) {
      active_stream_mode_ = "idle";
      active_session_expires_at_ms_ = 0;
      active_transport_generation_.store(0U);
      if (!outcome.success) {
        if (outcome.permanent_failure) {
          RCLCPP_ERROR(
            get_logger(),
            "Backend session cleanup failed permanently; media remains "
            "fail-closed until restart: %s",
            outcome.error.c_str());
        } else {
          schedule_session_retry(
            "media stopped, but backend session close failed: " +
            outcome.error);
        }
      } else {
        session_failure_count_ = 0;
        next_session_attempt_ =
          std::chrono::steady_clock::time_point::min();
        RCLCPP_INFO(get_logger(), "Device media session stopped");
      }
      return;
    }

    if (
      outcome.desired_available &&
      outcome.request_generation == session_generation_.load() &&
      !shutting_down_.load())
    {
      apply_desired_settings(outcome.session.desired, outcome.success);
    }

    if (!outcome.success) {
      if (outcome.transport_replaced) {
        active_stream_mode_ = "idle";
        active_session_expires_at_ms_ = 0;
        active_transport_generation_.store(0U);
      }
      if (outcome.permanent_failure) {
        RCLCPP_ERROR(
          get_logger(),
          "Device session failed permanently; media remains fail-closed "
          "until restart: %s",
          outcome.error.c_str());
      } else {
        schedule_session_retry(outcome.error);
      }
      return;
    }

    backend_session_may_be_open_ = true;
    active_session_id_ = outcome.session.session_id;
    cleanup_session_id_.clear();
    session_failure_count_ = 0;
    next_session_attempt_ = std::chrono::steady_clock::time_point::min();
    if (outcome.connected_peer_preserved) {
      // The backend lease was renewed, but the currently connected P2P
      // transport deliberately keeps its original credential deadline. That
      // deadline remains the bounded safety trigger for a later replacement.
      RCLCPP_INFO(
        get_logger(),
        "Backend session renewed without replacing the connected P2P peer");
      clear_session_credentials(&outcome.session.lease.credentials);
      return;
    }
    active_stream_mode_ =
      outcome.session.lease.mode == SessionMode::kStorage ?
      "storage" : "p2p";
    active_session_expires_at_ms_ =
      outcome.session.lease.credentials.expires_at_unix_ms;
    active_transport_generation_.store(session_generation_.load());
    RCLCPP_INFO(
      get_logger(),
      "Device media session %s started (refresh before %ld)",
      active_stream_mode_.c_str(),
      static_cast<long>(active_session_expires_at_ms_));
  }

  void launch_session_start_or_refresh(
    const bool preserve_connected_p2p_peer = false)
  {
    DeviceSessionClient * const client = session_client_.get();
    KvsTransport * const transport = transport_.get();
    std::mutex * const transport_mutex = &transport_mutex_;
    std::atomic<bool> * const shutting_down = &shutting_down_;
    std::atomic<bool> * const media_permitted = &media_permitted_;
    std::atomic<std::uint64_t> * const session_generation =
      &session_generation_;
    std::atomic<std::uint64_t> * const active_transport_generation =
      &active_transport_generation_;
    const std::uint64_t request_generation = session_generation_.load();
    const std::string expected_session_id = active_session_id_;
    const bool expected_camera_enabled = config_.camera_enabled;
    const bool expected_microphone_enabled = config_.microphone_enabled;
    const bool expected_monitoring_enabled = config_.monitoring_enabled;
    try {
      session_future_ = std::async(
        std::launch::async,
        [
          client, transport, transport_mutex, shutting_down,
          media_permitted, session_generation,
          active_transport_generation, request_generation,
          expected_session_id, expected_camera_enabled,
          expected_microphone_enabled, expected_monitoring_enabled,
          preserve_connected_p2p_peer
        ]() {
          SessionOutcome outcome;
          outcome.operation = SessionOperation::kStartOrRefresh;
          outcome.request_generation = request_generation;
          try {
            long http_status = 0;
            if (!client->create(
              MediaAgentNode::unix_now_ms(),
              &outcome.session, &outcome.error, &http_status))
            {
              if (session_create_requires_fail_closed(http_status)) {
                session_generation->fetch_add(1U);
                active_transport_generation->store(0U);
                {
                  std::lock_guard<std::mutex> lock(*transport_mutex);
                  transport->stop();
                }
                outcome.transport_replaced = true;
                outcome.permanent_failure = true;
                outcome.fail_closed = true;
              }
              return outcome;
            }
            outcome.session_created = true;
            outcome.desired_available = true;
            outcome.backend_session_state_known = true;
            outcome.backend_session_open = true;
            const bool backend_camera_enabled =
            outcome.session.desired.camera_enabled.value_or(false);
            const bool desired_matches_request =
            outcome.session.desired.camera_enabled ==
            expected_camera_enabled &&
            outcome.session.desired.microphone_enabled ==
            expected_microphone_enabled &&
            outcome.session.desired.monitoring_enabled ==
            expected_monitoring_enabled;
            const auto request_is_current = [&]() {
              return !shutting_down->load() &&
              backend_camera_enabled &&
              media_generation_allows_io(
                request_generation,
                session_generation->load(),
                media_permitted->load());
            };
            if (!request_is_current()) {
              std::lock_guard<std::mutex> lock(*transport_mutex);
              active_transport_generation->store(0U);
              transport->stop();
              outcome.transport_replaced = true;
              outcome.error =
              "device session cancelled by a newer desired state";
            } else {
              std::lock_guard<std::mutex> lock(*transport_mutex);
              if (request_is_current()) {
                const bool keep_connected_peer =
                preserve_connected_p2p_peer &&
                outcome.session.lease.mode ==
                SessionMode::kPeerToPeer &&
                outcome.session.session_id == expected_session_id &&
                desired_matches_request &&
                transport->peer_connected();
                if (keep_connected_peer) {
                  // A viewer connected while the renewal request was in
                  // flight. Keep that peer and retain the original transport
                  // deadline; the main loop will refresh after disconnect or
                  // at the bounded pre-expiry safety boundary.
                  outcome.success = true;
                  outcome.connected_peer_preserved = true;
                } else {
                  // The POST response can tighten microphone/camera privacy.
                  // Close both directions before replacing the transport;
                  // only the main executor re-opens this generation after
                  // applying desired pipelines.
                  active_transport_generation->store(0U);
                  outcome.transport_replaced = true;
                  outcome.success =
                  transport->start(outcome.session.lease, &outcome.error);
                  if (outcome.success && !request_is_current()) {
                    transport->stop();
                    active_transport_generation->store(0U);
                    outcome.success = false;
                    outcome.error =
                    "device session cancelled while signaling connected";
                  }
                }
              } else {
                active_transport_generation->store(0U);
                transport->stop();
                outcome.transport_replaced = true;
                outcome.error =
                "device session cancelled before signaling start";
              }
            }
          } catch (const std::exception & exception) {
            outcome.error =
            std::string("device session worker exception: ") +
            exception.what();
          }
          if (outcome.backend_session_open && !outcome.success) {
            try {
              const SessionCloseResult close_result =
              client->close(outcome.session.session_id);
              if (close_result.terminal()) {
                outcome.backend_session_open = false;
                outcome.closed_session_id = outcome.session.session_id;
              } else {
                append_session_error(
                  &outcome.error,
                  "backend session cleanup failed: " +
                  close_result.error);
                if (
                  close_result.disposition ==
                  SessionCloseDisposition::kRetryableFailure)
                {
                  outcome.cleanup_session_id =
                  outcome.session.session_id;
                } else {
                  outcome.permanent_failure = true;
                }
              }
            } catch (const std::exception & exception) {
              outcome.cleanup_session_id = outcome.session.session_id;
              append_session_error(
                &outcome.error,
                std::string("backend session cleanup exception: ") +
                exception.what());
            }
          }
          return outcome;
        });
    } catch (const std::exception & exception) {
      schedule_session_retry(
        std::string("cannot start device session worker: ") +
        exception.what());
    }
  }

  void launch_session_stop(const std::string & session_id)
  {
    DeviceSessionClient * const client = session_client_.get();
    KvsTransport * const transport = transport_.get();
    std::mutex * const transport_mutex = &transport_mutex_;
    try {
      session_future_ = std::async(
        std::launch::async,
        [client, transport, transport_mutex, session_id]() {
          SessionOutcome outcome;
          outcome.operation = SessionOperation::kStop;
          outcome.closed_session_id = session_id;
          try {
            {
              std::lock_guard<std::mutex> lock(*transport_mutex);
              transport->stop();
              outcome.transport_replaced = true;
            }
            if (!is_valid_session_id(session_id)) {
              outcome.permanent_failure = true;
              outcome.error =
              "refusing to close a backend session without a UUIDv4";
              return outcome;
            }
            const SessionCloseResult close_result =
            client->close(session_id);
            outcome.success = close_result.terminal();
            outcome.error = close_result.error;
            if (close_result.terminal()) {
              outcome.backend_session_state_known = true;
              outcome.backend_session_open = false;
            } else if (
              close_result.disposition ==
              SessionCloseDisposition::kRetryableFailure)
            {
              outcome.backend_session_state_known = true;
              outcome.backend_session_open = true;
              outcome.cleanup_session_id = session_id;
            } else {
              outcome.backend_session_state_known = true;
              outcome.backend_session_open = true;
              outcome.permanent_failure = true;
            }
          } catch (const std::exception & exception) {
            outcome.error =
            std::string("device session stop worker exception: ") +
            exception.what();
          }
          return outcome;
        });
    } catch (const std::exception & exception) {
      schedule_session_retry(
        std::string("cannot start device session stop worker: ") +
        exception.what());
    }
  }

  bool media_inputs_ready() const
  {
#if HOMECAM_HAVE_KVS && HOMECAM_HAVE_GSTREAMER
    return config_.camera_enabled &&
           camera_is_healthy() &&
           pipeline_ != nullptr &&
           audio_capture_pipeline_ != nullptr;
#else
    return false;
#endif
  }

  void maintain_device_session()
  {
    collect_session_result();
#if !(HOMECAM_HAVE_KVS && HOMECAM_HAVE_GSTREAMER)
    return;
#else
    const std::int64_t now = unix_now_ms();
    if (
      active_stream_mode_ != "idle" &&
      session_lease_expired(now, active_session_expires_at_ms_))
    {
      fail_closed_active_session("temporary device session lease expired");
    }

    if (!transport_->implemented() || !session_client_->available() ||
      shutting_down_.load() || session_future_.valid() ||
      session_permanent_failure_)
    {
      return;
    }

    const auto now_steady = std::chrono::steady_clock::now();
    if (now_steady < next_session_attempt_) {
      return;
    }

    if (!cleanup_session_id_.empty()) {
      launch_session_stop(cleanup_session_id_);
      return;
    }

    if (!media_inputs_ready()) {
      if (active_stream_mode_ != "idle" ||
        backend_session_may_be_open_ ||
        !active_session_id_.empty())
      {
        launch_session_stop(active_session_id_);
      }
      return;
    }

    if (active_stream_mode_ == "idle" &&
      backend_session_may_be_open_)
    {
      if (!active_session_id_.empty()) {
        launch_session_stop(active_session_id_);
      }
      return;
    }

    bool peer_connected = false;
    if (active_stream_mode_ != "idle") {
      std::lock_guard<std::mutex> lock(transport_mutex_);
      if (transport_->restart_required()) {
        active_session_expires_at_ms_ = 0;
        RCLCPP_WARN_THROTTLE(
          get_logger(), *get_clock(), 10000,
          "KVS cleanup/signaling worker exited; refreshing the device session");
      } else {
        peer_connected = transport_->peer_connected();
      }
    }

    const std::string wanted_mode =
      config_.monitoring_enabled ? "storage" : "p2p";
    constexpr std::int64_t refresh_margin_ms = 300'000;
    constexpr std::int64_t connected_peer_safety_margin_ms = 60'000;
    const bool no_session = active_stream_mode_ == "idle";
    const bool mode_changed =
      !no_session && active_stream_mode_ != wanted_mode;
    if (no_session || mode_changed) {
      launch_session_start_or_refresh();
      return;
    }
    const SessionMode session_mode =
      active_stream_mode_ == "storage" ?
      SessionMode::kStorage : SessionMode::kPeerToPeer;
    const SessionRefreshDecision refresh_decision = decide_session_refresh(
      session_mode,
      peer_connected,
      now,
      active_session_expires_at_ms_,
      refresh_margin_ms,
      connected_peer_safety_margin_ms);
    if (refresh_decision == SessionRefreshDecision::kFailClosed) {
      fail_closed_active_session("temporary device session lease expired");
      return;
    }
    if (refresh_decision == SessionRefreshDecision::kNotDue) {
      return;
    }
    if (
      refresh_decision ==
      SessionRefreshDecision::kDeferForConnectedPeer)
    {
      RCLCPP_DEBUG_THROTTLE(
        get_logger(), *get_clock(), 30000,
        "Deferring P2P credential refresh while a viewer is connected");
      return;
    }
    if (
      refresh_decision ==
      SessionRefreshDecision::kForceBeforeExpiry)
    {
      RCLCPP_WARN(
        get_logger(),
        "Refreshing connected P2P session inside the 60 s credential "
        "safety window");
      launch_session_start_or_refresh();
      return;
    }
    const bool preserve_peer_if_it_connects_during_request =
      session_mode == SessionMode::kPeerToPeer &&
      !peer_connected &&
      active_session_expires_at_ms_ > now;
    launch_session_start_or_refresh(
      preserve_peer_if_it_connects_during_request);
#endif
  }

  void maintain_media_runtime()
  {
    // Heartbeats keep flowing while KVS signaling is in progress, and their
    // desired state is collected at the 250 ms runtime cadence.
    collect_heartbeat_result();
#if HOMECAM_HAVE_GSTREAMER
    maintain_gstreamer_pipelines();
#endif
    maintain_device_session();
  }

  void publish_heartbeat()
  {
    collect_heartbeat_result();
    HeartbeatStatus status;
    status.device_id = config_.device_id;
    status.camera_enabled = config_.camera_enabled;
    status.microphone_enabled = config_.microphone_enabled;
    status.monitoring_enabled = config_.monitoring_enabled;
    status.camera_healthy = camera_is_healthy();
    bool transport_running = false;
    {
      // initSignaling can block while the network retries. Heartbeat must
      // remain independent so a camera/privacy change can cancel that start.
      std::unique_lock<std::mutex> lock(
        transport_mutex_, std::try_to_lock);
      if (lock.owns_lock()) {
        transport_running = transport_->running();
      }
      status.media_healthy =
        status.camera_healthy &&
        transport_->implemented() &&
        transport_running;
    }
    status.detector_healthy = detector_is_healthy();
    status.source_profile = config_.source_profile;
    status.image_topic = config_.image_topic;
    status.stream_mode =
      transport_running ? active_stream_mode_ : "idle";
    status.frames_received = frames_received_.load();
#if HOMECAM_HAVE_GSTREAMER
    const std::string local_state =
      pipeline_ == nullptr ? "waiting_for_camera" : "encoding_local";
#else
    const std::string local_state = "health_only";
#endif

    if (!status.camera_healthy) {
      RCLCPP_WARN_THROTTLE(
        get_logger(), *get_clock(), 10000,
        "Camera is unhealthy: no recent frame received on %s",
        config_.image_topic.c_str());
    }
    RCLCPP_DEBUG(
      get_logger(),
      "health frames=%lu camera_info=%s odom=%s state=%s",
      static_cast<unsigned long>(status.frames_received),
      camera_info_received_.load() ? "yes" : "no",
      odom_received_.load() ? "yes" : "no",
      local_state.c_str());

    if (heartbeat_client_->available() &&
      !heartbeat_future_.valid())
    {
      HeartbeatClient * const client = heartbeat_client_.get();
      try {
        heartbeat_future_ = std::async(
          std::launch::async,
          [client, status]() {
            HeartbeatOutcome outcome;
            try {
              outcome.success =
              client->post(status, &outcome.desired, &outcome.error);
            } catch (const std::exception & exception) {
              outcome.success = false;
              outcome.error =
              std::string("heartbeat worker exception: ") + exception.what();
            }
            return outcome;
          });
      } catch (const std::exception & exception) {
        RCLCPP_ERROR(
          get_logger(), "Cannot start heartbeat worker: %s", exception.what());
      }
    }
  }

  struct HeartbeatOutcome
  {
    bool success{false};
    DesiredDeviceSettings desired;
    std::string error;
  };

  void collect_heartbeat_result()
  {
    if (!heartbeat_future_.valid() ||
      heartbeat_future_.wait_for(0ms) != std::future_status::ready)
    {
      return;
    }
    HeartbeatOutcome outcome;
    try {
      outcome = heartbeat_future_.get();
    } catch (const std::exception & exception) {
      RCLCPP_WARN(
        get_logger(), "Heartbeat worker failed: %s", exception.what());
      return;
    }
    if (!outcome.success) {
      RCLCPP_WARN_THROTTLE(
        get_logger(), *get_clock(), 10000,
        "Device heartbeat failed: %s", outcome.error.c_str());
      return;
    }
    apply_desired_settings(outcome.desired, false);
  }

  void apply_desired_settings(
    const DesiredDeviceSettings & desired,
    const bool transport_matches_desired)
  {
    const bool first_confirmed_state = !desired_state_confirmed_;
    desired_state_confirmed_ = true;
    const bool camera_changed =
      desired.camera_enabled.has_value() &&
      *desired.camera_enabled != config_.camera_enabled;
    const bool microphone_changed =
      desired.microphone_enabled.has_value() &&
      *desired.microphone_enabled != config_.microphone_enabled;
    const bool monitoring_changed =
      desired.monitoring_enabled.has_value() &&
      *desired.monitoring_enabled != config_.monitoring_enabled;
    const bool media_state_changed =
      camera_changed || microphone_changed || monitoring_changed;
    if (media_state_changed) {
      // Invalidate both outbound frames and inbound PTT before changing any
      // pipeline. A session worker can only re-enable I/O for this generation.
      session_generation_.fetch_add(1U);
    }

    bool detector_state_may_have_changed = first_confirmed_state;
    if (camera_changed) {
      config_.camera_enabled = *desired.camera_enabled;
      media_permitted_.store(config_.camera_enabled);
      detector_state_may_have_changed = true;
#if HOMECAM_HAVE_GSTREAMER
      if (!config_.camera_enabled) {
        stop_pipeline();
        stop_ptt_playback();
      }
#endif
      RCLCPP_INFO(
        get_logger(), "Backend desired camera state applied: %s",
        config_.camera_enabled ? "on" : "off");
    }
    if (microphone_changed) {
      config_.microphone_enabled = *desired.microphone_enabled;
#if HOMECAM_HAVE_GSTREAMER
      start_audio_capture();
#endif
      RCLCPP_INFO(
        get_logger(), "Backend desired microphone state applied: %s",
        config_.microphone_enabled ? "on" : "silent-track");
    }
    if (monitoring_changed) {
      config_.monitoring_enabled = *desired.monitoring_enabled;
      detector_state_may_have_changed = true;
      RCLCPP_INFO(
        get_logger(), "Backend desired monitoring state applied: %s",
        config_.monitoring_enabled ? "on" : "off");
    }
    if (
      media_state_changed &&
      !transport_matches_desired &&
      active_stream_mode_ != "idle")
    {
      // Force a fresh backend snapshot even when only microphone state changed.
      active_session_expires_at_ms_ = 0;
    }
    if (detector_state_may_have_changed) {
      publish_monitoring_state();
    }
  }

  void publish_monitoring_state()
  {
    std_msgs::msg::Bool message;
    message.data =
      detector_monitoring_enabled(config_, desired_state_confirmed_);
    monitoring_publisher_->publish(message);
  }

  MediaConfig config_;
  std::unique_ptr<HeartbeatClient> heartbeat_client_;
  std::unique_ptr<DeviceSessionClient> session_client_;
  std::unique_ptr<KvsTransport> transport_;
  std::future<HeartbeatOutcome> heartbeat_future_;
  std::future<SessionOutcome> session_future_;
  std::mutex transport_mutex_;
  std::atomic<bool> shutting_down_{false};
  std::atomic<bool> media_permitted_{false};
  std::atomic<std::uint64_t> session_generation_{1U};
  std::atomic<std::uint64_t> active_transport_generation_{0U};
  std::string active_stream_mode_{"idle"};
  std::string active_session_id_;
  std::string cleanup_session_id_;
  std::int64_t active_session_expires_at_ms_{0};
  bool backend_session_may_be_open_{false};
  bool desired_state_confirmed_{false};
  bool session_permanent_failure_{false};
  int session_failure_count_{0};
  std::chrono::steady_clock::time_point next_session_attempt_{
    std::chrono::steady_clock::time_point::min()};
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr image_subscription_;
  rclcpp::Subscription<sensor_msgs::msg::CameraInfo>::SharedPtr
    camera_info_subscription_;
  rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr odom_subscription_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr monitoring_publisher_;
  rclcpp::Subscription<std_msgs::msg::Bool>::SharedPtr
    detector_health_subscription_;
  rclcpp::TimerBase::SharedPtr heartbeat_timer_;
  rclcpp::TimerBase::SharedPtr session_timer_;
  std::atomic<std::uint64_t> frames_received_{0};
  std::atomic<bool> has_frame_{false};
  std::atomic<bool> camera_info_received_{false};
  std::atomic<bool> odom_received_{false};
  std::atomic<bool> detector_reported_healthy_{false};
  std::atomic<std::int64_t> detector_health_received_at_ns_{0};
  SharedMediaTimeline media_timeline_;
  std::chrono::steady_clock::time_point last_frame_time_{
    std::chrono::steady_clock::now()};

#if HOMECAM_HAVE_GSTREAMER
  GstElement * pipeline_{nullptr};
  GstElement * video_source_{nullptr};
#if HOMECAM_HAVE_KVS
  GstElement * video_encoded_sink_{nullptr};
  GstElement * audio_encoded_sink_{nullptr};
#endif
  GstElement * audio_capture_pipeline_{nullptr};
  GstElement * ptt_playback_pipeline_{nullptr};
  GstElement * ptt_source_{nullptr};
  std::mutex ptt_mutex_;
  VideoFormat format_;
  std::chrono::steady_clock::time_point next_video_retry_{
    std::chrono::steady_clock::time_point::min()};
  std::chrono::steady_clock::time_point next_audio_retry_{
    std::chrono::steady_clock::time_point::min()};
  std::chrono::steady_clock::time_point next_ptt_retry_{
    std::chrono::steady_clock::time_point::min()};
#endif
};

}  // namespace homecam_media_agent

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    rclcpp::spin(std::make_shared<homecam_media_agent::MediaAgentNode>());
  } catch (const std::exception & exception) {
    RCLCPP_FATAL(
      rclcpp::get_logger("homecam_media_agent"),
      "Agent terminated during startup: %s", exception.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
