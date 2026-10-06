"""Exercise the actual PTT callback with delayed GStreamer device operations.

Only the I/O boundary is replaced; the C++ method body is compiled unchanged.
This test does not measure hardware playback or replace the ROS package build.
"""

from pathlib import Path
import shutil
import subprocess

import pytest


CPP_BOUNDARY = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <mutex>
#include <string>
#include <vector>
using namespace std::chrono_literals;
using GstClockTime = std::uint64_t;
struct GstElement {};
struct GstBuffer { GstClockTime pts{0}; };
constexpr int GST_FLOW_OK = 0;
#define GST_APP_SRC(value) value
#define GST_BUFFER_PTS(value) ((value)->pts)
#define RCLCPP_WARN_THROTTLE(...) do {} while (false)
std::int64_t clock_ns = 10000000000LL;
std::int64_t allocation_completes_at = 0;
int pushed = 0, allocated = 0, unreferenced = 0;
std::int64_t steady_now_ns() { return clock_ns; }
GstBuffer *gst_buffer_new_allocate(void *, std::size_t, void *) {
  ++allocated;
  if (allocation_completes_at) clock_ns = allocation_completes_at;
  return new GstBuffer;
}
void gst_buffer_fill(GstBuffer *, std::size_t, const void *, std::size_t) {}
void gst_buffer_unref(GstBuffer *buffer) { ++unreferenced; delete buffer; }
int gst_app_src_push_buffer(GstElement *, GstBuffer *buffer) {
  ++pushed; delete buffer; return GST_FLOW_OK;
}
struct EncodedFrame { std::vector<std::uint8_t> payload{1, 2}; std::int64_t presentation_time_ns{0}; };
struct Harness {
  std::mutex ptt_mutex_;
  std::atomic<bool> shutting_down_{false};
  std::atomic<std::int64_t> ptt_allowed_until_ns_{11400000000LL};
  GstElement *ptt_playback_pipeline_{nullptr}, *ptt_source_{nullptr};
  std::chrono::steady_clock::time_point next_ptt_retry_{};
  std::int64_t startup_completes_at{0};
  int stopped{0};
  bool pipeline_bus_healthy(GstElement *, const char *) { return true; }
  void stop_ptt_playback_locked() {
    ++stopped; ptt_playback_pipeline_ = ptt_source_ = nullptr;
  }
  bool start_ptt_playback_locked() {
    static GstElement element;
    ptt_playback_pipeline_ = ptt_source_ = &element;
    if (startup_completes_at) clock_ns = startup_completes_at;
    return true;
  }
  // PRODUCTION_PUSH
};
int main() {
  int failed = 0;
  auto reset = [] { clock_ns = 10000000000LL; allocation_completes_at = 0;
    pushed = allocated = unreferenced = 0; };
  auto check = [&failed](bool condition, const char *name) {
    std::cout << (condition ? "PASS " : "FAIL ") << name << std::endl;
    if (!condition) ++failed;
  };
  reset();
  { Harness h; h.push_ptt_audio(EncodedFrame{});
    check(pushed == 1, "valid_live_lease_pushes_one_buffer"); }
  reset();
  { Harness h; clock_ns = 12000000000LL; h.push_ptt_audio(EncodedFrame{});
    check(pushed == 0 && allocated == 0, "expired_entry_never_starts_or_allocates"); }
  reset();
  { Harness h; h.startup_completes_at = 12100000000LL; h.push_ptt_audio(EncodedFrame{});
    check(pushed == 0 && h.ptt_source_ == nullptr, "startup_passing_stt_expiry_never_pushes_audio");
    check(allocated == unreferenced, "startup_expiry_releases_any_allocated_buffer"); }
  reset();
  { Harness h; allocation_completes_at = 12100000000LL; h.push_ptt_audio(EncodedFrame{});
    check(pushed == 0 && h.ptt_source_ == nullptr, "allocation_passing_stt_expiry_never_pushes_audio");
    check(allocated == unreferenced, "allocation_expiry_releases_buffer"); }
  return failed ? 1 : 0;
}
'''


def test_ptt_playback_rechecks_permission_after_slow_device_operations(tmp_path):
    compiler = shutil.which('c++') or shutil.which('clang++')
    if compiler is None:
        pytest.skip('C++17 compiler is required for the PTT callback regression')
    node = Path(__file__).resolve().parents[1] / 'homecam_media_agent/src/media_agent_node.cpp'
    source = node.read_text()
    start = source.index('void push_ptt_audio(const EncodedFrame & frame)')
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    cpp = tmp_path / 'ptt_callback.cpp'
    binary = tmp_path / 'ptt_callback'
    cpp.write_text(CPP_BOUNDARY.replace('// PRODUCTION_PUSH', source[start:end]))
    compiled = subprocess.run(
        [compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
         str(cpp), '-o', str(binary)], text=True, capture_output=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
