// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include "malbut_bringup/keepout.hpp"
#include <algorithm>
#include <cmath>
#include "pluginlib/class_list_macros.hpp"
#include "nav2_core/exceptions.hpp"
#include "tf2/utils.h"
#include "tf2_geometry_msgs/tf2_geometry_msgs.hpp"

namespace malbut_bringup::keepout
{
namespace
{
Pose planar(const geometry_msgs::msg::PoseStamped & p)
{
  Pose result;
  result.x = p.pose.position.x; result.y = p.pose.position.y;
  result.theta = tf2::getYaw(p.pose.orientation); return result;
}

std::shared_ptr<Filter> findFilter(const std::shared_ptr<nav2_costmap_2d::Costmap2DROS> & map)
{
  for (const auto & plugin : *map->getLayeredCostmap()->getFilters()) {
    if (auto filter = std::dynamic_pointer_cast<Filter>(plugin)) {return filter;}
  }
  throw nav2_core::PlannerException("Keepout: the strict zone filter is required");
}
}  // namespace

void Planner::configure(const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent, std::string name,
  std::shared_ptr<tf2_ros::Buffer> tf, std::shared_ptr<nav2_costmap_2d::Costmap2DROS> map)
{
  NavfnPlanner::configure(parent, name, tf, map); filter_ = findFilter(map);
  // An ordinary publisher stays observable across the parent server's lifecycle.
  exit_pub_ = rclcpp::create_publisher<nav_msgs::msg::Path>(parent.lock(),
    "~/keepout_exit_path", rclcpp::QoS(1).transient_local());
}

nav_msgs::msg::Path Planner::createPlan(const geometry_msgs::msg::PoseStamped & start,
  const geometry_msgs::msg::PoseStamped & goal)
{
  const auto scene = filter_->snapshot();
  if (!overlap(scene, planar(goal), 0.03).empty()) {
    throw nav2_core::PlannerException("Keepout: goal footprint is in a restricted zone");
  }
  nav_msgs::msg::Path exit; exit.header = start.header;
  // The rolling local grid and DWB's rasterized footprint can touch a zone
  // before the exact polygon does. Also finish clearance from that narrow band;
  // shortestExit forbids entering the zone when the body is already outside.
  if (!overlap(scene, planar(start), .03).empty()) {exiting_ = true;}
  if (overlap(scene, planar(start), .05).empty()) {exiting_ = false;}
  if (!exiting_) {
    exit_pub_->publish(exit); return NavfnPlanner::createPlan(start, goal);
  }
  for (const auto & p : shortestExit(scene, planar(start))) {
    auto item = start; item.pose.position.x = p.x; item.pose.position.y = p.y;
    exit.poses.push_back(item);
  }
  exit_pub_->publish(exit);
  auto tail = NavfnPlanner::createPlan(exit.poses.back(), goal);
  if (tail.poses.empty()) {throw nav2_core::PlannerException("Keepout: exit found but destination is unreachable");}
  exit.poses.insert(exit.poses.end(), tail.poses.begin(), tail.poses.end());
  return exit;
}

void Controller::configure(const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent, std::string name,
  std::shared_ptr<tf2_ros::Buffer> tf, std::shared_ptr<nav2_costmap_2d::Costmap2DROS> map)
{
  DWBLocalPlanner::configure(parent, name, tf, map);
  filter_ = findFilter(map); transforms_ = tf; owner_ = parent;
}

void Controller::setPlan(const nav_msgs::msg::Path & path)
{
  DWBLocalPlanner::setPlan(path); path_ = path;
}

void Controller::setSpeedLimit(const double & speed, const bool & percentage)
{
  DWBLocalPlanner::setSpeedLimit(speed, percentage);
  speed_ = speed == 0 ? 0.08 : std::clamp(percentage ? 0.08 * speed / 100 : speed, 0.0, 0.08);
}

geometry_msgs::msg::TwistStamped Controller::computeVelocityCommands(
  const geometry_msgs::msg::PoseStamped & pose, const geometry_msgs::msg::Twist & velocity,
  nav2_core::GoalChecker * checker)
{
  const auto scene = filter_->snapshot();
  const auto current = planar(pose);
  const auto touched = overlap(scene, current);
  const bool was_exiting = exiting_;
  if (!overlap(scene, current, .03).empty()) {exiting_ = true;}
  if (overlap(scene, current, .05).empty()) {exiting_ = false;}
  if (exiting_ && !was_exiting) {
    received_exit_path_ = false; exit_started_ = scene.stamp;
  }
  auto result = geometry_msgs::msg::TwistStamped(); result.header = pose.header;
  if (!exiting_) {
    result = DWBLocalPlanner::computeVelocityCommands(pose, velocity, checker);
  } else {
    if (!scene.confident) {throw nav2_core::PlannerException("Keepout: localization is uncertain or stale");}
    auto awaitExit = [&](const char * reason) {
      // The local controller can reach the boundary before the 1 Hz global
      // replan. Stop while its first fixed-heading exit arrives, rather than
      // aborting under the ordinary 0.3 s controller failure tolerance.
      if (!received_exit_path_ && (scene.stamp - exit_started_).seconds() <= 2.0) {
        return result;
      }
      throw nav2_core::PlannerException(reason);
    };
    std::vector<Pose> exit;
    for (auto item : path_.poses) {
      item.header.frame_id = path_.header.frame_id; item.header.stamp = rclcpp::Time(0);
      if (item.header.frame_id != pose.header.frame_id) {
        try {item = transforms_->transform(item, pose.header.frame_id, tf2::durationFromSec(0.1));}
        catch (const tf2::TransformException & e) {throw nav2_core::PlannerException(e.what());}
      }
      exit.push_back(planar(item));
      if (overlap(scene, exit.back(), 0.05).empty()) {break;}
    }
    // NavFn can snap the first waypoint just beyond the clearance boundary.
    // That single nearby waypoint is a valid final exit segment; the deviation
    // and swept-footprint checks below still apply.
    if (exit.empty() || !overlap(scene, exit.back(), 0.05).empty()) {
      return awaitExit("Keepout: waiting for a complete exit path");
    }
    size_t nearest = 0;
    for (size_t i = 1; i < exit.size(); ++i) {
      if (std::hypot(exit[i].x - current.x, exit[i].y - current.y) <
        std::hypot(exit[nearest].x - current.x, exit[nearest].y - current.y)) {nearest = i;}
    }
    if (std::hypot(exit[nearest].x - current.x, exit[nearest].y - current.y) > 0.12 ||
      std::abs(std::remainder(exit[nearest].theta - current.theta, 2 * M_PI)) > 0.15)
    {return awaitExit("Keepout: pose deviated from the fixed-heading exit path");}
    received_exit_path_ = true;
    const auto & target = exit[std::min(nearest + 1, exit.size() - 1)];
    const double dx = target.x - current.x, dy = target.y - current.y;
    const double distance = std::hypot(dx, dy), scale = distance > 1e-6 ? std::min(speed_ / distance, 3.0) : 0;
    result.twist.linear.x = scale * (dx * std::cos(current.theta) + dy * std::sin(current.theta));
    result.twist.linear.y = scale * (-dx * std::sin(current.theta) + dy * std::cos(current.theta));
    result.twist.angular.z = std::clamp(2 * std::remainder(target.theta - current.theta, 2 * M_PI), -0.2, 0.2);
  }
  // Validate the complete swept body, including stopping room, against current
  // physical obstacles. Once clear, no later sample may re-enter a zone.
  const auto allowed = component(*scene.mask, touched);
  bool cleared = touched.empty();
  Pose projected = current;
  for (int step = 0; step < 10; ++step) {
    const auto & cmd = result.twist;
    projected.x += (cmd.linear.x * std::cos(projected.theta) - cmd.linear.y * std::sin(projected.theta)) * 0.05;
    projected.y += (cmd.linear.x * std::sin(projected.theta) + cmd.linear.y * std::cos(projected.theta)) * 0.05;
    projected.theta += cmd.angular.z * 0.05;
    const auto next = overlap(scene, projected);
    if ((cleared && !next.empty()) ||
      !std::includes(allowed.begin(), allowed.end(), next.begin(), next.end()) ||
      (exiting_ && !physicalFree(scene, projected)))
    {throw nav2_core::PlannerException("Keepout: motion would re-enter a zone or hit a physical obstacle");}
    cleared = cleared || next.empty();
  }
  return result;
}
}  // namespace malbut_bringup::keepout

PLUGINLIB_EXPORT_CLASS(malbut_bringup::keepout::Planner, nav2_core::GlobalPlanner)
PLUGINLIB_EXPORT_CLASS(malbut_bringup::keepout::Controller, nav2_core::Controller)
