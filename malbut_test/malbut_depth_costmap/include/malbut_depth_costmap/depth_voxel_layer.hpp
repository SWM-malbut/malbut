// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#ifndef MALBUT_DEPTH_COSTMAP__DEPTH_VOXEL_LAYER_HPP_
#define MALBUT_DEPTH_COSTMAP__DEPTH_VOXEL_LAYER_HPP_

#include <chrono>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>

#include "malbut_depth_costmap/depth_projector.hpp"
#include "nav2_costmap_2d/voxel_layer.hpp"
#include "rclcpp/rclcpp.hpp"

namespace malbut_depth_costmap
{

/// Adapt complete depth images directly into the standard Nav2 voxel buffers.
class DepthVoxelLayer : public nav2_costmap_2d::VoxelLayer
{
public:
  void onInitialize() override;
  void activate() override;
  void deactivate() override;
  void reset() override;
  void updateBounds(
    double robot_x, double robot_y, double robot_yaw,
    double * min_x, double * min_y, double * max_x, double * max_y) override;

private:
  struct Frame
  {
    sensor_msgs::msg::Image::ConstSharedPtr image;
    sensor_msgs::msg::CameraInfo::ConstSharedPtr info;
    std::chrono::steady_clock::time_point received;
  };

  void depthCallback(sensor_msgs::msg::Image::ConstSharedPtr image);
  void infoCallback(sensor_msgs::msg::CameraInfo::ConstSharedPtr info);
  void pairLatestImage();
  void retryPending();
  void clearInput();
  void rebuildBuffers();
  bool fresh(const sensor_msgs::msg::Image & image) const;

  std::recursive_mutex input_mutex_;
  DepthProjector projector_;
  bool active_{false};
  bool received_frame_{false};
  bool depth_is_rectified_{false};
  int64_t active_since_ns_{0};
  int64_t last_queued_ns_{0};
  int64_t last_buffered_ns_{0};
  double expected_update_rate_{0.5};
  double transform_tolerance_{0.3};
  double marking_min_height_{0.05}, marking_max_height_{0.20};
  double clearing_min_height_{-0.05}, clearing_max_height_{0.48};
  double obstacle_min_range_{0.0}, obstacle_max_range_{2.5};
  double raytrace_min_range_{0.0}, raytrace_max_range_{3.0};
  std::string depth_topic_, camera_info_topic_;
  sensor_msgs::msg::Image::ConstSharedPtr unpaired_image_;
  sensor_msgs::msg::CameraInfo::ConstSharedPtr camera_info_;
  std::optional<Frame> pending_, latest_;
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr depth_sub_;
  rclcpp::Subscription<sensor_msgs::msg::CameraInfo>::SharedPtr info_sub_;
  rclcpp::TimerBase::SharedPtr retry_timer_;
  std::shared_ptr<nav2_costmap_2d::ObservationBuffer> marking_buffer_, clearing_buffer_;
};

}  // namespace malbut_depth_costmap
#endif  // MALBUT_DEPTH_COSTMAP__DEPTH_VOXEL_LAYER_HPP_
