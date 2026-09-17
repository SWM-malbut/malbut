// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0

#include <gtest/gtest.h>

#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <string>
#include <type_traits>
#include <vector>

#include "image_geometry/pinhole_camera_model.h"
#include "malbut_bringup/depth_projector.hpp"
#include "opencv2/imgproc.hpp"
#include "sensor_msgs/image_encodings.hpp"
#include "sensor_msgs/point_cloud2_iterator.hpp"

namespace
{

template<typename T>
sensor_msgs::msg::Image::SharedPtr depthImage(
  uint32_t width, uint32_t height, const std::vector<T> & values,
  uint32_t padding_bytes = 0)
{
  auto image = std::make_shared<sensor_msgs::msg::Image>();
  image->header.frame_id = "test_depth_optical";
  image->header.stamp.sec = 5;
  image->width = width;
  image->height = height;
  image->encoding = std::is_same<T, uint16_t>::value ?
    sensor_msgs::image_encodings::TYPE_16UC1 : sensor_msgs::image_encodings::TYPE_32FC1;
  const uint16_t native_endian = 1;
  image->is_bigendian = *reinterpret_cast<const uint8_t *>(&native_endian) == 0;
  image->step = width * sizeof(T) + padding_bytes;
  image->data.assign(image->step * height, 0xfe);
  for (uint32_t row = 0; row < height; ++row) {
    std::memcpy(
      image->data.data() + row * image->step, values.data() + row * width,
      width * sizeof(T));
  }
  return image;
}

sensor_msgs::msg::CameraInfo::SharedPtr cameraInfo(
  const sensor_msgs::msg::Image & image)
{
  auto info = std::make_shared<sensor_msgs::msg::CameraInfo>();
  info->header = image.header;
  info->width = image.width;
  info->height = image.height;
  info->distortion_model = "plumb_bob";
  info->d.assign(5, 0.0);
  info->k = {2.0, 0.0, 1.0, 0.0, 4.0, 0.5, 0.0, 0.0, 1.0};
  info->r = {1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0};
  info->p = {2.0, 0.0, 1.0, 0.0, 0.0, 4.0, 0.5, 0.0, 0.0, 0.0, 1.0, 0.0};
  return info;
}

std::vector<std::array<float, 3>> points(const sensor_msgs::msg::PointCloud2 & cloud)
{
  std::vector<std::array<float, 3>> output;
  sensor_msgs::PointCloud2ConstIterator<float> x(cloud, "x");
  sensor_msgs::PointCloud2ConstIterator<float> y(cloud, "y");
  sensor_msgs::PointCloud2ConstIterator<float> z(cloud, "z");
  for (; x != x.end(); ++x, ++y, ++z) {
    output.push_back({*x, *y, *z});
  }
  return output;
}

TEST(DepthProjector, ProjectsEveryPixelAtNativeResolutionAndHonorsRowStride)
{
  const std::vector<uint16_t> millimeters{
    1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100};
  const auto image = depthImage<uint16_t>(4, 3, millimeters, 4);
  const auto info = cameraInfo(*image);
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto cloud = projector.project(image, info, true, error);
  ASSERT_NE(cloud, nullptr) << error;
  EXPECT_EQ(cloud->width, image->width);
  EXPECT_EQ(cloud->height, image->height);
  EXPECT_EQ(cloud->header, image->header);
  const auto actual = points(*cloud);
  ASSERT_EQ(actual.size(), millimeters.size());
  for (size_t index = 0; index < actual.size(); ++index) {
    SCOPED_TRACE(index);
    const float depth = millimeters[index] * 0.001F;
    const float u = static_cast<float>(index % image->width);
    const float v = static_cast<float>(index / image->width);
    EXPECT_NEAR(actual[index][0], (u - 1.0F) * depth / 2.0F, 1e-6F);
    EXPECT_NEAR(actual[index][1], (v - 0.5F) * depth / 4.0F, 1e-6F);
    EXPECT_NEAR(actual[index][2], depth, 1e-6F);
  }
}

TEST(DepthProjector, MillimeterAndMeterEncodingsProduceIdenticalGeometry)
{
  const auto integer_image = depthImage<uint16_t>(3, 2, {500, 1000, 1500, 2000, 2500, 3000});
  const auto float_image = depthImage<float>(3, 2, {0.5F, 1.0F, 1.5F, 2.0F, 2.5F, 3.0F}, 8);
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto integers = projector.project(integer_image, cameraInfo(*integer_image), true, error);
  ASSERT_NE(integers, nullptr) << error;
  const auto floats = projector.project(float_image, cameraInfo(*float_image), true, error);
  ASSERT_NE(floats, nullptr) << error;
  const auto expected = points(*integers);
  const auto actual = points(*floats);
  ASSERT_EQ(actual.size(), expected.size());
  for (size_t index = 0; index < actual.size(); ++index) {
    for (size_t axis = 0; axis < 3; ++axis) {
      EXPECT_FLOAT_EQ(actual[index][axis], expected[index][axis]);
    }
  }
}

TEST(DepthProjector, InvalidIntegerDepthRemainsAnInvalidPixelNotAFarObstacle)
{
  const auto image = depthImage<uint16_t>(3, 1, {1000, 0, 2000});
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto cloud = projector.project(image, cameraInfo(*image), true, error);
  ASSERT_NE(cloud, nullptr) << error;
  const auto actual = points(*cloud);
  ASSERT_EQ(actual.size(), 3U);
  EXPECT_FLOAT_EQ(actual[0][2], 1.0F);
  for (float coordinate : actual[1]) {
    EXPECT_TRUE(std::isnan(coordinate));
  }
  EXPECT_FLOAT_EQ(actual[2][2], 2.0F);
}

TEST(DepthProjector, AuroraMono16DepthIsProjectedInMeters)
{
  const auto image = depthImage<uint16_t>(3, 1, {500, 0, 2500});
  image->encoding = sensor_msgs::image_encodings::MONO16;
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto cloud = projector.project(image, cameraInfo(*image), true, error);
  ASSERT_NE(cloud, nullptr) << error;
  const auto actual = points(*cloud);
  ASSERT_EQ(actual.size(), 3U);
  EXPECT_FLOAT_EQ(actual[0][2], 0.5F);
  EXPECT_TRUE(std::isnan(actual[1][2]));
  EXPECT_FLOAT_EQ(actual[2][2], 2.5F);
}

TEST(DepthProjector, InvalidFloatDepthCannotCreateOrClearAnObstacle)
{
  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float inf = std::numeric_limits<float>::infinity();
  const auto image = depthImage<float>(6, 1, {nan, inf, -inf, 0.0F, -1.0F, 1.5F});
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto cloud = projector.project(image, cameraInfo(*image), true, error);
  ASSERT_NE(cloud, nullptr) << error;
  const auto actual = points(*cloud);
  ASSERT_EQ(actual.size(), 6U);
  for (size_t index = 0; index < 5; ++index) {
    SCOPED_TRACE(index);
    for (float coordinate : actual[index]) {
      EXPECT_TRUE(std::isnan(coordinate));
    }
  }
  EXPECT_FLOAT_EQ(actual[5][2], 1.5F);
}

TEST(DepthProjector, RejectsMismatchedOrUnusableCalibration)
{
  const auto image = depthImage<uint16_t>(3, 2, {1000, 1000, 1000, 1000, 1000, 1000});
  using Mutation = void (*)(sensor_msgs::msg::CameraInfo &);
  const std::vector<Mutation> mutations{
    [](auto & info) {info.width += 1;},
    [](auto & info) {info.height += 1;},
    [](auto & info) {info.header.frame_id = "different_camera";},
    [](auto & info) {info.k[0] = 0.0;},
    [](auto & info) {info.p[5] = std::numeric_limits<double>::quiet_NaN();},
    [](auto & info) {info.binning_x = 2;},
    [](auto & info) {info.roi.x_offset = 1;},
    [](auto & info) {info.p[3] = -0.1;},
    [](auto & info) {info.r[0] = 0.0;}
  };
  malbut_bringup::DepthProjector projector;
  for (size_t index = 0; index < mutations.size(); ++index) {
    SCOPED_TRACE(index);
    auto info = cameraInfo(*image);
    mutations[index](*info);
    std::string error;
    EXPECT_EQ(projector.project(image, info, true, error), nullptr);
    EXPECT_FALSE(error.empty());
  }
}

TEST(DepthProjector, AcceptsStaticCalibrationAndRefreshesChangedIntrinsics)
{
  const auto image = depthImage<uint16_t>(3, 1, {1000, 1000, 1000});
  auto info = cameraInfo(*image);
  info->header.stamp.sec = 0;
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto first = projector.project(image, info, true, error);
  ASSERT_NE(first, nullptr) << error;
  auto changed = std::make_shared<sensor_msgs::msg::CameraInfo>(*info);
  // Aurora calibration is factory data stamped with the last SDK frame, not
  // necessarily this depth frame. Its geometry, not timestamp, must match.
  changed->header.stamp.sec = image->header.stamp.sec + 1;
  changed->k[0] = changed->p[0] = 4.0;
  const auto second = projector.project(image, changed, true, error);
  ASSERT_NE(second, nullptr) << error;
  EXPECT_EQ(second->header, image->header);
  EXPECT_FLOAT_EQ(points(*first)[2][0], 0.5F);
  EXPECT_FLOAT_EQ(points(*second)[2][0], 0.25F);
}

TEST(DepthProjector, RejectsTruncatedUnsupportedAndInvalidEndianImages)
{
  const auto original = depthImage<uint16_t>(3, 2, {1000, 1000, 1000, 1000, 1000, 1000});
  const auto info = cameraInfo(*original);
  using Mutation = void (*)(sensor_msgs::msg::Image &);
  const std::vector<Mutation> mutations{
    [](auto & image) {image.data.pop_back();},
    [](auto & image) {image.step -= 1;},
    [](auto & image) {image.encoding = sensor_msgs::image_encodings::RGB8;},
    [](auto & image) {image.is_bigendian = 2;}
  };
  malbut_bringup::DepthProjector projector;
  for (size_t index = 0; index < mutations.size(); ++index) {
    SCOPED_TRACE(index);
    auto image = std::make_shared<sensor_msgs::msg::Image>(*original);
    mutations[index](*image);
    std::string error;
    EXPECT_EQ(projector.project(image, info, true, error), nullptr);
    EXPECT_FALSE(error.empty());
  }
}

TEST(DepthProjector, NormalizesOppositeEndianAndUnalignedRowPadding)
{
  const std::vector<uint16_t> values{500, 1000, 1500, 2000, 2500, 3000};
  auto image = depthImage<uint16_t>(3, 2, values, 1);
  image->is_bigendian = !image->is_bigendian;
  for (uint32_t row = 0; row < image->height; ++row) {
    for (uint32_t column = 0; column < image->width; ++column) {
      const size_t offset = row * image->step + column * sizeof(uint16_t);
      std::swap(image->data[offset], image->data[offset + 1]);
    }
  }
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto cloud = projector.project(image, cameraInfo(*image), true, error);
  ASSERT_NE(cloud, nullptr) << error;
  const auto actual = points(*cloud);
  ASSERT_EQ(actual.size(), values.size());
  for (size_t index = 0; index < values.size(); ++index) {
    EXPECT_NEAR(actual[index][2], values[index] * 0.001F, 1e-6F);
  }
}

TEST(DepthProjector, RawZeroDistortionKeepsEveryMeasuredPixel)
{
  const auto image = depthImage<uint16_t>(3, 2, {500, 1000, 1500, 2000, 2500, 3000});
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto raw = projector.project(image, cameraInfo(*image), false, error);
  ASSERT_NE(raw, nullptr) << error;
  const auto rectified = projector.project(image, cameraInfo(*image), true, error);
  ASSERT_NE(rectified, nullptr) << error;
  EXPECT_EQ(points(*raw), points(*rectified));
  EXPECT_EQ(raw->width * raw->height, 6U);
}

TEST(DepthProjector, RawDistortedDepthUsesOfficialNearestNeighborRectification)
{
  std::vector<uint16_t> millimeters;
  for (uint16_t index = 0; index < 20; ++index) {
    millimeters.push_back(1000 + index * 100);
  }
  const auto image = depthImage<uint16_t>(5, 4, millimeters);
  auto info = cameraInfo(*image);
  info->k = {2.0, 0.0, 2.0, 0.0, 2.0, 1.5, 0.0, 0.0, 1.0};
  info->p = {2.0, 0.0, 2.0, 0.0, 0.0, 2.0, 1.5, 0.0, 0.0, 0.0, 1.0, 0.0};
  info->d = {0.3, 0.0, 0.0, 0.0, 0.0};
  image_geometry::PinholeCameraModel model;
  model.fromCameraInfo(*info);
  const cv::Mat native(
    image->height, image->width, CV_16UC1, image->data.data(), image->step);
  cv::Mat expected;
  model.rectifyImage(native, expected, cv::INTER_NEAREST);
  malbut_bringup::DepthProjector projector;
  std::string error;
  const auto cloud = projector.project(image, info, false, error);
  ASSERT_NE(cloud, nullptr) << error;
  ASSERT_EQ(cloud->width, image->width);
  ASSERT_EQ(cloud->height, image->height);
  EXPECT_EQ(cloud->header, image->header);
  const auto actual = points(*cloud);
  ASSERT_EQ(actual.size(), millimeters.size());
  bool distortion_changed_a_pixel = false;
  for (uint32_t row = 0; row < image->height; ++row) {
    for (uint32_t column = 0; column < image->width; ++column) {
      const auto index = row * image->width + column;
      const auto depth_mm = expected.at<uint16_t>(row, column);
      distortion_changed_a_pixel = distortion_changed_a_pixel || depth_mm != millimeters[index];
      if (depth_mm == 0) {
        EXPECT_TRUE(std::isnan(actual[index][2]));
      } else {
        const float z = depth_mm * 0.001F;
        EXPECT_NEAR(actual[index][0], (static_cast<float>(column) - 2.0F) * z / 2.0F, 1e-6F);
        EXPECT_NEAR(actual[index][1], (static_cast<float>(row) - 1.5F) * z / 2.0F, 1e-6F);
        EXPECT_NEAR(actual[index][2], z, 1e-6F);
      }
    }
  }
  EXPECT_TRUE(distortion_changed_a_pixel) << "the fixture must exercise actual rectification";
}

}  // namespace
