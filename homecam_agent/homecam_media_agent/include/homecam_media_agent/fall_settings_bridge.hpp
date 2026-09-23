#pragma once

#include <deque>
#include <optional>
#include <string>
#include "homecam_media_agent/heartbeat_client.hpp"
#include "malbut_interfaces/msg/fall_settings_snapshot.hpp"
#include "malbut_interfaces/msg/fall_settings_report.hpp"

namespace homecam_media_agent
{

// Only the owning ROS executor mutates this state. HTTP supplies its original
// completion time; reading a future later must not renew server confirmation.
class FallSettingsBridge
{
public:
  using Snapshot = malbut_interfaces::msg::FallSettingsSnapshot;
  using Report = malbut_interfaces::msg::FallSettingsReport;
  FallSettingsBridge(std::string bridge_id, std::string manager_id, std::string vlm_id,
    double now);
  const Snapshot & update(const DesiredDeviceSettings & desired,
    const std::string & failure_code, double observed_at);
  const Snapshot & snapshot() const {return snapshot_;}
  bool accept_report(const Report & report, double now);
  std::optional<nlohmann::json> report_payload(double now) const;

private:
  Snapshot snapshot_;
  std::string manager_id_;
  std::string vlm_id_;
  bool server_supports_falls_{false};
  std::deque<Snapshot> history_;
  std::optional<Report> report_;
};

}  // namespace homecam_media_agent
