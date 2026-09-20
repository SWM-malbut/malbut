"""Exercise the compiled bridge with a controllable, model-free Whisper backend."""

from concurrent.futures import ThreadPoolExecutor
import ctypes
from pathlib import Path
import shutil
import subprocess
from time import monotonic, sleep

import pytest

from malbut_stt.cpp_transcription import CppWhisperTranscriber


# Only the Whisper declarations used by the bridge are needed. The production
# bridge is compiled unchanged, so these tests exercise its native callbacks,
# atomic cancellation, return-code normalization, and Python lifetime handling.
HEADER = r'''
#include <atomic>
#include <chrono>
#include <cstdint>
#include <thread>
struct whisper_context {};
struct whisper_state {};
struct whisper_context_params { bool use_gpu, flash_attn; };
enum whisper_sampling_strategy { WHISPER_SAMPLING_GREEDY };
struct whisper_full_params {
    int n_threads;
    const char *language, *initial_prompt;
    bool detect_language, translate, no_context, carry_initial_prompt;
    struct { int best_of; } greedy;
    struct { int beam_size; } beam_search;
    float temperature, temperature_inc;
    bool print_realtime, print_progress, print_timestamps, print_special;
    bool token_timestamps, vad;
    bool (*abort_callback)(void *);
    void *abort_callback_user_data;
    bool (*encoder_begin_callback)(whisper_context *, whisper_state *, void *);
    void *encoder_begin_callback_user_data;
};
static std::atomic<int> mode{0}, entered{0}, freed{0};
extern "C" void test_set_mode(int value) { mode = value; entered = 0; }
extern "C" int test_entered() { return entered; }
extern "C" int test_freed() { return freed; }
inline whisper_context_params whisper_context_default_params() { return {}; }
inline whisper_context *whisper_init_from_file_with_params(
        const char *, whisper_context_params) { return new whisper_context; }
inline void whisper_free(whisper_context *ctx) { delete ctx; ++freed; }
inline whisper_full_params whisper_full_default_params(whisper_sampling_strategy) {
    return {};
}
inline int whisper_full(whisper_context *ctx, whisper_full_params p, const float *, int) {
    entered = 1;
    if (mode == 2) {
        // Model a backend step that only returns after its callback deadline.
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        return 0;
    }
    const auto started = std::chrono::steady_clock::now();
    while (mode == 1) {
        if (p.abort_callback(p.abort_callback_user_data)) return 0;
        if (!p.encoder_begin_callback(ctx, nullptr, p.encoder_begin_callback_user_data)) return 0;
        if (std::chrono::steady_clock::now() - started > std::chrono::seconds(1)) return 47;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return 0;
}
inline int whisper_full_n_segments(whisper_context *) { return 1; }
inline const char *whisper_full_get_segment_text(whisper_context *, int) { return "result"; }
inline int64_t whisper_full_get_segment_t0(whisper_context *, int) { return 0; }
inline int64_t whisper_full_get_segment_t1(whisper_context *, int) { return 100; }
inline int whisper_model_ftype(whisper_context *) { return 1; }
inline const char *whisper_model_type_readable(whisper_context *) { return "test"; }
inline const char *whisper_print_system_info() { return "test backend"; }
'''


@pytest.fixture(scope='module')
def bridge(tmp_path_factory):
    compiler = shutil.which('c++')
    if compiler is None:
        pytest.skip('native bridge regression requires a C++ compiler')
    directory = tmp_path_factory.mktemp('native-bridge')
    (directory / 'whisper.h').write_text(HEADER)
    model = directory / 'model.bin'
    model.write_bytes(b'fake model')
    library = directory / 'libbridge.so'
    source = Path(__file__).resolve().parents[1] / 'native/whisper_cpp_bridge.cpp'
    subprocess.run([compiler, '-std=c++17', '-shared', '-fPIC', '-pthread',
                    '-I', str(directory), str(source), '-o', str(library)],
                   check=True, capture_output=True, text=True, timeout=30)
    control = ctypes.CDLL(str(library))
    control.test_set_mode.argtypes = [ctypes.c_int]
    control.test_set_mode.restype = None
    control.test_entered.restype = control.test_freed.restype = ctypes.c_int
    return model, library, control


def await_native_entry(control):
    deadline = monotonic() + 1
    while not control.test_entered() and monotonic() < deadline:
        sleep(.001)
    assert control.test_entered(), 'native test decode did not start'


@pytest.mark.parametrize('mode', [1, 2])
def test_native_deadline_discards_even_successful_partial_and_recovers(bridge, mode):
    model, library, control = bridge
    control.test_set_mode(mode)
    with CppWhisperTranscriber(model, library, decode_timeout_s=.01) as transcriber:
        with pytest.raises(TimeoutError, match='deadline exceeded'):
            transcriber.transcribe(b'\x01\x00' * 320, 16000)
        assert transcriber.model.last_segments == []
        control.test_set_mode(0)
        assert transcriber.transcribe(b'\x01\x00' * 320, 16000) == 'result'


def test_native_cancel_releases_decode_and_close_without_early_free(bridge):
    model, library, control = bridge
    control.test_set_mode(1)
    initial_freed = control.test_freed()
    with CppWhisperTranscriber(model, library) as transcriber:
        with ThreadPoolExecutor(max_workers=2) as executor:
            decode = executor.submit(transcriber.transcribe, b'\x01\x00' * 320, 16000)
            await_native_entry(control)
            executor.submit(transcriber.cancel).result(timeout=1)
            with pytest.raises(RuntimeError, match='cancelled'):
                decode.result(timeout=1)
            assert control.test_freed() == initial_freed
            assert transcriber.model.last_segments == []
            control.test_set_mode(0)
            assert transcriber.transcribe(b'\x01\x00' * 320, 16000) == 'result'
            control.test_set_mode(1)
            decode = executor.submit(transcriber.transcribe, b'\x01\x00' * 320, 16000)
            await_native_entry(control)
            executor.submit(transcriber.close).result(timeout=1)
            with pytest.raises(RuntimeError, match='cancelled'):
                decode.result(timeout=1)
    assert control.test_freed() == initial_freed + 1
