"""Exercise the laptop entry point without ROS, hardware, or network I/O."""

import io
import json
import sys
from types import SimpleNamespace
from uuid import UUID
import wave

import pytest

from malbut_stt.smoke import main
from malbut_stt.transcription import LocalWhisperTranscriber
from malbut_stt.wake import LocalWakeRecognizer


@pytest.fixture
def runtime(monkeypatch):
    """Replace SDK boundaries while keeping the real pipeline and WAV adapter."""
    state = SimpleNamespace(
        frames=[[1] * 512] + [[0] * 512] * 32, recordings=None,
        text='  거실로 가줘.\n', failure=None, interrupt=None,
        events=[], requests=[], recorder_options=None, wake_options=None,
        wake_texts=['제이크야'], wake_requests=[],
        model_loads=[], local_requests=[],
        sample_rate=16000, fail_at=None, reads=0,
    )

    def fail(phase):
        if state.fail_at == phase:
            raise RuntimeError('PRIVATE-HARDWARE-DETAIL')

    class Recorder:
        def __init__(self, **kwargs):
            state.events.append('recorder')
            fail('opening_microphone')
            self.sample_rate = state.sample_rate
            state.recorder_options = kwargs
            self.frames = iter(state.recordings.pop(0) if state.recordings is not None
                               else state.frames)

        @staticmethod
        def get_available_devices():
            state.events.append('devices')
            return ['Laptop microphone', 'USB microphone']

        def start(self):
            state.events.append('start')
            fail('starting_microphone')

        def read(self):
            state.reads += 1
            fail('reading_microphone')
            if state.interrupt == 'read':
                raise KeyboardInterrupt
            samples = next(self.frames)
            if isinstance(samples, BaseException):
                raise samples
            return samples

        def stop(self):
            state.events.append('stop')

        def delete(self):
            state.events.append('delete')

    def create_transcription(**kwargs):
        assert state.events[-2:] == ['stop', 'delete']
        state.events.append('transcribe')
        state.requests.append(kwargs)
        if state.failure is not None:
            raise state.failure
        return SimpleNamespace(text=state.text)

    def create_client(**kwargs):
        assert kwargs['timeout'] == 30.0 and kwargs['max_retries'] == 0
        state.events.append('client')
        return SimpleNamespace(
            audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create_transcription)),
            close=lambda: state.events.append('client_close'),
        )

    def create_wake(path, *, compute_type='int8'):
        state.events.append('wake')
        state.wake_options = path
        state.wake_compute_type = compute_type

        def transcribe(pcm, sample_rate):
            assert state.events[-2:] == ['stop', 'delete']
            assert sample_rate == 16000
            state.events.append('recognize_wake')
            state.wake_requests.append(pcm)
            fail('recognizing_wake')
            return state.wake_texts.pop(0)

        return SimpleNamespace(transcribe=transcribe)

    # Local mode keeps the real wake wrapper and sentence transcriber; only the
    # inference backend is replaced, proving that the command hint stays empty.
    create_wake.from_transcriber = LocalWakeRecognizer.from_transcriber

    def create_local_model(path, **kwargs):
        state.events.append('local_model')
        state.model_loads.append((path, kwargs))

        def transcribe(audio, **options):
            assert state.events[-2:] == ['stop', 'delete']
            state.local_requests.append((audio, options))
            is_wake = options['initial_prompt'] is not None
            state.events.append('recognize_wake' if is_wake else 'transcribe_local')
            text = state.wake_texts.pop(0) if is_wake else state.text
            return iter([SimpleNamespace(text=text)]), None

        return SimpleNamespace(transcribe=transcribe)

    def enter():
        state.events.append('enter')
        if state.interrupt == 'input':
            raise KeyboardInterrupt
        return ''

    monkeypatch.setitem(sys.modules, 'rclpy', None)
    monkeypatch.setitem(sys.modules, 'pvrecorder', SimpleNamespace(PvRecorder=Recorder))
    monkeypatch.setitem(sys.modules, 'pvporcupine', None)
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(
        WhisperModel=create_local_model))
    monkeypatch.setattr('malbut_stt.smoke.LocalWakeRecognizer', create_wake)
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=create_client))
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(
        Vad=lambda mode: SimpleNamespace(is_speech=lambda pcm, rate: pcm[:2] != b'\x00\x00'),
    ))
    monkeypatch.setattr('builtins.input', enter)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-openai')
    monkeypatch.delenv('PICOVOICE_ACCESS_KEY', raising=False)
    return state


def events(capsys):
    """Read the CLI's JSON stream separately from its interactive prompt."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_manual_once_preserves_text_and_closes_capture_before_api(runtime, monkeypatch, capsys):
    """A ROS-free run uses real capture/WAV logic and emits one final result."""
    assert main(['--manual', '--once', '--device-index', '1']) == 0
    output = events(capsys)
    assert [item['event'] for item in output] == [
        'ready', 'listening', 'transcribing', 'transcript',
    ]
    assert output[0]['backend'] == 'openai'
    assert output[-1]['text'] == runtime.text
    assert str(UUID(output[-1]['utterance_id'])) == output[-1]['utterance_id']
    assert output[-1]['transcription_s'] >= 0
    assert runtime.recorder_options == {'frame_length': 512, 'device_index': 1}
    assert runtime.events == [
        'client', 'enter', 'recorder', 'start', 'stop', 'delete', 'transcribe', 'client_close',
    ]
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request['model'] == 'gpt-transcribe'
    assert request['extra_body'] == {'languages': ['ko']}
    with wave.open(io.BytesIO(request['file'][1]), 'rb') as recording:
        assert (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) == (
            1, 2, 16000,
        )
        pcm = recording.readframes(recording.getnframes())
    assert pcm.startswith(b'\x01\x00' * 512)
    assert b'\x07\x00' not in pcm


def test_manual_once_silence_never_calls_api(runtime, capsys):
    """One attempt with no speech ends locally after the capture timeout."""
    runtime.frames = [[0] * 512] * 157
    assert main(['--manual', '--once']) == 0
    assert events(capsys)[-1]['event'] == 'no_speech'
    assert runtime.requests == []
    assert runtime.events[-3:] == ['stop', 'delete', 'client_close']


def test_vad_diagnostic_starts_at_last_positive_frame(runtime, monkeypatch, capsys):
    """Measure from VAD processing, preserving the separate API duration."""
    runtime.frames = (
        [[1] * 320] + [[0] * 320] * 3
        + [[1] * 320] + [[0] * 320] * 50
    )
    clock = iter([10.0, 11.0, 12.0, 14.0])
    monkeypatch.setattr('malbut_stt.smoke.monotonic', lambda: next(clock))
    assert main(['--manual', '--once']) == 0
    transcript = events(capsys)[-1]
    assert transcript['event'] == 'transcript'
    assert transcript['transcription_s'] == 2.0
    assert transcript['vad_last_speech_to_text_s'] == 3.0


@pytest.mark.parametrize('previous_outcome', ['result', 'failure', 'wake'])
def test_new_listening_resets_vad_diagnostic_after_result_failure_or_wake(
    runtime, monkeypatch, capsys, previous_outcome,
):
    """A later callback without VAD evidence cannot reuse an earlier timestamp."""
    clock = iter([10.0] + ([] if previous_outcome == 'wake' else [12.0])
                 + ([14.0] if previous_outcome == 'result' else []) + [20.0, 22.0])
    monkeypatch.setattr('malbut_stt.smoke.monotonic', lambda: next(clock))

    def fake_pipeline(**callbacks):
        def run():
            callbacks['report']('waiting_for_wake' if previous_outcome == 'wake' else 'listening')
            assert callbacks['is_speech'](b'\x01\x00' * 320, 16000)
            if previous_outcome == 'wake':
                callbacks['report']('wake_detected')
            else:
                callbacks['report']('transcribing')
                if previous_outcome == 'failure':
                    callbacks['report']('transcription_failed:RuntimeError')
                else:
                    callbacks['publish']('previous', '이전 발화')
            callbacks['report']('listening')
            assert not callbacks['is_speech'](bytes(640), 16000)
            callbacks['report']('transcribing')
            callbacks['publish']('current', 'VAD 시각 없는 결과')

        return SimpleNamespace(run=run)

    monkeypatch.setattr('malbut_stt.smoke.SpeechPipeline', fake_pipeline)
    assert main(['--manual']) == (1 if previous_outcome == 'failure' else 0)
    transcripts = [item for item in events(capsys) if item['event'] == 'transcript']
    assert transcripts[-1]['utterance_id'] == 'current'
    assert transcripts[-1]['transcription_s'] == 2.0
    assert transcripts[-1]['vad_last_speech_to_text_s'] is None
    if previous_outcome == 'result':
        assert transcripts[0]['vad_last_speech_to_text_s'] == 4.0
    assert runtime.requests == []


def test_api_error_once_exits_without_retry_or_exception_body(runtime, capsys):
    """A failed API request cannot leak its body or start another capture."""
    runtime.failure = RuntimeError('PRIVATE-REQUEST-BODY')
    assert main(['--manual', '--once']) == 1
    output = capsys.readouterr()
    assert 'transcription_failed:RuntimeError' in output.out
    assert 'PRIVATE-REQUEST-BODY' not in output.out + output.err
    assert '"event": "transcript"' not in output.out
    assert len(runtime.requests) == runtime.events.count('recorder') == 1
    assert runtime.events[-1] == 'client_close'


@pytest.mark.parametrize('phase', ['input', 'read'])
def test_ctrl_c_releases_open_resources(runtime, phase):
    """Interrupting the prompt or microphone releases everything already opened."""
    runtime.interrupt = phase
    assert main(['--manual', '--once']) == 0
    assert runtime.requests == []
    expected = ['client', 'enter']
    if phase == 'read':
        expected += ['recorder', 'start', 'stop', 'delete']
    assert runtime.events == expected + ['client_close']


def model_args(tmp_path, wake_only=False):
    """Use a directory placeholder consumed only by the fake recognizer."""
    model = tmp_path / 'wake-model'
    model.mkdir(exist_ok=True)
    (model / 'tokenizer.json').write_text('{}')
    args = ['--model-path', str(model)]
    return args + ['--wake-only'] if wake_only else args


def test_manual_local_once_transcribes_without_api_key_or_sdk(
    runtime, monkeypatch, tmp_path, capsys,
):
    """The full command uses the local model and needs no OpenAI dependency."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    monkeypatch.setitem(sys.modules, 'malbut_stt.mlx_transcription', None)
    assert main(model_args(tmp_path) + [
        '--manual', '--local', '--once', '--device-index', '1',
    ]) == 0
    output = events(capsys)
    assert [item['event'] for item in output] == [
        'ready', 'listening', 'transcribing', 'transcript',
    ]
    assert output[0] == {
        'event': 'ready', 'mode': 'manual', 'model': 'small',
        'backend': 'local', 'device_index': 1, 'local_compute_type': 'int8',
        'local_backend': 'faster-whisper',
    }
    assert output[-1]['text'] == runtime.text.strip()
    assert output[-1]['transcription_s'] >= 0
    assert runtime.wake_options is None
    assert runtime.events == [
        'local_model', 'enter', 'recorder', 'start', 'stop', 'delete', 'transcribe_local',
    ]
    assert runtime.model_loads == [(str(tmp_path / 'wake-model'), {
        'device': 'cpu', 'compute_type': 'int8', 'cpu_threads': 6, 'local_files_only': True,
    })]
    audio, options = runtime.local_requests[0]
    assert list(audio[:512]) == [1 / 32768.0] * 512
    assert options == {
        'language': 'ko', 'beam_size': 1, 'condition_on_previous_text': False,
        'initial_prompt': None,
    }
    assert runtime.requests == []


def test_wake_local_once_reuses_model_for_a_separate_full_command(
    runtime, monkeypatch, tmp_path, capsys,
):
    """One local model handles the wake phrase and then a fresh command capture."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    runtime.recordings = [
        [[2] * 512] + [[0] * 512] * 13,
        [[1] * 512] + [[0] * 512] * 32,
    ]
    assert main(model_args(tmp_path) + ['--local', '--once']) == 0
    output = events(capsys)
    assert [item['event'] for item in output] == [
        'ready', 'waiting_for_wake', 'recognizing_wake', 'wake_detected',
        'listening', 'transcribing', 'transcript',
    ]
    assert output[0]['mode'] == 'wake'
    assert output[0]['model'] == 'small'
    assert output[0]['backend'] == 'local'
    assert output[-1]['text'] == runtime.text.strip()
    assert runtime.events == [
        'local_model', 'recorder', 'start', 'stop', 'delete', 'recognize_wake',
        'recorder', 'start', 'stop', 'delete', 'transcribe_local',
    ]
    assert len(runtime.model_loads) == 1
    assert len(runtime.local_requests) == 2
    wake_audio, wake_options = runtime.local_requests[0]
    command_audio, command_options = runtime.local_requests[1]
    assert list(wake_audio[:512]) == [2 / 32768.0] * 512
    assert list(command_audio[:512]) == [1 / 32768.0] * 512
    assert 2 / 32768.0 not in command_audio
    assert wake_options['initial_prompt'] == '로봇 이름은 제이크입니다.'
    assert command_options['initial_prompt'] is None
    assert runtime.requests == []


@pytest.mark.parametrize('outcome', ['no_speech', 'too_long'])
def test_manual_local_discarded_audio_never_runs_inference(
    runtime, monkeypatch, tmp_path, capsys, outcome,
):
    """Silence and an unbounded utterance are discarded before local inference."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    runtime.frames = [[0] * 512] * 157 if outcome == 'no_speech' else [[1] * 512] * 626
    assert main(model_args(tmp_path) + ['--manual', '--local', '--once']) == 0
    assert events(capsys)[-1]['event'] == outcome
    assert runtime.wake_requests == runtime.requests == []
    assert runtime.local_requests == []
    assert runtime.events == ['local_model', 'enter', 'recorder', 'start', 'stop', 'delete']


def test_manual_local_missing_tokenizer_cannot_load_runtime_or_open_microphone(
    runtime, monkeypatch, tmp_path, capsys,
):
    """The notebook command uses the same local-asset validation as the ROS transcriber."""
    args = model_args(tmp_path)
    (tmp_path / 'wake-model' / 'tokenizer.json').unlink()
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    assert main(args + ['--manual', '--local', '--once']) == 1
    assert events(capsys) == [{
        'event': 'stopped', 'phase': 'loading_local_model', 'error': 'ValueError',
    }]
    assert runtime.events == []
    assert runtime.model_loads == runtime.local_requests == runtime.requests == []


@pytest.mark.parametrize('manual', [False, True])
@pytest.mark.parametrize('missing', ['argument', 'directory'])
def test_local_requires_model_directory_before_opening_runtime(
    runtime, monkeypatch, tmp_path, manual, missing,
):
    """Both local modes require an existing model directory before any SDK starts."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    args = ['--local', '--once'] + (['--manual'] if manual else [])
    if missing == 'directory':
        args += ['--model-path', str(tmp_path / 'missing-model')]
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert runtime.events == []


def test_api_manual_still_rejects_model_path(runtime, tmp_path):
    """A model directory must not silently change the existing API manual mode."""
    with pytest.raises(SystemExit) as error:
        main(model_args(tmp_path) + ['--manual', '--once'])
    assert error.value.code == 2
    assert runtime.events == []


def test_api_only_manual_rejects_compute_type_before_loading_sdks(runtime):
    with pytest.raises(SystemExit) as error:
        main(['--manual', '--once', '--compute-type', 'float32'])
    assert error.value.code == 2
    assert runtime.events == []


@pytest.mark.parametrize('mode', ['manual', 'wake', 'wake_only'])
def test_float32_compute_type_reaches_local_model_in_each_smoke_mode(
    runtime, monkeypatch, tmp_path, capsys, mode,
):
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    args = model_args(tmp_path) + ['--compute-type', 'float32', '--once']
    if mode == 'wake_only':
        args.append('--wake-only')
    else:
        args.append('--local')
        if mode == 'manual':
            args.append('--manual')
        else:
            runtime.recordings = [
                [[2] * 512] + [[0] * 512] * 13,
                [[1] * 512] + [[0] * 512] * 32,
            ]
    assert main(args) == 0
    assert events(capsys)[0]['local_compute_type'] == 'float32'
    assert runtime.requests == []
    if mode == 'wake_only':
        assert runtime.wake_compute_type == 'float32'
        assert runtime.model_loads == []
    else:
        assert len(runtime.model_loads) == 1
        assert runtime.model_loads[0][1]['compute_type'] == 'float32'
        assert runtime.model_loads[0][1]['local_files_only'] is True


@pytest.mark.parametrize('mode', ['manual', 'wake', 'wake_only'])
def test_mlx_local_modes_share_one_adapter_without_ct2_or_api(
    runtime, monkeypatch, tmp_path, capsys, mode,
):
    class MlxTranscriber(LocalWhisperTranscriber):
        def __init__(self, path):
            runtime.model_loads.append((path, {'backend': 'mlx'}))

            def transcribe(audio, **options):
                assert runtime.events[-2:] == ['stop', 'delete']
                runtime.local_requests.append((audio, options))
                is_wake = options['initial_prompt'] is not None
                text = runtime.wake_texts.pop(0) if is_wake else runtime.text
                return iter([SimpleNamespace(text=text)]), None

            self.model = SimpleNamespace(transcribe=transcribe)

    monkeypatch.setitem(sys.modules, 'malbut_stt.mlx_transcription', SimpleNamespace(
        MlxWhisperTranscriber=MlxTranscriber))
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    monkeypatch.setitem(sys.modules, 'openai', None)
    monkeypatch.delenv('OPENAI_API_KEY')
    args = model_args(tmp_path) + ['--backend', 'mlx', '--once']
    if mode == 'wake_only':
        args.append('--wake-only')
    else:
        args.append('--local')
        if mode == 'manual':
            args.append('--manual')
        else:
            runtime.recordings = [
                [[2] * 512] + [[0] * 512] * 13,
                [[1] * 512] + [[0] * 512] * 32,
            ]
    assert main(args) == 0
    ready = events(capsys)[0]
    assert ready['backend'] == 'local' and ready['local_backend'] == 'mlx'
    assert ready['local_compute_type'] == 'float16'
    assert len(runtime.model_loads) == 1 and runtime.requests == []
    prompts = [options['initial_prompt'] for _, options in runtime.local_requests]
    assert prompts == ({'manual': [None], 'wake_only': ['로봇 이름은 제이크입니다.'],
                        'wake': ['로봇 이름은 제이크입니다.', None]}[mode])


@pytest.mark.parametrize('args', [
    ['--backend', 'mlx', '--manual'],
    ['--backend', 'mlx'],
    ['--backend', 'mlx', '--local', '--compute-type', 'float32'],
    ['--backend', 'mlx', '--wake-only', '--compute-type', 'int8'],
    ['--backend', 'faster-whisper', '--manual'],
])
def test_backend_misuse_is_rejected_before_hardware_and_sdk_loading(runtime, args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2 and runtime.events == []


@pytest.fixture
def dialogue_runtime(runtime, monkeypatch):
    runtime.dialogue_active = False
    runtime.dialogue_closed = False
    runtime.dialogue_failure = None
    runtime.dialogue_waits = []

    class Dialogue:
        def __init__(self, **kwargs):
            runtime.dialogue_options = kwargs
            self.phase = 'idle'
            self.recorder = None
            self.started = False
            self.polls = 0
            self.session = SimpleNamespace(activate=lambda: setattr(
                runtime, 'dialogue_active', True))

        def start(self):
            self.phase = 'opening_microphone'
            self.recorder = runtime.dialogue_options['recorder_factory']()
            self.recorder.start()
            self.started = True
            if runtime.dialogue_failure == 'start':
                raise RuntimeError('PRIVATE-START-DETAIL')
            runtime.dialogue_options['report']('waiting_for_wake')

        def poll(self):
            self.phase = 'running'
            self.polls += 1
            if runtime.dialogue_failure == 'poll':
                raise RuntimeError('PRIVATE-POLL-DETAIL')
            if self.polls > 2:
                raise KeyboardInterrupt
            runtime.dialogue_options['report']('checking_endpoint:silence_s=1.50')
            runtime.dialogue_options['publish_transcript'](f'u{self.polls}', f'문장 {self.polls}')

        def close(self):
            runtime.dialogue_closed = True
            if self.recorder is not None:
                if self.started:
                    self.recorder.stop()
                self.recorder.delete()

    monkeypatch.setitem(sys.modules, 'malbut_stt.dialogue_pipeline', SimpleNamespace(
        DialoguePipeline=Dialogue))
    monkeypatch.setattr('threading.Event', lambda: SimpleNamespace(
        wait=lambda seconds: runtime.dialogue_waits.append(seconds)))
    monkeypatch.setitem(sys.modules, 'openai', None)
    monkeypatch.delenv('OPENAI_API_KEY')
    return runtime


@pytest.mark.parametrize('manual, backend', [
    (False, 'faster-whisper'), (True, 'faster-whisper'), (False, 'mlx'), (True, 'mlx'),
])
def test_dialogue_uses_one_capture_and_continues_until_ctrl_c_without_api(
    dialogue_runtime, monkeypatch, tmp_path, capsys, manual, backend,
):
    runtime = dialogue_runtime
    if backend == 'mlx':
        class MlxTranscriber:
            def __init__(self, path):
                runtime.model_loads.append((path, {'backend': 'mlx'}))
                self.model = object()

        monkeypatch.setitem(sys.modules, 'malbut_stt.mlx_transcription', SimpleNamespace(
            MlxWhisperTranscriber=MlxTranscriber))
        monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    args = model_args(tmp_path) + ['--local', '--dialogue', '--backend', backend]
    assert main(args + (['--manual'] if manual else [])) == 0
    output = events(capsys)
    ready = output[0]
    assert ready['mode'] == 'dialogue' and ready['local_backend'] == backend
    assert ready['wake_required'] is not manual
    assert ready['output'] == 'terminal_only'
    assert ready['tts_completion_events'] is ready['tts_5s_timeout_testable'] is False
    assert runtime.dialogue_active is manual and runtime.dialogue_closed
    assert runtime.events.count('enter') == int(manual)
    assert runtime.events.count('recorder') == runtime.events.count('start') == 1
    assert runtime.events[-2:] == ['stop', 'delete']
    assert runtime.dialogue_waits == [0.01, 0.01]
    assert output[1]['event'] == ('listening' if manual else 'waiting_for_wake')
    assert [row for row in output if row['event'] == 'transcript'] == [
        {'event': 'transcript', 'utterance_id': 'u1', 'text': '문장 1'},
        {'event': 'transcript', 'utterance_id': 'u2', 'text': '문장 2'},
    ]
    options = runtime.dialogue_options
    assert options['wake'].model is options['transcriber'].model
    assert len(runtime.model_loads) == 1 and runtime.requests == []


@pytest.mark.parametrize('mode_args', [[], ['--local', '--once'], ['--local', '--wake-only']])
def test_dialogue_rejects_api_or_incompatible_attempt_modes(runtime, mode_args):
    with pytest.raises(SystemExit) as error:
        main(['--dialogue'] + mode_args)
    assert error.value.code == 2 and runtime.events == []


@pytest.mark.parametrize('phase', ['start', 'poll'])
def test_dialogue_failure_always_closes_capture_and_hides_private_error(
    dialogue_runtime, tmp_path, capsys, phase,
):
    dialogue_runtime.dialogue_failure = phase
    assert main(model_args(tmp_path) + ['--local', '--dialogue']) == 1
    assert dialogue_runtime.dialogue_closed
    assert dialogue_runtime.events[-2:] == ['stop', 'delete']
    output = events(capsys)
    assert output[-1]['event'] == 'stopped' and output[-1]['error'] == 'RuntimeError'
    assert 'PRIVATE' not in json.dumps(output)


def test_manual_dialogue_prompt_interrupt_closes_pipeline_before_opening_microphone(
    dialogue_runtime, tmp_path,
):
    dialogue_runtime.interrupt = 'input'
    assert main(model_args(tmp_path) + ['--local', '--dialogue', '--manual']) == 0
    assert dialogue_runtime.dialogue_closed
    assert dialogue_runtime.events == ['local_model', 'enter']


@pytest.mark.parametrize('missing', ['OPENAI_API_KEY', 'model'])
def test_missing_configuration_never_initializes_hardware(runtime, monkeypatch, tmp_path, missing):
    """Reject missing API credentials or a local model before SDK boundaries."""
    args = model_args(tmp_path)
    if missing == 'model':
        (tmp_path / 'wake-model' / 'tokenizer.json').unlink()
        (tmp_path / 'wake-model').rmdir()
    else:
        monkeypatch.delenv(missing)
    with pytest.raises(SystemExit) as error:
        main(args + ['--once'])
    assert error.value.code == 2
    assert runtime.events == []


def test_list_devices_needs_no_keys_models_or_recognition_sdks(runtime, monkeypatch, capsys):
    """Device discovery does not initialize either recognition path."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    monkeypatch.setitem(sys.modules, 'webrtcvad', None)
    assert main(['--list-devices']) == 0
    assert events(capsys) == [
        {'event': 'device', 'index': 0, 'name': 'Laptop microphone'},
        {'event': 'device', 'index': 1, 'name': 'USB microphone'},
    ]
    assert runtime.events == ['devices']


def test_wake_once_sends_only_a_fresh_command_capture_to_api(runtime, tmp_path, capsys):
    """Local wake PCM is closed and discarded before the command microphone opens."""
    args = model_args(tmp_path)
    runtime.recordings = [
        [[2] * 512] + [[0] * 512] * 13,
        [[1] * 512] + [[0] * 512] * 32,
    ]
    assert main(args + ['--once']) == 0
    output = events(capsys)
    assert [item['event'] for item in output] == [
        'ready', 'waiting_for_wake', 'recognizing_wake', 'wake_detected',
        'listening', 'transcribing', 'transcript',
    ]
    assert output[0]['mode'] == 'wake'
    assert runtime.wake_options == str(tmp_path / 'wake-model')
    assert runtime.events == [
        'wake', 'client', 'recorder', 'start', 'stop', 'delete', 'recognize_wake',
        'recorder', 'start', 'stop', 'delete', 'transcribe', 'client_close',
    ]
    assert runtime.wake_requests[0].startswith(b'\x02\x00' * 512)
    with wave.open(io.BytesIO(runtime.requests[0]['file'][1]), 'rb') as audio:
        pcm = audio.readframes(audio.getnframes())
    assert pcm.startswith(b'\x01\x00' * 512)
    assert b'\x02\x00' not in pcm


def test_command_once_keeps_listening_after_rejected_wake(runtime, tmp_path, capsys):
    """The command attempt begins only after a complete valid wake phrase."""
    runtime.wake_texts = ['제이크야 거실로 가', '제이크야']
    runtime.recordings = [list(runtime.frames) for _ in range(3)]
    assert main(model_args(tmp_path) + ['--once']) == 0
    output = [item['event'] for item in events(capsys)]
    assert output.count('not_wake') == output.count('wake_detected') == 1
    assert output.count('waiting_for_wake') == 2
    assert output.count('listening') == output.count('transcript') == 1
    assert len(runtime.wake_requests) == 2 and len(runtime.requests) == 1
    assert runtime.events.count('recorder') == 3


@pytest.mark.parametrize('outcome', ['wake_detected', 'not_wake', 'wake_no_speech', 'wake_too_long'])
def test_wake_only_once_needs_no_openai_and_stops_after_first_attempt(
    runtime, monkeypatch, tmp_path, capsys, outcome,
):
    """Wake-only attempts never create an API client or a command recorder."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    if outcome == 'not_wake':
        runtime.wake_texts = ['제이크야 거실로 가']
    elif outcome == 'wake_no_speech':
        runtime.frames = [[0] * 512] * 157
    elif outcome == 'wake_too_long':
        runtime.frames = [[1] * 512] * 189
    assert main(model_args(tmp_path, wake_only=True) + ['--once', '--device-index', '1']) == 0
    output = events(capsys)
    assert output[0]['mode'] == 'wake_only'
    assert output[0]['backend'] == 'local'
    assert output[-1]['event'] == outcome
    assert 'listening' not in [item['event'] for item in output]
    assert runtime.recorder_options == {'frame_length': 512, 'device_index': 1}
    assert runtime.events.count('recorder') == 1
    assert runtime.events.count('stop') == runtime.events.count('delete') == 1
    assert 'client' not in runtime.events and runtime.requests == []
    assert len(runtime.wake_requests) == (1 if outcome in ('wake_detected', 'not_wake') else 0)


def test_wake_only_reopens_capture_after_detection_until_ctrl_c(runtime, monkeypatch, tmp_path, capsys):
    """Continuous local detection returns to wake capture without a command path."""
    monkeypatch.delenv('OPENAI_API_KEY')
    monkeypatch.setitem(sys.modules, 'openai', None)
    runtime.wake_texts = ['제이크야', '제이크야']
    runtime.recordings = [list(runtime.frames), list(runtime.frames), [KeyboardInterrupt()]]
    assert main(model_args(tmp_path, wake_only=True)) == 0
    output = [item['event'] for item in events(capsys)]
    assert output.count('wake_detected') == 2
    assert output.count('waiting_for_wake') == 3
    assert 'listening' not in output
    assert len(runtime.wake_requests) == 2 and runtime.requests == []
    assert runtime.events.count('recorder') == runtime.events.count('delete') == 3


@pytest.mark.parametrize('failure', [
    'sample_rate', 'opening_microphone', 'starting_microphone',
    'reading_microphone', 'recognizing_wake',
])
def test_wake_only_failure_closes_resources_without_details(runtime, tmp_path, capsys, failure):
    """A hardware or local inference failure stops without exposing its body."""
    if failure == 'sample_rate':
        runtime.sample_rate = 48000
    else:
        runtime.fail_at = failure
    assert main(model_args(tmp_path, wake_only=True) + ['--once']) == 1
    output = capsys.readouterr()
    assert 'PRIVATE-HARDWARE-DETAIL' not in output.out + output.err
    assert json.loads(output.out.splitlines()[-1]) == {
        'event': 'stopped',
        'phase': 'opening_microphone' if failure == 'sample_rate' else failure,
        'error': 'ValueError' if failure == 'sample_rate' else 'RuntimeError',
    }
    expected = ['wake', 'recorder']
    if failure not in ('sample_rate', 'opening_microphone'):
        expected.append('start')
    if failure in ('reading_microphone', 'recognizing_wake'):
        expected.append('stop')
    if failure != 'opening_microphone':
        expected.append('delete')
    if failure == 'recognizing_wake':
        expected.append('recognize_wake')
    assert runtime.events == expected
    assert runtime.requests == []


@pytest.mark.parametrize('invalid', ['manual', 'model'])
def test_wake_only_invalid_configuration_never_opens_hardware(runtime, tmp_path, invalid):
    """Reject mutually exclusive modes or an absent local model up front."""
    args = model_args(tmp_path, wake_only=True)
    if invalid == 'manual':
        args.append('--manual')
    else:
        (tmp_path / 'wake-model' / 'tokenizer.json').unlink()
        (tmp_path / 'wake-model').rmdir()
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert runtime.events == []


@pytest.mark.parametrize('option', ['--keyword-path', '--language-model-path'])
def test_legacy_wake_options_are_not_accepted(runtime, option):
    """Removed vendor-specific options fail before any device or API opens."""
    with pytest.raises(SystemExit) as error:
        main(['--manual', option, 'unused'])
    assert error.value.code == 2
    assert runtime.events == []
