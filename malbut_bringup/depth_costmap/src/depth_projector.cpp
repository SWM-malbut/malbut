// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include "malbut_bringup/depth_projector.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>

#include "depth_image_proc/conversions.hpp"
#include "opencv2/core.hpp"
#include "opencv2/imgproc.hpp"
#include "sensor_msgs/image_encodings.hpp"
#include "sensor_msgs/point_cloud2_iterator.hpp"

namespace malbut_bringup
{
namespace
{
bool hostBigEndian()
{
  const uint16_t value = 1;
  return *reinterpret_cast<const uint8_t *>(&value) == 0;
}

template<typename Values>
bool allFinite(const Values & values)
{
  return std::all_of(values.begin(), values.end(), [](double v) {return std::isfinite(v);});
}

bool validateCalibration(
  const sensor_msgs::msg::Image & image, const sensor_msgs::msg::CameraInfo & info,
  bool rectified, std::string & error)
{
  if (image.header.frame_id.empty() || image.header.frame_id != info.header.frame_id ||
    image.width != info.width || image.height != info.height)
  {
    error = "depth and CameraInfo must have identical nonempty frame and dimensions";
    return false;
  }
  if (!allFinite(info.k) || !allFinite(info.p) || !allFinite(info.r) || !allFinite(info.d) ||
    info.k[0] <= 0.0 || info.k[4] <= 0.0 || info.p[0] <= 0.0 || info.p[5] <= 0.0)
  {
    error = "CameraInfo requires finite calibration and positive K/P focal lengths";
    return false;
  }
  if (info.binning_x > 1 || info.binning_y > 1 || info.roi.x_offset != 0 ||
    info.roi.y_offset != 0 || (info.roi.width != 0 && info.roi.width != image.width) ||
    (info.roi.height != 0 && info.roi.height != image.height) || info.roi.do_rectify)
  {
    error = "cropped/binned CameraInfo is not supported by this full-resolution depth input";
    return false;
  }
  // Axial depth is not a radial distance. Rotating its image without rotating
  // each 3D point would change Z, and convertDepth intentionally does not do that.
  for (size_t i = 0; i < info.r.size(); ++i) {
    const double expected = (i == 0 || i == 4 || i == 8) ? 1.0 : 0.0;
    if (std::abs(info.r[i] - expected) > 1e-9) {
      error = "axial depth requires identity CameraInfo R in its optical frame";
      return false;
    }
  }
  if (info.p[1] != 0.0 || info.p[3] != 0.0 || info.p[4] != 0.0 || info.p[7] != 0.0 ||
    info.p[8] != 0.0 || info.p[9] != 0.0 || info.p[10] != 1.0 || info.p[11] != 0.0)
  {
    error = "depth requires a monocular rectified P matrix without skew or translation";
    return false;
  }
  const bool distorted = std::any_of(info.d.begin(), info.d.end(), [](double d) {return d != 0.0;});
  if (!rectified && distorted) {
    const bool standard =
      (info.distortion_model == "plumb_bob" && info.d.size() == 5) ||
      (info.distortion_model == "rational_polynomial" && info.d.size() == 8) ||
      (info.distortion_model == "equidistant" && info.d.size() == 4);
    if (!standard) {
      error = "raw depth requires supported distortion model and coefficient count";
      return false;
    }
  }
  if (!rectified && !distorted &&
    (info.k[0] != info.p[0] || info.k[4] != info.p[5] ||
    info.k[2] != info.p[2] || info.k[5] != info.p[6]))
  {
    // image_geometry copies an undistorted image unchanged; do not silently
    // reinterpret raw pixels with a different output focal length/principal point.
    error = "undistorted raw depth requires matching K/P intrinsics";
    return false;
  }
  return true;
}

sensor_msgs::msg::Image::SharedPtr packedImage(
  const sensor_msgs::msg::Image & image, size_t bytes_per_pixel)
{
  auto packed = std::make_shared<sensor_msgs::msg::Image>();
  packed->header = image.header;
  packed->width = image.width;
  packed->height = image.height;
  packed->encoding = image.encoding;
  packed->is_bigendian = hostBigEndian();
  packed->step = image.width * bytes_per_pixel;
  packed->data.resize(static_cast<size_t>(packed->step) * packed->height);
  return packed;
}
}  // namespace

sensor_msgs::msg::PointCloud2::SharedPtr DepthProjector::project(
  const sensor_msgs::msg::Image::ConstSharedPtr & image,
  const sensor_msgs::msg::CameraInfo::ConstSharedPtr & info,
  bool input_is_rectified, std::string & error)
{
  error.clear();
  if (!image || !info) {
    error = "depth image and CameraInfo are required";
    return nullptr;
  }
  const bool is_uint16 = image->encoding == sensor_msgs::image_encodings::TYPE_16UC1 ||
    image->encoding == sensor_msgs::image_encodings::MONO16;
  if (!is_uint16 && image->encoding != sensor_msgs::image_encodings::TYPE_32FC1) {
    error = "depth encoding must be 16UC1/MONO16 (millimeters) or 32FC1 (meters)";
    return nullptr;
  }
  const size_t bytes_per_pixel = is_uint16 ? sizeof(uint16_t) : sizeof(float);
  const size_t row_bytes = static_cast<size_t>(image->width) * bytes_per_pixel;
  const size_t data_bytes = static_cast<size_t>(image->height) * image->step;
  if (image->width == 0 || image->height == 0 || image->is_bigendian > 1 ||
    image->width > static_cast<uint32_t>(std::numeric_limits<int>::max()) ||
    image->height > static_cast<uint32_t>(std::numeric_limits<int>::max()) ||
    image->step < row_bytes || image->data.size() < data_bytes ||
    row_bytes > std::numeric_limits<uint32_t>::max() ||
    image->width > std::numeric_limits<uint32_t>::max() / 16U)
  {
    error = "invalid depth dimensions, endian flag, row stride, or truncated data";
    return nullptr;
  }
  if (!validateCalibration(*image, *info, input_is_rectified, error)) {
    return nullptr;
  }
  try {
    model_.fromCameraInfo(info);
    sensor_msgs::msg::Image::ConstSharedPtr depth = image;
    // convertDepth reads native-endian typed rows. Only unusual layouts need
    // normalization; the normal native/padded input is read without a copy.
    if (static_cast<bool>(image->is_bigendian) != hostBigEndian() ||
      image->step % bytes_per_pixel != 0)
    {
      auto packed = packedImage(*image, bytes_per_pixel);
      for (size_t row = 0; row < image->height; ++row) {
        auto * target = packed->data.data() + row * packed->step;
        std::memcpy(target, image->data.data() + row * image->step, row_bytes);
        if (static_cast<bool>(image->is_bigendian) != hostBigEndian()) {
          for (size_t offset = 0; offset < row_bytes; offset += bytes_per_pixel) {
            std::reverse(target + offset, target + offset + bytes_per_pixel);
          }
        }
      }
      depth = packed;
    }
    const bool distorted =
      std::any_of(info->d.begin(), info->d.end(), [](double d) {return d != 0.0;});
    if (!input_is_rectified && distorted) {
      auto rectified = packedImage(*depth, bytes_per_pixel);
      const int type = is_uint16 ? CV_16UC1 : CV_32FC1;
      const cv::Mat raw(
        depth->height, depth->width, type,
        const_cast<uint8_t *>(depth->data.data()), depth->step);
      cv::Mat output(
        rectified->height, rectified->width, type, rectified->data.data(), rectified->step);
      model_.rectifyImage(raw, output, cv::INTER_NEAREST);
      if (output.rows != static_cast<int>(image->height) ||
        output.cols != static_cast<int>(image->width) || output.data != rectified->data.data())
      {
        error = "rectification unexpectedly changed the full-resolution image layout";
        return nullptr;
      }
      depth = rectified;
    }
    auto cloud = std::make_shared<sensor_msgs::msg::PointCloud2>();
    cloud->header = image->header;
    cloud->width = image->width;
    cloud->height = image->height;
    cloud->is_dense = false;
    cloud->is_bigendian = hostBigEndian();
    sensor_msgs::PointCloud2Modifier modifier(*cloud);
    modifier.setPointCloud2FieldsByString(1, "xyz");
    if (is_uint16) {
      depth_image_proc::convertDepth<uint16_t>(depth, cloud, model_);
    } else {
      depth_image_proc::convertDepth<float>(depth, cloud, model_);
      // The official float DepthTraits only checks finiteness. Nonpositive
      // axial measurements are not physical obstacle/clearing observations.
      sensor_msgs::PointCloud2Iterator<float> x(*cloud, "x"), y(*cloud, "y"), z(*cloud, "z");
      for (; z != z.end(); ++x, ++y, ++z) {
        if (*z <= 0.0F) {
          *x = *y = *z = std::numeric_limits<float>::quiet_NaN();
        }
      }
    }
    return cloud;
  } catch (const std::exception & exception) {
    error = std::string("depth projection failed: ") + exception.what();
    return nullptr;
  }
}
}  // namespace malbut_bringup
