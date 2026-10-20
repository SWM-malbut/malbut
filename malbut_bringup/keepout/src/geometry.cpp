// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include "malbut_bringup/keepout.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <queue>
#include "nav2_core/exceptions.hpp"
#include "nav2_costmap_2d/footprint.hpp"

namespace malbut_bringup::keepout
{
using nav2_costmap_2d::LETHAL_OBSTACLE;
using nav2_costmap_2d::NO_INFORMATION;
namespace
{
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

std::set<unsigned int> overlap(const Scene & scene, const Pose & pose, double margin)
{
  // zone_filter's documented one-cell-free sentinel means no saved zones.
  if (scene.mask && scene.mask->getSizeInCellsX() == 1 && scene.mask->getSizeInCellsY() == 1 &&
    scene.mask->getCost(0, 0) == nav2_costmap_2d::FREE_SPACE) {return {};}
  const auto point = scene.to_mask * tf2::Vector3(pose.x, pose.y, 0);
  Pose mapped;
  mapped.x = point.x(); mapped.y = point.y();
  mapped.theta = pose.theta + std::atan2(scene.to_mask.getBasis()[1][0], scene.to_mask.getBasis()[0][0]);
  auto footprint = scene.footprint;
  nav2_costmap_2d::padFootprint(footprint, margin);
  std::set<unsigned int> cells, result;
  if (!scene.mask || !footprintCells(*scene.mask, footprint, mapped, cells)) {
    throw nav2_core::PlannerException("Keepout: footprint outside the known zone map");
  }
  for (auto i : cells) {
    if (scene.mask->getCharMap()[i] == LETHAL_OBSTACLE) {result.insert(i);}
  }
  return result;
}

std::set<unsigned int> component(const Grid & mask, std::set<unsigned int> seeds)
{
  std::queue<unsigned int> pending;
  for (auto i : seeds) {pending.push(i);}
  const int width = mask.getSizeInCellsX(), height = mask.getSizeInCellsY();
  while (!pending.empty()) {
    const auto i = pending.front(); pending.pop();
    for (const auto & d : {std::pair<int, int>{1, 0}, {-1, 0}, {0, 1}, {0, -1}}) {
      const int x = i % width + d.first, y = i / width + d.second;
      if (x < 0 || y < 0 || x >= width || y >= height) {continue;}
      const auto j = mask.getIndex(x, y);
      if (mask.getCharMap()[j] == LETHAL_OBSTACLE && seeds.insert(j).second) {pending.push(j);}
    }
  }
  return seeds;
}

bool physicalFree(const Scene & scene, const Pose & pose)
{
  auto footprint = scene.footprint;
  nav2_costmap_2d::padFootprint(footprint, 0.02);
  std::set<unsigned int> cells;
  if (!scene.physical || !footprintCells(*scene.physical, footprint, pose, cells)) {return false;}
  return std::none_of(cells.begin(), cells.end(), [&scene](auto i) {
    return scene.physical->getCharMap()[i] >= LETHAL_OBSTACLE;
  });
}

std::vector<Pose> shortestExit(const Scene & scene, const Pose & start, double limit)
{
  if (!scene.confident) {throw nav2_core::PlannerException("Keepout: localization is uncertain or stale");}
  if (!physicalFree(scene, start)) {throw nav2_core::PlannerException("Keepout: physical collision at start");}
  const auto touched = overlap(scene, start);
  const bool already_clear = touched.empty();
  const auto allowed = component(*scene.mask, already_clear ? overlap(scene, start, .05) : touched);
  if (allowed.empty()) {return {};}
  // The mecanum base translates without rotating. Dijkstra is distance-optimal
  // on this fixed-heading, eight-connected lattice, not in continuous SE(2).
  const double step = scene.physical->getResolution();
  const int radius = std::ceil(limit / step), width = 2 * radius + 1;
  const int count = width * width, initial = radius * width + radius;
  std::vector<double> distances(count, std::numeric_limits<double>::infinity());
  std::vector<int> previous(count, -1);
  using Entry = std::pair<double, int>;
  std::priority_queue<Entry, std::vector<Entry>, std::greater<Entry>> queue;
  auto position = [&](int i) {
    Pose p = start; p.x += (i % width - radius) * step; p.y += (i / width - radius) * step;
    return p;
  };
  auto allowed_pose = [&](const Pose & p) {
    if (!physicalFree(scene, p)) {return false;}
    try {
      const auto touched = overlap(scene, p);
      return (!already_clear || touched.empty()) &&
             std::includes(allowed.begin(), allowed.end(), touched.begin(), touched.end());
    } catch (const nav2_core::PlannerException &) {return false;}
  };
  distances[initial] = 0; queue.emplace(0, initial);
  while (!queue.empty()) {
    const auto [distance, i] = queue.top(); queue.pop();
    if (distance != distances[i]) {continue;}
    const auto p = position(i);
    if (overlap(scene, p, 0.05).empty()) {
      std::vector<Pose> path;
      for (int at = i; at != -1; at = previous[at]) {path.push_back(position(at));}
      std::reverse(path.begin(), path.end());
      return path;
    }
    const int x = i % width, y = i / width;
    for (int dy = -1; dy <= 1; ++dy) {
      for (int dx = -1; dx <= 1; ++dx) {
        const int nx = x + dx, ny = y + dy;
        if ((!dx && !dy) || nx < 0 || ny < 0 || nx >= width || ny >= width) {continue;}
        const int j = ny * width + nx;
        const double length = std::hypot(dx, dy) * step, next = distance + length;
        if (next > limit || next >= distances[j]) {continue;}
        bool safe = true;
        // Sweep the whole filled footprint; checking endpoints could cut a corner.
        const int samples = std::ceil(length / 0.01);
        for (int k = 1; k <= samples && safe; ++k) {
          Pose q = p; q.x += dx * step * k / samples; q.y += dy * step * k / samples;
          safe = allowed_pose(q);
        }
        if (safe) {distances[j] = next; previous[j] = i; queue.emplace(next, j);}
      }
    }
  }
  throw nav2_core::PlannerException("Keepout: no collision-free exit within the search limit");
}
}  // namespace malbut_bringup::keepout
