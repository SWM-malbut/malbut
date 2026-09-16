// ABI 2: initial_prompt belongs to one synchronous call; native history is reset.
#include "whisper.h"

extern "C" {

int mb_whisper_abi_version() { return 2; }

void * mb_whisper_create(const char * model_path, int use_gpu) {
    whisper_context_params params = whisper_context_default_params();
    params.use_gpu = use_gpu != 0;
    params.flash_attn = true;
    return whisper_init_from_file_with_params(model_path, params);
}

void mb_whisper_free(void * context) {
    whisper_free(static_cast<whisper_context *>(context));
}

int mb_whisper_transcribe(void * context, const float * samples, int count,
                         int threads, const char * initial_prompt) {
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
    return whisper_full(static_cast<whisper_context *>(context), params, samples, count);
}

int mb_whisper_segment_count(void * context) {
    return whisper_full_n_segments(static_cast<whisper_context *>(context));
}

const char * mb_whisper_segment_text(void * context, int index) {
    return whisper_full_get_segment_text(static_cast<whisper_context *>(context), index);
}

int64_t mb_whisper_segment_start(void * context, int index) {
    return whisper_full_get_segment_t0(static_cast<whisper_context *>(context), index);
}

int64_t mb_whisper_segment_end(void * context, int index) {
    return whisper_full_get_segment_t1(static_cast<whisper_context *>(context), index);
}

int mb_whisper_model_ftype(void * context) {
    return whisper_model_ftype(static_cast<whisper_context *>(context));
}

const char * mb_whisper_model_type(void * context) {
    return whisper_model_type_readable(static_cast<whisper_context *>(context));
}

const char * mb_whisper_system_info() {
    return whisper_print_system_info();
}
}
