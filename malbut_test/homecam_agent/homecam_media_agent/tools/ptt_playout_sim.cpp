// Listen to the guardian's voice playout on this PC, without the robot (2026-10-13).
//
// A voice recording (or a tone) is encoded to 20 ms Opus frames as a browser
// would send them, delayed by a simulated network (jitter, stalls, loss), and
// played through the robot's own playback pipeline and playout classes. Each
// part can be switched on or off to compare by ear:
//
//   ptt_playout_sim --input voice.wav --mode adaptive --plc on --fec off --jitter phone
//
// The sound goes to this PC's default output (or --sink <PulseAudio sink>).
// Built with the package tests; never installed on the robot.

#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "homecam_media_agent/config.hpp"
#include "homecam_media_agent/kvs_transport.hpp"
#include "homecam_media_agent/pipeline_builder.hpp"

namespace
{
using homecam_media_agent::AdaptivePlayout;
using homecam_media_agent::BufferedPlayout;
using homecam_media_agent::MediaConfig;
using homecam_media_agent::PlayoutSchedule;
using homecam_media_agent::PttPipelineOptions;

constexpr std::int64_t kFrameNs = 20'000'000;

struct Options
{
  std::string input{"tone"};
  std::string mode{"arrival"};
  std::string jitter{"phone"};
  std::string sink{"default"};
  std::int64_t delay_ms{300};
  bool plc{true};
  bool fec{false};
  double loss{0.0};
  double seconds{20.0};
  unsigned seed{1};
};

struct Frame
{
  std::vector<std::uint8_t> payload;
  std::int64_t sent_ns{0};
  std::int64_t arrival_ns{0};
  bool lost{false};
};

bool flag(const std::string & value)
{
  return value == "on" || value == "true" || value == "1";
}

bool parse(int argc, char ** argv, Options * options)
{
  for (int i = 1; i + 1 < argc; i += 2) {
    const std::string key = argv[i];
    const std::string value = argv[i + 1];
    if (key == "--input") {
      options->input = value;
    } else if (key == "--mode") {
      options->mode = value;
    } else if (key == "--jitter") {
      options->jitter = value;
    } else if (key == "--sink") {
      options->sink = value;
    } else if (key == "--delay-ms") {
      options->delay_ms = std::stoll(value);
    } else if (key == "--plc") {
      options->plc = flag(value);
    } else if (key == "--fec") {
      options->fec = flag(value);
    } else if (key == "--loss") {
      options->loss = std::stod(value);
    } else if (key == "--seconds") {
      options->seconds = std::stod(value);
    } else if (key == "--seed") {
      options->seed = static_cast<unsigned>(std::stoul(value));
    } else {
      std::cerr << "unknown option " << key << "\n";
      return false;
    }
  }
  const auto & m = options->mode;
  return m == "arrival" || m == "scheduled" || m == "buffered" || m == "adaptive";
}

// Opus frames as a browser sends them: 20 ms, VBR voice, FEC when asked.
std::vector<Frame> encode(const Options & options)
{
  const int buffers = static_cast<int>(options.seconds * 50.0);
  const std::string source = options.input == "tone" ?
    "audiotestsrc wave=sine freq=440 volume=0.3 num-buffers=" + std::to_string(buffers) +
    " samplesperbuffer=960" :
    "filesrc location=\"" + options.input + "\" ! decodebin";
  const std::string description = source +
    " ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
    "opusenc frame-size=20 bitrate=32000 bitrate-type=vbr" +
    std::string(options.fec ? " inband-fec=true packet-loss-percentage=" +
    std::to_string(static_cast<int>(std::max(options.loss, 0.05) * 100.0)) : "") +
    " ! appsink name=frames sync=false";
  GError * error = nullptr;
  GstElement * pipeline = gst_parse_launch(description.c_str(), &error);
  if (pipeline == nullptr || error != nullptr) {
    std::cerr << "cannot encode: " << (error ? error->message : "unknown") << "\n";
    std::exit(1);
  }
  GstElement * sink = gst_bin_get_by_name(GST_BIN(pipeline), "frames");
  gst_element_set_state(pipeline, GST_STATE_PLAYING);
  std::vector<Frame> frames;
  while (true) {
    GstSample * sample = gst_app_sink_pull_sample(GST_APP_SINK(sink));
    if (sample == nullptr) {
      break;
    }
    GstBuffer * buffer = gst_sample_get_buffer(sample);
    GstMapInfo map;
    if (gst_buffer_map(buffer, &map, GST_MAP_READ)) {
      Frame frame;
      frame.payload.assign(map.data, map.data + map.size);
      frame.sent_ns = static_cast<std::int64_t>(frames.size()) * kFrameNs;
      frames.push_back(std::move(frame));
      gst_buffer_unmap(buffer, &map);
    }
    gst_sample_unref(sample);
    if (frames.size() >= static_cast<std::size_t>(options.seconds * 50.0)) {
      break;
    }
  }
  gst_element_set_state(pipeline, GST_STATE_NULL);
  gst_object_unref(sink);
  gst_object_unref(pipeline);
  return frames;
}

// Arrival times: a base transit, jitter with a long tail, stalls that make the
// following frames bunch up, and random loss. Frames stay in order.
void simulate_network(const Options & options, std::vector<Frame> * frames)
{
  std::mt19937 random(options.seed);
  std::uniform_real_distribution<double> unit(0.0, 1.0);
  double mean_ms = 0.0, spike_p = 0.0, spike_lo = 0.0, spike_hi = 0.0;
  double stall_p = 0.0, stall_lo = 0.0, stall_hi = 0.0;
  if (options.jitter == "wifi") {
    mean_ms = 6; spike_p = 0.02; spike_lo = 30; spike_hi = 80;
  } else if (options.jitter == "phone") {
    mean_ms = 15; spike_p = 0.05; spike_lo = 60; spike_hi = 200;
    stall_p = 0.01; stall_lo = 200; stall_hi = 400;
  } else if (options.jitter == "bad") {
    mean_ms = 30; spike_p = 0.08; spike_lo = 100; spike_hi = 300;
    stall_p = 0.02; stall_lo = 300; stall_hi = 600;
  }
  std::exponential_distribution<double> exponential(mean_ms > 0 ? 1.0 / mean_ms : 1.0);
  std::int64_t previous = 0;
  for (auto & frame : *frames) {
    double transit_ms = 40.0 + (mean_ms > 0 ? exponential(random) : 0.0);
    const double roll = unit(random);
    if (roll < stall_p) {
      transit_ms += stall_lo + unit(random) * (stall_hi - stall_lo);
    } else if (roll < stall_p + spike_p) {
      transit_ms += spike_lo + unit(random) * (spike_hi - spike_lo);
    }
    frame.arrival_ns = std::max<std::int64_t>(
      frame.sent_ns + static_cast<std::int64_t>(transit_ms * 1e6), previous);
    previous = frame.arrival_ns;
    frame.lost = unit(random) < options.loss;
  }
}

std::int64_t running_time(GstElement * pipeline)
{
  GstClock * clock = gst_element_get_clock(pipeline);
  if (clock == nullptr) {
    return 0;
  }
  const GstClockTime now = gst_clock_get_time(clock);
  gst_object_unref(clock);
  const GstClockTime base = gst_element_get_base_time(pipeline);
  return now > base ? static_cast<std::int64_t>(now - base) : 0;
}

}  // namespace

int main(int argc, char ** argv)
{
  gst_init(&argc, &argv);
  Options options;
  if (!parse(argc, argv, &options)) {
    std::cerr <<
      "usage: ptt_playout_sim [--input tone|<wav>] [--mode arrival|scheduled|buffered|adaptive]\n"
      "  [--jitter none|wifi|phone|bad] [--loss 0.02] [--delay-ms 300] [--plc on|off]\n"
      "  [--fec on|off] [--seconds 20] [--seed 1] [--sink default|<pulse sink>]\n";
    return 2;
  }
  std::vector<Frame> frames = encode(options);
  simulate_network(options, &frames);

  MediaConfig config;
  PttPipelineOptions pipeline_options;
  pipeline_options.scheduled = options.mode != "arrival";
  pipeline_options.plc = pipeline_options.scheduled && options.plc;
  pipeline_options.fec = options.fec;
  pipeline_options.queue_ns = options.mode == "arrival" ? 200'000'000 :
    options.mode == "scheduled" ? 500'000'000 : 1'500'000'000;
  std::string description = build_ptt_playback_pipeline(config, pipeline_options);
  if (options.sink != "default") {
    const auto at = description.find("alsasink device=");
    const auto end = description.find(" sync=", at);
    description.replace(at, end - at, "pulsesink device=" + options.sink);
  }
  std::cout << "pipeline: " << description << "\n";
  GError * error = nullptr;
  GstElement * pipeline = gst_parse_launch(description.c_str(), &error);
  if (pipeline == nullptr || error != nullptr) {
    std::cerr << "cannot play: " << (error ? error->message : "unknown") << "\n";
    return 1;
  }
  GstElement * source = gst_bin_get_by_name(GST_BIN(pipeline), "ptt_audio_source");
  gst_element_set_state(pipeline, GST_STATE_PLAYING);

  PlayoutSchedule scheduled(120'000'000);
  BufferedPlayout buffered(options.delay_ms * 1'000'000);
  AdaptivePlayout adaptive;
  int pushed = 0, lost = 0, dropped = 0, skipped = 0, concealed = 0, reanchored = 0;
  std::int64_t last_playout = 0, max_delay = 0, last_delay = 0;
  // Sent to heard: the running clock starts with the loop, like the sender's.
  std::int64_t latency_sum = 0, latency_max = 0;
  const auto start = std::chrono::steady_clock::now();
  for (const auto & frame : frames) {
    std::this_thread::sleep_until(start + std::chrono::nanoseconds(frame.arrival_ns));
    if (frame.lost) {
      ++lost;
      continue;
    }
    const std::int64_t now = running_time(pipeline);
    std::int64_t playout = frame.sent_ns;
    std::int64_t conceal_from = -1;
    if (options.mode == "scheduled") {
      playout = scheduled.schedule(frame.sent_ns, now);
    } else if (options.mode == "buffered") {
      const auto plan = buffered.schedule(frame.sent_ns, now);
      playout = plan.playout_ns;
      dropped += plan.dropped;
      reanchored += plan.reanchored;
    } else if (options.mode == "adaptive") {
      const auto plan = adaptive.schedule(frame.sent_ns, now, frame.payload.size());
      playout = plan.playout_ns;
      dropped += plan.dropped;
      skipped += plan.skipped;
      reanchored += plan.reanchored;
      last_delay = plan.delay_ns;
      max_delay = std::max(max_delay, plan.delay_ns);
      if (options.plc && plan.gap_start_ns >= 0 && playout > plan.gap_start_ns) {
        conceal_from = plan.gap_start_ns;
        ++concealed;
      }
    }
    if (playout < 0) {
      continue;
    }
    GstBuffer * buffer = gst_buffer_new_allocate(nullptr, frame.payload.size(), nullptr);
    gst_buffer_fill(buffer, 0, frame.payload.data(), frame.payload.size());
    GST_BUFFER_PTS(buffer) = static_cast<GstClockTime>(playout);
    if (conceal_from >= 0) {
      gst_element_send_event(source, gst_event_new_gap(
          static_cast<GstClockTime>(conceal_from),
          static_cast<GstClockTime>(playout - conceal_from)));
    }
    gst_app_src_push_buffer(GST_APP_SRC(source), buffer);
    last_playout = playout;
    const std::int64_t latency = (options.mode == "arrival" ? now : playout) - frame.sent_ns;
    latency_sum += latency;
    latency_max = std::max(latency_max, latency);
    ++pushed;
  }
  // Let the last frames play out, then report.
  const std::int64_t tail_ns = options.mode == "arrival" ? 500'000'000 :
    std::max<std::int64_t>(last_playout - running_time(pipeline), 0) + 500'000'000;
  std::this_thread::sleep_for(std::chrono::nanoseconds(tail_ns));
  GstElement * decoder = nullptr;
  GstIterator * it = gst_bin_iterate_elements(GST_BIN(pipeline));
  GValue item = G_VALUE_INIT;
  while (gst_iterator_next(it, &item) == GST_ITERATOR_OK) {
    GstElement * element = GST_ELEMENT(g_value_get_object(&item));
    if (std::strstr(GST_OBJECT_NAME(element), "opusdec") != nullptr) {
      decoder = GST_ELEMENT(gst_object_ref(element));
    }
    g_value_reset(&item);
  }
  g_value_unset(&item);
  gst_iterator_free(it);
  guint64 plc_samples = 0, gaps = 0;
  if (decoder != nullptr) {
    GstStructure * stats = nullptr;
    g_object_get(decoder, "stats", &stats, nullptr);
    if (stats != nullptr) {
      gst_structure_get_uint64(stats, "plc-num-samples", &plc_samples);
      gst_structure_get_uint64(stats, "num-gap", &gaps);
      gst_structure_free(stats);
    }
    gst_object_unref(decoder);
  }
  gst_app_src_end_of_stream(GST_APP_SRC(source));
  gst_element_set_state(pipeline, GST_STATE_NULL);
  gst_object_unref(source);
  gst_object_unref(pipeline);

  std::int64_t max_late = 0;
  for (std::size_t i = 0; i < frames.size(); ++i) {
    max_late = std::max(max_late, frames[i].arrival_ns - frames[i].sent_ns);
  }
  std::printf(
    "mode=%s jitter=%s loss=%.0f%% plc=%s fec=%s frames=%zu pushed=%d lost=%d dropped_late=%d "
    "skipped=%d concealed=%d reanchored=%d decoder_gaps=%lu decoder_plc_ms=%lu "
    "delay_now=%ldms delay_max=%ldms worst_transit=%ldms latency_avg=%ldms latency_max=%ldms\n",
    options.mode.c_str(), options.jitter.c_str(), options.loss * 100.0,
    options.plc ? "on" : "off", options.fec ? "on" : "off", frames.size(), pushed, lost,
    dropped, skipped, concealed, reanchored, static_cast<unsigned long>(gaps),
    static_cast<unsigned long>(plc_samples / 48), static_cast<long>(last_delay / 1'000'000),
    static_cast<long>(max_delay / 1'000'000), static_cast<long>(max_late / 1'000'000),
    static_cast<long>(pushed ? latency_sum / pushed / 1'000'000 : 0),
    static_cast<long>(latency_max / 1'000'000));
  return 0;
}
