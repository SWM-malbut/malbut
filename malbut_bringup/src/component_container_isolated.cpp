// Copyright 2026 Malbut
// SPDX-License-Identifier: Apache-2.0

#include <atomic>
#include <chrono>
#include <csignal>
#include <dlfcn.h>
#include <link.h>
#include <memory>
#include <thread>
#include <unordered_map>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_components/component_manager.hpp"

namespace
{
volatile std::sig_atomic_t closing = 0;

void request_shutdown(int)
{
  closing = 1;
}

int retain_plugin_code(struct dl_phdr_info * library, size_t, void *)
{
  // A plugin can leave weak callback handles in LifecycleNode. Their control
  // block's destructor still lives in that plugin after its instances die.
  // Keep loaded code mapped until process exit, before cleanup unloads it.
  // No extra reference is retained and no ROS object/thread is leaked.
  if (library->dlpi_name[0] == '/') {
    void * handle = dlopen(library->dlpi_name, RTLD_NOW | RTLD_NOLOAD | RTLD_NODELETE);
    if (handle != nullptr) {
      dlclose(handle);
    }
  }
  return 0;
}
}  // namespace

// Same per-component SingleThreadedExecutor model as Humble's isolated
// container. Its cancel_executor waits forever for is_spinning() after a
// context shutdown has already stopped that executor. Wait for startup when
// adding the component, never when removing an already stopped component.
class ShutdownSafeContainer : public rclcpp_components::ComponentManager
{
public:
  using rclcpp_components::ComponentManager::ComponentManager;

  ~ShutdownSafeContainer() override
  {
    for (auto & item : executors_) {
      stop(item.second);
    }
    node_wrappers_.clear();
  }

protected:
  void add_node_to_executor(uint64_t id) override
  {
    auto executor = std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
    executor->add_node(node_wrappers_[id].get_node_base_interface());
    auto finished = std::make_shared<std::atomic_bool>(false);
    auto & worker = executors_[id];
    worker.executor = executor;
    worker.thread = std::thread([executor, finished]() {
        executor->spin();
        finished->store(true);
      });
    // This also handles a context shutdown that happened before spin started.
    while (!executor->is_spinning() && !finished->load()) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
  }

  void remove_node_from_executor(uint64_t id) override
  {
    auto found = executors_.find(id);
    if (found != executors_.end()) {
      stop(found->second);
      executors_.erase(found);
    }
  }

private:
  struct Worker
  {
    std::shared_ptr<rclcpp::Executor> executor;
    std::thread thread;
  };

  static void stop(Worker & worker)
  {
    worker.executor->cancel();
    if (worker.thread.joinable()) {
      worker.thread.join();
    }
  }

  std::unordered_map<uint64_t, Worker> executors_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv, rclcpp::InitOptions(), rclcpp::SignalHandlerOptions::None);
  std::signal(SIGINT, request_shutdown);
  std::signal(SIGTERM, request_shutdown);
  auto executor = std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
  auto manager = std::make_shared<ShutdownSafeContainer>(executor);
  executor->add_node(manager);
  // Preserve the native continuous executor during operation. The small
  // signal waiter replaces rclcpp's default signal thread only for shutdown.
  std::thread shutdown_thread([]() {
      while (rclcpp::ok() && !closing) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      }
      dl_iterate_phdr(retain_plugin_code, nullptr);
      rclcpp::shutdown();
    });
  try {
    executor->spin();
  } catch (...) {
    closing = 1;
    shutdown_thread.join();
    throw;
  }
  shutdown_thread.join();
  return 0;
}
