// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include "malbut_bringup/keepout_escape.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include "nav2_costmap_2d/cost_values.hpp"
#include "nav2_costmap_2d/footprint.hpp"
#include "pluginlib/class_list_macros.hpp"
#include "tf2_geometry_msgs/tf2_geometry_msgs.hpp"

namespace malbut_bringup
{
using nav2_costmap_2d::LETHAL_OBSTACLE;
using nav2_costmap_2d::NO_INFORMATION;

void KeepoutEscapeFilter::initializeFilter(const std::string & info_topic)
{
  std::lock_guard<mutex_t> guard(*getMutex());
  auto node = node_.lock();
  if (!node) {throw std::runtime_error("Keepout filter node expired");}
  declareParameter("escape_max_distance", rclcpp::ParameterValue(1.0));
  declareParameter("escape_timeout", rclcpp::ParameterValue(15.0));
  node->get_parameter(name_ + ".escape_max_distance", maximum_distance_);
  node->get_parameter(name_ + ".escape_timeout", timeout_);
  if (!std::isfinite(maximum_distance_) || maximum_distance_ <= 0 ||
    !std::isfinite(timeout_) || timeout_ <= 0)
  {
    throw std::invalid_argument("Keepout escape distance and timeout must be positive");
  }
  rclcpp::SubscriptionOptions options;
  options.callback_group = callback_group_;
  info_sub_ = node->create_subscription<nav2_msgs::msg::CostmapFilterInfo>(
    info_topic, rclcpp::QoS(1).transient_local().reliable(),
    std::bind(&KeepoutEscapeFilter::info, this, std::placeholders::_1), options);
  // Volatile: an old successful localization must never authorize a new escape.
  trust_sub_ = node->create_subscription<geometry_msgs::msg::PoseStamped>(
    "/malbut/localization/verified_pose", rclcpp::QoS(1),
    [this](geometry_msgs::msg::PoseStamped::SharedPtr message) {
      std::lock_guard<mutex_t> lock(*getMutex());
      trusted_ = *message;
      received_ = Steady::now();
    }, options);
}

void KeepoutEscapeFilter::info(const nav2_msgs::msg::CostmapFilterInfo::SharedPtr message)
{
  std::lock_guard<mutex_t> guard(*getMutex());
  if (message->type != 0 || message->base != 0.0 || message->multiplier != 1.0 ||
    message->filter_mask_topic.empty())
  {
    current_ = false;
    mask_.reset();
    escaping_ = false;
    return;
  }
  if (mask_sub_ && mask_topic_ == message->filter_mask_topic) {return;}
  mask_topic_ = message->filter_mask_topic;
  mask_.reset();
  escaping_ = false;
  rclcpp::SubscriptionOptions options;
  options.callback_group = callback_group_;
  mask_sub_ = node_.lock()->create_subscription<nav_msgs::msg::OccupancyGrid>(
    mask_topic_, rclcpp::QoS(1).transient_local().reliable(),
    std::bind(&KeepoutEscapeFilter::mask, this, std::placeholders::_1), options);
}

void KeepoutEscapeFilter::mask(const nav_msgs::msg::OccupancyGrid::SharedPtr message)
{
  std::lock_guard<mutex_t> guard(*getMutex());
  const auto & m = message->info;
  const auto & q = m.origin.orientation;
  escaping_ = blocked_ = moving_ = false;
  trusted_ = geometry_msgs::msg::PoseStamped();  // Evidence must follow this mask revision.
  mask_received_ = clock_->now();
  if (message->header.frame_id.empty() || !m.width || !m.height ||
    message->data.size() != static_cast<size_t>(m.width) * m.height ||
    !std::isfinite(m.resolution) || m.resolution <= 0 ||
    !std::isfinite(m.origin.position.x) || !std::isfinite(m.origin.position.y) ||
    !std::isfinite(q.x) || !std::isfinite(q.y) || !std::isfinite(q.z) ||
    std::abs(q.x) > 1e-6 || std::abs(q.y) > 1e-6 || std::abs(q.z) > 1e-6 ||
    !std::isfinite(q.w) || std::abs(std::abs(q.w) - 1.0) > 1e-6 ||
    !std::all_of(message->data.begin(), message->data.end(), [](auto v) {return v >= -1 && v <= 100;}))
  {
    current_ = false;
    mask_.reset();
    return;
  }
  mask_ = std::make_unique<nav2_costmap_2d::Costmap2D>(*message);
  mask_frame_ = message->header.frame_id;
}

void KeepoutEscapeFilter::updateBounds(
  double x, double y, double yaw, double * min_x, double * min_y,
  double * max_x, double * max_y)
{
  CostmapFilter::updateBounds(x, y, yaw, min_x, min_y, max_x, max_y);
  // Rebuild from the physical layers every cycle, including when a lease expires
  // or the robot stops. Otherwise softened cells can survive in the master grid.
  const auto * map = layered_costmap_->getCostmap();
  *min_x = std::min(*min_x, map->getOriginX());
  *min_y = std::min(*min_y, map->getOriginY());
  *max_x = std::max(*max_x, map->getOriginX() + map->getSizeInCellsX() * map->getResolution());
  *max_y = std::max(*max_y, map->getOriginY() + map->getSizeInCellsY() * map->getResolution());
}

bool KeepoutEscapeFilter::confident(const geometry_msgs::msg::Pose2D & pose) const
{
  const double age = (clock_->now() - rclcpp::Time(trusted_.header.stamp)).seconds();
  const auto & p = trusted_.pose.position;
  const auto & q = trusted_.pose.orientation;
  const double norm = q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w;
  const double yaw = std::atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y*q.y + q.z*q.z));
  return trusted_.header.frame_id == mask_frame_ && age >= 0 && age <= 0.75 &&
         rclcpp::Time(trusted_.header.stamp) >= mask_received_ &&
         std::chrono::duration<double>(Steady::now() - received_).count() <= 0.75 &&
         std::isfinite(p.x) && std::isfinite(p.y) && std::isfinite(yaw) &&
         std::isfinite(norm) && std::abs(norm - 1.0) < 1e-3 &&
         std::hypot(pose.x - p.x, pose.y - p.y) <= 0.10 &&
         std::abs(std::remainder(pose.theta - yaw, 2 * M_PI)) <= 0.15;
}

void KeepoutEscapeFilter::process(
  nav2_costmap_2d::Costmap2D & master, int min_i, int min_j, int max_i, int max_j,
  const geometry_msgs::msg::Pose2D & pose)
{
  std::lock_guard<mutex_t> guard(*getMutex());
  current_ = false;
  if (!mask_) {return;}
  const std::string global = layered_costmap_->getGlobalFrameID();
  geometry_msgs::msg::Pose2D in_mask;
  if (!transformPose(global, pose, mask_frame_, in_mask)) {return;}
  tf2::Transform transform;
  transform.setIdentity();
  if (global != mask_frame_) {
    try {
      tf2::fromMsg(tf_->lookupTransform(
          mask_frame_, global, tf2::TimePointZero, transform_tolerance_).transform, transform);
    } catch (const tf2::TransformException &) {return;}
  }

  auto footprint = layered_costmap_->getFootprint();
  // The controller rasterizes the outline in the master grid, while the mask
  // may have another resolution/frame. Include the cell-centre rounding error
  // so a diagonal outline cannot remain trapped on the corridor's edge.
  nav2_costmap_2d::padFootprint(
    footprint, std::max(master.getResolution(), mask_->getResolution()) / std::sqrt(2.0));
  std::set<unsigned int> occupied;
  const bool known = footprintCells(*mask_, footprint, in_mask, occupied);
  const bool overlaps = known && std::any_of(occupied.begin(), occupied.end(), [this](auto i) {
      return mask_->getCharMap()[i] == LETHAL_OBSTACLE;
    });
  if (known && !overlaps) {
    escaping_ = blocked_ = moving_ = false;
    exit_.cells.clear();
  }
  bool permit = known && overlaps && confident(in_mask);
  if (escaping_ &&
    ((moving_ && std::chrono::duration<double>(Steady::now() - started_).count() > timeout_) ||
    !advanceExit(*mask_, footprint, in_mask, exit_)))
  {
    escaping_ = false;
    blocked_ = true;
  }
  if (escaping_ && !moving_ && exit_.progress > 0.01) {
    // Waiting for a user command must not consume the escape time budget.
    moving_ = true;
    started_ = Steady::now();
  }
  if (permit && !escaping_ && !blocked_) {
    escaping_ = makeExit(*mask_, footprint, in_mask, maximum_distance_, exit_);
    blocked_ = !escaping_;
    moving_ = false;
    if (escaping_) {
      RCLCPP_INFO(logger_, "Keepout: verified pose; bounded %.2f m %s exit corridor",
        exit_.length, exit_.direction > 0 ? "forward" : "reverse");
    }
  }
  permit = permit && escaping_;
  for (int y = min_j; y < max_j; ++y) {
    for (int x = min_i; x < max_i; ++x) {
      double wx, wy;
      master.mapToWorld(x, y, wx, wy);
      const auto point = transform * tf2::Vector3(wx, wy, 0);
      unsigned int mx, my;
      if (!mask_->worldToMap(point.x(), point.y(), mx, my)) {continue;}
      auto cost = mask_->getCost(mx, my);
      if (cost == NO_INFORMATION) {continue;}
      const auto previous = master.getCost(x, y);
      if (permit && cost == LETHAL_OBSTACLE && exit_.cells.count(mask_->getIndex(mx, my))) {
        // Only the synthetic zone cost is softened. A real obstacle or unknown
        // physical cell keeps its original cost and cannot become traversable.
        if (previous == NO_INFORMATION) {continue;}
        cost = nav2_costmap_2d::MAX_NON_OBSTACLE;
      }
      if (cost > previous || previous == NO_INFORMATION) {master.setCost(x, y, cost);}
    }
  }
  current_ = true;
}

void KeepoutEscapeFilter::resetFilter()
{
  std::lock_guard<mutex_t> guard(*getMutex());
  info_sub_.reset(); mask_sub_.reset(); trust_sub_.reset(); mask_.reset();
  trusted_ = geometry_msgs::msg::PoseStamped();
  escaping_ = blocked_ = moving_ = false;
  exit_.cells.clear();
  current_ = false;
}
}  // namespace malbut_bringup

PLUGINLIB_EXPORT_CLASS(malbut_bringup::KeepoutEscapeFilter, nav2_costmap_2d::Layer)
