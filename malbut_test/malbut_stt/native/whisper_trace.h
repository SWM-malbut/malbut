// Private observation API; does not change upstream Whisper structures or decisions.
#pragma once
#include "whisper.h"

struct mb_whisper_window {
    double no_speech_prob, avg_logprobs, no_speech_threshold, logprob_threshold;
    double start_s, end_s, next_seek_s;
    int skipped_no_speech, single_timestamp_ending;
    int segment_start, segment_count, token_count;
};

using mb_whisper_observer = void (*)(const mb_whisper_window *, void *);
extern "C" {
WHISPER_API void mb_whisper_trace_set(mb_whisper_observer observer, void * data);
WHISPER_API void mb_whisper_trace_emit(const mb_whisper_window * window);
}
