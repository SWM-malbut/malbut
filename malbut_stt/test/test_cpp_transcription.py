"""Check the native adapter ABI and lifetime without hardware or model loading."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_stt.cpp_transcription import CppWhisperTranscriber
from malbut_stt.incremental import IncrementalWhisperStream
from malbut_stt.wake import LocalWakeRecognizer


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *arguments):
        return self.callback(*arguments)


@pytest.fixture
def native(monkeypatch, tmp_path):
    model, bridge = tmp_path / 'small.bin', tmp_path / 'bridge.dylib'
    model.write_bytes(b'model')
    bridge.write_bytes(b'library')
    state = SimpleNamespace(
        model=model, bridge=bridge, loaded=[], freed=[], calls=[], abi=3,
        context=123, result=0, segments=[('  제이크야.', 10, 125)],
        entered=None, release=None, cancelled=Event(), timeouts=[],
    )

    def create(path, gpu):
        state.loaded.append((path, gpu))
        return state.context

    def decode(context, samples, count, threads, prompt, timeout_s):
        state.calls.append((context, np.ctypeslib.as_array(samples, shape=(count,)).copy(),
                            threads, prompt))
        state.timeouts.append(timeout_s)
        if state.entered is not None:
            state.entered.set()
            assert state.release.wait(2), 'test did not release fake inference'
        if state.cancelled.is_set():
            return -100
        return state.result

    functions = {
        'mb_whisper_abi_version': lambda: state.abi,
        'mb_whisper_create': create,
        'mb_whisper_free': lambda context: state.freed.append(context),
        'mb_whisper_reset_cancel': lambda context: state.cancelled.clear(),
        'mb_whisper_cancel': lambda context: state.cancelled.set(),
        'mb_whisper_transcribe': decode,
        'mb_whisper_segment_count': lambda context: len(state.segments),
        'mb_whisper_segment_text': lambda context, index: state.segments[index][0].encode(),
        'mb_whisper_segment_start': lambda context, index: state.segments[index][1],
        'mb_whisper_segment_end': lambda context, index: state.segments[index][2],
        'mb_whisper_model_ftype': lambda context: 1,
        'mb_whisper_model_type': lambda context: b'small',
        'mb_whisper_system_info': lambda: b'MTL : EMBED_LIBRARY = 1',
    }
    state.library = SimpleNamespace(**{name: Function(fn) for name, fn in functions.items()})
    monkeypatch.setattr('malbut_stt.cpp_transcription.ctypes.CDLL', lambda path: state.library)
    return state


def engine(native, **options):
    return CppWhisperTranscriber(native.model, native.bridge, **options)


def test_one_local_context_preserves_samples_text_and_native_segments(native):
    transcriber = engine(native, use_gpu=False)
    pcm = np.array([-32768, -1, 0, 1, 32767], dtype='<i2').tobytes()
    assert transcriber.transcribe(pcm, 16000) == '제이크야.'
    assert native.loaded == [(str(native.model).encode(), False)]
    context, audio, threads, prompt = native.calls[0]
    assert context == 123 and threads == 6 and prompt is None
    np.testing.assert_array_equal(audio, [-1, -1/32768, 0, 1/32768, 32767/32768])
    assert audio.dtype == np.float32
    segment = transcriber.model.last_segments[0]
    assert (segment.start, segment.end, segment.text) == (0.1, 1.25, '  제이크야.')
    assert transcriber.metadata['bridge_abi'] == 3
    assert transcriber.metadata['decode_timeout_s'] == 30.0
    assert native.timeouts == [30.0]
    transcriber.close()
    transcriber.close()
    assert native.freed == [123]


def test_shared_wake_prompt_is_sent_once_and_does_not_leak_into_commands(native):
    transcriber = engine(native)
    wake = LocalWakeRecognizer.from_transcriber(transcriber)
    assert wake.model is transcriber.model
    pcm = b'\x01\x00' * 320
    assert wake.transcribe(pcm, 16000) == '제이크야.'
    native.segments = [(' 날씨를 알려줘.', 0, 1)]
    assert transcriber.transcribe(pcm, 16000) == '날씨를 알려줘.'
    stream = transcriber.create_stream()
    assert type(stream) is IncrementalWhisperStream
    assert stream.model is transcriber.model
    assert stream.transcribe(pcm, 16000, speech_end_s=.02) == '날씨를 알려줘.'
    assert [call[-1] for call in native.calls] == [
        '로봇 이름은 제이크입니다.'.encode(), None, None,
    ]
    assert len(native.loaded) == 1
    transcriber.close()


def test_returned_segment_iterator_survives_next_decode_and_close(native):
    transcriber = engine(native)
    arguments = dict(language='ko', beam_size=1, condition_on_previous_text=False,
                     initial_prompt=None)
    result, _ = transcriber.model.transcribe(np.ones(320, dtype=np.float32), **arguments)
    native.segments = [(' 다른 결과', 0, 1)]
    transcriber.model.transcribe(np.ones(320, dtype=np.float32), **arguments)
    transcriber.close()
    assert [segment.text for segment in result] == ['  제이크야.']


@pytest.mark.parametrize('pcm, rate', [(b'\x01', 16000), (bytes(640), 8000)])
def test_invalid_pcm_does_not_reach_native_decoder(native, pcm, rate):
    with engine(native) as transcriber:
        with pytest.raises(ValueError):
            transcriber.transcribe(pcm, rate)
    assert native.calls == []


def test_exact_silence_is_empty_without_native_inference(native):
    with engine(native) as transcriber:
        assert transcriber.transcribe(bytes(32000), 16000) == ''
    assert native.calls == []


def test_native_failure_does_not_expose_stale_segments(native):
    with engine(native) as transcriber:
        assert transcriber.transcribe(b'\x01\x00' * 320, 16000) == '제이크야.'
        native.result = 7
        with pytest.raises(RuntimeError, match='code 7'):
            transcriber.transcribe(b'\x01\x00' * 320, 16000)
        assert transcriber.model.last_segments == []


def test_close_signals_cancel_before_waiting_and_never_frees_active_context(native):
    native.entered, native.release = Event(), Event()
    transcriber = engine(native)
    close_started = Event()

    def close():
        close_started.set()
        transcriber.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        decode = executor.submit(transcriber.transcribe, b'\x01\x00' * 320, 16000)
        try:
            assert native.entered.wait(1)
            closing = executor.submit(close)
            assert close_started.wait(1)
            assert native.cancelled.wait(1)
            assert not closing.done() and native.freed == []
        finally:
            native.release.set()
        with pytest.raises(RuntimeError, match='cancelled'):
            decode.result(timeout=1)
        closing.result(timeout=1)
    assert native.freed == [123]
    with pytest.raises(RuntimeError, match='closed'):
        transcriber.transcribe(b'\x01\x00' * 320, 16000)


@pytest.mark.parametrize('missing', ['model', 'bridge'])
def test_missing_local_files_fail_before_loading(native, missing):
    getattr(native, missing).unlink()
    with pytest.raises(ValueError, match='existing local'):
        engine(native)
    assert native.loaded == []


@pytest.mark.parametrize('threads', [True, 0, -1, 1.5])
def test_invalid_thread_count_fails_before_model_loading(native, threads):
    with pytest.raises(ValueError, match='positive integer'):
        engine(native, n_threads=threads)
    assert native.loaded == []


@pytest.mark.parametrize('old_abi', [1, 2, None])
def test_incompatible_bridge_cannot_receive_the_new_decode_abi(native, old_abi):
    if old_abi is not None:
        native.abi = old_abi
    else:
        del native.library.mb_whisper_abi_version
    with pytest.raises(ValueError, match='ABI 3'):
        engine(native)
    assert native.loaded == []


def test_failed_model_load_does_not_construct_a_live_context(native):
    native.context = None
    with pytest.raises(RuntimeError, match='failed to load'):
        engine(native)
    assert native.freed == []


def test_metadata_failure_releases_the_already_loaded_context(native):
    native.library.mb_whisper_model_type.callback = lambda context: None
    with pytest.raises(AttributeError):
        engine(native)
    assert native.freed == [123]


@pytest.mark.parametrize('timeout', [True, 0, -1, float('inf'), float('nan'), '30'])
def test_invalid_deadline_fails_before_model_loading(native, timeout):
    with pytest.raises(ValueError, match='finite and positive'):
        engine(native, decode_timeout_s=timeout)
    assert native.loaded == []


def test_timeout_discards_partial_result_and_next_decode_reuses_context(native):
    with engine(native, decode_timeout_s=0.5) as transcriber:
        native.result = -101
        with pytest.raises(TimeoutError, match='deadline exceeded'):
            transcriber.transcribe(b'\x01\x00' * 320, 16000)
        assert transcriber.model.last_segments == []
        native.result = 0
        assert transcriber.transcribe(b'\x01\x00' * 320, 16000) == '제이크야.'
    assert native.timeouts == [0.5, 0.5]
    assert len(native.loaded) == 1


def test_cancel_does_not_wait_for_decode_lock_and_is_reset_for_next_call(native):
    native.entered, native.release = Event(), Event()
    with engine(native) as transcriber, ThreadPoolExecutor(max_workers=2) as executor:
        decode = executor.submit(transcriber.transcribe, b'\x01\x00' * 320, 16000)
        try:
            assert native.entered.wait(1)
            executor.submit(transcriber.cancel).result(timeout=1)
            assert native.cancelled.is_set() and native.freed == []
        finally:
            native.release.set()
        with pytest.raises(RuntimeError, match='cancelled'):
            decode.result(timeout=1)
        assert transcriber.model.last_segments == []
        assert transcriber.transcribe(b'\x01\x00' * 320, 16000) == '제이크야.'


def test_cancel_in_gap_before_native_entry_is_not_lost(native):
    with engine(native) as transcriber:
        decode = native.library.mb_whisper_transcribe.callback

        def cancel_before_decode(*args):
            transcriber.cancel()
            return decode(*args)

        native.library.mb_whisper_transcribe.callback = cancel_before_decode
        with pytest.raises(RuntimeError, match='cancelled'):
            transcriber.transcribe(b'\x01\x00' * 320, 16000)
        assert transcriber.model.last_segments == []
