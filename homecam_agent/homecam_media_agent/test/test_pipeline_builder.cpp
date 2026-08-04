#include <gtest/gtest.h>

#include <stdexcept>
#include <string>

#include "homecam_media_agent/config.hpp"
#include "homecam_media_agent/pipeline_builder.hpp"

using homecam_media_agent::MediaConfig;
using homecam_media_agent::build_audio_capture_pipeline;
using homecam_media_agent::build_video_pipeline;
using homecam_media_agent::video_format_from_ros;
using homecam_media_agent::video_format_matches;

TEST(PipelineBuilder, MapsSupportedRosEncodings)
{
  const auto rgb = video_format_from_ros("rgb8", 640, 400);
  EXPECT_EQ(rgb.gst_format, "RGB");
  EXPECT_EQ(rgb.bytes_per_pixel, 3U);
  const auto bgra = video_format_from_ros("bgra8", 320, 240);
  EXPECT_EQ(bgra.gst_format, "BGRA");
  EXPECT_EQ(bgra.bytes_per_pixel, 4U);
}
TEST(PipelineBuilder, RejectsUnsupportedEncoding)
{
  EXPECT_THROW(video_format_from_ros("16UC1", 640, 400), std::invalid_argument);
}

TEST(PipelineBuilder, DetectsCameraFormatChanges)
{
  const auto format = video_format_from_ros("bgr8", 640, 400);
  EXPECT_TRUE(video_format_matches(format, "bgr8", 640, 400));
  EXPECT_FALSE(video_format_matches(format, "rgb8", 640, 400));
  EXPECT_FALSE(video_format_matches(format, "bgr8", 1280, 400));
  EXPECT_FALSE(video_format_matches(format, "bgr8", 640, 480));
}

TEST(PipelineBuilder, BuildsX264AndJetsonPipelines)
{
  MediaConfig config;
  const auto format = video_format_from_ros("bgr8", 640, 400);
  const std::string x264 = build_video_pipeline(config, format, true);
  EXPECT_NE(x264.find("x264enc"), std::string::npos);
  EXPECT_NE(x264.find("framerate=15/1"), std::string::npos);
  EXPECT_EQ(x264.find("framerate=0/1"), std::string::npos);
  EXPECT_NE(x264.find("fakesink"), std::string::npos);

  config.encoder = "nvv4l2h264enc";
  const std::string jetson = build_video_pipeline(config, format, false);
  EXPECT_NE(jetson.find("nvvidconv"), std::string::npos);
  EXPECT_NE(
    jetson.find("video/x-raw(memory:NVMM),format=I420"),
    std::string::npos);
  EXPECT_NE(jetson.find("nvv4l2h264enc"), std::string::npos);
  EXPECT_NE(jetson.find("kvs_video_sink"), std::string::npos);
}

TEST(PipelineBuilder, UsesSilenceWhenMicrophoneIsPrivate)
{
  MediaConfig config;
  const auto pipeline = build_audio_capture_pipeline(config, true, true);
  EXPECT_NE(pipeline.find("audiotestsrc wave=silence"), std::string::npos);
  EXPECT_NE(pipeline.find("opusenc"), std::string::npos);
}
