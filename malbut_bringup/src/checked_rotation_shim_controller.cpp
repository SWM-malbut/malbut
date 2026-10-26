// Copyright 2026 Malbut. Licensed under the Apache License, Version 2.0.
#include <algorithm>
#include <cmath>
#include <mutex>
#include "checked_rotation_shim_controller.hpp"
#include "pluginlib/class_list_macros.hpp"

namespace malbut_bringup
{
geometry_msgs::msg::TwistStamped CheckedRotationShimController::computeVelocityCommands(
  const geometry_msgs::msg::PoseStamped & pose,
  const geometry_msgs::msg::Twist & velocity,
  nav2_core::GoalChecker * goal_checker)
{
  auto command = RotationShimController::computeVelocityCommands(pose, velocity, goal_checker);
  if (!in_rotation_) {
    return command;  // DWB already checked its complete translation/rotation trajectory.
  }
  try {
    auto * costmap = costmap_ros_->getCostmap();
    std::unique_lock<nav2_costmap_2d::Costmap2D::mutex_t> lock(*costmap->getMutex());
    std::lock_guard<std::mutex> parameters_lock(mutex_);
    const auto point = transformPoseToBaseFrame(getSampledPathPt());
    if (rotationIsSafe(
        pose, command.twist.angular.z, std::atan2(point.position.y, point.position.x)))
    {
      return command;
    }
  } catch (const std::runtime_error &) {
    // A missing path/transform cannot authorize an unchecked rotation.
  }
  // Match the upstream shim's collision fallback: ask DWB for a checked path
  // command, which can translate out of a corner instead of rotating into it.
  path_updated_ = false;
  in_rotation_ = false;
  command = primary_controller_->computeVelocityCommands(pose, velocity, goal_checker);
  last_angular_vel_ = command.twist.angular.z;
  return command;
}

bool CheckedRotationShimController::rotationIsSafe(
  const geometry_msgs::msg::PoseStamped & pose, double angular_velocity, double heading_error)
{
  double duration = simulate_ahead_time_;
  if (angular_velocity * heading_error > 0.0) {
    const double remaining = std::max(0.0, std::abs(heading_error) - angular_disengage_threshold_);
    // Include at least the next command interval, even when it crosses the exit
    // angle. Check the last sample before stopping, for either rotation sign.
    duration = std::min(duration, std::max(control_duration_, remaining / std::abs(angular_velocity)));
  }
  const double initial_yaw = tf2::getYaw(pose.pose.orientation);
  double elapsed = 0.0;
  while (true) {
    const double cost = collision_checker_->footprintCostAtPose(
      pose.pose.position.x, pose.pose.position.y, initial_yaw + angular_velocity * elapsed,
      costmap_ros_->getRobotFootprint());
    if (cost >= nav2_costmap_2d::LETHAL_OBSTACLE) {
      return false;
    }
    if (elapsed >= duration) {
      return true;
    }
    elapsed = std::min(duration, elapsed + control_duration_);
  }
}
}  // namespace malbut_bringup

PLUGINLIB_EXPORT_CLASS(malbut_bringup::CheckedRotationShimController, nav2_core::Controller)
