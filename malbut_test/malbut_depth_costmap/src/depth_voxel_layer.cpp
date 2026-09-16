// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include "malbut_depth_costmap/depth_voxel_layer.hpp"

#include <cmath>
#include <functional>
#include <memory>
#include <stdexcept>
#include <utility>

#include "pluginlib/class_list_macros.hpp"
#include "tf2/time.h"

namespace malbut_depth_costmap
{

void DepthVoxelLayer::onInitialize()
{
  auto node = node_.lock();
  if (!node) {
    throw std::runtime_error("DepthVoxelLayer parent expired");
  }
  declareParameter("observation_sources", rclcpp::ParameterValue(std::string("")));
  if (!node->get_parameter(name_ + ".observation_sources").as_string().empty()) {
    throw std::runtime_error("DepthVoxelLayer observation_sources must be empty; it consumes images");
  }
  nav2_costmap_2d::VoxelLayer::onInitialize();
  const auto string_parameter = [&](const std::string & key, const std::string & value) {
      declareParameter(key, rclcpp::ParameterValue(value));
      return node->get_parameter(name_ + "." + key).as_string();
    };
  const auto double_parameter = [&](const std::string & key, double value) {
      declareParameter(key, rclcpp::ParameterValue(value));
      return node->get_parameter(name_ + "." + key).as_double();
    };
  depth_topic_ = string_parameter("depth_topic", "/depth_cam/depth0/image_raw");
  camera_info_topic_ = string_parameter("camera_info_topic", "/depth_cam/depth0/camera_info");
  declareParameter("depth_is_rectified", rclcpp::ParameterValue(false));
  depth_is_rectified_ = node->get_parameter(name_ + ".depth_is_rectified").as_bool();
  expected_update_rate_ = double_parameter("expected_update_rate", 0.5);
  node->get_parameter("transform_tolerance", transform_tolerance_);
  marking_min_height_ = double_parameter("marking.min_obstacle_height", 0.05);
  marking_max_height_ = double_parameter("marking.max_obstacle_height", 0.20);
  clearing_min_height_ = double_parameter("clearing.min_obstacle_height", -0.05);
  clearing_max_height_ = double_parameter("clearing.max_obstacle_height", 0.48);
  obstacle_min_range_ = double_parameter("marking.obstacle_min_range", 0.0);
  obstacle_max_range_ = double_parameter("marking.obstacle_max_range", 2.5);
  raytrace_min_range_ = double_parameter("clearing.raytrace_min_range", 0.0);
  raytrace_max_range_ = double_parameter("clearing.raytrace_max_range", 3.0);
  for (double value : {
      expected_update_rate_, transform_tolerance_, marking_min_height_, marking_max_height_,
      clearing_min_height_, clearing_max_height_, obstacle_min_range_, obstacle_max_range_,
      raytrace_min_range_, raytrace_max_range_})
  {
    if (!std::isfinite(value)) {
      throw std::runtime_error("DepthVoxelLayer numeric parameters must be finite");
    }
  }
  if (depth_topic_.empty() || camera_info_topic_.empty() || expected_update_rate_ <= 0.0 ||
    transform_tolerance_ < 0.0 || marking_min_height_ > marking_max_height_ ||
    clearing_min_height_ > clearing_max_height_ || obstacle_min_range_ < 0.0 ||
    raytrace_min_range_ < 0.0 || obstacle_max_range_ <= obstacle_min_range_ ||
    raytrace_max_range_ <= raytrace_min_range_)
  {
    throw std::runtime_error("invalid DepthVoxelLayer topics, freshness, heights, or ranges");
  }
  rebuildBuffers();
  // Retry only while one exact-time frame awaits TF. There is no periodic
  // reprojection and no frame-rate cap; the timer never blocks on TF.
  retry_timer_ = node->create_wall_timer(
    std::chrono::milliseconds(10), std::bind(&DepthVoxelLayer::retryPending, this), callback_group_);
  retry_timer_->cancel();
  current_ = false;
}

void DepthVoxelLayer::rebuildBuffers()
{
  const auto make_buffer = [&](const std::string & role, double min_height, double max_height) {
      return std::make_shared<nav2_costmap_2d::ObservationBuffer>(
        node_, depth_topic_ + ":" + role, 0.0, expected_update_rate_, min_height, max_height,
        obstacle_max_range_, obstacle_min_range_, raytrace_max_range_, raytrace_min_range_,
        *tf_, global_frame_, "", tf2::durationFromSec(0.0));
    };
  marking_buffer_ = make_buffer("marking", marking_min_height_, marking_max_height_);
  clearing_buffer_ = make_buffer("clearing", clearing_min_height_, clearing_max_height_);
  observation_buffers_ = {marking_buffer_, clearing_buffer_};
  marking_buffers_ = {marking_buffer_};
  clearing_buffers_ = {clearing_buffer_};
}

void DepthVoxelLayer::clearInput()
{
  if (retry_timer_) {
    retry_timer_->cancel();
  }
  unpaired_image_.reset();
  camera_info_.reset();
  pending_.reset();
  latest_.reset();
  received_frame_ = false;
  last_queued_ns_ = 0;
  last_buffered_ns_ = 0;
  active_since_ns_ = clock_->now().nanoseconds();
  current_ = false;
}

void DepthVoxelLayer::activate()
{
  std::lock_guard<nav2_costmap_2d::Costmap2D::mutex_t> grid_lock(*getMutex());
  std::lock_guard<std::recursive_mutex> input_lock(input_mutex_);
  auto node = node_.lock();
  if (!node) {
    throw std::runtime_error("DepthVoxelLayer parent expired during activation");
  }
  clearInput();
  nav2_costmap_2d::VoxelLayer::reset();
  // ObstacleLayer::updateCosts otherwise marks a reset layer current even when
  // no depth has arrived. Our exact-time input freshness controls readiness.
  was_reset_ = false;
  rebuildBuffers();
  nav2_costmap_2d::VoxelLayer::activate();
  auto options = rclcpp::SubscriptionOptions();
  options.callback_group = callback_group_;
  // Image transport remains standard DDS; no large PointCloud2 subscription exists.
  const auto qos = rclcpp::SensorDataQoS().keep_last(1);
  depth_sub_ = node->create_subscription<sensor_msgs::msg::Image>(
    depth_topic_, qos, std::bind(&DepthVoxelLayer::depthCallback, this, std::placeholders::_1),
    options);
  info_sub_ = node->create_subscription<sensor_msgs::msg::CameraInfo>(
    camera_info_topic_, qos,
    std::bind(&DepthVoxelLayer::infoCallback, this, std::placeholders::_1), options);
  active_ = true;
  current_ = false;
}

void DepthVoxelLayer::deactivate()
{
  std::lock_guard<nav2_costmap_2d::Costmap2D::mutex_t> grid_lock(*getMutex());
  std::lock_guard<std::recursive_mutex> input_lock(input_mutex_);
  active_ = false;
  depth_sub_.reset();
  info_sub_.reset();
  clearInput();
  nav2_costmap_2d::VoxelLayer::deactivate();
  rebuildBuffers();
}

void DepthVoxelLayer::reset()
{
  std::lock_guard<nav2_costmap_2d::Costmap2D::mutex_t> grid_lock(*getMutex());
  std::lock_guard<std::recursive_mutex> input_lock(input_mutex_);
  clearInput();
  nav2_costmap_2d::VoxelLayer::reset();
  // Keep the inherited reset shortcut from bypassing the fresh-depth gate.
  was_reset_ = false;
  rebuildBuffers();
  current_ = false;
}

bool DepthVoxelLayer::fresh(const sensor_msgs::msg::Image & image) const
{
  if (image.header.stamp.sec < 0 || image.header.stamp.nanosec >= 1000000000U) {
    return false;
  }
  const int64_t stamp = rclcpp::Time(image.header.stamp).nanoseconds();
  const double age = (clock_->now().nanoseconds() - stamp) * 1e-9;
  return stamp > active_since_ns_ && age <= expected_update_rate_ && age >= -transform_tolerance_;
}

void DepthVoxelLayer::depthCallback(sensor_msgs::msg::Image::ConstSharedPtr image)
{
  std::lock_guard<std::recursive_mutex> lock(input_mutex_);
  if (!active_ || !fresh(*image)) {
    return;
  }
  if (rclcpp::Time(image->header.stamp).nanoseconds() <= last_queued_ns_) {
    return;
  }
  unpaired_image_ = std::move(image);
  pairLatestImage();
}

void DepthVoxelLayer::infoCallback(sensor_msgs::msg::CameraInfo::ConstSharedPtr info)
{
  std::lock_guard<std::recursive_mutex> lock(input_mutex_);
  if (!active_) {
    return;
  }
  camera_info_ = std::move(info);
  pairLatestImage();
}

void DepthVoxelLayer::pairLatestImage()
{
  if (!unpaired_image_ || !camera_info_) {
    if (unpaired_image_ && !camera_info_) {
      RCLCPP_WARN_THROTTLE(
        logger_, *clock_, 5000, "DepthVoxelLayer waiting for depth CameraInfo calibration");
    }
    return;
  }
  if (!fresh(*unpaired_image_)) {
    unpaired_image_.reset();
    return;
  }
  // CameraInfo describes calibration, not a new depth measurement. Aurora
  // stamps its factory IR calibration using the last SDK frame in a batch,
  // which need not be the depth frame. Preserve this calibration snapshot;
  // only the image's own timestamp is used for freshness and exact-time TF.
  Frame frame{unpaired_image_, camera_info_, std::chrono::steady_clock::now()};
  last_queued_ns_ = rclcpp::Time(unpaired_image_->header.stamp).nanoseconds();
  unpaired_image_.reset();
  if (!pending_) {
    pending_ = std::move(frame);
  } else {
    latest_ = std::move(frame);
  }
  retryPending();
  if (pending_ && retry_timer_->is_canceled()) {
    retry_timer_->reset();
  }
}

void DepthVoxelLayer::retryPending()
{
  std::lock_guard<std::recursive_mutex> lock(input_mutex_);
  if (!active_) {
    return;
  }
  // Preserve a waiting frame while new images replace only its successor.
  // Replacing the waiting frame on every input would starve slightly delayed TF.
  for (int attempt = 0; pending_ && attempt < 2; ++attempt) {
    const auto & image = pending_->image;
    const auto stamp = rclcpp::Time(image->header.stamp);
    const double waited = std::chrono::duration<double>(
      std::chrono::steady_clock::now() - pending_->received).count();
    const bool ready = fresh(*image) && tf_->canTransform(
      global_frame_, image->header.frame_id, tf2_ros::fromRclcpp(stamp));
    if (ready) {
      std::string error;
      auto cloud = projector_.project(image, pending_->info, depth_is_rectified_, error);
      if (cloud) {
        pointCloud2Callback(cloud, marking_buffer_);
        pointCloud2Callback(cloud, clearing_buffer_);
        received_frame_ = true;
        last_buffered_ns_ = stamp.nanoseconds();
      } else {
        RCLCPP_WARN_THROTTLE(logger_, *clock_, 5000, "DepthVoxelLayer: %s", error.c_str());
      }
    } else if (fresh(*image) && waited < transform_tolerance_) {
      break;
    } else {
      RCLCPP_WARN_THROTTLE(
        logger_, *clock_, 5000, "DepthVoxelLayer dropped stale depth or timed out waiting for exact-time TF");
    }
    pending_ = std::move(latest_);
    latest_.reset();
  }
  if (!pending_ && retry_timer_) {
    retry_timer_->cancel();
  }
}

void DepthVoxelLayer::updateBounds(
  double robot_x, double robot_y, double robot_yaw,
  double * min_x, double * min_y, double * max_x, double * max_y)
{
  std::lock_guard<nav2_costmap_2d::Costmap2D::mutex_t> grid_lock(*getMutex());
  std::lock_guard<std::recursive_mutex> input_lock(input_mutex_);
  nav2_costmap_2d::VoxelLayer::updateBounds(
    robot_x, robot_y, robot_yaw, min_x, min_y, max_x, max_y);
  const double age = (clock_->now().nanoseconds() - last_buffered_ns_) * 1e-9;
  current_ = current_ && active_ && received_frame_ && age >= 0.0 && age <= expected_update_rate_;
}

}  // namespace malbut_depth_costmap

PLUGINLIB_EXPORT_CLASS(malbut_depth_costmap::DepthVoxelLayer, nav2_costmap_2d::Layer)
