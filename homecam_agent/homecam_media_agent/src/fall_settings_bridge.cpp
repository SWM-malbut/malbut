#include "homecam_media_agent/fall_settings_bridge.hpp"

#include <algorithm>
#include <cmath>
#include <regex>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace homecam_media_agent
{

FallSettingsBridge::FallSettingsBridge(std::string bridge_id, std::string manager_id,
  std::string vlm_id, const double now)
: manager_id_(std::move(manager_id)), vlm_id_(std::move(vlm_id))
{
  const std::regex id("[A-Za-z0-9._:-]{1,128}");
  if (!std::regex_match(bridge_id, id) || !std::regex_match(manager_id_, id) ||
    !std::regex_match(vlm_id_, id) || !std::isfinite(now) || now < 0)
  {
    throw std::invalid_argument("invalid fall startup binding");
  }
  snapshot_.bridge_runtime_id = std::move(bridge_id);
  snapshot_.sequence = 1;
  snapshot_.observed_at = now;
}

const FallSettingsBridge::Snapshot & FallSettingsBridge::update(
  const DesiredDeviceSettings & desired, const std::string & failure_code,
  const double observed_at)
{
  if (!std::isfinite(observed_at) || observed_at < snapshot_.observed_at) {
    throw std::invalid_argument("invalid fall result time");
  }
  ++snapshot_.sequence;
  snapshot_.observed_at = observed_at;
  std::string reason = failure_code == "none" ? desired.fall_reason : failure_code;
  if (reason == "none" && desired.fall) {
    const auto & value = *desired.fall;
    if (value.revision < snapshot_.settings_revision || value.revision == 0 ||
      (value.revision == snapshot_.settings_revision &&
      (value.enabled != snapshot_.enabled || value.camera_enabled != snapshot_.camera_enabled ||
      value.cloud_consent != snapshot_.cloud_consent)))
    {
      reason = "server_invalid_settings";
    } else {
      snapshot_.settings_revision = value.revision;
      snapshot_.enabled = value.enabled;
      snapshot_.camera_enabled = value.camera_enabled;
      snapshot_.cloud_consent = value.cloud_consent;
      snapshot_.server_checked_at = observed_at;
      snapshot_.check_state = "confirmed";
      snapshot_.reason_code = "none";
      server_supports_falls_ = true;
    }
  } else if (reason == "none") {
    reason = "server_invalid_settings";
  }
  if (reason != "none") {
    const bool temporary = reason == "server_timeout" || reason == "server_transport_error";
    snapshot_.check_state = temporary ? "unavailable" : "rejected";
    if (!temporary) {
      snapshot_.server_checked_at = -1;
      server_supports_falls_ = false;
    }
    const std::unordered_set<std::string> allowed{
      "server_timeout", "server_transport_error", "server_auth_failed",
      "server_invalid_settings", "server_settings_missing"};
    snapshot_.reason_code = allowed.count(reason) ? reason : "server_invalid_settings";
  }
  history_.push_back(snapshot_);
  while (history_.size() > 256U) {history_.pop_front();}
  return snapshot_;
}

bool FallSettingsBridge::accept_report(const Report & report, const double now)
{
  if (report.bridge_runtime_id != snapshot_.bridge_runtime_id ||
    report.manager_runtime_id != manager_id_ || report.runtime_id != vlm_id_ ||
    report.sequence == 0 || (report_ && report.sequence <= report_->sequence) ||
    !std::isfinite(now) || !std::isfinite(report.reported_at) ||
    report.reported_at < 0 || report.reported_at > now)
  {
    return false;
  }
  const auto source = std::find_if(history_.begin(), history_.end(), [&report](const auto & s) {
      return s.sequence == report.snapshot_sequence && s.check_state == "confirmed";
    });
  if (source == history_.end() || report.requested_revision != source->settings_revision ||
    report.reported_at < source->observed_at)
  {
    return false;
  }
  const std::unordered_set<std::string> allowed{"applied", "already_applied", "runtime_mismatch",
    "stale_revision", "revision_conflict", "invalid_request", "internal_error"};
  const bool success = report.reason_code == "applied" || report.reason_code == "already_applied";
  if (!allowed.count(report.reason_code) || success != report.applied ||
    (success && (report.applied_revision != source->settings_revision ||
    report.enabled != source->enabled || report.camera_enabled != source->camera_enabled ||
    report.cloud_consent != source->cloud_consent)))
  {
    return false;
  }
  report_ = report;
  return true;
}

std::optional<nlohmann::json> FallSettingsBridge::report_payload(const double now) const
{
  if (!server_supports_falls_ || !report_ || !std::isfinite(now) || now < report_->reported_at) {
    return std::nullopt;
  }
  const auto & r = *report_;
  return nlohmann::json{
    {"bridgeRuntimeId", r.bridge_runtime_id}, {"managerRuntimeId", r.manager_runtime_id},
    {"sequence", std::to_string(r.sequence)}, {"snapshotSequence", std::to_string(r.snapshot_sequence)},
    {"runtimeId", r.runtime_id}, {"requestedRevision", std::to_string(r.requested_revision)},
    {"appliedRevision", std::to_string(r.applied_revision)}, {"applied", r.applied},
    {"enabled", r.enabled}, {"cameraEnabled", r.camera_enabled}, {"cloudConsent", r.cloud_consent},
    {"reasonCode", r.reason_code}, {"reportAgeS", now - r.reported_at}};
}

}  // namespace homecam_media_agent
