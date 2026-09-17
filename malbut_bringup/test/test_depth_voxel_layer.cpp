// Copyright 2026 Malbut contributors
// SPDX-License-Identifier: Apache-2.0

#include <gtest/gtest.h>

#include <chrono>
#include <cstdint>
#include <cstring>
#include <functional>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "geometry_msgs/msg/transform_stamped.hpp"
#include "malbut_bringup/depth_voxel_layer.hpp"
#include "nav2_costmap_2d/cost_values.hpp"
#include "nav2_costmap_2d/footprint.hpp"
#include "nav2_costmap_2d/layered_costmap.hpp"
#include "nav2_util/lifecycle_node.hpp"
#include "pluginlib/class_loader.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/image_encodings.hpp"
#include "sensor_msgs/msg/camera_info.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "tf2_ros/buffer.h"
#include "tf2_ros/transform_listener.h"

namespace
{

using namespace std::chrono_literals;

class InspectableDepthLayer : public malbut_bringup::DepthVoxelLayer
{
public:
  int64_t latestObservationStamp() const
  {
    std::vector<nav2_costmap_2d::Observation> observations;
    getMarkingObservations(observations);
    if (observations.empty()) {
      return 0;
    }
    return rclcpp::Time(observations.back().cloud_->header.stamp).nanoseconds();
  }

};

class DepthVoxelLayerTest : public ::testing::Test
{
protected:
  static void SetUpTestSuite()
  {
    rclcpp::init(0, nullptr);
  }

  static void TearDownTestSuite()
  {
    rclcpp::shutdown();
  }

  void SetUp() override
  {
    rclcpp::NodeOptions options;
    options.parameter_overrides({
      rclcpp::Parameter("depth.depth_topic", "/depth_layer_test/image"),
      rclcpp::Parameter("depth.camera_info_topic", "/depth_layer_test/camera_info"),
      rclcpp::Parameter("depth.depth_is_rectified", true),
      rclcpp::Parameter("depth.enabled", true),
      rclcpp::Parameter("depth.observation_sources", ""),
      rclcpp::Parameter("depth.footprint_clearing_enabled", true),
      rclcpp::Parameter("depth.origin_z", 0.0),
      rclcpp::Parameter("depth.z_resolution", 0.1),
      rclcpp::Parameter("depth.z_voxels", 16),
      rclcpp::Parameter("depth.mark_threshold", 0),
      rclcpp::Parameter("depth.unknown_threshold", 15),
      rclcpp::Parameter("depth.marking.min_obstacle_height", 0.0),
      rclcpp::Parameter("depth.marking.max_obstacle_height", 1.5),
      rclcpp::Parameter("depth.marking.obstacle_min_range", 0.0),
      rclcpp::Parameter("depth.marking.obstacle_max_range", 3.0),
      rclcpp::Parameter("depth.clearing.min_obstacle_height", 0.0),
      rclcpp::Parameter("depth.clearing.max_obstacle_height", 1.5),
      rclcpp::Parameter("depth.clearing.raytrace_min_range", 0.0),
      rclcpp::Parameter("depth.clearing.raytrace_max_range", 3.5),
      rclcpp::Parameter("depth.expected_update_rate", 0.5),
      rclcpp::Parameter("transform_tolerance", 0.3)
    });
    node_ = std::make_shared<nav2_util::LifecycleNode>(
      "depth_test_costmap", "/depth_layer_test", options);
    // Costmap2DROS normally declares the parent parameters read by ObstacleLayer.
    node_->declare_parameter("track_unknown_space", false);
    node_->declare_parameter("transform_tolerance", 0.3);
    source_ = std::make_shared<rclcpp::Node>("depth_test_source", "/depth_layer_test");
    executor_ = std::make_unique<rclcpp::executors::SingleThreadedExecutor>();
    executor_->add_node(node_->get_node_base_interface());
    executor_->add_node(source_);
    tf_ = std::make_unique<tf2_ros::Buffer>(node_->get_clock());
    // Nav2 owns a dedicated listener thread; ObservationBuffer relies on it.
    tf_listener_ = std::make_unique<tf2_ros::TransformListener>(*tf_, source_, true);
    ASSERT_TRUE(tf_->isUsingDedicatedThread());
    installCameraTransform();
    layered_ = std::make_unique<nav2_costmap_2d::LayeredCostmap>("map", false, false);
    layered_->resizeMap(160, 80, 0.05, -1.0, -2.0);
    layer_ = std::make_shared<InspectableDepthLayer>();
    layered_->addPlugin(layer_);
    callback_group_ = node_->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    layer_->initialize(layered_.get(), "depth", tf_.get(), node_, callback_group_);
    // Model Costmap2DROS's real footprint, not an empty stand-alone grid.
    // Humble VoxelLayer expands raytrace bounds at the endpoints; its enabled
    // footprint supplies the robot-side bounds when merging into the master.
    layered_->setFootprint(nav2_costmap_2d::makeFootprintFromRadius(0.18));
    layer_->activate();
    depth_pub_ = source_->create_publisher<sensor_msgs::msg::Image>(
      "/depth_layer_test/image", rclcpp::SensorDataQoS());
    info_pub_ = source_->create_publisher<sensor_msgs::msg::CameraInfo>(
      "/depth_layer_test/camera_info", rclcpp::SensorDataQoS());
    ASSERT_TRUE(spinUntil([this]() {
        return depth_pub_->get_subscription_count() == 1 &&
               info_pub_->get_subscription_count() == 1;
      })) << "depth and calibration subscriptions were not discovered";
  }

  void TearDown() override
  {
    if (layer_) {
      layer_->deactivate();
    }
    // Release executor-owned callback-group associations while all entities
    // are alive, then destroy the plugins and their parent lifecycle node.
    executor_.reset();
    layered_.reset();
    layer_.reset();
    callback_group_.reset();
    depth_pub_.reset();
    info_pub_.reset();
    tf_listener_.reset();
    tf_.reset();
    source_.reset();
    node_.reset();
  }

  void installCameraTransform(
    const std::string & child_frame = "test_depth_optical",
    builtin_interfaces::msg::Time stamp = builtin_interfaces::msg::Time(),
    bool is_static = true)
  {
    geometry_msgs::msg::TransformStamped transform;
    transform.header.frame_id = "map";
    transform.child_frame_id = child_frame;
    transform.header.stamp = stamp;
    if (stamp.sec == 0 && stamp.nanosec == 0) {
      transform.header.stamp = node_->now();
    }
    transform.transform.translation.z = 0.5;
    // Optical +Z is map +X, optical +X is map -Y, optical +Y is map -Z.
    transform.transform.rotation.x = -0.5;
    transform.transform.rotation.y = 0.5;
    transform.transform.rotation.z = -0.5;
    transform.transform.rotation.w = 0.5;
    ASSERT_TRUE(tf_->setTransform(transform, "depth_layer_test", is_static));
  }

  bool spinUntil(const std::function<bool()> & predicate, std::chrono::milliseconds budget = 2000ms)
  {
    const auto deadline = std::chrono::steady_clock::now() + budget;
    while (std::chrono::steady_clock::now() < deadline) {
      executor_->spin_some();
      if (predicate()) {
        return true;
      }
      std::this_thread::sleep_for(2ms);
    }
    return predicate();
  }

  sensor_msgs::msg::Image depthImage(uint16_t millimeters)
  {
    sensor_msgs::msg::Image image;
    image.header.frame_id = "test_depth_optical";
    image.header.stamp = node_->now();
    image.width = image.height = 1;
    image.encoding = sensor_msgs::image_encodings::TYPE_16UC1;
    const uint16_t endian = 1;
    image.is_bigendian = *reinterpret_cast<const uint8_t *>(&endian) == 0;
    image.step = sizeof(millimeters);
    image.data.resize(image.step);
    std::memcpy(image.data.data(), &millimeters, sizeof(millimeters));
    return image;
  }

  sensor_msgs::msg::CameraInfo cameraInfo(const sensor_msgs::msg::Image & image)
  {
    sensor_msgs::msg::CameraInfo info;
    info.header = image.header;
    info.width = info.height = 1;
    info.distortion_model = "plumb_bob";
    info.d.assign(5, 0.0);
    info.k = {100.0, 0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.0, 1.0};
    info.r = {1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0};
    info.p = {100.0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0};
    return info;
  }

  bool publishDepth(uint16_t millimeters, bool camera_stamp_differs = false)
  {
    const auto image = depthImage(millimeters);
    auto info = cameraInfo(image);
    if (camera_stamp_differs) {
      info.header.stamp.sec += 1;
    }
    info_pub_->publish(info);
    // Both real subscription callbacks run; no private callback or cloud is injected.
    executor_->spin_some();
    depth_pub_->publish(image);
    return spinUntil([this, &image]() {
        return layer_->latestObservationStamp() == rclcpp::Time(image.header.stamp).nanoseconds();
      });
  }

  unsigned char costAt(double x, double y)
  {
    unsigned int cell_x = 0;
    unsigned int cell_y = 0;
    EXPECT_TRUE(layered_->getCostmap()->worldToMap(x, y, cell_x, cell_y));
    return layered_->getCostmap()->getCost(cell_x, cell_y);
  }

  std::shared_ptr<nav2_util::LifecycleNode> node_;
  rclcpp::Node::SharedPtr source_;
  std::unique_ptr<rclcpp::executors::SingleThreadedExecutor> executor_;
  rclcpp::CallbackGroup::SharedPtr callback_group_;
  std::unique_ptr<tf2_ros::Buffer> tf_;
  std::unique_ptr<tf2_ros::TransformListener> tf_listener_;
  std::unique_ptr<nav2_costmap_2d::LayeredCostmap> layered_;
  std::shared_ptr<InspectableDepthLayer> layer_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr depth_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CameraInfo>::SharedPtr info_pub_;
};

TEST_F(DepthVoxelLayerTest, RealDepthAndOpticalTransformMarkThenRayClearAnObstacle)
{
  // The real Aurora driver stamps fixed calibration from a different SDK frame.
  ASSERT_TRUE(publishDepth(1000, true));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_EQ(costAt(1.0, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
  ASSERT_TRUE(publishDepth(2000));
  layered_->updateMap(0.0, 0.0, 0.0);
  unsigned int cleared_x = 0;
  unsigned int cleared_y = 0;
  ASSERT_TRUE(layer_->worldToMap(1.0, 0.0, cleared_x, cleared_y));
  EXPECT_EQ(layer_->getCost(cleared_x, cleared_y), nav2_costmap_2d::FREE_SPACE);
  EXPECT_EQ(costAt(1.0, 0.0), nav2_costmap_2d::FREE_SPACE);
  EXPECT_EQ(costAt(2.0, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
  EXPECT_TRUE(layer_->isCurrent());
}

TEST_F(DepthVoxelLayerTest, InvalidDepthDoesNotInventAFreespaceRay)
{
  ASSERT_TRUE(publishDepth(1000));
  layered_->updateMap(0.0, 0.0, 0.0);
  ASSERT_EQ(costAt(1.0, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
  ASSERT_TRUE(publishDepth(0));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_EQ(costAt(1.0, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
}

TEST_F(DepthVoxelLayerTest, MismatchedCalibrationDoesNotMarkAndTheNextValidFrameRecovers)
{
  const auto image = depthImage(1000);
  auto wrong_info = cameraInfo(image);
  wrong_info.width = 2;
  info_pub_->publish(wrong_info);
  executor_->spin_some();
  depth_pub_->publish(image);
  EXPECT_FALSE(spinUntil([this]() {return layer_->latestObservationStamp() != 0;}, 100ms));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_EQ(costAt(1.0, 0.0), nav2_costmap_2d::FREE_SPACE);
  ASSERT_TRUE(publishDepth(2000));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_EQ(costAt(2.0, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
}

TEST_F(DepthVoxelLayerTest, DeactivationRemovesInputsAndReactivationAcceptsFreshDepth)
{
  ASSERT_TRUE(publishDepth(1000));
  layer_->deactivate();
  ASSERT_TRUE(spinUntil([this]() {
      return depth_pub_->get_subscription_count() == 0 &&
             info_pub_->get_subscription_count() == 0;
    }));
  layer_->activate();
  ASSERT_TRUE(spinUntil([this]() {
      return depth_pub_->get_subscription_count() == 1 &&
             info_pub_->get_subscription_count() == 1;
    }));
  ASSERT_TRUE(publishDepth(2000));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_EQ(costAt(2.0, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
}

TEST_F(DepthVoxelLayerTest, NewFramesCannotStarveTheFrameWaitingForExactTimeTransform)
{
  auto first = depthImage(1000);
  first.header.frame_id = "waiting_depth_optical";
  info_pub_->publish(cameraInfo(first));
  depth_pub_->publish(first);
  EXPECT_FALSE(spinUntil([this]() {return layer_->latestObservationStamp() != 0;}, 20ms));
  sensor_msgs::msg::Image latest;
  for (uint16_t index = 0; index < 3; ++index) {
    latest = depthImage(1500 + index * 100);
    latest.header.frame_id = first.header.frame_id;
    info_pub_->publish(cameraInfo(latest));
    depth_pub_->publish(latest);
    EXPECT_FALSE(spinUntil([this]() {return layer_->latestObservationStamp() != 0;}, 10ms));
  }
  // A dynamic TF sample satisfies only the first image; later frames still need
  // their own transform. Replacing the pending frame would fail this assertion.
  installCameraTransform(first.header.frame_id, first.header.stamp, false);
  ASSERT_TRUE(spinUntil([this, &first]() {
      return layer_->latestObservationStamp() == rclcpp::Time(first.header.stamp).nanoseconds();
    }, 150ms));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_EQ(costAt(1.0, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
  installCameraTransform(latest.header.frame_id, latest.header.stamp, false);
  ASSERT_TRUE(spinUntil([this, &latest]() {
      return layer_->latestObservationStamp() == rclcpp::Time(latest.header.stamp).nanoseconds();
    }, 150ms));
}

TEST_F(DepthVoxelLayerTest, MissingAndStaleDepthKeepTheLayerNonCurrent)
{
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_FALSE(layer_->isCurrent());
  ASSERT_TRUE(publishDepth(1000));
  layered_->updateMap(0.0, 0.0, 0.0);
  ASSERT_TRUE(layer_->isCurrent());
  ASSERT_TRUE(spinUntil([this]() {
      layered_->updateMap(0.0, 0.0, 0.0);
      return !layer_->isCurrent();
    }, 1000ms));
  ASSERT_TRUE(publishDepth(2000));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_TRUE(layer_->isCurrent());
}

TEST_F(DepthVoxelLayerTest, ResetCannotReplayAPreResetDepthFrame)
{
  ASSERT_TRUE(publishDepth(1000));
  auto old_image = depthImage(2000);
  layer_->reset();
  layered_->updateMap(0.0, 0.0, 0.0);
  ASSERT_FALSE(layer_->isCurrent());
  EXPECT_EQ(layer_->latestObservationStamp(), 0);
  info_pub_->publish(cameraInfo(old_image));
  depth_pub_->publish(old_image);
  EXPECT_FALSE(spinUntil([this]() {return layer_->latestObservationStamp() != 0;}, 100ms));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_FALSE(layer_->isCurrent());
  ASSERT_TRUE(publishDepth(1500));
  layered_->updateMap(0.0, 0.0, 0.0);
  EXPECT_TRUE(layer_->isCurrent());
  EXPECT_EQ(costAt(1.5, 0.0), nav2_costmap_2d::LETHAL_OBSTACLE);
}

TEST_F(DepthVoxelLayerTest, SubscribesToNativeDepthAndCalibrationButNeverPointCloud)
{
  const auto subscriptions = source_->get_node_graph_interface()->
    get_subscriber_names_and_types_by_node("depth_test_costmap", "/depth_layer_test");
  ASSERT_NE(subscriptions.find("/depth_layer_test/image"), subscriptions.end());
  ASSERT_NE(subscriptions.find("/depth_layer_test/camera_info"), subscriptions.end());
  bool image_seen = false;
  bool info_seen = false;
  for (const auto & topic : subscriptions) {
    for (const auto & type : topic.second) {
      EXPECT_NE(type, "sensor_msgs/msg/PointCloud2") << topic.first;
      image_seen = image_seen || type == "sensor_msgs/msg/Image";
      info_seen = info_seen || type == "sensor_msgs/msg/CameraInfo";
    }
  }
  EXPECT_TRUE(image_seen);
  EXPECT_TRUE(info_seen);
}

TEST_F(DepthVoxelLayerTest, ExportedNav2PluginLoadsThroughPluginlib)
{
  pluginlib::ClassLoader<nav2_costmap_2d::Layer> loader(
    "nav2_costmap_2d", "nav2_costmap_2d::Layer");
  auto plugin = loader.createSharedInstance("malbut_bringup::DepthVoxelLayer");
  ASSERT_NE(plugin, nullptr);
  EXPECT_NE(std::dynamic_pointer_cast<malbut_bringup::DepthVoxelLayer>(plugin), nullptr);
}

}  // namespace
