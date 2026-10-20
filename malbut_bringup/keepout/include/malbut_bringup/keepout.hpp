// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <memory>
#include <set>
#include <vector>
#include "dwb_core/dwb_local_planner.hpp"
#include "geometry_msgs/msg/pose_with_covariance_stamped.hpp"
#include "nav2_costmap_2d/costmap_filters/costmap_filter.hpp"
#include "nav2_msgs/msg/costmap_filter_info.hpp"
#include "nav2_navfn_planner/navfn_planner.hpp"
#include "tf2/LinearMath/Transform.h"

namespace malbut_bringup::keepout
{
using Pose = geometry_msgs::msg::Pose2D;
using Grid = nav2_costmap_2d::Costmap2D;
struct Scene
{
  std::shared_ptr<const Grid> physical, mask;
  std::vector<geometry_msgs::msg::Point> footprint;
  tf2::Transform to_mask;
  rclcpp::Time stamp{0, 0, RCL_ROS_TIME};
  uint64_t revision{0};
  bool confident{false};
};

bool footprintCells(const Grid & map, const std::vector<geometry_msgs::msg::Point> & footprint,
  const Pose & pose, std::set<unsigned int> & cells);
std::set<unsigned int> overlap(const Scene & scene, const Pose & pose, double margin = 0);
std::set<unsigned int> component(const Grid & mask, std::set<unsigned int> seeds);
bool physicalFree(const Scene & scene, const Pose & pose);
std::vector<Pose> shortestExit(const Scene & scene, const Pose & start, double limit = 3.0);

// Always writes the strict zone mask. Only the Nav2 planner/controller can read
// the separate physical map; no filter is disabled or corridor erased.
class Filter : public nav2_costmap_2d::CostmapFilter
{
public:
  void initializeFilter(const std::string & topic) override;
  void updateBounds(double x, double y, double yaw, double * min_x, double * min_y,
    double * max_x, double * max_y) override;
  void process(Grid & master, int min_i, int min_j, int max_i, int max_j,
    const Pose & pose) override;
  void resetFilter() override;
  bool isActive() {return mask_ != nullptr;}
  Scene snapshot();
private:
  rclcpp::Subscription<nav2_msgs::msg::CostmapFilterInfo>::SharedPtr info_sub_;
  rclcpp::Subscription<nav_msgs::msg::OccupancyGrid>::SharedPtr mask_sub_;
  rclcpp::Subscription<geometry_msgs::msg::PoseWithCovarianceStamped>::SharedPtr pose_sub_;
  std::shared_ptr<const Grid> mask_;
  geometry_msgs::msg::PoseWithCovarianceStamped localization_;
  std::string mask_topic_, mask_frame_;
  Scene scene_;
  uint64_t revision_{0};
};

class Planner : public nav2_navfn_planner::NavfnPlanner
{
public:
  void configure(const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent, std::string name,
    std::shared_ptr<tf2_ros::Buffer> tf,
    std::shared_ptr<nav2_costmap_2d::Costmap2DROS> map) override;
  nav_msgs::msg::Path createPlan(const geometry_msgs::msg::PoseStamped & start,
    const geometry_msgs::msg::PoseStamped & goal) override;
private:
  std::shared_ptr<Filter> filter_;
  rclcpp::Publisher<nav_msgs::msg::Path>::SharedPtr exit_pub_;
  bool exiting_{false};
};

class Controller : public dwb_core::DWBLocalPlanner
{
public:
  void configure(const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent, std::string name,
    std::shared_ptr<tf2_ros::Buffer> tf,
    std::shared_ptr<nav2_costmap_2d::Costmap2DROS> map) override;
  void setPlan(const nav_msgs::msg::Path & path) override;
  void setSpeedLimit(const double & speed, const bool & percentage) override;
  geometry_msgs::msg::TwistStamped computeVelocityCommands(
    const geometry_msgs::msg::PoseStamped & pose, const geometry_msgs::msg::Twist & velocity,
    nav2_core::GoalChecker * checker) override;
private:
  std::shared_ptr<Filter> filter_;
  std::shared_ptr<tf2_ros::Buffer> transforms_;
  rclcpp_lifecycle::LifecycleNode::WeakPtr owner_;
  nav_msgs::msg::Path path_;
  double speed_{0.08};
  bool exiting_{false};
  bool received_exit_path_{false};
  rclcpp::Time exit_started_{0, 0, RCL_ROS_TIME};
};
}  // namespace malbut_bringup::keepout
