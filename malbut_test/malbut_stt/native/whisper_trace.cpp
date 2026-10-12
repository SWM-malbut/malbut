#include "whisper_trace.h"

namespace {
// whisper_full runs its window loop on the caller's thread. Concurrent bridge
// contexts must never observe each other's scores or change global log handlers.
thread_local mb_whisper_observer current_observer = nullptr;
thread_local void * current_data = nullptr;
}

extern "C" void mb_whisper_trace_set(mb_whisper_observer observer, void * data) {
    current_observer = observer;
    current_data = data;
}

extern "C" void mb_whisper_trace_emit(const mb_whisper_window * window) {
    if (current_observer) current_observer(window, current_data);
}
