// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include <gtest/gtest.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <functional>
#include <thread>

#include "dwb_core/dwb_local_planner.hpp"
#include "nav2_core/exceptions.hpp"
#include "nav2_costmap_2d/costmap_2d_ros.hpp"
#include "nav2_costmap_2d/costmap_filters/costmap_filter.hpp"
#include "nav2_costmap_2d/footprint_collision_checker.hpp"
#include "nav2_msgs/msg/costmap_filter_info.hpp"
#include "nav2_navfn_planner/navfn_planner.hpp"
#include "nav2_util/lifecycle_node.hpp"

using namespace std::chrono_literals;
using nav2_costmap_2d::LETHAL_OBSTACLE;
using nav2_costmap_2d::NO_INFORMATION;

namespace
{
geometry_msgs::msg::PoseStamped pose(double x, double y, double yaw = 0)
{
  geometry_msgs::msg::PoseStamped p;
  p.header.frame_id = "map";
  p.pose.position.x = x; p.pose.position.y = y;
  p.pose.orientation.z = std::sin(yaw / 2); p.pose.orientation.w = std::cos(yaw / 2);
  return p;
}

class GlobalKeepoutTest : public ::testing::Test
{
protected:
  static void SetUpTestSuite() {rclcpp::init(0, nullptr);}
  static void TearDownTestSuite() {rclcpp::shutdown();}

  void SetUp() override
  {
    source = std::make_shared<rclcpp::Node>("global_keepout_test_source");
    executor.add_node(source);
    global = configure("global_costmap"); local = configure("local_costmap");
    ASSERT_TRUE(global && local);
    ASSERT_EQ(global->getLayeredCostmap()->getFilters()->size(), 1u);
    ASSERT_TRUE(local->getLayeredCostmap()->getFilters()->empty());
    filter = std::dynamic_pointer_cast<nav2_costmap_2d::CostmapFilter>(
      global->getLayeredCostmap()->getFilters()->at(0));
    ASSERT_TRUE(filter);
    filter->activate();
    auto latched = rclcpp::QoS(1).transient_local().reliable();
    info_pub = source->create_publisher<nav2_msgs::msg::CostmapFilterInfo>(
      "/keepout_costmap_filter_info", latched);
    mask_pub = source->create_publisher<nav_msgs::msg::OccupancyGrid>(
      "/keepout_filter_mask", latched);
    nav2_msgs::msg::CostmapFilterInfo info;
    info.type = 0; info.base = 0; info.multiplier = 1;
    info.filter_mask_topic = "/keepout_filter_mask";
    info_pub->publish(info);
    ASSERT_TRUE(until([this]() {return mask_pub->get_subscription_count() > 0;}));
    mask.header.frame_id = "map";
    mask.info.width = mask.info.height = 80; mask.info.resolution = .05;
    mask.info.origin.position.x = mask.info.origin.position.y = -2;
    mask.info.origin.orientation.w = 1;
    mask.data.assign(80 * 80, 0);
    // Recorded robot zone, including the production 20 cm preferred-clearance band.
    for (unsigned y = 0; y < 80; ++y) {
      for (unsigned x = 0; x < 80; ++x) {
        const double wx = -2 + (x + .5) * .05, wy = -2 + (y + .5) * .05;
        const double dx = std::max({.986 - wx, 0.0, wx - 1.913});
        const double dy = std::max({-.757 - wy, 0.0, wy - .737});
        if (std::hypot(dx, dy) <= .2) {mask.data[y * 80 + x] = 70;}
        if (dx == 0 && dy == 0) {mask.data[y * 80 + x] = 100;}
      }
    }
    mask_pub->publish(mask);
    ASSERT_TRUE(until([this]() {
      applyMask(); return cost(global->getCostmap(), 1.2, .5) == LETHAL_OBSTACLE;
    }));
    geometry_msgs::msg::TransformStamped tf;
    tf.header.frame_id = "map"; tf.child_frame_id = "odom";
    tf.transform.rotation.w = 1;
    ASSERT_TRUE(local->getTfBuffer()->setTransform(tf, "test", true));
  }

  std::shared_ptr<nav2_costmap_2d::Costmap2DROS> configure(const std::string & name)
  {
    // Load the shipped YAML: no test-only substitution of the filter lists.
    auto options = rclcpp::NodeOptions().arguments({"--ros-args", "--params-file",
      NAV2_TEST_PARAMS, "-r", "__node:=" + name, "-r", "__ns:=/" + name});
    auto node = std::make_shared<nav2_costmap_2d::Costmap2DROS>(options);
    executor.add_node(node->get_node_base_interface());
    if (node->on_configure(rclcpp_lifecycle::State()) != nav2_util::CallbackReturn::SUCCESS) {
      ADD_FAILURE() << "Costmap configuration failed: " << name; return nullptr;
    }
    const double resolution = node->get_parameter("resolution").as_double();
    const unsigned size = std::ceil(4.0 / resolution);
    node->getLayeredCostmap()->resizeMap(size, size, resolution, -2, -2);
    // A known empty room isolates the virtual-zone policy. Obstacle tests add real costs.
    node->getCostmap()->resetMapToValue(0, 0, size, size, 0);
    return node;
  }

  void TearDown() override
  {
    if (filter) {filter->deactivate(); filter.reset();}
    for (auto & node : {global, local}) {
      if (node) {
        node->on_cleanup(rclcpp_lifecycle::State());
        executor.remove_node(node->get_node_base_interface());
      }
    }
    global.reset(); local.reset();
    executor.remove_node(source); source.reset();
  }

  bool until(const std::function<bool()> & done)
  {
    const auto deadline = std::chrono::steady_clock::now() + 3s;
    while (std::chrono::steady_clock::now() < deadline) {
      executor.spin_some();
      if (done()) {return true;}
      std::this_thread::sleep_for(5ms);
    }
    return done();
  }

  void applyMask()
  {
    auto * map = global->getCostmap();
    filter->process(*map, 0, 0, map->getSizeInCellsX(), map->getSizeInCellsY(),
      geometry_msgs::msg::Pose2D());
  }

  unsigned char cost(nav2_costmap_2d::Costmap2D * map, double x, double y)
  {
    unsigned int mx, my;
    EXPECT_TRUE(map->worldToMap(x, y, mx, my)); return map->getCost(mx, my);
  }

  double collision(nav2_costmap_2d::Costmap2D * map, const geometry_msgs::msg::PoseStamped & p)
  {
    nav2_costmap_2d::FootprintCollisionChecker<nav2_costmap_2d::Costmap2D *> checker(map);
    return checker.footprintCostAtPose(p.pose.position.x, p.pose.position.y,
      2 * std::atan2(p.pose.orientation.z, p.pose.orientation.w), local->getRobotFootprint());
  }

  nav_msgs::msg::Path plan(const geometry_msgs::msg::PoseStamped & start,
    const geometry_msgs::msg::PoseStamped & goal)
  {
    auto options = rclcpp::NodeOptions().arguments({"--ros-args", "--params-file", NAV2_TEST_PARAMS});
    auto node = std::make_shared<nav2_util::LifecycleNode>("planner_server", "", options);
    nav2_navfn_planner::NavfnPlanner planner;
    planner.configure(node, "GridBased", global->getTfBuffer(), global);
    planner.activate();
    const auto result = planner.createPlan(start, goal);
    planner.deactivate(); planner.cleanup();
    return result;
  }

  rclcpp::executors::SingleThreadedExecutor executor;
  std::shared_ptr<rclcpp::Node> source;
  std::shared_ptr<nav2_costmap_2d::Costmap2DROS> global, local;
  std::shared_ptr<nav2_costmap_2d::CostmapFilter> filter;
  rclcpp::Publisher<nav2_msgs::msg::CostmapFilterInfo>::SharedPtr info_pub;
  rclcpp::Publisher<nav_msgs::msg::OccupancyGrid>::SharedPtr mask_pub;
  nav_msgs::msg::OccupancyGrid mask;
};

TEST_F(GlobalKeepoutTest, RecordedCornerContactCanFollowTheGlobalPathOut)
{
  auto p = pose(.9474096644, .8486944211, -1.6836921954);
  EXPECT_LT(cost(global->getCostmap(), p.pose.position.x, p.pose.position.y), LETHAL_OBSTACLE);
  EXPECT_GE(collision(global->getCostmap(), p), LETHAL_OBSTACLE);
  EXPECT_LT(collision(local->getCostmap(), p), LETHAL_OBSTACLE);
  const auto path = plan(p, pose(1.03, 1.60, -1.6836921954));
  ASSERT_FALSE(path.poses.empty());
  auto options = rclcpp::NodeOptions().arguments({"--ros-args", "--params-file", NAV2_TEST_PARAMS});
  auto node = std::make_shared<nav2_util::LifecycleNode>("controller_server", "", options);
  dwb_core::DWBLocalPlanner controller;
  controller.configure(node, "FollowPath", local->getTfBuffer(), local);
  controller.activate(); controller.setPlan(path);
  geometry_msgs::msg::Twist velocity;
  const auto initial = p;
  for (int step = 0; step < 40; ++step) {
    p.header.stamp = source->now();
    const auto command = controller.computeVelocityCommands(p, velocity, nullptr).twist;
    if (step == 0) {EXPECT_LT(command.linear.x, -.01);}
    const double yaw = 2 * std::atan2(p.pose.orientation.z, p.pose.orientation.w);
    p.pose.position.x += command.linear.x * std::cos(yaw) * .1;
    p.pose.position.y += command.linear.x * std::sin(yaw) * .1;
    p.pose.orientation.z = std::sin((yaw + command.angular.z * .1) / 2);
    p.pose.orientation.w = std::cos((yaw + command.angular.z * .1) / 2);
    velocity = command;
    EXPECT_LT(collision(local->getCostmap(), p), LETHAL_OBSTACLE);
  }
  EXPECT_GT(std::hypot(p.pose.position.x - initial.pose.position.x,
    p.pose.position.y - initial.pose.position.y), .2);
  EXPECT_LT(collision(global->getCostmap(), p), LETHAL_OBSTACLE);
  EXPECT_EQ(cost(global->getCostmap(), 1.2, .5), LETHAL_OBSTACLE);
  controller.deactivate(); controller.cleanup();
}

TEST_F(GlobalKeepoutTest, GlobalRoutesAvoidZonesAndDeepInteriorCannotPlanOut)
{
  const auto route = plan(pose(.5, 0), pose(1.5, 1.5));
  ASSERT_FALSE(route.poses.empty());
  for (const auto & p : route.poses) {
    EXPECT_LT(cost(global->getCostmap(), p.pose.position.x, p.pose.position.y), LETHAL_OBSTACLE);
  }
  EXPECT_TRUE(plan(pose(1.5, 0), pose(.5, 0)).poses.empty());
  // NavfnPlanner clears only its start cell; surrounding zone cells stay blocked.
  EXPECT_EQ(cost(global->getCostmap(), 1.6, 0), LETHAL_OBSTACLE);
}

TEST_F(GlobalKeepoutTest, LocalDwbStillRejectsRealObstaclesAndUnknownSpace)
{
  const auto p = pose(.9474096644, .8486944211, -1.6836921954);
  const auto path = plan(p, pose(1.03, 1.60));
  ASSERT_FALSE(path.poses.empty());
  auto options = rclcpp::NodeOptions().arguments({"--ros-args", "--params-file", NAV2_TEST_PARAMS});
  auto node = std::make_shared<nav2_util::LifecycleNode>("controller_server", "", options);
  dwb_core::DWBLocalPlanner controller;
  controller.configure(node, "FollowPath", local->getTfBuffer(), local);
  controller.activate();
  auto * map = local->getCostmap();
  for (auto blocked : {LETHAL_OBSTACLE, NO_INFORMATION}) {
    // A surrounding physical obstruction prevents every translational/rotational trajectory.
    map->resetMapToValue(0, 0, map->getSizeInCellsX(), map->getSizeInCellsY(), blocked);
    controller.setPlan(path);
    EXPECT_THROW(controller.computeVelocityCommands(p, geometry_msgs::msg::Twist(), nullptr),
      nav2_core::PlannerException);
  }
  controller.deactivate(); controller.cleanup();
}
}  // namespace
