// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0
#include <gtest/gtest.h>
#include <chrono>
#include <cmath>
#include <functional>
#include <thread>

#include "malbut_bringup/keepout_escape.hpp"
#include "dwb_core/dwb_local_planner.hpp"
#include "nav2_costmap_2d/costmap_2d_ros.hpp"
#include "nav2_costmap_2d/cost_values.hpp"
#include "nav2_costmap_2d/footprint.hpp"
#include "nav2_costmap_2d/footprint_collision_checker.hpp"
#include "nav2_costmap_2d/layered_costmap.hpp"
#include "nav2_util/lifecycle_node.hpp"
#include "nav2_navfn_planner/navfn.hpp"
#include "pluginlib/class_loader.hpp"
#include "tf2_ros/buffer.h"
#include "tf2_ros/transform_listener.h"

using namespace std::chrono_literals;
using nav2_costmap_2d::LETHAL_OBSTACLE;
using nav2_costmap_2d::MAX_NON_OBSTACLE;
using nav2_costmap_2d::NO_INFORMATION;
using Pose = geometry_msgs::msg::Pose2D;

namespace
{
Pose pose(double x, double y = 0, double yaw = 0)
{
  Pose result; result.x = x; result.y = y; result.theta = yaw; return result;
}

class KeepoutTest : public ::testing::Test
{
protected:
  static void SetUpTestSuite() {rclcpp::init(0, nullptr);}
  static void TearDownTestSuite() {rclcpp::shutdown();}
  void SetUp() override
  {
    node = std::make_shared<nav2_util::LifecycleNode>("exit_test_costmap");
    node->declare_parameter("keepout.filter_info_topic", "/exit_test/info");
    node->declare_parameter("keepout.escape_timeout", 15.0);
    source = std::make_shared<rclcpp::Node>("exit_test_source");
    executor.add_node(node->get_node_base_interface()); executor.add_node(source);
    tf = std::make_unique<tf2_ros::Buffer>(node->get_clock());
    listener = std::make_unique<tf2_ros::TransformListener>(*tf);
    map = std::make_unique<nav2_costmap_2d::LayeredCostmap>("map", false, false);
    map->resizeMap(80, 80, 0.05, -2, -2);
    footprint.resize(4);
    footprint[0].x = .1485; footprint[0].y = .116;
    footprint[1].x = .1485; footprint[1].y = -.116;
    footprint[2].x = -.1485; footprint[2].y = -.116;
    footprint[3].x = -.1485; footprint[3].y = .116;
    map->setFootprint(footprint);
    // Test loading the shipped plugin, not only a separately linked helper.
    loader = std::make_unique<pluginlib::ClassLoader<nav2_costmap_2d::Layer>>(
      "nav2_costmap_2d", "nav2_costmap_2d::Layer");
    layer = std::dynamic_pointer_cast<malbut_bringup::KeepoutEscapeFilter>(
      loader->createSharedInstance("malbut_bringup::KeepoutEscapeFilter"));
    ASSERT_TRUE(layer);
    map->addFilter(layer);
    group = node->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    layer->initialize(map.get(), "keepout", tf.get(), node, group);
    layer->activate();
    info_pub = source->create_publisher<nav2_msgs::msg::CostmapFilterInfo>(
      "/exit_test/info", rclcpp::QoS(1).transient_local().reliable());
    mask_pub = source->create_publisher<nav_msgs::msg::OccupancyGrid>(
      "/exit_test/mask", rclcpp::QoS(1).transient_local().reliable());
    trust_pub = source->create_publisher<geometry_msgs::msg::PoseStamped>(
      "/malbut/localization/verified_pose", rclcpp::QoS(1));
    nav2_msgs::msg::CostmapFilterInfo info;
    info.type = 0; info.base = 0; info.multiplier = 1;
    info.filter_mask_topic = "/exit_test/mask";
    info_pub->publish(info);
    ASSERT_TRUE(until([this]() {return mask_pub->get_subscription_count() > 0;}));
    mask.header.frame_id = "map";
    mask.info.width = mask.info.height = 80;
    mask.info.resolution = .05;
    mask.info.origin.position.x = mask.info.origin.position.y = -2;
    mask.info.origin.orientation.w = 1;
    mask.data.assign(80 * 80, 0);
    rectangle(0, -.6, .5, .6);
    publishMask();
    ASSERT_TRUE(until([this]() {return layer->isActive();}));
  }
  void TearDown() override
  {
    layer->resetFilter();
    executor.remove_node(source); executor.remove_node(node->get_node_base_interface());
    map.reset(); layer.reset(); loader.reset(); group.reset();
    listener.reset(); tf.reset(); source.reset(); node.reset();
  }
  bool until(const std::function<bool()> & done, double seconds = 2)
  {
    const auto end = std::chrono::steady_clock::now() + std::chrono::duration<double>(seconds);
    while (std::chrono::steady_clock::now() < end) {
      executor.spin_some();
      if (done()) {return true;}
      std::this_thread::sleep_for(5ms);
    }
    return done();
  }
  void drain() {until([]() {return false;}, .06);}
  void rectangle(double x0, double y0, double x1, double y1)
  {
    for (unsigned y = 0; y < 80; ++y) {
      for (unsigned x = 0; x < 80; ++x) {
        const double wx = -2 + (x + .5) * .05, wy = -2 + (y + .5) * .05;
        if (wx >= x0 && wx <= x1 && wy >= y0 && wy <= y1) {mask.data[y * 80 + x] = 100;}
      }
    }
  }
  void publishMask() {mask_pub->publish(mask); drain();}
  void trust(const Pose & p, double age = 0, const std::string & frame = "map")
  {
    geometry_msgs::msg::PoseStamped message;
    message.header.frame_id = frame;
    message.header.stamp = source->now() - rclcpp::Duration::from_seconds(age);
    message.pose.position.x = p.x; message.pose.position.y = p.y;
    message.pose.orientation.z = std::sin(p.theta / 2);
    message.pose.orientation.w = std::cos(p.theta / 2);
    trust_pub->publish(message); drain();
  }
  void update(const Pose & p, bool reset = true)
  {
    const auto width = map->getCostmap()->getSizeInCellsX();
    const auto height = map->getCostmap()->getSizeInCellsY();
    if (reset) {map->getCostmap()->resetMap(0, 0, width, height);}
    layer->process(*map->getCostmap(), 0, 0, width, height, p);
  }
  unsigned char cost(double x, double y = 0)
  {
    unsigned int mx, my;
    EXPECT_TRUE(map->getCostmap()->worldToMap(x, y, mx, my));
    return map->getCostmap()->getCost(mx, my);
  }
  double collision(const Pose & p)
  {
    nav2_costmap_2d::FootprintCollisionChecker<nav2_costmap_2d::Costmap2D *> checker(map->getCostmap());
    return checker.footprintCostAtPose(p.x, p.y, p.theta, footprint);
  }
  rclcpp::executors::SingleThreadedExecutor executor;
  std::shared_ptr<nav2_util::LifecycleNode> node;
  std::shared_ptr<rclcpp::Node> source;
  rclcpp::CallbackGroup::SharedPtr group;
  std::unique_ptr<tf2_ros::Buffer> tf;
  std::unique_ptr<tf2_ros::TransformListener> listener;
  std::unique_ptr<nav2_costmap_2d::LayeredCostmap> map;
  std::unique_ptr<pluginlib::ClassLoader<nav2_costmap_2d::Layer>> loader;
  std::shared_ptr<malbut_bringup::KeepoutEscapeFilter> layer;
  rclcpp::Publisher<nav2_msgs::msg::CostmapFilterInfo>::SharedPtr info_pub;
  rclcpp::Publisher<nav_msgs::msg::OccupancyGrid>::SharedPtr mask_pub;
  rclcpp::Publisher<geometry_msgs::msg::PoseStamped>::SharedPtr trust_pub;
  nav_msgs::msg::OccupancyGrid mask;
  std::vector<geometry_msgs::msg::Point> footprint;
};

TEST_F(KeepoutTest, NoConfidenceKeepsAllZoneCellsLethal)
{
  update(pose(-.05));
  EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  EXPECT_GE(collision(pose(-.05)), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, BodyInsideCanDriveOutAndCannotReenter)
{
  Pose p = pose(.35);
  trust(p); update(p);
  EXPECT_EQ(cost(.375), MAX_NON_OBSTACLE);
  // Nav2's real footprint checker accepts the complete straight exit.
  for (double x = .35; x <= .70; x += .01) {EXPECT_LT(collision(pose(x)), LETHAL_OBSTACLE);}
  p.x = .72; trust(p); update(p);
  EXPECT_EQ(cost(.375), LETHAL_OBSTACLE);
  EXPECT_GE(collision(pose(.45)), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, CentreOutsideButCornerInsideReproducesRobotPose)
{
  mask.data.assign(80 * 80, 0);
  rectangle(.986, -.757, 1.913, .737);
  publishMask();
  const auto p = pose(.9474096644, .8486944211, -1.6836921954);
  update(p);
  EXPECT_EQ(cost(p.x, p.y), 0);
  EXPECT_GE(collision(p), LETHAL_OBSTACLE);
  trust(p); update(p);
  EXPECT_LT(collision(p), LETHAL_OBSTACLE);
  for (double d = 0; d <= .20; d += .005) {
    EXPECT_LT(collision(pose(p.x - d * std::cos(p.theta),
      p.y - d * std::sin(p.theta), p.theta)), LETHAL_OBSTACLE);
  }
  // Deeper parts of the same zone are never made traversable.
  EXPECT_EQ(cost(1.2, .5), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, PhysicalObstacleAndUnknownAreNotSoftened)
{
  const auto p = pose(-.05); trust(p); update(p);
  unsigned int x, y;
  map->getCostmap()->worldToMap(.025, 0, x, y);
  map->getCostmap()->resetMap(0, 0, 80, 80);
  map->getCostmap()->setCost(x, y, LETHAL_OBSTACLE);
  update(p, false);
  EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  map->getCostmap()->setCost(x, y, NO_INFORMATION);
  update(p, false);
  EXPECT_EQ(cost(.025), NO_INFORMATION);
}

TEST_F(KeepoutTest, OtherZoneAndInteriorStayBlocked)
{
  rectangle(-.5, .8, .5, 1.1); publishMask();
  const auto p = pose(-.05); trust(p); update(p);
  EXPECT_EQ(cost(.025), MAX_NON_OBSTACLE);
  EXPECT_EQ(cost(.025, 1), LETHAL_OBSTACLE);
  EXPECT_EQ(cost(.375), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, UncertainStaleFutureOrWrongFramePoseCannotAuthorizeEscape)
{
  const auto p = pose(-.05);
  for (const auto & frame : {"", "odom"}) {
    trust(p, 0, frame); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  }
  for (double age : {1.0, -1.0}) {
    trust(p, age); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  }
  trust(pose(.4)); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, LostConfidenceRestoresLethalCosts)
{
  const auto p = pose(-.05);
  trust(p); update(p); EXPECT_EQ(cost(.025), MAX_NON_OBSTACLE);
  trust(p, 0, ""); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, TimeoutCannotRearmAtSamePosition)
{
  layer->deactivate();
  node->set_parameter(rclcpp::Parameter("keepout.escape_timeout", 0.1));
  layer->activate();
  ASSERT_TRUE(until([this]() {return layer->isActive();}));
  auto p = pose(-.05); trust(p); update(p);
  // Idle time before a motion request does not exhaust the exit opportunity.
  std::this_thread::sleep_for(150ms);
  trust(p); update(p); EXPECT_EQ(cost(.025), MAX_NON_OBSTACLE);
  p.x -= .025; trust(p); update(p);
  std::this_thread::sleep_for(150ms);
  trust(p); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  trust(p); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, NewMaskRevokesOldLease)
{
  const auto p = pose(-.05); trust(p); update(p);
  publishMask(); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  // A delayed message produced before the new mask cannot reopen the corridor.
  trust(p, .3); update(p); EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  trust(p); update(p); EXPECT_EQ(cost(.025), MAX_NON_OBSTACLE);
}

TEST_F(KeepoutTest, LeavingCorridorDoesNotOpenMoreZone)
{
  const auto p = pose(-.05); trust(p); update(p);
  const auto wrong = pose(.1); trust(wrong); update(wrong);
  EXPECT_EQ(cost(.025), LETHAL_OBSTACLE);
  EXPECT_EQ(cost(.175), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, NoBoundedExitDoesNotRelaxTheZone)
{
  mask.data.assign(80 * 80, 100); publishMask();
  trust(pose(0)); update(pose(0)); EXPECT_EQ(cost(0), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, MissingTransformDoesNotReportCostmapCurrent)
{
  // A mask in another frame without its TF must block navigation readiness.
  mask.header.frame_id = "unavailable"; publishMask();
  update(pose(-.05)); EXPECT_FALSE(layer->isCurrent());
}

TEST_F(KeepoutTest, BoundsIncludeExpiredCellsEvenWhenRobotDoesNotMove)
{
  double min_x = 10, min_y = 10, max_x = -10, max_y = -10;
  layer->updateBounds(0, 0, 0, &min_x, &min_y, &max_x, &max_y);
  EXPECT_LE(min_x, -2); EXPECT_LE(min_y, -2);
  EXPECT_GE(max_x, 2); EXPECT_GE(max_y, 2);
}

TEST_F(KeepoutTest, CorridorClosesBehindTheMovingBody)
{
  for (double x : {.35, .40, .45, .50, .55, .60}) {
    const auto p = pose(x);
    trust(p); update(p);
    EXPECT_LT(collision(p), LETHAL_OBSTACLE);
  }
  EXPECT_EQ(cost(.325), LETHAL_OBSTACLE);
  EXPECT_EQ(cost(.475), MAX_NON_OBSTACLE);
  trust(pose(.70)); update(pose(.70));
  EXPECT_EQ(cost(.475), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, MaskAndLocalCostmapCanUseDifferentFrames)
{
  // Production uses 3 cm local cells and 5 cm saved-map/mask cells.
  map->resizeMap(134, 134, .03, -2, -2);
  geometry_msgs::msg::TransformStamped transform;
  transform.header.frame_id = "zone_map";
  transform.child_frame_id = "map";
  transform.transform.translation.x = .5;
  transform.transform.translation.y = .25;
  transform.transform.rotation.z = std::sin(.4 / 2);
  transform.transform.rotation.w = std::cos(.4 / 2);
  ASSERT_TRUE(tf->setTransform(transform, "test", true));
  mask.header.frame_id = "zone_map"; publishMask();
  const auto p = pose(-.55 * std::cos(.4) - .25 * std::sin(.4),
    .55 * std::sin(.4) - .25 * std::cos(.4), -.4);
  trust(pose(-.05), 0, "zone_map"); update(p);
  EXPECT_TRUE(layer->isCurrent());
  EXPECT_LT(collision(p), LETHAL_OBSTACLE);
  trust(pose(-.05), 0, ""); update(p);
  EXPECT_GE(collision(p), LETHAL_OBSTACLE);
}

TEST_F(KeepoutTest, NavfnAndConfiguredDwbChooseReverseToEscapeCorner)
{
  mask.data.assign(80 * 80, 0);
  rectangle(.986, -.757, 1.913, .737);
  publishMask();
  auto p = pose(.9474096644, .8486944211, -1.6836921954);
  trust(p); update(p);
  const auto target = pose(p.x - .75 * std::cos(p.theta), p.y - .75 * std::sin(p.theta), p.theta);
  nav2_navfn_planner::NavFn planner(80, 80);
  planner.setCostmap(map->getCostmap()->getCharMap(), true, false);
  unsigned int sx, sy, gx, gy;
  ASSERT_TRUE(map->getCostmap()->worldToMap(p.x, p.y, sx, sy));
  ASSERT_TRUE(map->getCostmap()->worldToMap(target.x, target.y, gx, gy));
  int start[] = {static_cast<int>(sx), static_cast<int>(sy)};
  int goal[] = {static_cast<int>(gx), static_cast<int>(gy)};
  planner.setStart(start); planner.setGoal(goal);
  ASSERT_TRUE(planner.calcNavFnDijkstra(true));
  ASSERT_GT(planner.calcPath(80 * 80), 0);
  nav_msgs::msg::Path path;
  path.header.frame_id = "map";
  for (int i = 0; i < planner.getPathLen(); ++i) {
    geometry_msgs::msg::PoseStamped point;
    point.header = path.header;
    point.pose.position.x = -2 + (planner.getPathX()[i] + .5) * .05;
    point.pose.position.y = -2 + (planner.getPathY()[i] + .5) * .05;
    point.pose.orientation.z = std::sin(p.theta / 2);
    point.pose.orientation.w = std::cos(p.theta / 2);
    path.poses.push_back(point);
  }
  auto options = rclcpp::NodeOptions().arguments(
    {"--ros-args", "--params-file", NAV2_TEST_PARAMS});
  auto controller_node = std::make_shared<nav2_util::LifecycleNode>("controller_server", "", options);
  auto costmap = std::make_shared<nav2_costmap_2d::Costmap2DROS>("dwb_exit_costmap");
  costmap->set_parameters({
    rclcpp::Parameter("plugins", std::vector<std::string>{}),
    rclcpp::Parameter("global_frame", "map"),
    rclcpp::Parameter("footprint_padding", 0.0)});
  ASSERT_EQ(costmap->on_configure(rclcpp_lifecycle::State()), nav2_util::CallbackReturn::SUCCESS);
  costmap->setRobotFootprint(footprint);
  *costmap->getCostmap() = *map->getCostmap();
  dwb_core::DWBLocalPlanner controller;
  controller.configure(controller_node, "FollowPath", costmap->getTfBuffer(), costmap);
  controller.activate(); controller.setPlan(path);
  geometry_msgs::msg::Twist velocity;
  const auto initial = p;
  for (int step = 0; step < 40; ++step) {
    trust(p); update(p);
    *costmap->getCostmap() = *map->getCostmap();
    geometry_msgs::msg::PoseStamped current;
    current.header.frame_id = "map"; current.header.stamp = node->now();
    current.pose.position.x = p.x; current.pose.position.y = p.y;
    current.pose.orientation.z = std::sin(p.theta / 2);
    current.pose.orientation.w = std::cos(p.theta / 2);
    const auto command = controller.computeVelocityCommands(current, velocity, nullptr).twist;
    if (step == 0) {EXPECT_LT(command.linear.x, -.01);}
    p.x += command.linear.x * std::cos(p.theta) * .1;
    p.y += command.linear.x * std::sin(p.theta) * .1;
    p.theta += command.angular.z * .1;
    velocity = command;
    EXPECT_LT(collision(p), LETHAL_OBSTACLE);
  }
  EXPECT_GT(std::hypot(p.x - initial.x, p.y - initial.y), .2);
  // All of the body has left the zone and the original corner cell is hard again.
  EXPECT_EQ(cost(1.025, .725), LETHAL_OBSTACLE);
  controller.deactivate(); controller.cleanup();
  costmap->on_cleanup(rclcpp_lifecycle::State());
}
}  // namespace
