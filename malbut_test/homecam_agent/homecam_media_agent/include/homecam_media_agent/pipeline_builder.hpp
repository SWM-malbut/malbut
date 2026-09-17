#pragma once

#include <cstddef>
#include <string>

#include "homecam_media_agent/config.hpp"

namespace homecam_media_agent
{

struct VideoFormat
{
  std::string ros_encoding;
  std::string gst_format;
  int width{0};
  int height{0};
  std::size_t bytes_per_pixel{0};
};

VideoFormat video_format_from_ros(
  const std::string & encoding, int width, int height);
bool video_format_matches(
  const VideoFormat & format,
  const std::string & encoding,
  int width,
  int height);
std::string build_video_pipeline(
  const MediaConfig & config, const VideoFormat & format, bool use_fake_sink);
std::string build_audio_capture_pipeline(
  const MediaConfig & config, bool use_silent_audio, bool use_fake_sink);
std::string build_audio_playback_pipeline(const MediaConfig & config);

}  // namespace homecam_media_agent
