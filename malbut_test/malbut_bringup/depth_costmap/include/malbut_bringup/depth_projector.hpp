// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#ifndef MALBUT_BRINGUP__DEPTH_PROJECTOR_HPP_
#define MALBUT_BRINGUP__DEPTH_PROJECTOR_HPP_

#include <string>

#include "image_geometry/pinhole_camera_model.h"
#include "sensor_msgs/msg/camera_info.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/point_cloud2.hpp"

namespace malbut_bringup
{

/// Validate and project a complete axial-depth image using the official ROS implementation.
class DepthProjector
{
public:
  sensor_msgs::msg::PointCloud2::SharedPtr project(
    const sensor_msgs::msg::Image::ConstSharedPtr & image,
    const sensor_msgs::msg::CameraInfo::ConstSharedPtr & info,
    bool input_is_rectified, std::string & error);

private:
  image_geometry::PinholeCameraModel model_;
};

}  // namespace malbut_bringup
#endif  // MALBUT_BRINGUP__DEPTH_PROJECTOR_HPP_
