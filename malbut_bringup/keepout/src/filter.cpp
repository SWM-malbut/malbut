// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include "malbut_bringup/keepout.hpp"
#include <algorithm>
#include <cmath>
#include "nav2_core/exceptions.hpp"
#include "pluginlib/class_list_macros.hpp"
#include "tf2_geometry_msgs/tf2_geometry_msgs.hpp"

namespace malbut_bringup::keepout
{
void Filter::initializeFilter(const std::string & topic)
{
  auto node = node_.lock();
  rclcpp::SubscriptionOptions options; options.callback_group = callback_group_;
  info_sub_ = node->create_subscription<nav2_msgs::msg::CostmapFilterInfo>(
    topic, rclcpp::QoS(1).transient_local().reliable(),
    [this, options](nav2_msgs::msg::CostmapFilterInfo::SharedPtr info) {
      std::lock_guard<mutex_t> lock(*getMutex());
      if (info->type != 0 || info->base != 0 || info->multiplier != 1 || info->filter_mask_topic.empty()) {
        mask_.reset(); scene_ = Scene(); current_ = false; return;
      }
      if (mask_sub_ && mask_topic_ == info->filter_mask_topic) {return;}
      mask_topic_ = info->filter_mask_topic; mask_.reset(); scene_ = Scene();
      mask_sub_ = node_.lock()->create_subscription<nav_msgs::msg::OccupancyGrid>(
        mask_topic_, rclcpp::QoS(1).transient_local().reliable(),
        [this](nav_msgs::msg::OccupancyGrid::SharedPtr message) {
          std::lock_guard<mutex_t> guard(*getMutex());
          const auto & m = message->info;
          const auto & q = m.origin.orientation;
          ++revision_; mask_.reset(); scene_ = Scene(); current_ = false;
          if (message->header.frame_id.empty() || !m.width || !m.height ||
            message->data.size() != static_cast<size_t>(m.width) * m.height ||
            !std::isfinite(m.resolution) || m.resolution <= 0 ||
            !std::isfinite(m.origin.position.x) || !std::isfinite(m.origin.position.y) ||
            !std::isfinite(q.x + q.y + q.z + q.w) || std::abs(q.x) > 1e-6 ||
            std::abs(q.y) > 1e-6 || std::abs(q.z) > 1e-6 || std::abs(std::abs(q.w) - 1) > 1e-6 ||
            !std::all_of(message->data.begin(), message->data.end(), [](auto v) {return v >= -1 && v <= 100;}))
          {return;}
          mask_frame_ = message->header.frame_id;
          mask_ = std::make_shared<Grid>(*message);
        }, options);
    }, options);
  pose_sub_ = node->create_subscription<geometry_msgs::msg::PoseWithCovarianceStamped>(
    "/amcl_pose", rclcpp::QoS(1),
    [this](geometry_msgs::msg::PoseWithCovarianceStamped::SharedPtr pose) {
      std::lock_guard<mutex_t> lock(*getMutex()); localization_ = *pose;
    }, options);
}

void Filter::updateBounds(double x, double y, double yaw, double * min_x, double * min_y,
  double * max_x, double * max_y)
{
  CostmapFilter::updateBounds(x, y, yaw, min_x, min_y, max_x, max_y);
  // Rebuild the physical snapshot from layers, never from yesterday's zone costs.
  const auto * map = layered_costmap_->getCostmap();
  *min_x = std::min(*min_x, map->getOriginX());
  *min_y = std::min(*min_y, map->getOriginY());
  *max_x = std::max(*max_x, map->getOriginX() + map->getSizeInCellsX() * map->getResolution());
  *max_y = std::max(*max_y, map->getOriginY() + map->getSizeInCellsY() * map->getResolution());
}

void Filter::process(Grid & master, int min_i, int min_j, int max_i, int max_j, const Pose &)
{
  std::lock_guard<mutex_t> lock(*getMutex());
  current_ = false; scene_ = Scene();
  if (!mask_) {
    master.resetMapToValue(min_i, min_j, max_i, max_j, nav2_costmap_2d::LETHAL_OBSTACLE);
    return;
  }
  tf2::Transform transform; transform.setIdentity();
  const bool empty = mask_->getSizeInCellsX() == 1 && mask_->getSizeInCellsY() == 1 &&
    mask_->getCost(0, 0) == nav2_costmap_2d::FREE_SPACE;
  if (!empty && mask_frame_ != layered_costmap_->getGlobalFrameID()) {
    try {
      tf2::fromMsg(tf_->lookupTransform(mask_frame_, layered_costmap_->getGlobalFrameID(),
        tf2::TimePointZero, transform_tolerance_).transform, transform);
    } catch (const tf2::TransformException &) {
      master.resetMapToValue(min_i, min_j, max_i, max_j, nav2_costmap_2d::LETHAL_OBSTACLE);
      return;
    }
  }
  scene_.physical = std::make_shared<Grid>(master);
  scene_.mask = mask_; scene_.to_mask = transform;
  scene_.footprint = layered_costmap_->getFootprint();
  scene_.stamp = clock_->now(); scene_.revision = revision_;
  const double age = (scene_.stamp - rclcpp::Time(localization_.header.stamp)).seconds();
  const auto & cov = localization_.pose.covariance;
  const auto & q = localization_.pose.pose.orientation;
  const double norm = q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w;
  scene_.confident = localization_.header.frame_id == mask_frame_ && age >= 0 && age <= 10 &&
    std::isfinite(norm) && std::abs(norm - 1) < 1e-3 &&
    std::isfinite(cov[0]) && cov[0] >= 0 && cov[0] <= 0.04 &&
    std::isfinite(cov[7]) && cov[7] >= 0 && cov[7] <= 0.04 &&
    std::isfinite(cov[35]) && cov[35] >= 0 && cov[35] <= 0.09;
  for (int y = min_j; y < max_j; ++y) {
    for (int x = min_i; x < max_i; ++x) {
      double wx, wy; master.mapToWorld(x, y, wx, wy);
      const auto point = transform * tf2::Vector3(wx, wy, 0);
      unsigned int mx, my;
      if (!mask_->worldToMap(point.x(), point.y(), mx, my)) {continue;}
      const auto zone = mask_->getCost(mx, my), previous = master.getCost(x, y);
      // Unknown physical space and real obstacles are never cleared by a zone.
      if (zone == nav2_costmap_2d::LETHAL_OBSTACLE ||
        (zone != nav2_costmap_2d::NO_INFORMATION && zone > previous)) {master.setCost(x, y, zone);}
    }
  }
  current_ = true;
}

Scene Filter::snapshot()
{
  std::lock_guard<mutex_t> lock(*getMutex());
  if (!current_ || !scene_.physical || !scene_.mask ||
    (clock_->now() - scene_.stamp).seconds() > 1.5)
  {throw nav2_core::PlannerException("Keepout: map or transform is missing/stale");}
  return scene_;
}

void Filter::resetFilter()
{
  std::lock_guard<mutex_t> lock(*getMutex());
  info_sub_.reset(); mask_sub_.reset(); pose_sub_.reset(); mask_.reset();
  scene_ = Scene(); localization_ = geometry_msgs::msg::PoseWithCovarianceStamped(); current_ = false;
}
}  // namespace malbut_bringup::keepout

PLUGINLIB_EXPORT_CLASS(malbut_bringup::keepout::Filter, nav2_costmap_2d::Layer)
