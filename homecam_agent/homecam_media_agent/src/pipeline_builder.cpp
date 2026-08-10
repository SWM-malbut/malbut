#include "homecam_media_agent/pipeline_builder.hpp"

#include <stdexcept>

namespace homecam_media_agent
{

namespace
{

std::string quote_gst(const std::string & value)
{
  std::string escaped;
  escaped.reserve(value.size() + 2);
  escaped.push_back('"');
  for (const char character : value) {
    if (character == '\\' || character == '"') {
      escaped.push_back('\\');
    }
    escaped.push_back(character);
  }
  escaped.push_back('"');
  return escaped;
}

std::string video_sink(const bool use_fake_sink)
{
  return use_fake_sink ?
         "fakesink name=video_sink sync=false" :
         "appsink name=kvs_video_sink emit-signals=true sync=false max-buffers=2 drop=true";
}

std::string audio_sink(const bool use_fake_sink)
{
  return use_fake_sink ?
         "fakesink name=audio_sink sync=false" :
         "appsink name=kvs_audio_sink emit-signals=true sync=false max-buffers=8 drop=true";
}

}  // namespace

VideoFormat video_format_from_ros(
  const std::string & encoding, const int width, const int height)
{
  if (width <= 0 || height <= 0) {
    throw std::invalid_argument("video dimensions must be positive");
  }
  if (encoding == "rgb8") {
    return {encoding, "RGB", width, height, 3};
  }
  if (encoding == "bgr8") {
    return {encoding, "BGR", width, height, 3};
  }
  if (encoding == "rgba8") {
    return {encoding, "RGBA", width, height, 4};
  }
  if (encoding == "bgra8") {
    return {encoding, "BGRA", width, height, 4};
  }
  throw std::invalid_argument(
          "unsupported ROS image encoding '" + encoding +
          "'; expected rgb8, bgr8, rgba8, or bgra8");
}

bool video_format_matches(
  const VideoFormat & format,
  const std::string & encoding,
  const int width,
  const int height)
{
  return format.ros_encoding == encoding &&
         format.width == width &&
         format.height == height;
}

std::string build_video_pipeline(
  const MediaConfig & config, const VideoFormat & format, const bool use_fake_sink)
{
  const auto errors = validate_config(config);
  if (!errors.empty()) {
    throw std::invalid_argument(errors.front());
  }
  if (format.width <= 0 || format.height <= 0 || format.bytes_per_pixel == 0) {
    throw std::invalid_argument("invalid video format");
  }

  std::string converter;
  std::string encoder;
  if (config.encoder == "x264") {
    converter =
      "videoconvert ! videoscale ! videorate drop-only=true ! "
      "video/x-raw,format=I420,width=640,height=400,framerate=" +
      std::to_string(config.fps) + "/1";
    encoder =
      "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=" +
      std::to_string(config.fps * 2) + " bitrate=" +
      std::to_string(config.bitrate_kbps) +
      " byte-stream=true aud=true";
  } else {
    converter =
      "videoconvert ! videoscale ! videorate drop-only=true ! "
      "video/x-raw,format=I420,width=640,height=400,framerate=" +
      std::to_string(config.fps) +
      "/1 ! nvvidconv ! video/x-raw(memory:NVMM),format=I420,"
      "width=640,height=400,framerate=" +
      std::to_string(config.fps) + "/1";
    encoder =
      "nvv4l2h264enc maxperf-enable=true insert-sps-pps=true iframeinterval=" +
      std::to_string(config.fps * 2) + " bitrate=" +
      std::to_string(config.bitrate_kbps * 1000);
  }

  return
    "appsrc name=ros_video_source is-live=true block=false format=time "
    "do-timestamp=true caps=video/x-raw,format=" + format.gst_format +
    ",width=" + std::to_string(format.width) +
    ",height=" + std::to_string(format.height) +
    ",framerate=" + std::to_string(config.fps) +
    "/1 ! queue max-size-buffers=2 leaky=downstream ! " +
    converter + " ! " + encoder +
    " ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,"
    "alignment=au,profile=baseline ! " + video_sink(use_fake_sink);
}

std::string build_audio_capture_pipeline(
  const MediaConfig & config, const bool use_silent_audio, const bool use_fake_sink)
{
  const std::string source = use_silent_audio ?
    "audiotestsrc wave=silence is-live=true" :
    "alsasrc device=" + quote_gst(config.audio_source);
  return source +
         " ! queue max-size-time=200000000 leaky=downstream ! "
         "audioconvert ! audioresample ! "
         "audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
         "opusenc bitrate=24000 frame-size=20 audio-type=voice ! " +
         audio_sink(use_fake_sink);
}

std::string build_audio_playback_pipeline(const MediaConfig & config)
{
  return
    "appsrc name=ptt_audio_source is-live=true block=false format=time "
    "do-timestamp=true caps=audio/x-opus,rate=48000,channels=1 ! "
    "queue max-size-time=200000000 leaky=downstream ! opusdec ! "
    "audioconvert ! audioresample ! alsasink device=" +
    quote_gst(config.audio_sink) + " sync=false";
}

}  // namespace homecam_media_agent
