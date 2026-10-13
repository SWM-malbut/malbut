"""Compile the real session worker/collector against offline HTTP/KVS boundaries.

These tests advance a synthetic clock; they are not a robot or AWS soak test.
"""

from pathlib import Path
import shutil
import subprocess

import pytest


CPP = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
using namespace std::chrono_literals;
template<class... Args> void log_stub(Args&&...) {}
#define RCLCPP_ERROR(...) log_stub(__VA_ARGS__)
#define RCLCPP_INFO(...) log_stub(__VA_ARGS__)
#define RCLCPP_WARN_THROTTLE(...) do {} while(false)
#define HOMECAM_HAVE_KVS 1
#define HOMECAM_HAVE_GSTREAMER 1
std::int64_t now_ms = 600000;
enum class SessionMode { kPeerToPeer, kStorage };
// REFRESH_POLICY
struct SessionCredentials {
  std::string access_key_id{"id"}, secret_access_key{"secret"}, session_token{"token"};
  std::int64_t expires_at_unix_ms{900000};
};
struct SessionLease { SessionMode mode{SessionMode::kPeerToPeer}; SessionCredentials credentials; };
struct Settings {
  std::optional<bool> camera_enabled{true}, microphone_enabled{true}, monitoring_enabled{true};
};
struct DeviceSessionResult { SessionLease lease; Settings desired; std::string session_id{"same-session"}; };
enum class SessionCloseDisposition { kTerminal, kRetryableFailure, kPermanentFailure };
struct SessionCloseResult {
  SessionCloseDisposition disposition{SessionCloseDisposition::kTerminal};
  std::string error;
  bool terminal() const {return disposition == SessionCloseDisposition::kTerminal;}
};
bool session_create_requires_fail_closed(long status) {return status == 401 || status == 403;}
bool media_generation_allows_io(std::uint64_t request, std::uint64_t current, bool allowed) {
  return allowed && request == current;
}
struct DeviceSessionClient {
  bool ok{true}; long status{200}; int closed{0};
  bool available() const {return true;}
  DeviceSessionResult next;
  std::function<void()> before_reply;
  bool create(SessionMode, std::int64_t now, DeviceSessionResult * result, std::string * error, long * code) {
    if (before_reply) before_reply();
    *code=status;
    if (!ok) { *error="injected failure"; return false; }
    *result=next;
    result->lease.credentials.expires_at_unix_ms=now+900000;
    return true;
  }
  SessionCloseResult close(const std::string &) {++closed; return {};}
};
struct KvsTransport {
  bool peer{true}, allow_renewal{true}; int restarts{0}, stops{0}, renewals{0};
  std::int64_t deadline{900000};
  bool implemented() const {return true;}
  bool restart_required() const {return false;}
  bool peer_connected() const {return peer;}
  void stop() {++stops; peer=false;}
  bool start(const SessionLease & lease, std::string *) {
    ++restarts; deadline=lease.credentials.expires_at_unix_ms; return true;
  }
  bool renew_credentials(const SessionLease & lease, std::string * error) {
    ++renewals;
    if (!allow_renewal) { *error="injected renewal failure"; return false; }
    deadline=lease.credentials.expires_at_unix_ms; return true;
  }
};
struct MediaAgentNode {
  // OUTCOME
  std::unique_ptr<DeviceSessionClient> session_client_{std::make_unique<DeviceSessionClient>()};
  std::unique_ptr<KvsTransport> transport_{std::make_unique<KvsTransport>()};
  std::mutex transport_mutex_;
  std::atomic<bool> shutting_down_{false}, media_permitted_{true};
  std::atomic<std::uint64_t> session_generation_{1}, active_transport_generation_{1};
  std::string active_session_id_{"same-session"}, cleanup_session_id_, active_stream_mode_{"p2p"};
  bool backend_session_may_be_open_{true}, session_permanent_failure_{false};
  std::int64_t active_session_expires_at_ms_{900000};
  int session_failure_count_{0}, retried{0};
  std::chrono::steady_clock::time_point next_session_attempt_{}, active_transport_started_at_{};
  struct { bool camera_enabled{true}, microphone_enabled{true}, monitoring_enabled{true}; } config_;
  std::future<SessionOutcome> session_future_;
  int get_logger() const {return 0;}
  bool media_inputs_ready() const {return true;}
  static std::int64_t unix_now_ms() {return now_ms;}
  static bool is_valid_session_id(const std::string & id) {return !id.empty();}
  static void append_session_error(std::string * error, const std::string & detail) {*error+=detail;}
  static void clear_session_credentials(SessionCredentials *) {}
  void schedule_session_retry(const std::string &) {++retried;}
  void publish_storage_session_id() {}
  // CLEAR_LOCAL_STATE
  // FAIL_CLOSED
  void apply_desired_settings(const Settings & desired, bool) {
    if (!desired.camera_enabled.value_or(false)) {
      media_permitted_.store(false); active_transport_generation_.store(0); transport_->stop();
    }
  }
  // WORKER
  // COLLECTOR
  // MAINTAIN
  void launch_session_stop(const std::string & id) {session_client_->close(id);}
  void run() {launch_session_start_or_refresh(true); session_future_.wait(); collect_session_result();}
  void tick() {
    maintain_device_session();
    if(session_future_.valid()) session_future_.wait();
    collect_session_result();
  }
};
int main() {
  int failures=0;
  auto check=[&](bool ok,const char * label) {
    std::cout<<(ok?"PASS ":"FAIL ")<<label<<"\n"; if(!ok) ++failures;
  };
  { MediaAgentNode node;
    for(now_ms=0; now_ms<=2700000; now_ms+=1000) node.tick();
    check(node.transport_->renewals==4 && node.transport_->restarts==0 && node.transport_->stops==0,
      "45min schedule uses four renewals and zero peer restarts");
    check(node.active_session_expires_at_ms_==3300000 && node.active_transport_generation_.load()==1,
      "successful renewals advance deadline without changing media generation"); }
  now_ms=600000;
  { MediaAgentNode node; node.session_client_->ok=false; node.session_client_->status=503; node.run();
    check(node.retried==1 && node.transport_->stops==0 && node.active_session_expires_at_ms_==900000,
      "HTTP failure keeps old deadline and retries"); }
  { MediaAgentNode node; node.transport_->allow_renewal=false; node.run();
    check(node.retried==1 && node.session_client_->closed==0 && node.transport_->restarts==0 &&
      node.active_session_expires_at_ms_==900000,
      "provider failure does not close the reused backend session or extend its local deadline"); }
  { MediaAgentNode node; node.session_client_->ok=false; node.session_client_->status=503;
    node.tick();
    node.next_session_attempt_=std::chrono::steady_clock::time_point::max();
    now_ms=899999; node.tick();
    check(node.transport_->stops==0, "retry backoff preserves an unexpired peer");
    now_ms=900000; node.tick();
    check(node.transport_->stops==1 && node.active_stream_mode_=="idle" &&
      node.active_transport_generation_.load()==0,
      "lease expiration still stops playback during retry backoff"); }
  now_ms=600000;
  { MediaAgentNode node;
    std::promise<void> entered, release;
    auto ready=entered.get_future(); auto proceed=release.get_future();
    node.session_client_->before_reply=[&] {entered.set_value(); proceed.wait();};
    node.maintain_device_session(); ready.wait();
    now_ms=900000; node.maintain_device_session();
    const bool stopped_at_expiry=node.transport_->stops==1 && node.active_transport_generation_.load()==0;
    release.set_value(); node.session_future_.wait(); node.collect_session_result();
    check(stopped_at_expiry && node.transport_->renewals==0 && node.transport_->restarts==0 &&
      node.active_transport_generation_.load()==0,
      "in-flight renewal cannot postpone expiration or revive the expired peer"); }
  now_ms=600000;
  { MediaAgentNode node; node.session_client_->ok=false; node.session_client_->status=401; node.run();
    check(node.transport_->stops==1 && node.active_transport_generation_.load()==0 &&
      node.session_permanent_failure_, "authorization failure still stops playback immediately"); }
  { MediaAgentNode node; node.session_client_->before_reply=[&] {node.session_generation_.fetch_add(1);}; node.run();
    check(node.transport_->renewals==0 && node.transport_->stops==1 && node.session_client_->closed==1,
      "stale generation cannot renew or revive the peer"); }
  { MediaAgentNode node; node.session_client_->next.session_id="replacement"; node.run();
    check(node.transport_->renewals==0 && node.transport_->restarts==1,
      "new backend session is not silently attached to old peer"); }
  { MediaAgentNode node; node.session_client_->next.desired.camera_enabled=false; node.run();
    check(node.transport_->renewals==0 && node.transport_->stops>=1 &&
      !node.media_permitted_.load(), "privacy revocation never uses credential-only renewal"); }
  { MediaAgentNode node; node.session_client_->next.desired.microphone_enabled=false; node.run();
    check(node.transport_->renewals==0 && node.transport_->restarts==1,
      "microphone privacy change does not preserve old transport"); }
  { MediaAgentNode node; node.transport_->peer=false;
    node.session_client_->before_reply=[&] {node.transport_->peer=true;}; node.run();
    check(node.transport_->renewals==1 && node.transport_->restarts==0,
      "viewer arriving during renewal request is preserved"); }
  return failures?1:0;
}
'''


def method(source, signature):
    start = source.index(signature)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def test_actual_session_worker_and_collector_across_renewals(tmp_path):
    compiler = shutil.which('c++')
    if compiler is None:
        pytest.skip('C++17 compiler required')
    source = (Path(__file__).resolve().parents[1] /
              'homecam_media_agent/src/media_agent_node.cpp').read_text()
    outcome = source[source.index('  enum class SessionOperation'):
                     source.index('  struct StorageSessionOutcome')]
    cpp = CPP.replace('// OUTCOME', outcome)
    cpp = cpp.replace('// WORKER', method(source, '  void launch_session_start_or_refresh('))
    cpp = cpp.replace('// COLLECTOR', method(source, '  void collect_session_result()'))
    cpp = cpp.replace('// CLEAR_LOCAL_STATE', method(source, '  void clear_local_session_state()'))
    cpp = cpp.replace('// FAIL_CLOSED', method(source, '  void fail_closed_active_session('))
    cpp = cpp.replace('// MAINTAIN', method(source, '  void maintain_device_session()'))
    transport_root = Path(__file__).resolve().parents[1] / 'homecam_media_agent'
    header = (transport_root / 'include/homecam_media_agent/kvs_transport.hpp').read_text()
    policy = header[header.index('enum class SessionRefreshDecision'):]
    policy = policy[:policy.index('};') + 2]
    transport = (transport_root / 'src/kvs_transport.cpp').read_text()
    for signature in ('bool session_lease_expired(', 'bool storage_session_refresh_due(',
                      'bool storage_session_hard_expired(',
                      'SessionRefreshDecision decide_session_refresh('):
        policy += '\n' + method(transport, signature)
    cpp = cpp.replace('// REFRESH_POLICY', policy)
    path = tmp_path / 'renewal.cpp'
    path.write_text(cpp)
    binary = tmp_path / 'renewal'
    compiled = subprocess.run(
        [compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
         str(path), '-o', str(binary)], capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
