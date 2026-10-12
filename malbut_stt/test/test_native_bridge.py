"""Exercise the compiled bridge with a controllable, model-free Whisper backend."""

from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
from pathlib import Path
import shutil
import subprocess
from time import monotonic, sleep

import pytest
import numpy as np

from malbut_stt.cpp_transcription import CppWhisperTranscriber
from malbut_stt.incident import IncidentRecorder


# Only the Whisper declarations used by the bridge are needed. The production
# bridge is compiled unchanged, so these tests exercise its native callbacks,
# atomic cancellation, return-code normalization, and Python lifetime handling.
HEADER = r'''
#pragma once
#define WHISPER_API
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
    bool token_timestamps, vad, suppress_nst;
    bool (*abort_callback)(void *);
    void *abort_callback_user_data;
    bool (*encoder_begin_callback)(whisper_context *, whisper_state *, void *);
    void *encoder_begin_callback_user_data;
};
inline std::atomic<int> mode{0}, entered{0}, freed{0};
inline whisper_context_params whisper_context_default_params() { return {}; }
inline whisper_context *whisper_init_from_file_with_params(
        const char *, whisper_context_params) { return new whisper_context; }
inline void whisper_free(whisper_context *ctx) { delete ctx; ++freed; }
inline whisper_full_params whisper_full_default_params(whisper_sampling_strategy) {
    return {};
}
int whisper_full(whisper_context *, whisper_full_params, const float *, int);
inline int whisper_full_n_segments(whisper_context *) { return mode == 3 ? 0 : 1; }
inline const char *whisper_full_get_segment_text(whisper_context *, int) { return "result"; }
inline int64_t whisper_full_get_segment_t0(whisper_context *, int) { return 0; }
inline int64_t whisper_full_get_segment_t1(whisper_context *, int) { return 100; }
inline int whisper_model_ftype(whisper_context *) { return 1; }
inline const char *whisper_model_type_readable(whisper_context *) { return "test"; }
inline const char *whisper_print_system_info() { return "test backend"; }
'''

BACKEND = r'''
#include "whisper_trace.h"
#include <limits>
extern "C" void test_set_mode(int value) { mode = value; entered = 0; }
extern "C" int test_entered() { return entered; }
extern "C" int test_freed() { return freed; }
int whisper_full(whisper_context *ctx, whisper_full_params p, const float * samples, int) {
    if (!p.suppress_nst) return 48;
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
    const int repeats = mode == 5 ? 80 : 1;
    for (int i = 0; i < repeats; ++i) {
        mb_whisper_window window{.95, mode == 3 ? -1.2 : -.2, .6, -1.,
                                 0., 30., 30., mode == 3, 1, 0, mode == 3 ? 0 : 1, 5};
        if (mode == 6) window.avg_logprobs = -std::numeric_limits<double>::infinity();
        if (mode == 7) {
            window.no_speech_prob = samples[0];
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        mb_whisper_trace_emit(&window);
    }
    return 0;
}
'''


@pytest.fixture(scope='module')
def bridge(tmp_path_factory):
    compiler = shutil.which('c++')
    if compiler is None:
        pytest.skip('native bridge regression requires a C++ compiler')
    directory = tmp_path_factory.mktemp('native-bridge')
    (directory / 'whisper.h').write_text(HEADER)
    (directory / 'backend.cpp').write_text(BACKEND)
    model = directory / 'model.bin'
    model.write_bytes(b'fake model')
    library = directory / 'libbridge.so'
    source = Path(__file__).resolve().parents[1] / 'native/whisper_cpp_bridge.cpp'
    subprocess.run([compiler, '-std=c++17', '-shared', '-fPIC', '-pthread',
                    '-I', str(directory), '-I', str(source.parent), str(source),
                    str(source.parent / 'whisper_trace.cpp'), str(directory / 'backend.cpp'),
                    '-o', str(library)],
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


DECODE_OPTIONS = dict(language='ko', beam_size=1, condition_on_previous_text=False,
                      initial_prompt=None)


@pytest.mark.parametrize('mode,skipped', [(0, False), (3, True)])
def test_native_decisions_reach_incident_and_transaction_even_without_text(bridge, tmp_path, mode, skipped):
    model, library, control = bridge
    control.test_set_mode(mode)
    recorder = IncidentRecorder(tmp_path, save_audio=False, post_seconds=0)
    try:
        with CppWhisperTranscriber(model, library) as transcriber:
            with recorder.context(kind='command', utterance_id='u1'):
                segments, info = recorder.wrap_model(transcriber.model).transcribe(
                    np.full(320, .25, dtype=np.float32), **DECODE_OPTIONS)
                assert len(list(segments)) == (0 if skipped else 1)
            assert transcriber.metadata['bridge_abi'] == 4
            trace = info.native_decode
            assert trace['status'] == 'observed' and trace['return_code'] == 0
            window, = trace['windows']
            assert window['no_speech_prob'] == .95
            assert window['avg_logprobs'] == (-1.2 if skipped else -.2)
            assert window['skipped_no_speech'] is skipped
            assert window['single_timestamp_ending'] and window['next_seek_s'] == 30
            recorder.event('transcript_published', utterance_id='u1', text='구독과 좋아요!')
    finally:
        recorder.close()
    assert recorder.error is None
    path = recorder.last_incident
    assert not list(path.rglob('*.wav'))
    decoded, = (path / 'decodes').glob('*.json')
    record = json.loads(decoded.read_text())
    assert record['native_decode'] == trace
    transactions = json.loads((path / 'transactions.json').read_text())
    stage, = [s for s in transactions['transactions'][0]['stages'] if s['event'] == 'decode_result']
    assert stage['native_decode'] == trace


def test_trace_is_bounded_and_copied_before_later_decode_or_silence(bridge):
    model, library, control = bridge
    control.test_set_mode(5)
    with CppWhisperTranscriber(model, library) as transcriber:
        segments, info = transcriber.model.transcribe(np.ones(320), **DECODE_OPTIONS)
        assert len(info.native_decode['windows']) == 64
        assert info.native_decode['windows_truncated']
        control.test_set_mode(3)
        _, later = transcriber.model.transcribe(np.ones(320), **DECODE_OPTIONS)
        assert len(later.native_decode['windows']) == 1
        assert not later.native_decode['windows_truncated']
        _, silent = transcriber.model.transcribe(np.zeros(320), **DECODE_OPTIONS)
        assert silent.native_decode == dict(status='not_run', reason='exact_silence', windows=[])
    assert [segment.text for segment in segments] == ['result']
    assert len(info.native_decode['windows']) == 64
    assert not info.native_decode['windows'][0]['skipped_no_speech']


def test_parallel_native_contexts_do_not_mix_trace_windows(bridge):
    model, library, control = bridge
    control.test_set_mode(7)

    def decode(value):
        with CppWhisperTranscriber(model, library) as transcriber:
            _, info = transcriber.model.transcribe(np.full(320, value), **DECODE_OPTIONS)
            return info.native_decode['windows'][0]['no_speech_prob']

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(decode, [.25, .5])) == [.25, .5]


def test_nonfinite_native_score_is_serializable_without_inventing_a_value(bridge):
    model, library, control = bridge
    control.test_set_mode(6)
    with CppWhisperTranscriber(model, library) as transcriber:
        _, info = transcriber.model.transcribe(np.ones(320), **DECODE_OPTIONS)
    window, = info.native_decode['windows']
    assert window['avg_logprobs'] is None
    assert window['nonfinite_fields'] == {'avg_logprobs': '-inf'}
    json.dumps(info.native_decode, allow_nan=False)
