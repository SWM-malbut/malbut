// Copyright 2026 Malbut. Licensed under the Apache License, Version 2.0.
#include <cmath>
#include <memory>
#include <gtest/gtest.h>
#include "checked_rotation_shim_controller.hpp"

class PrimaryController : public nav2_core::Controller
{
public:
  int calls = 0;
  void configure(const rclcpp_lifecycle::LifecycleNode::WeakPtr &, std::string,
    std::shared_ptr<tf2_ros::Buffer>, std::shared_ptr<nav2_costmap_2d::Costmap2DROS>) override {}
  void activate() override {}
  void deactivate() override {}
  void cleanup() override {}
  void setPlan(const nav_msgs::msg::Path &) override {}
  void setSpeedLimit(const double &, const bool &) override {}
  geometry_msgs::msg::TwistStamped computeVelocityCommands(
    const geometry_msgs::msg::PoseStamped &, const geometry_msgs::msg::Twist &,
    nav2_core::GoalChecker *) override
  {
    ++calls;
    return geometry_msgs::msg::TwistStamped();  // Stand in for DWB's checked stop.
  }
};

class Probe : public malbut_bringup::CheckedRotationShimController
{
public:
  void setup(const std::shared_ptr<nav2_costmap_2d::Costmap2DROS> & map,
    const std::shared_ptr<PrimaryController> & primary, double angle)
  {
    costmap_ros_ = map;
    primary_controller_ = primary;
    tf_ = map->getTfBuffer();
    clock_ = map->get_clock();
    collision_checker_ = std::make_unique<
      nav2_costmap_2d::FootprintCollisionChecker<nav2_costmap_2d::Costmap2D *>>(map->getCostmap());
    angular_dist_threshold_ = .2;
    angular_disengage_threshold_ = .05;
    control_duration_ = .05;
    simulate_ahead_time_ = 1.;
    max_angular_accel_ = 3.2;
    rotate_to_heading_angular_vel_ = .5;
    forward_sampling_distance_ = .4;
    rotate_to_goal_heading_ = false;
    closed_loop_ = true;
    path_updated_ = in_rotation_ = true;
    current_path_.header.frame_id = map->getBaseFrameID();
    current_path_.poses.resize(2);
    current_path_.poses.back().pose.position.x = .6 * std::cos(angle);
    current_path_.poses.back().pose.position.y = .6 * std::sin(angle);
  }
  double footprintCost(double angle)
  {
    return collision_checker_->footprintCostAtPose(0., 0., angle, costmap_ros_->getRobotFootprint());
  }
};

class RotationCollision : public testing::TestWithParam<double>
{
protected:
  static void SetUpTestSuite() {rclcpp::init(0, nullptr);}
  static void TearDownTestSuite() {rclcpp::shutdown();}
  void SetUp() override
  {
    map = std::make_shared<nav2_costmap_2d::Costmap2DROS>(rclcpp::NodeOptions().parameter_overrides({
        rclcpp::Parameter("plugins", std::vector<std::string>{}),
        rclcpp::Parameter("filters", std::vector<std::string>{}),
        rclcpp::Parameter("resolution", .03),
        rclcpp::Parameter("width", 1), rclcpp::Parameter("height", 1),
        rclcpp::Parameter("origin_x", -.51), rclcpp::Parameter("origin_y", -.51),
        rclcpp::Parameter("footprint_padding", 0.),
        rclcpp::Parameter("footprint", "[[0.1485,0.116],[0.1485,-0.116],[-0.1485,-0.116],[-0.1485,0.116]]")
      }));
    ASSERT_EQ(map->on_configure(rclcpp_lifecycle::State()), nav2_util::CallbackReturn::SUCCESS);
    primary = std::make_shared<PrimaryController>();
    probe = std::make_unique<Probe>();
    probe->setup(map, primary, GetParam());
    pose.header.frame_id = map->getBaseFrameID();
    pose.pose.orientation.w = 1.;
    velocity.angular.z = std::copysign(.5, GetParam());
  }
  void TearDown() override
  {
    probe.reset();
    map->on_cleanup(rclcpp_lifecycle::State());
    map.reset();
  }
  std::shared_ptr<nav2_costmap_2d::Costmap2DROS> map;
  std::shared_ptr<PrimaryController> primary;
  std::unique_ptr<Probe> probe;
  geometry_msgs::msg::PoseStamped pose;
  geometry_msgs::msg::Twist velocity;
};

TEST_P(RotationCollision, BlocksCollisionInFirstOrFinalInterval)
{
  unsigned int x, y;
  ASSERT_TRUE(map->getCostmap()->worldToMap(.1505, -std::copysign(.1125, GetParam()), x, y));
  map->getCostmap()->setCost(x, y, nav2_costmap_2d::LETHAL_OBSTACLE);
  ASSERT_EQ(probe->footprintCost(0.), 0.);
  ASSERT_EQ(probe->footprintCost(std::copysign(.025, GetParam())), 254.);
  auto command = probe->computeVelocityCommands(pose, velocity, nullptr);
  EXPECT_EQ(primary->calls, 1);
  EXPECT_EQ(command.twist.angular.z, 0.);
}

TEST_P(RotationCollision, AllowsFreeRotation)
{
  auto command = probe->computeVelocityCommands(pose, velocity, nullptr);
  EXPECT_EQ(primary->calls, 0);
  EXPECT_DOUBLE_EQ(command.twist.angular.z, velocity.angular.z);
}

INSTANTIATE_TEST_SUITE_P(BothDirections, RotationCollision, testing::Values(-.15, .15, -.06, .06));

TEST(PluginRegistration, LoadsFromInstalledDescription)
{
  pluginlib::ClassLoader<nav2_core::Controller> loader("nav2_core", "nav2_core::Controller");
  EXPECT_NO_THROW(loader.createSharedInstance("malbut_bringup::CheckedRotationShimController"));
}
