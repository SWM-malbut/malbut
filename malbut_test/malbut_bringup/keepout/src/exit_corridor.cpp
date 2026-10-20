// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include "malbut_bringup/keepout_escape.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <queue>
#include "nav2_costmap_2d/cost_values.hpp"
#include "nav2_costmap_2d/footprint.hpp"

namespace malbut_bringup
{
namespace
{
using nav2_costmap_2d::LETHAL_OBSTACLE;
using nav2_costmap_2d::NO_INFORMATION;
using Pose = geometry_msgs::msg::Pose2D;

// Separating-axis test includes cell edges and the polygon interior, not only its centre.
bool intersects(
  const std::vector<geometry_msgs::msg::Point> & polygon, double x, double y, double half)
{
  std::vector<std::pair<double, double>> axes{{1, 0}, {0, 1}};
  for (size_t i = 0; i < polygon.size(); ++i) {
    const auto & a = polygon[i];
    const auto & b = polygon[(i + 1) % polygon.size()];
    axes.emplace_back(a.y - b.y, b.x - a.x);
  }
  for (const auto & axis : axes) {
    double low = std::numeric_limits<double>::max(), high = -low;
    for (const auto & point : polygon) {
      const double p = axis.first * point.x + axis.second * point.y;
      low = std::min(low, p);
      high = std::max(high, p);
    }
    const double centre = axis.first * x + axis.second * y;
    const double radius = half * (std::abs(axis.first) + std::abs(axis.second));
    if (high < centre - radius - 1e-9 || low > centre + radius + 1e-9) {
      return false;
    }
  }
  return true;
}

Pose along(const ExitCorridor & exit, double distance)
{
  Pose pose = exit.start;
  pose.x += exit.direction * distance * std::cos(pose.theta);
  pose.y += exit.direction * distance * std::sin(pose.theta);
  return pose;
}

std::set<unsigned int> lethal(
  const nav2_costmap_2d::Costmap2D & map, const std::set<unsigned int> & cells)
{
  std::set<unsigned int> result;
  for (auto index : cells) {
    if (map.getCharMap()[index] == LETHAL_OBSTACLE) {
      result.insert(index);
    }
  }
  return result;
}
}  // namespace

bool footprintCells(
  const nav2_costmap_2d::Costmap2D & map, const std::vector<geometry_msgs::msg::Point> & footprint,
  const Pose & pose, std::set<unsigned int> & cells)
{
  cells.clear();
  if (footprint.size() < 3 || !std::isfinite(pose.x) || !std::isfinite(pose.y) ||
    !std::isfinite(pose.theta))
  {
    return false;
  }
  std::vector<geometry_msgs::msg::Point> polygon;
  nav2_costmap_2d::transformFootprint(pose.x, pose.y, pose.theta, footprint, polygon);
  double left = polygon[0].x, right = left, bottom = polygon[0].y, top = bottom;
  for (const auto & p : polygon) {
    if (!std::isfinite(p.x) || !std::isfinite(p.y)) {return false;}
    left = std::min(left, p.x); right = std::max(right, p.x);
    bottom = std::min(bottom, p.y); top = std::max(top, p.y);
  }
  unsigned int x0, y0, x1, y1;
  if (!map.worldToMap(left, bottom, x0, y0) || !map.worldToMap(right, top, x1, y1)) {
    return false;  // A path beyond the known mask is not an escape route.
  }
  // Include neighbouring cells when a polygon edge lies exactly on a grid line.
  if (x0) {--x0;}
  if (y0) {--y0;}
  for (unsigned int y = y0; y <= y1; ++y) {
    for (unsigned int x = x0; x <= x1; ++x) {
      double wx, wy;
      map.mapToWorld(x, y, wx, wy);
      if (intersects(polygon, wx, wy, map.getResolution() / 2)) {
        const auto index = map.getIndex(x, y);
        if (map.getCharMap()[index] == NO_INFORMATION) {return false;}
        cells.insert(index);
      }
    }
  }
  return !cells.empty();
}

bool makeExit(
  const nav2_costmap_2d::Costmap2D & map, const std::vector<geometry_msgs::msg::Point> & footprint,
  const Pose & pose, double maximum_distance, ExitCorridor & exit)
{
  std::set<unsigned int> cells;
  if (!footprintCells(map, footprint, pose, cells)) {return false;}
  const auto touched = lethal(map, cells);
  if (touched.empty()) {return false;}

  // Only components already touching the body can be relaxed, never a second zone.
  auto component = touched;
  std::queue<unsigned int> pending;
  for (auto index : touched) {pending.push(index);}
  const auto width = map.getSizeInCellsX(), height = map.getSizeInCellsY();
  while (!pending.empty()) {
    const auto index = pending.front();
    pending.pop();
    const int x = index % width, y = index / width;
    for (const auto & d : {std::pair<int, int>{1, 0}, {-1, 0}, {0, 1}, {0, -1}}) {
      const int nx = x + d.first, ny = y + d.second;
      if (nx < 0 || ny < 0 || nx >= static_cast<int>(width) || ny >= static_cast<int>(height)) {
        continue;
      }
      const auto neighbour = map.getIndex(nx, ny);
      if (map.getCharMap()[neighbour] == LETHAL_OBSTACLE && component.insert(neighbour).second) {
        pending.push(neighbour);
      }
    }
  }
  bool found = false;
  // The configured controller supports forward/reverse, not lateral motion.
  // A straight exit avoids requiring an in-place turn inside a restricted zone.
  for (double direction : {1.0, -1.0}) {
    ExitCorridor candidate;
    candidate.start = pose;
    candidate.direction = direction;
    candidate.cells = touched;
    const double step = map.getResolution() / 2;
    for (double distance = step; distance <= maximum_distance + 1e-9; distance += step) {
      if (!footprintCells(map, footprint, along(candidate, distance), cells)) {break;}
      const auto overlap = lethal(map, cells);
      if (!std::includes(component.begin(), component.end(), overlap.begin(), overlap.end())) {
        break;
      }
      candidate.cells.insert(overlap.begin(), overlap.end());
      if (overlap.empty()) {
        candidate.length = distance;
        if (!found || distance < exit.length) {exit = candidate; found = true;}
        break;
      }
    }
  }
  return found;
}

bool advanceExit(
  const nav2_costmap_2d::Costmap2D & map, const std::vector<geometry_msgs::msg::Point> & footprint,
  const Pose & pose, ExitCorridor & exit)
{
  const double dx = pose.x - exit.start.x, dy = pose.y - exit.start.y;
  const double forward = exit.direction *
    (dx * std::cos(exit.start.theta) + dy * std::sin(exit.start.theta));
  const double side = -dx * std::sin(exit.start.theta) + dy * std::cos(exit.start.theta);
  const double yaw_error = std::remainder(pose.theta - exit.start.theta, 2 * M_PI);
  if (forward < exit.progress - map.getResolution() || std::abs(side) > 0.05 ||
    std::abs(yaw_error) > 0.15)
  {
    return false;
  }
  exit.progress = std::max(exit.progress, forward);
  std::set<unsigned int> remaining, cells;
  for (double d = exit.progress; d <= exit.length; d += map.getResolution() / 2) {
    if (!footprintCells(map, footprint, along(exit, d), cells)) {return false;}
    remaining.insert(cells.begin(), cells.end());
  }
  // Close the corridor behind the body. It cannot be extended or used to re-enter.
  for (auto it = exit.cells.begin(); it != exit.cells.end();) {
    if (!remaining.count(*it)) {it = exit.cells.erase(it);} else {++it;}
  }
  return true;
}
}  // namespace malbut_bringup
