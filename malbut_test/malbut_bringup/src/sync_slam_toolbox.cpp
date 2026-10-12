// Copyright 2026 Malbut
// SPDX-License-Identifier: Apache-2.0

#include <memory>
#include <sys/resource.h>

#include "rclcpp/rclcpp.hpp"
#include "slam_toolbox/slam_toolbox_sync.hpp"

class ShutdownSafeSlam : public slam_toolbox::SynchronousSlamToolbox
{
public:
  explicit ShutdownSafeSlam(const rclcpp::NodeOptions & options)
  : slam_toolbox::SynchronousSlamToolbox(options) {}

  ~ShutdownSafeSlam() override
  {
    stop();
  }

  void stop()
  {
    if (rclcpp::ok()) {
      rclcpp::shutdown();
    }
    // Humble joins these in SlamToolbox's BASE destructor, after the sync
    // subclass has destroyed q_ and q_mutex_ used by its processing thread.
    // Join while that storage is still alive; leave mapping itself unchanged.
    for (auto & worker : threads_) {
      if (worker->joinable()) {
        worker->join();
      }
    }
    threads_.clear();
    // These helpers retain shared_from_this(). Relying on the destructor
    // leaves a reference cycle and lets mapper workers outlive main(), then
    // access Karto globals while shared libraries are being unloaded.
    closure_assistant_.reset();
    map_saver_.reset();
    laser_assistant_.reset();
  }
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  {
    auto parameters = std::make_shared<rclcpp::Node>("slam_toolbox");
    int stack_size = 40000000;
    parameters->declare_parameter("stack_size_to_use", rclcpp::ParameterType::PARAMETER_INTEGER);
    if (parameters->get_parameter("stack_size_to_use", stack_size)) {
      struct rlimit limit;
      if (getrlimit(RLIMIT_STACK, &limit) == 0 && limit.rlim_cur < stack_size) {
        limit.rlim_cur = stack_size;
        setrlimit(RLIMIT_STACK, &limit);
      }
    }
  }
  auto node = std::make_shared<ShutdownSafeSlam>(rclcpp::NodeOptions());
  node->configure();
  node->loadPoseGraphByParams();
  rclcpp::spin(node->get_node_base_interface());
  rclcpp::shutdown();
  node->stop();
  node.reset();
  return 0;
}
