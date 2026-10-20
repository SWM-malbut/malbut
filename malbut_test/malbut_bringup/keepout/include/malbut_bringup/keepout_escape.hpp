// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <chrono>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "geometry_msgs/msg/pose_stamped.hpp"
#include "nav2_costmap_2d/costmap_filters/costmap_filter.hpp"
#include "nav2_msgs/msg/costmap_filter_info.hpp"

namespace malbut_bringup
{
// A short, fixed-heading exit corridor. It never opens an entire restricted zone.
struct ExitCorridor
{
  geometry_msgs::msg::Pose2D start;
  double direction{1.0}, length{0.0}, progress{0.0};
  std::set<unsigned int> cells;
};

bool footprintCells(
  const nav2_costmap_2d::Costmap2D & map, const std::vector<geometry_msgs::msg::Point> & footprint,
  const geometry_msgs::msg::Pose2D & pose, std::set<unsigned int> & cells);
bool makeExit(
  const nav2_costmap_2d::Costmap2D & map, const std::vector<geometry_msgs::msg::Point> & footprint,
  const geometry_msgs::msg::Pose2D & pose, double maximum_distance, ExitCorridor & exit);
bool advanceExit(
  const nav2_costmap_2d::Costmap2D & map, const std::vector<geometry_msgs::msg::Point> & footprint,
  const geometry_msgs::msg::Pose2D & pose, ExitCorridor & exit);

class KeepoutEscapeFilter : public nav2_costmap_2d::CostmapFilter
{
public:
  void initializeFilter(const std::string & info_topic) override;
  void updateBounds(
    double x, double y, double yaw, double * min_x, double * min_y,
    double * max_x, double * max_y) override;
  void process(
    nav2_costmap_2d::Costmap2D & master, int min_i, int min_j, int max_i, int max_j,
    const geometry_msgs::msg::Pose2D & pose) override;
  void resetFilter() override;
  bool isActive() {return mask_ != nullptr;}

private:
  using Steady = std::chrono::steady_clock;
  void info(const nav2_msgs::msg::CostmapFilterInfo::SharedPtr message);
  void mask(const nav_msgs::msg::OccupancyGrid::SharedPtr message);
  bool confident(const geometry_msgs::msg::Pose2D & pose) const;
  rclcpp::Subscription<nav2_msgs::msg::CostmapFilterInfo>::SharedPtr info_sub_;
  rclcpp::Subscription<nav_msgs::msg::OccupancyGrid>::SharedPtr mask_sub_;
  rclcpp::Subscription<geometry_msgs::msg::PoseStamped>::SharedPtr trust_sub_;
  std::unique_ptr<nav2_costmap_2d::Costmap2D> mask_;
  std::string mask_frame_, mask_topic_;
  geometry_msgs::msg::PoseStamped trusted_;
  rclcpp::Time mask_received_{0, 0, RCL_ROS_TIME};
  Steady::time_point received_{}, started_{};
  ExitCorridor exit_;
  bool escaping_{false}, blocked_{false}, moving_{false};
  double maximum_distance_{1.0}, timeout_{15.0};
};
}  // namespace malbut_bringup
