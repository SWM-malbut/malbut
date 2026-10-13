#pragma once

#include <cstddef>
#include <cstdint>
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
// How the guardian's voice is played; each part can be switched on the robot
// (ptt_playout_mode, ptt_plc, ptt_fec) to compare by ear (2026-10-13).
struct PttPipelineOptions
{
  bool scheduled{true};   // Play on each buffer's time (sync); else as it arrives.
  bool plc{true};         // opusdec conceals reported gaps.
  bool fec{false};        // opusdec uses Opus in-band FEC to recover a lost frame.
  std::int64_t queue_ns{500'000'000};
};
std::string build_ptt_playback_pipeline(
  const MediaConfig & config, const PttPipelineOptions & options);
std::string build_audio_playback_pipeline(const MediaConfig & config);
// Scheduled playback behind a jitter buffer (ptt_playout_mode=buffered).
std::string build_buffered_audio_playback_pipeline(const MediaConfig & config);
// The guardian's voice as it arrives, as before 2026-10-09 (ptt_playout_mode=arrival).
std::string build_arrival_audio_playback_pipeline(const MediaConfig & config);

}  // namespace homecam_media_agent
