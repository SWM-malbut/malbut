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
GstClockTime pushed_pts = 0;
std::int64_t steady_now_ns() { return clock_ns; }
GstBuffer *gst_buffer_new_allocate(void *, std::size_t, void *) {
  ++allocated;
  if (allocation_completes_at) clock_ns = allocation_completes_at;
  return new GstBuffer;
}
void gst_buffer_fill(GstBuffer *, std::size_t, const void *, std::size_t) {}
void gst_buffer_unref(GstBuffer *buffer) { ++unreferenced; delete buffer; }
int gst_app_src_push_buffer(GstElement *, GstBuffer *buffer) {
  ++pushed; pushed_pts = buffer->pts; delete buffer; return GST_FLOW_OK;
}
// Frames play on the sender's timeline after a fixed delay (PlayoutSchedule).
struct PlayoutSchedule {
  std::int64_t schedule(std::int64_t frame_ns, std::int64_t running_now_ns) {
    return running_now_ns + 120000000LL + frame_ns;
  }
};
enum class PttPlayout { kScheduled, kArrival, kBuffered, kAdaptive };
struct PttFrameNote { bool dropped{false}, skipped{false}, reanchored{false}, concealed{false};
  std::int64_t late_ns{0}, delay_ns{-1}, target_ns{-1}; };
// The jitter buffer drops a frame marked late (frame time 13) and plays the rest 300 ms on.
struct BufferedPlayout {
  struct Plan { std::int64_t playout_ns{-1}; bool dropped{false}; bool reanchored{false};
    std::int64_t late_ns{0}; };
  std::int64_t delay{300000000LL};
  void set_delay(std::int64_t value) { delay = value; }
  std::int64_t delay_ns() const { return delay; }
  Plan schedule(std::int64_t frame_ns, std::int64_t running_now_ns) {
    Plan plan;
    if (frame_ns == 13) { plan.dropped = true; return plan; }
    plan.playout_ns = running_now_ns + delay + frame_ns;
    return plan;
  }
};
// The adaptive buffer plays 200 ms on; frame time 60 ms comes after two lost frames.
struct AdaptivePlayout {
  struct Plan { std::int64_t playout_ns{-1}; bool dropped{false}, skipped{false}, reanchored{false};
    std::int64_t gap_start_ns{-1}, late_ns{0}, delay_ns{0}, target_ns{0}; };
  Plan schedule(std::int64_t frame_ns, std::int64_t running_now_ns, std::size_t) {
    Plan plan;
    plan.playout_ns = running_now_ns + 200000000LL + frame_ns;
    if (frame_ns == 60000000LL) plan.gap_start_ns = plan.playout_ns - 40000000LL;
    return plan;
  }
};
struct EncodedFrame { std::vector<std::uint8_t> payload{1, 2}; std::int64_t presentation_time_ns{0}; };
struct Harness {
  std::mutex ptt_mutex_;
  std::atomic<bool> shutting_down_{false};
  std::atomic<std::int64_t> ptt_allowed_until_ns_{11400000000LL};
  std::atomic<PttPlayout> ptt_mode_{PttPlayout::kScheduled};
  PttPlayout ptt_pipeline_mode_{PttPlayout::kScheduled};
  std::atomic<std::int64_t> ptt_delay_ns_{300000000LL};
  std::atomic<bool> ptt_plc_{true}, ptt_fec_{false};
  bool ptt_pipeline_plc_{true}, ptt_pipeline_fec_{false};
  BufferedPlayout ptt_buffer_;
  AdaptivePlayout ptt_adaptive_;
  int noted{0}, concealed{0};
  std::int64_t gap_start{-1}, gap_duration{-1};
  void note_ptt_frame_locked(const PttFrameNote & note) { ++noted; concealed += note.concealed; }
  void send_ptt_gap_locked(std::int64_t start, std::int64_t duration) {
    gap_start = start; gap_duration = duration;
  }
  GstElement *ptt_playback_pipeline_{nullptr}, *ptt_source_{nullptr};
  std::chrono::steady_clock::time_point next_ptt_retry_{};
  std::int64_t startup_completes_at{0};
  int stopped{0};
  PlayoutSchedule ptt_schedule_;
  std::int64_t ptt_running_time_locked() const { return 5; }
  bool pipeline_bus_healthy(GstElement *, const char *) { return true; }
  void stop_ptt_playback_locked() {
    ++stopped; ptt_playback_pipeline_ = ptt_source_ = nullptr;
  }
  bool start_ptt_playback_locked() {
    static GstElement element;
    ptt_pipeline_mode_ = ptt_mode_.load();
    ptt_pipeline_plc_ = ptt_plc_.load(); ptt_pipeline_fec_ = ptt_fec_.load();
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
  { Harness h; EncodedFrame frame; frame.presentation_time_ns = 40000000LL;
    h.push_ptt_audio(frame);
    check(pushed == 1, "valid_live_lease_pushes_one_buffer");
    check(pushed_pts == 5 + 120000000LL + 40000000LL, "buffer_plays_at_its_scheduled_time"); }
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
  reset();
  { Harness h; h.ptt_mode_.store(PttPlayout::kArrival); EncodedFrame frame;
    frame.presentation_time_ns = 40000000LL; h.push_ptt_audio(frame);
    check(pushed == 1 && pushed_pts == 40000000LL, "arrival_mode_keeps_the_frame_time"); }
  reset();
  { Harness h; EncodedFrame frame; h.push_ptt_audio(frame);
    h.ptt_mode_.store(PttPlayout::kArrival); h.push_ptt_audio(frame);
    check(h.stopped == 1 && h.ptt_pipeline_mode_ == PttPlayout::kArrival && pushed == 2,
          "switching_the_mode_rebuilds_playback_at_the_next_frame"); }
  reset();
  { Harness h; h.ptt_mode_.store(PttPlayout::kBuffered); h.ptt_delay_ns_.store(400000000LL);
    EncodedFrame frame; frame.presentation_time_ns = 40000000LL; h.push_ptt_audio(frame);
    check(pushed == 1 && pushed_pts == 5 + 400000000LL + 40000000LL,
          "buffered_mode_plays_behind_the_set_delay");
    frame.presentation_time_ns = 13; h.push_ptt_audio(frame);
    check(pushed == 1 && allocated == 1, "buffered_mode_drops_a_late_frame_without_allocating");
    check(h.noted == 2, "every_frame_is_counted_for_the_5s_line"); }
  reset();
  { Harness h; h.ptt_mode_.store(PttPlayout::kAdaptive); EncodedFrame frame;
    frame.presentation_time_ns = 60000000LL; h.push_ptt_audio(frame);
    check(pushed == 1 && pushed_pts == 5 + 200000000LL + 60000000LL,
          "adaptive_mode_plays_at_the_planned_time");
    check(h.gap_start == 5 + 200000000LL + 20000000LL && h.gap_duration == 40000000LL &&
          h.concealed == 1, "adaptive_mode_conceals_frames_lost_before_it"); }
  reset();
  { Harness h; h.ptt_mode_.store(PttPlayout::kAdaptive); h.ptt_plc_.store(false);
    EncodedFrame frame; frame.presentation_time_ns = 60000000LL; h.push_ptt_audio(frame);
    check(pushed == 1 && h.gap_start == -1 && h.concealed == 0,
          "turning_plc_off_leaves_lost_frames_silent"); }
  reset();
  { Harness h; h.ptt_mode_.store(PttPlayout::kAdaptive); EncodedFrame frame;
    frame.presentation_time_ns = 60000000LL; allocation_completes_at = 12100000000LL;
    h.push_ptt_audio(frame);
    check(pushed == 0 && h.gap_start == -1, "expired_permission_sends_no_concealment"); }
  reset();
  { Harness h; EncodedFrame frame; h.push_ptt_audio(frame);
    h.ptt_fec_.store(true); h.push_ptt_audio(frame);
    check(h.stopped == 1 && h.ptt_pipeline_fec_ && pushed == 2,
          "switching_fec_rebuilds_playback_at_the_next_frame"); }
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
