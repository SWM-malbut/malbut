// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include <gtest/gtest.h>
#include "malbut_bringup/keepout.hpp"
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
    ASSERT_EQ(local->getLayeredCostmap()->getFilters()->size(), 1u);
    local_filter = std::dynamic_pointer_cast<nav2_costmap_2d::CostmapFilter>(
      local->getLayeredCostmap()->getFilters()->at(0));
    local_filter->activate();
    geometry_msgs::msg::TransformStamped transform;
    transform.header.frame_id = "map"; transform.child_frame_id = "odom";
    transform.transform.rotation.w = 1;
    local->getTfBuffer()->setTransform(transform, "test", true);
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
    ASSERT_TRUE(until([this]() {return mask_pub->get_subscription_count() >= 2;}));
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
    pose_pub = source->create_publisher<geometry_msgs::msg::PoseWithCovarianceStamped>("/amcl_pose", 1);
    geometry_msgs::msg::PoseWithCovarianceStamped trust;
    trust.header.frame_id = "map"; trust.header.stamp = source->now();
    trust.pose.pose.orientation.w = 1;
    pose_pub->publish(trust);
    ASSERT_TRUE(until([this]() {
      applyMask(); return cost(global->getCostmap(), 1.2, .5) == LETHAL_OBSTACLE &&
        cost(local->getCostmap(), 1.2, .5) == LETHAL_OBSTACLE &&
        std::dynamic_pointer_cast<malbut_bringup::keepout::Filter>(filter)->snapshot().confident;
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
    if (local_filter) {local_filter->deactivate(); local_filter.reset();}
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
    map->resetMapToValue(0, 0, map->getSizeInCellsX(), map->getSizeInCellsY(), 0);
    auto * near = local->getCostmap();
    near->resetMapToValue(0, 0, near->getSizeInCellsX(), near->getSizeInCellsY(), 0);
    local_filter->process(*near, 0, 0, near->getSizeInCellsX(), near->getSizeInCellsY(),
      geometry_msgs::msg::Pose2D());
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
    malbut_bringup::keepout::Planner planner;
    planner.configure(node, "GridBased", global->getTfBuffer(), global);
    planner.activate();
    const auto result = planner.createPlan(start, goal);
    planner.deactivate(); planner.cleanup();
    return result;
  }

  rclcpp::executors::SingleThreadedExecutor executor;
  std::shared_ptr<rclcpp::Node> source;
  std::shared_ptr<nav2_costmap_2d::Costmap2DROS> global, local;
  std::shared_ptr<nav2_costmap_2d::CostmapFilter> filter, local_filter;
  rclcpp::Publisher<geometry_msgs::msg::PoseWithCovarianceStamped>::SharedPtr pose_pub;
  rclcpp::Publisher<nav2_msgs::msg::CostmapFilterInfo>::SharedPtr info_pub;
  rclcpp::Publisher<nav_msgs::msg::OccupancyGrid>::SharedPtr mask_pub;
  nav_msgs::msg::OccupancyGrid mask;
};

TEST_F(GlobalKeepoutTest, RecordedCornerContactCanFollowTheGlobalPathOut)
{
  auto p = pose(.9474096644, .8486944211, -1.6836921954);
  EXPECT_LT(cost(global->getCostmap(), p.pose.position.x, p.pose.position.y), LETHAL_OBSTACLE);
  EXPECT_GE(collision(global->getCostmap(), p), LETHAL_OBSTACLE);
  EXPECT_GE(collision(local->getCostmap(), p), LETHAL_OBSTACLE);
  const auto path = plan(p, pose(1.03, 1.60, -1.6836921954));
  ASSERT_FALSE(path.poses.empty());
  auto options = rclcpp::NodeOptions().arguments({"--ros-args", "--params-file", NAV2_TEST_PARAMS});
  auto node = std::make_shared<nav2_util::LifecycleNode>("controller_server", "", options);
  malbut_bringup::keepout::Controller controller;
  controller.configure(node, "FollowPath", local->getTfBuffer(), local);
  controller.activate(); controller.setPlan(path);
  geometry_msgs::msg::Twist velocity;
  const auto initial = p;
  for (int step = 0; step < 40; ++step) {
    p.header.stamp = source->now();
    const auto command = controller.computeVelocityCommands(p, velocity, nullptr).twist;
    if (step == 0) {EXPECT_GT(std::hypot(command.linear.x, command.linear.y), .01);}
    const double yaw = 2 * std::atan2(p.pose.orientation.z, p.pose.orientation.w);
    p.pose.position.x += (command.linear.x * std::cos(yaw) - command.linear.y * std::sin(yaw)) * .1;
    p.pose.position.y += (command.linear.x * std::sin(yaw) + command.linear.y * std::cos(yaw)) * .1;
    p.pose.orientation.z = std::sin((yaw + command.angular.z * .1) / 2);
    p.pose.orientation.w = std::cos((yaw + command.angular.z * .1) / 2);
    velocity = command;
    auto scene = std::dynamic_pointer_cast<malbut_bringup::keepout::Filter>(local_filter)->snapshot();
    geometry_msgs::msg::Pose2D physical_pose;
    physical_pose.x = p.pose.position.x; physical_pose.y = p.pose.position.y; physical_pose.theta = yaw;
    EXPECT_TRUE(malbut_bringup::keepout::physicalFree(scene, physical_pose));
  }
  EXPECT_GT(std::hypot(p.pose.position.x - initial.pose.position.x,
    p.pose.position.y - initial.pose.position.y), .2);
  EXPECT_LT(collision(global->getCostmap(), p), LETHAL_OBSTACLE);
  EXPECT_EQ(cost(global->getCostmap(), 1.2, .5), LETHAL_OBSTACLE);
  controller.deactivate(); controller.cleanup();
}

TEST_F(GlobalKeepoutTest, GlobalRoutesAvoidZonesAndDeepInteriorGetsAnExit)
{
  const auto route = plan(pose(.5, 0), pose(1.5, 1.5));
  ASSERT_FALSE(route.poses.empty());
  for (const auto & p : route.poses) {
    EXPECT_LT(cost(global->getCostmap(), p.pose.position.x, p.pose.position.y), LETHAL_OBSTACLE);
  }
  EXPECT_FALSE(plan(pose(1.5, 0), pose(.5, 0)).poses.empty());
  EXPECT_THROW(plan(pose(.5, 0), pose(1.5, 0)), nav2_core::PlannerException);
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
  malbut_bringup::keepout::Controller controller;
  controller.configure(node, "FollowPath", local->getTfBuffer(), local);
  controller.activate();
  auto * map = local->getCostmap();
  for (auto blocked : {LETHAL_OBSTACLE, NO_INFORMATION}) {
    // A surrounding physical obstruction prevents every translational/rotational trajectory.
    map->resetMapToValue(0, 0, map->getSizeInCellsX(), map->getSizeInCellsY(), blocked);
    local_filter->process(*map, 0, 0, map->getSizeInCellsX(), map->getSizeInCellsY(),
      geometry_msgs::msg::Pose2D());
    controller.setPlan(path);
    EXPECT_THROW(controller.computeVelocityCommands(p, geometry_msgs::msg::Twist(), nullptr),
      nav2_core::PlannerException);
  }
  controller.deactivate(); controller.cleanup();
}

TEST_F(GlobalKeepoutTest, ClearBodyNearRasterBoundaryCanMoveOutWithoutEnteringZone)
{
  const auto p = pose(.86875481345, .81809298131, .62372116633);
  auto scene = std::dynamic_pointer_cast<malbut_bringup::keepout::Filter>(local_filter)->snapshot();
  geometry_msgs::msg::Pose2D current;
  current.x = p.pose.position.x; current.y = p.pose.position.y; current.theta = .62372116633;
  ASSERT_TRUE(malbut_bringup::keepout::overlap(scene, current).empty());
  ASSERT_FALSE(malbut_bringup::keepout::overlap(scene, current, .03).empty());
  auto options = rclcpp::NodeOptions().arguments({"--ros-args", "--params-file", NAV2_TEST_PARAMS});
  auto node = std::make_shared<nav2_util::LifecycleNode>("controller_server", "", options);
  malbut_bringup::keepout::Controller controller;
  controller.configure(node, "FollowPath", local->getTfBuffer(), local);
  controller.activate();
  nav_msgs::msg::Path ordinary_path;
  ordinary_path.header = p.header;
  ordinary_path.poses = {pose(p.pose.position.x, p.pose.position.y), pose(.5, 1.2)};
  controller.setPlan(ordinary_path);
  const auto waiting = controller.computeVelocityCommands(p, geometry_msgs::msg::Twist(), nullptr).twist;
  EXPECT_DOUBLE_EQ(waiting.linear.x, 0); EXPECT_DOUBLE_EQ(waiting.linear.y, 0);
  EXPECT_DOUBLE_EQ(waiting.angular.z, 0);
  std::this_thread::sleep_for(2100ms);
  applyMask();
  EXPECT_THROW(controller.computeVelocityCommands(p, geometry_msgs::msg::Twist(), nullptr),
    nav2_core::PlannerException);
  controller.setPlan(plan(p, pose(.5, 1.2, current.theta)));
  const auto command = controller.computeVelocityCommands(p, geometry_msgs::msg::Twist(), nullptr).twist;
  EXPECT_GT(std::hypot(command.linear.x, command.linear.y), .01);
  current.x += (command.linear.x * std::cos(current.theta) - command.linear.y * std::sin(current.theta)) * .5;
  current.y += (command.linear.x * std::sin(current.theta) + command.linear.y * std::cos(current.theta)) * .5;
  EXPECT_TRUE(malbut_bringup::keepout::overlap(scene, current).empty());
  EXPECT_TRUE(malbut_bringup::keepout::physicalFree(scene, current));
  controller.deactivate(); controller.cleanup();
}

TEST_F(GlobalKeepoutTest, ReplanAfterBodyClearsStillCompletesFiveCentimeterMargin)
{
  auto options = rclcpp::NodeOptions().arguments({"--ros-args", "--params-file", NAV2_TEST_PARAMS});
  auto node = std::make_shared<nav2_util::LifecycleNode>("controller_server", "", options);
  malbut_bringup::keepout::Controller controller;
  controller.configure(node, "FollowPath", local->getTfBuffer(), local);
  controller.activate();
  const auto goal = pose(.4, 0);
  controller.setPlan(plan(pose(1.5, 0), goal));
  controller.computeVelocityCommands(pose(1.5, 0), geometry_msgs::msg::Twist(), nullptr);
  // The body and 4 cm margin are clear, but the 5 cm exit margin still overlaps.
  // A normal replan starts at this pose and must not trap the robot again.
  const auto near_edge = pose(.805, 0);
  controller.setPlan(plan(near_edge, goal));
  const auto command = controller.computeVelocityCommands(
    near_edge, geometry_msgs::msg::Twist(), nullptr).twist;
  EXPECT_LT(command.linear.x, -.01);
  nav_msgs::msg::Path exact_start;
  exact_start.header = near_edge.header;
  exact_start.poses = {near_edge, goal};
  controller.setPlan(exact_start);
  EXPECT_LT(controller.computeVelocityCommands(
      near_edge, geometry_msgs::msg::Twist(), nullptr).twist.linear.x, -.01);
  controller.deactivate(); controller.cleanup();
}

class ExitSearchTest : public ::testing::Test
{
protected:
  void SetUp() override
  {
    physical = std::make_shared<malbut_bringup::keepout::Grid>(100, 100, .05, -2.5, -2.5, 0);
    mask = std::make_shared<malbut_bringup::keepout::Grid>(*physical);
    scene.physical = physical; scene.mask = mask; scene.to_mask.setIdentity(); scene.confident = true;
    for (auto xy : {std::pair<double, double>{.1485, .116}, {.1485, -.116},
        {-.1485, -.116}, {-.1485, .116}})
    {
      geometry_msgs::msg::Point p; p.x = xy.first; p.y = xy.second; scene.footprint.push_back(p);
    }
    box(*mask, -.8, -.8, .8, .8, LETHAL_OBSTACLE);
  }
  void box(malbut_bringup::keepout::Grid & map, double x0, double y0, double x1, double y1,
    unsigned char value)
  {
    for (unsigned y = 0; y < 100; ++y) {
      for (unsigned x = 0; x < 100; ++x) {
        double wx, wy; map.mapToWorld(x, y, wx, wy);
        if (wx >= x0 && wx <= x1 && wy >= y0 && wy <= y1) {map.setCost(x, y, value);}
      }
    }
  }
  double length(const std::vector<malbut_bringup::keepout::Pose> & path)
  {
    double distance = 0;
    for (size_t i = 1; i < path.size(); ++i) {
      distance += std::hypot(path[i].x - path[i-1].x, path[i].y - path[i-1].y);
      EXPECT_TRUE(malbut_bringup::keepout::physicalFree(scene, path[i]));
    }
    EXPECT_TRUE(malbut_bringup::keepout::overlap(scene, path.back(), .05).empty());
    return distance;
  }
  malbut_bringup::keepout::Scene scene;
  std::shared_ptr<malbut_bringup::keepout::Grid> physical, mask;
};

TEST_F(ExitSearchTest, NearestExitAndBlockedNearestExitHaveKnownOptimalDistances)
{
  malbut_bringup::keepout::Pose p; p.x = .65;
  EXPECT_NEAR(length(shortestExit(scene, p)), .35, 1e-6);
  box(*physical, .925, -.9, 1.075, .9, LETHAL_OBSTACLE);
  EXPECT_NEAR(length(shortestExit(scene, p)), 1.0, 1e-6);
}

TEST_F(ExitSearchTest, EnclosedRobotAndUncertainLocalizationCannotEscape)
{
  malbut_bringup::keepout::Pose p;
  scene.confident = false;
  EXPECT_THROW(shortestExit(scene, p), nav2_core::PlannerException);
  scene.confident = true;
  box(*physical, -.575, -.9, -.425, .9, LETHAL_OBSTACLE);
  box(*physical, .425, -.9, .575, .9, LETHAL_OBSTACLE);
  box(*physical, -.9, -.575, .9, -.425, LETHAL_OBSTACLE);
  box(*physical, -.9, .425, .9, .575, LETHAL_OBSTACLE);
  EXPECT_THROW(shortestExit(scene, p), nav2_core::PlannerException);
}

TEST_F(ExitSearchTest, EscapeCannotCrossAnotherZoneOrUnknownPhysicalSpace)
{
  malbut_bringup::keepout::Pose p; p.x = .65;
  box(*mask, .9, -.5, 1.5, .5, LETHAL_OBSTACLE);
  const auto allowed = malbut_bringup::keepout::component(*mask, overlap(scene, p));
  const auto path = shortestExit(scene, p);
  for (const auto & at : path) {
    const auto touched = overlap(scene, at);
    EXPECT_TRUE(std::includes(allowed.begin(), allowed.end(), touched.begin(), touched.end()));
  }
  EXPECT_GT(length(path), .35);
  EXPECT_LE(length(path), 1.0 + 1e-6);
  physical->resetMapToValue(0, 0, 100, 100, NO_INFORMATION);
  EXPECT_THROW(shortestExit(scene, p), nav2_core::PlannerException);
}

TEST_F(ExitSearchTest, OneCellFreeSentinelDoesNotBlockMapsWithoutZones)
{
  scene.mask = std::make_shared<malbut_bringup::keepout::Grid>(1, 1, .05, 0, 0, 0);
  malbut_bringup::keepout::Pose p; p.x = 1.5; p.y = -1.5;
  EXPECT_TRUE(overlap(scene, p).empty());
}
}  // namespace
