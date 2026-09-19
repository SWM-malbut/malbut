// ABI 3: cooperative cancellation and a per-call monotonic decode deadline.
#include "whisper.h"
#include <atomic>
#include <chrono>
#include <cmath>
#include <new>

namespace {
struct Bridge {
    whisper_context * context;
    std::atomic<bool> cancelled{false};
};

struct Decode {
    Bridge * bridge;
    std::chrono::steady_clock::time_point started;
    double timeout_s;

    int status() const {
        if (bridge->cancelled.load()) return -100;
        if (std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count()
                >= timeout_s) return -101;
        return 0;
    }
};

bool abort_decode(void * data) {
    return static_cast<Decode *>(data)->status() != 0;
}

whisper_context * unwrap(void * context) {
    return static_cast<Bridge *>(context)->context;
}
}

extern "C" {

int mb_whisper_abi_version() { return 3; }

void * mb_whisper_create(const char * model_path, int use_gpu) {
    whisper_context_params params = whisper_context_default_params();
    params.use_gpu = use_gpu != 0;
    params.flash_attn = true;
    whisper_context * context = whisper_init_from_file_with_params(model_path, params);
    if (!context) return nullptr;
    Bridge * bridge = new (std::nothrow) Bridge{context};
    if (!bridge) whisper_free(context);
    return bridge;
}

void mb_whisper_free(void * context) {
    whisper_free(unwrap(context));
    delete static_cast<Bridge *>(context);
}

void mb_whisper_reset_cancel(void * context) {
    static_cast<Bridge *>(context)->cancelled.store(false);
}

void mb_whisper_cancel(void * context) {
    static_cast<Bridge *>(context)->cancelled.store(true);
}

int mb_whisper_transcribe(void * context, const float * samples, int count,
                         int threads, const char * initial_prompt, double timeout_s) {
    if (!std::isfinite(timeout_s) || timeout_s <= 0) return -101;
    Decode decode{static_cast<Bridge *>(context), std::chrono::steady_clock::now(), timeout_s};
    if (const int status = decode.status()) return status;
    whisper_full_params params = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    params.n_threads = threads;
    params.language = "ko";
    params.detect_language = false;
    params.translate = false;
    params.no_context = true;
    params.initial_prompt = initial_prompt;
    params.carry_initial_prompt = false;
    params.greedy.best_of = 1;
    params.beam_search.beam_size = 1;
    params.temperature = 0.0f;
    params.temperature_inc = 0.0f;
    params.print_realtime = false;
    params.print_progress = false;
    params.print_timestamps = false;
    params.print_special = false;
    params.token_timestamps = false;
    params.vad = false;
    params.abort_callback = abort_decode;
    params.abort_callback_user_data = &decode;
    params.encoder_begin_callback = [](whisper_context *, whisper_state *, void * data) {
        return !abort_decode(data);
    };
    params.encoder_begin_callback_user_data = &decode;
    const int result = whisper_full(unwrap(context), params, samples, count);
    // whisper.cpp may return success after an encoder callback abort. Never
    // expose a partial or stale transcript after cancellation or timeout.
    if (const int status = decode.status()) return status;
    return result;
}

int mb_whisper_segment_count(void * context) {
    return whisper_full_n_segments(unwrap(context));
}

const char * mb_whisper_segment_text(void * context, int index) {
    return whisper_full_get_segment_text(unwrap(context), index);
}

int64_t mb_whisper_segment_start(void * context, int index) {
    return whisper_full_get_segment_t0(unwrap(context), index);
}

int64_t mb_whisper_segment_end(void * context, int index) {
    return whisper_full_get_segment_t1(unwrap(context), index);
}

int mb_whisper_model_ftype(void * context) {
    return whisper_model_ftype(unwrap(context));
}

const char * mb_whisper_model_type(void * context) {
    return whisper_model_type_readable(unwrap(context));
}

const char * mb_whisper_system_info() {
    return whisper_print_system_info();
}
}
