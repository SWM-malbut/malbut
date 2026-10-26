// Copyright 2026 Malbut. Licensed under the Apache License, Version 2.0.
#ifndef MALBUT_BRINGUP__CHECKED_ROTATION_SHIM_CONTROLLER_HPP_
#define MALBUT_BRINGUP__CHECKED_ROTATION_SHIM_CONTROLLER_HPP_

#include "nav2_rotation_shim_controller/nav2_rotation_shim_controller.hpp"

namespace malbut_bringup
{
// Humble's shim collision horizon uses the entry threshold even while rotating
// to the smaller exit threshold. Validate its returned rotation before use.
class CheckedRotationShimController
  : public nav2_rotation_shim_controller::RotationShimController
{
public:
  geometry_msgs::msg::TwistStamped computeVelocityCommands(
    const geometry_msgs::msg::PoseStamped & pose,
    const geometry_msgs::msg::Twist & velocity,
    nav2_core::GoalChecker * goal_checker) override;

protected:
  bool rotationIsSafe(
    const geometry_msgs::msg::PoseStamped & pose, double angular_velocity,
    double heading_error);
};
}  // namespace malbut_bringup

#endif  // MALBUT_BRINGUP__CHECKED_ROTATION_SHIM_CONTROLLER_HPP_
