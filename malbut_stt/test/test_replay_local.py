"""Check offline replay without real models, microphones, speakers, ROS, or APIs."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from threading import Event, Thread
from types import SimpleNamespace
import wave

import pytest

from malbut_stt.cpp_transcription import CppWhisperTranscriber
from malbut_stt.transcription import LocalWhisperTranscriber


def fake_segment(text, end=0.1):
    return SimpleNamespace(text=text, start=0.0, end=end)


def write_wav(path, pcm, *, rate=16000, channels=1, width=2):
    with wave.open(str(path), 'wb') as output:
        output.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(pcm)
    return path


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'tools' / 'replay_local.py'
    spec = importlib.util.spec_from_file_location('replay_local_test_subject', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = SimpleNamespace(module=module, loads=[], requests=[])
    model_dir = tmp_path / 'model'
    model_dir.mkdir()
    (model_dir / 'tokenizer.json').write_text('{}')
    state.model_dir = model_dir

    class Model:
        def __init__(self, path, **options):
            state.loads.append((path, options))

        def transcribe(self, audio, **options):
            state.requests.append((audio, options))
            text = '  알려줘.  ' if len(state.requests) == 1 else '이동해줘.'
            return iter([fake_segment(text)]), None

    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(Vad=lambda _: SimpleNamespace(
        is_speech=lambda frame, rate: any(frame))))
    for forbidden in ('pvrecorder', 'rclpy', 'openai', 'pvporcupine'):
        monkeypatch.setitem(sys.modules, forbidden, None)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    return state


def test_real_pipeline_emits_full_wav_but_accepts_one_request_until_reply(runtime, tmp_path):
    """Continue observing the WAV while the pending reply blocks additional requests."""
    pcm = b'\x01\x00' * 1600 + bytes(2 * 28800) + b'\x02\x00' * 1600
    path = write_wav(tmp_path / 'continuation.wav', pcm)
    transcriber = LocalWhisperTranscriber(runtime.model_dir)
    original = transcriber.model
    result = runtime.module.replay_wav(path, transcriber, max_runtime_s=12.0)
    assert result['status'] == 'completed' and result['clean_shutdown']
    assert result['replay']['all_input_emitted']
    assert result['replay']['elapsed_s'] >= 6.0
    assert result['transcript_count'] == 1
    assert [item['text'] for item in result['transcripts']] == ['알려줘.']
    assert result['transcripts'][0]['at_s'] < result['source']['duration_s']
    assert all(item['vad_last_speech_to_text_s'] > 0.9 for item in result['transcripts'])
    assert result['source']['pcm_sha256'] == hashlib.sha256(pcm).hexdigest()
    assert result['model_transcribe_calls'] == 1
    assert len(result['inference']) == len(runtime.requests) == 1
    assert all(call['elapsed_s'] is not None for call in result['inference'])
    assert all(call['initial_prompt'] is None for call in result['inference'])
    assert any(item['event'].startswith('checking_endpoint:') for item in result['events'])
    assert transcriber.model is original
    assert len(runtime.loads) == 1

    # A second clip can reuse the exact loaded model. This short deadline tests
    # cleanup independently of whether the second clip produces speech.
    silence = write_wav(tmp_path / 'silence.wav', bytes(320))
    second_run = runtime.module.replay_wav(silence, transcriber, max_runtime_s=0.05)
    assert second_run['status'] == 'timed_out' and second_run['clean_shutdown']
    assert second_run['runtime']['model_reused']
    assert transcriber.model is original and len(runtime.loads) == 1


def test_busy_discarded_speech_cannot_shorten_an_earlier_transcripts_latency(runtime, tmp_path):
    """A second voice after the fallback endpoint is not part of the pending UID."""
    pcm = b'\x01\x00' * 1600 + bytes(2 * 52800) + b'\x02\x00' * 3200
    path = write_wav(tmp_path / 'busy-discarded.wav', pcm)
    transcriber = LocalWhisperTranscriber(runtime.model_dir)

    def slow_transcribe(*_args, **_kwargs):
        Event().wait(2.7)
        return iter([fake_segment('알려줘.')]), None

    transcriber.model.transcribe = slow_transcribe
    result = runtime.module.replay_wav(path, transcriber, max_runtime_s=12.0)
    assert result['status'] == 'completed' and result['clean_shutdown']
    assert result['transcript_count'] == result['model_transcribe_calls'] == 1
    assert any(item['event'] == 'speech_discarded:busy' for item in result['events'])
    transcript = result['transcripts'][0]
    assert 0 <= transcript['first_vad_speech_at_s'] <= transcript['last_vad_speech_at_s'] < 0.3
    inference = result['inference'][0]
    discarded = next(item for item in result['events'] if item['event'] == 'speech_discarded:busy')
    assert (transcript['last_vad_speech_at_s'] < inference['started_s']
            < discarded['at_s'] < inference['finished_s'] <= transcript['at_s'])
    assert transcript['vad_last_speech_to_text_s'] >= inference['elapsed_s'] >= 2.7
    assert transcript['vad_last_speech_to_text_s'] == pytest.approx(
        transcript['at_s'] - transcript['last_vad_speech_at_s'])


def test_slow_inference_after_eof_keeps_capture_alive(runtime, tmp_path):
    path = write_wav(tmp_path / 'slow-after-eof.wav', b'\x01\x00' * 1600)
    transcriber = LocalWhisperTranscriber(runtime.model_dir)

    def slow_transcribe(*_args, **_kwargs):
        Event().wait(9.0)
        return iter([fake_segment('알려줘.')]), None

    transcriber.model.transcribe = slow_transcribe
    result = runtime.module.replay_wav(path, transcriber, max_runtime_s=15.0)
    assert result['status'] == 'completed' and result['error'] is None
    assert result['clean_shutdown'] and result['replay']['all_input_emitted']
    assert [item['text'] for item in result['transcripts']] == ['알려줘.']
    assert result['inference'][0]['elapsed_s'] >= 9.0
    assert result['replay']['emitted_chunks'] * 0.032 > 9.0


def test_resumed_voice_keeps_first_onset_but_updates_its_own_last_voice(runtime, tmp_path):
    pcm = b'\x01\x00' * 1600 + bytes(2 * 27200) + b'\x02\x00' * 3200
    path = write_wav(tmp_path / 'resumed-before-finalization.wav', pcm)
    transcriber = LocalWhisperTranscriber(runtime.model_dir)
    calls = []

    def delayed_first_transcribe(*_args, **_kwargs):
        calls.append(None)
        if len(calls) == 1:
            Event().wait(2.7)
        text = '알려줘.' if len(calls) == 1 else '거실로 이동해줘.'
        return iter([fake_segment(text, end=0.1 if len(calls) == 1 else 2.0)]), None

    transcriber.model.transcribe = delayed_first_transcribe
    result = runtime.module.replay_wav(path, transcriber, max_runtime_s=12.0)
    assert result['status'] == 'completed' and result['clean_shutdown']
    assert result['transcript_count'] == 1 and result['model_transcribe_calls'] == 2
    transcript = result['transcripts'][0]
    assert transcript['text'] == '거실로 이동해줘.'
    assert 0 <= transcript['first_vad_speech_at_s'] < 0.3
    assert 1.8 <= transcript['last_vad_speech_at_s'] < 2.3
    assert transcript['vad_last_speech_to_text_s'] > 0.9


def test_required_wake_accepts_one_command_and_blocks_followup_without_reply(runtime, tmp_path):
    pcm = (b'\x01\x00' * 1600 + bytes(2 * 12800)
           + b'\x02\x00' * 1600 + bytes(2 * 32000) + b'\x03\x00' * 1600)
    path = write_wav(tmp_path / 'wake-and-two-commands.wav', pcm)
    transcriber = LocalWhisperTranscriber(runtime.model_dir)
    commands = iter(['날씨 알려줘.', '거실로 이동해줘.'])

    def transcribe(audio, **options):
        runtime.requests.append((audio, options))
        text = '제이크야' if options['initial_prompt'] is not None else next(commands)
        return iter([fake_segment(text)]), None

    transcriber.model.transcribe = transcribe
    result = runtime.module.replay_wav(path, transcriber, wake_required=True)
    assert result['status'] == 'completed' and result['clean_shutdown']
    assert result['replay']['wake_required'] and result['replay']['all_input_emitted']
    event_names = [item['event'] for item in result['events']]
    assert event_names.count('wake_detected') == 1
    assert 'replay_started:waiting_for_wake' in event_names
    assert result['model_transcribe_calls'] == 2 and result['transcript_count'] == 1
    assert [item['text'] for item in result['transcripts']] == ['날씨 알려줘.']
    first = result['transcripts'][0]
    assert 0.8 < first['first_vad_speech_at_s'] < 1.2
    assert 'waiting_for_reply' in event_names
    # PCM catches up after scheduler stalls; wall time need not span its silence.
    silences = [float(event.split('=', 1)[1]) for event in event_names
                if event.startswith('endpoint_finalized:silence_s=')]
    assert silences and min(silences) >= 1.0
    assert first['vad_last_speech_to_text_s'] >= 0.0
    assert first['vad_last_speech_to_text_s'] == pytest.approx(
        first['at_s'] - first['last_vad_speech_at_s'])
    assert [call['initial_prompt'] for call in result['inference']] == [
        '로봇 이름은 제이크입니다.', None,
    ]
    assert len(runtime.loads) == 1


@pytest.mark.parametrize('wake_required', [None, 0, 1, 'yes'])
def test_wake_requirement_must_be_a_boolean_before_opening_input(runtime, wake_required):
    with pytest.raises(ValueError, match='wake_required must be a boolean'):
        runtime.module.replay_wav('not-opened.wav', wake_required=wake_required)
    assert runtime.loads == []


def test_recorder_paces_exact_512_sample_frames_and_unblocks_on_stop(runtime, monkeypatch):
    now, waits = [0.0], []

    class FakeEvent:
        def __init__(self):
            self.set_flag = False

        def wait(self, timeout=None):
            if not self.set_flag and timeout is not None:
                waits.append(timeout)
                now[0] += timeout
            return self.set_flag

        def is_set(self):
            return self.set_flag

        def set(self):
            self.set_flag = True

    monkeypatch.setattr(runtime.module, 'Event', FakeEvent)
    monkeypatch.setattr(runtime.module, 'monotonic', lambda: now[0])
    recorder = runtime.module._WavRecorder(b'\x01\x00' * 513, tail_s=0.0)
    recorder.start()
    assert recorder.read() == [1] * 512
    assert recorder.read() == [1] + [0] * 511
    assert waits == [0.032, 0.032]
    assert recorder.finished_at == 0.064 and recorder.chunks == 2
    assert recorder.read() == [0] * 512
    assert waits == pytest.approx([0.032, 0.032, 0.032])
    assert recorder.finished_at == 0.064 and recorder.chunks == 3
    recorder.stop()
    assert recorder.read() == [0] * 512
    recorder.delete()
    assert recorder.deleted and recorder.pcm == b''


def test_model_measurement_includes_lazy_segment_iteration(runtime, monkeypatch):
    times = iter([10.0, 12.5])
    monkeypatch.setattr(runtime.module, 'monotonic', lambda: next(times))
    calls = []
    model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (
        iter([SimpleNamespace(text='앞 '), SimpleNamespace(text='뒤')]), None))
    probe = runtime.module._MeasuredModel(model, calls, 8.0)
    segments, _ = probe.transcribe([0.0] * 320, language='ko', initial_prompt=None)
    assert calls[0]['finished_s'] is None
    assert [segment.text for segment in segments] == ['앞 ', '뒤']
    assert calls[0]['text'] == '앞 뒤'
    assert calls[0]['started_s'] == 2.0 and calls[0]['finished_s'] == 4.5
    assert calls[0]['elapsed_s'] == 2.5


@pytest.mark.parametrize('lazy', [False, True])
def test_measurement_records_exception_type_without_private_error_text(runtime, lazy):
    def transcribe(*_args, **_kwargs):
        def fail_segments():
            raise RuntimeError('PRIVATE-NATIVE-DETAIL')
            yield
        if lazy:
            return fail_segments(), None
        raise RuntimeError('PRIVATE-NATIVE-DETAIL')

    calls = []
    probe = runtime.module._MeasuredModel(SimpleNamespace(transcribe=transcribe), calls, 0.0)
    with pytest.raises(RuntimeError):
        segments, _ = probe.transcribe([0.0] * 320)
        list(segments)
    assert calls[0]['error'] == 'RuntimeError'
    assert calls[0]['elapsed_s'] is not None
    assert 'PRIVATE' not in json.dumps(calls)


@pytest.mark.parametrize('format_options', [
    {'rate': 8000}, {'channels': 2}, {'width': 1},
])
def test_invalid_wav_format_is_rejected_before_loading_model(runtime, tmp_path, format_options):
    path = write_wav(tmp_path / 'invalid.wav', bytes(320), **format_options)
    with pytest.raises(ValueError, match='mono 16kHz PCM16'):
        runtime.module.replay_wav(path, model_path=runtime.model_dir)
    assert runtime.loads == []


@pytest.mark.parametrize('maximum', [0, -1, float('inf'), float('nan'), True])
def test_runtime_limit_must_be_finite_and_positive(runtime, maximum):
    with pytest.raises(ValueError, match='finite and positive'):
        runtime.module.replay_wav('not-opened.wav', model_path=runtime.model_dir,
                                  max_runtime_s=maximum)
    assert runtime.loads == []


def test_cli_writes_failure_metadata_without_opening_an_audio_device(runtime, tmp_path, capsys):
    output = tmp_path / 'results' / 'result.json'
    assert runtime.module.main([
        '--wav', str(tmp_path / 'absent.wav'), '--model-path', str(runtime.model_dir),
        '--output', str(output), '--max-runtime-s', '1',
    ]) == 1
    result = json.loads(output.read_text())
    assert result == {'schema_version': 1, 'status': 'failed', 'error': 'ValueError'}
    assert json.loads(capsys.readouterr().out)['output'] == str(output)
    assert runtime.loads == []


def test_timed_out_native_worker_prevents_reusing_its_model(runtime, tmp_path, monkeypatch):
    """Local timeout cannot claim a native call has been cancelled remotely."""
    class BusyPipeline:
        def __init__(self, **kwargs):
            self.recorder = kwargs['recorder_factory']()
            self.session = SimpleNamespace(activate=lambda: None)
            self.capture_thread = None
            self.asr_thread = SimpleNamespace(is_alive=lambda: True)

        def start(self):
            self.recorder.start()

        def poll(self):
            pass

        def close(self):
            self.recorder.stop()
            self.recorder.delete()

    monkeypatch.setattr(runtime.module, 'DialoguePipeline', BusyPipeline)
    path = write_wav(tmp_path / 'short.wav', bytes(320))
    transcriber = LocalWhisperTranscriber(runtime.model_dir)
    result = runtime.module.replay_wav(path, transcriber, max_runtime_s=0.01)
    assert result['status'] == 'timed_out' and not result['clean_shutdown']
    with pytest.raises(ValueError, match='unfinished replay'):
        runtime.module.replay_wav(path, transcriber)


def test_cpp_replay_timeout_cancels_and_restores_reusable_native_model(runtime, tmp_path):
    lifecycle = []
    native = SimpleNamespace(
        cancel=lambda: lifecycle.append('cancel'),
        close=lambda: lifecycle.append('close'),
    )
    transcriber = CppWhisperTranscriber.__new__(CppWhisperTranscriber)
    transcriber.model = native
    path = write_wav(tmp_path / 'cpp-silence.wav', bytes(320))
    try:
        for _ in range(2):
            result = runtime.module.replay_wav(path, transcriber, max_runtime_s=0.01)
            assert result['status'] == 'timed_out' and result['error'] is None
            assert result['clean_shutdown'] and transcriber.model is native
            assert result['runtime']['backend'] == 'whisper.cpp'
            assert result['model_transcribe_calls'] == 0
        assert lifecycle == ['cancel', 'cancel']
    finally:
        transcriber.model = native
        transcriber.close()
    assert lifecycle == ['cancel', 'cancel', 'close']


def test_measured_cpp_model_preserves_stream_and_lifecycle_ownership(runtime):
    lifecycle = []
    native = SimpleNamespace(
        cancel=lambda: lifecycle.append('cancel'),
        close=lambda: lifecycle.append('close'),
    )
    transcriber = CppWhisperTranscriber.__new__(CppWhisperTranscriber)
    transcriber.model = runtime.module._MeasuredModel(native, [], 0.0)
    assert transcriber.create_stream().model is transcriber.model
    transcriber.cancel()
    transcriber.close()
    assert lifecycle == ['cancel', 'close']


def test_owned_cpp_model_is_released_once_after_both_late_workers_exit(
    runtime, monkeypatch, tmp_path,
):
    lifecycle, workers = [], []
    capture_done, inference_done, closed = Event(), Event(), Event()

    class Native(CppWhisperTranscriber):
        def __init__(self, *_args):
            def close():
                assert all(not worker.is_alive() for worker in workers)
                lifecycle.append('close')
                closed.set()
            self.model = SimpleNamespace(
                cancel=lambda: lifecycle.append('cancel'), close=close,
            )

    class LatePipeline:
        def __init__(self, **kwargs):
            self.transcriber = kwargs['transcriber']
            self.session = SimpleNamespace(activate=lambda: None)
            self.capture_thread = Thread(target=capture_done.wait, daemon=True)
            self.asr_thread = Thread(target=inference_done.wait, daemon=True)
            workers.extend([self.capture_thread, self.asr_thread])

        def start(self):
            for worker in workers:
                worker.start()

        def poll(self):
            pass

        def close(self):
            self.transcriber.cancel()

    monkeypatch.setattr('malbut_stt.cpp_transcription.CppWhisperTranscriber', Native)
    monkeypatch.setattr(runtime.module, 'DialoguePipeline', LatePipeline)
    path = write_wav(tmp_path / 'late-native.wav', bytes(320))
    results = []
    replay = Thread(target=lambda: results.append(runtime.module.replay_wav(
        path, backend='whisper_cpp', model_path='small.bin', library_path='native.so',
        max_runtime_s=0.01,
    )), daemon=True)
    try:
        replay.start()
        replay.join(timeout=1.0)
        assert not replay.is_alive()
        result = results[0]
        assert result['status'] == 'timed_out' and not result['clean_shutdown']
        assert lifecycle == ['cancel'] and not closed.is_set()
        inference_done.set()
        workers[1].join(timeout=1.0)
        assert not closed.is_set()
        capture_done.set()
        workers[0].join(timeout=1.0)
        assert closed.wait(1.0)
        assert lifecycle == ['cancel', 'close']
    finally:
        capture_done.set()
        inference_done.set()
        for worker in workers:
            worker.join(timeout=1.0)
        replay.join(timeout=1.0)


def test_pipeline_initialization_failure_restores_reusable_model(runtime, tmp_path, monkeypatch):
    def fail_pipeline(**_kwargs):
        raise RuntimeError('pipeline startup failed')

    monkeypatch.setattr(runtime.module, 'DialoguePipeline', fail_pipeline)
    path = write_wav(tmp_path / 'short.wav', bytes(320))
    transcriber = LocalWhisperTranscriber(runtime.model_dir)
    original = transcriber.model
    with pytest.raises(RuntimeError):
        runtime.module.replay_wav(path, transcriber)
    assert transcriber.model is original


@pytest.mark.parametrize('backend_value, expected', [
    ('float32', 'float32'), (None, None), (42, None),
])
def test_reused_model_reports_observed_compute_type_or_unknown(runtime, tmp_path,
                                                               backend_value, expected):
    path = write_wav(tmp_path / 'silence.wav', bytes(320))
    transcriber = LocalWhisperTranscriber(runtime.model_dir)
    transcriber.model.model = SimpleNamespace(compute_type=backend_value)
    result = runtime.module.replay_wav(path, transcriber, max_runtime_s=0.01)
    assert result['runtime']['compute_type'] == expected
    assert result['runtime']['requested_compute_type'] is None
    assert result['runtime']['model_reused']


def test_reused_model_cannot_claim_a_new_compute_type(runtime):
    transcriber = LocalWhisperTranscriber(runtime.model_dir)
    with pytest.raises(ValueError, match='constructing the reused model'):
        runtime.module.replay_wav('not-opened.wav', transcriber, compute_type='float32')


def test_cli_compute_type_reaches_local_model_without_api_or_microphone(runtime, tmp_path):
    path = write_wav(tmp_path / 'silence.wav', bytes(320))
    output = tmp_path / 'float32.json'
    assert runtime.module.main([
        '--wav', str(path), '--model-path', str(runtime.model_dir), '--output', str(output),
        '--compute-type', 'float32', '--max-runtime-s', '0.01',
    ]) == 1  # The observation deadline intentionally expires before the silence tail.
    result = json.loads(output.read_text())
    assert result['status'] == 'timed_out' and result['clean_shutdown']
    assert result['runtime']['requested_compute_type'] == 'float32'
    assert result['runtime']['compute_type'] is None  # The fake backend exposes no metadata.
    assert runtime.loads[0][1]['compute_type'] == 'float32'
    assert runtime.loads[0][1]['local_files_only'] is True


def test_mlx_replay_measures_shared_adapter_and_reports_its_backend(
    runtime, monkeypatch, tmp_path,
):
    class MlxTranscriber(LocalWhisperTranscriber):
        backend = 'mlx'

        def __init__(self, path):
            runtime.loads.append((str(path), {'backend': 'mlx'}))

            def transcribe(audio, **options):
                runtime.requests.append((audio, options))
                return iter([fake_segment('알려줘.')]), None

            self.model = SimpleNamespace(transcribe=transcribe, compute_type='float16',
                                         beam_search=False, decoding='greedy with fallback')

    monkeypatch.setitem(sys.modules, 'malbut_stt.mlx_transcription', SimpleNamespace(
        MlxWhisperTranscriber=MlxTranscriber))
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    path = write_wav(tmp_path / 'mlx-voice.wav', b'\x01\x00' * 1600)
    result = runtime.module.replay_wav(path, model_path=runtime.model_dir, backend='mlx')
    assert result['status'] == 'completed' and result['clean_shutdown']
    assert result['runtime']['backend'] == result['runtime']['requested_backend'] == 'mlx'
    assert result['runtime']['compute_type'] == 'float16'
    assert result['runtime']['requested_compute_type'] == 'float16'
    assert result['model_transcribe_calls'] == result['transcript_count'] == 1
    assert result['inference'][0]['initial_prompt'] is None
    assert result['inference'][0]['beam_size'] is None
    assert result['inference'][0]['decoding'] == 'greedy with fallback'
    assert result['inference'][0]['elapsed_s'] is not None
    # PCM catches up after scheduler stalls; wall time need not span its silence.
    silences = [float(item['event'].split('=', 1)[1]) for item in result['events']
                if item['event'].startswith('endpoint_finalized:silence_s=')]
    assert silences and min(silences) >= 1.0
    transcript = result['transcripts'][0]
    assert transcript['vad_last_speech_to_text_s'] >= 0.0
    assert transcript['vad_last_speech_to_text_s'] == pytest.approx(
        transcript['at_s'] - transcript['last_vad_speech_at_s'])
    assert len(runtime.loads) == len(runtime.requests) == 1

    transcriber = MlxTranscriber(runtime.model_dir)
    reused = runtime.module.replay_wav(path, transcriber, max_runtime_s=0.01)
    assert reused['runtime']['backend'] == 'mlx'
    assert reused['runtime']['compute_type'] == 'float16'
    assert reused['runtime']['requested_backend'] is None
    assert reused['runtime']['requested_compute_type'] is None
    with pytest.raises(ValueError, match='constructing the reused model'):
        runtime.module.replay_wav(path, transcriber, backend='mlx')


def test_mlx_replay_cli_rejects_compute_override_before_loading_anything(runtime, tmp_path):
    output = tmp_path / 'not-created.json'
    with pytest.raises(SystemExit) as error:
        runtime.module.main([
            '--wav', 'not-opened.wav', '--model-path', str(runtime.model_dir),
            '--output', str(output), '--backend', 'mlx', '--compute-type', 'int8',
        ])
    assert error.value.code == 2
    assert runtime.loads == [] and not output.exists()


def test_cli_can_observe_eof_without_a_detected_wake(runtime, tmp_path):
    path = write_wav(tmp_path / 'no-wake.wav', bytes(320))
    output = tmp_path / 'wake-result.json'
    assert runtime.module.main([
        '--wav', str(path), '--model-path', str(runtime.model_dir),
        '--output', str(output), '--wake',
    ]) == 0
    result = json.loads(output.read_text())
    assert result['status'] == 'completed' and result['clean_shutdown']
    assert result['replay']['wake_required'] and result['replay']['all_input_emitted']
    assert result['transcript_count'] == result['model_transcribe_calls'] == 0
    assert not any(item['event'] == 'wake_detected' for item in result['events'])


def test_cpp_cli_uses_native_model_and_jetson_endpoint_then_releases_it(
    runtime, monkeypatch, tmp_path,
):
    lifecycle = []

    class Native(CppWhisperTranscriber):
        def __init__(self, model_path, library_path):
            runtime.loads.append((model_path, library_path))
            self.metadata = {'model_ftype': 7, 'requested_use_gpu': True}
            self.model = SimpleNamespace(
                cancel=lambda: lifecycle.append('cancel'),
                close=lambda: lifecycle.append('close'),
            )

    monkeypatch.setattr('malbut_stt.cpp_transcription.CppWhisperTranscriber', Native)
    path = write_wav(tmp_path / 'native-silence.wav', bytes(320))
    output = tmp_path / 'native.json'
    assert runtime.module.main([
        '--wav', str(path), '--model-path', 'small-q8.bin', '--library-path', 'native.so',
        '--backend', 'whisper_cpp', '--output', str(output), '--max-runtime-s', '0.01',
    ]) == 1
    result = json.loads(output.read_text())
    assert result['status'] == 'timed_out' and result['clean_shutdown']
    assert runtime.loads == [('small-q8.bin', 'native.so')]
    assert lifecycle == ['cancel', 'close']
    assert result['runtime']['native']['model_ftype'] == 7
    assert result['runtime']['requested_compute_type'] is None
    assert result['runtime']['backend'] == 'whisper.cpp'
    assert result['replay']['endpoint_predecode_s'] == 0.8


@pytest.mark.parametrize('options', [
    {'backend': 'whisper_cpp'},
    {'backend': 'whisper_cpp', 'library_path': 'native.so', 'compute_type': 'int8'},
    {'backend': 'mlx', 'library_path': 'native.so'},
    {'library_path': 'native.so'},
])
def test_native_backend_options_are_validated_before_input(runtime, options):
    with pytest.raises(ValueError, match='library_path|compute_type'):
        runtime.module.replay_wav('not-opened.wav', **options)
    assert runtime.loads == []
