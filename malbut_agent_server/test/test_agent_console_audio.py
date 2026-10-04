"""Offline checks: no microphone, speaker, native model, or API calls."""

from threading import Event, Thread
from time import sleep as real_sleep
from types import SimpleNamespace

import pytest

import test_agent_console_support  # noqa: F401

pytest.importorskip('numpy')
pytest.importorskip('webrtcvad')

import console_audio as audio


@pytest.mark.parametrize('mode', ['text', 'silence', 'asr_error', 'capture_error', 'stalled'])
def test_listen_once_reuses_model_and_closes_each_microphone(tmp_path, monkeypatch, mode):
    now = [0.0]
    resources = []
    pipelines = []
    loaded = []
    freed = []
    original_pipeline = audio.DialoguePipeline

    def pipeline(**kwargs):
        value = original_pipeline(**kwargs)
        capture_gate = Event()
        capture = value._capture
        start_session = value.start_session
        close = value.close

        def capture_when_ready():
            capture_gate.wait()
            capture()

        def start_ready_session(session_id):
            started = start_session(session_id)
            if started:
                capture_gate.set()
                if mode not in ('capture_error', 'stalled'):
                    assert value.capture_ready.wait(3)
            return started

        def close_ready_pipeline():
            capture_gate.set()
            close()

        # Session activation drains pre-session frames and increments the
        # capture generation. Start this short fixture utterance only after
        # that boundary, before the fake clock advances its silence deadline.
        value._capture = capture_when_ready
        value.start_session = start_ready_session
        value.close = close_ready_pipeline
        pipelines.append(value)
        return value

    def pause(_seconds):
        now[0] += 0.5
        real_sleep(0.002)

    def transcribe(_pcm, rate):
        assert rate == 16000
        if mode == 'asr_error':
            raise RuntimeError('synthetic ASR failure')
        return '안녕하세요'

    transcriber = SimpleNamespace(transcribe=transcribe, cancel=lambda: None,
                                 close=lambda: freed.append(True))

    class Recorder:
        sample_rate = 16000

        def __init__(self, **_kwargs):
            resources.append(self)
            self.frames = 0
            self.stopped = False
            self.deleted = False
            self.release = Event()

        def start(self):
            pass

        def read(self):
            real_sleep(0.001)
            if mode == 'capture_error':
                raise OSError('synthetic microphone failure')
            if mode == 'stalled':
                self.release.wait(3)
            self.frames += 1
            return [int(mode != 'silence' and self.frames <= 8)] * 320

        def stop(self):
            self.stopped = True
            self.release.set()

        def delete(self):
            self.deleted = True

    monkeypatch.setattr(audio, 'monotonic', lambda: now[0])
    monkeypatch.setattr(audio, 'sleep', pause)
    monkeypatch.setattr(audio, 'SoundDeviceRecorder', Recorder)
    monkeypatch.setattr(audio, 'DialoguePipeline', pipeline)
    monkeypatch.setattr(audio, 'CppWhisperTranscriber',
                        lambda *a, **kw: (loaded.append(True), transcriber)[1])
    model = tmp_path / 'fake-model.bin'
    library = tmp_path / 'fake-library.so'
    model.touch()
    library.touch()
    console = audio.ConsoleAudio(None, model_path=model, library_path=library,
                                 emit=lambda _: None)
    console._vad = SimpleNamespace(is_speech=lambda pcm, _rate: pcm[:2] != b'\0\0')
    assert not loaded  # Text-only use must not load the native Metal model.
    try:
        if mode in ('asr_error', 'capture_error', 'stalled'):
            with pytest.raises((RuntimeError, OSError)):
                console.listen_once()
            assert resources[0].stopped and resources[0].deleted
            assert not pipelines[0].capture_thread.is_alive()
            assert not pipelines[0].asr_thread.is_alive()
            mode = 'text'
            assert console.listen_once() == '안녕하세요'
        else:
            expected = '안녕하세요' if mode == 'text' else None
            assert console.listen_once() == expected
            assert console.listen_once() == expected
    finally:
        console.close()
        console.close()
    assert len(loaded) == len(freed) == 1
    assert len(resources) == len(pipelines) == 2
    assert all(recorder.stopped and recorder.deleted for recorder in resources)
    assert all(not pipeline.capture_thread.is_alive() and not pipeline.asr_thread.is_alive()
               for pipeline in pipelines)


def test_speak_waits_for_device_drain_without_loading_stt(monkeypatch):
    entered = Event()
    drain = Event()
    closed = []
    result = []
    validated = []

    def player(*, on_state, cancel_event, device):
        assert device == 7

        def finish():
            entered.set()
            assert drain.wait(3)

        return SimpleNamespace(
            write=lambda *_: (validated == [True] or pytest.fail('unvalidated audio'),
                              on_state('playing')), finish=finish,
            stop=lambda: (cancel_event.set(), drain.set()),
            close=lambda: closed.append(True),
        )

    monkeypatch.setattr(audio, 'StreamingPlayer', player)
    monkeypatch.setattr(audio, 'CppWhisperTranscriber',
                        lambda *_a, **_kw: pytest.fail('TTS loaded STT'))
    console = audio.ConsoleAudio(
        SimpleNamespace(generate=lambda *_: iter([([0.0] * 320, 24000)])),
        output_device=7,
    )
    worker = Thread(target=lambda: result.append(console.speak(
        '안녕하세요', validate=lambda: validated.append(True))))
    worker.start()
    try:
        assert entered.wait(3)
        assert result == []
    finally:
        drain.set()
        worker.join(3)
        console.close()
    assert not worker.is_alive() and result == [True] and closed == [True]


def test_rejected_reply_never_reaches_speaker(monkeypatch):
    monkeypatch.setattr(audio, 'StreamingPlayer', lambda **_kw: SimpleNamespace(
        write=lambda *_: pytest.fail('stale reply reached speaker'),
        close=lambda: None,
    ))
    console = audio.ConsoleAudio(
        SimpleNamespace(generate=lambda *_: iter([([0.0] * 320, 24000)])))

    def reject():
        raise RuntimeError('stale reply')

    assert console.speak('옛 답변', validate=reject) is False
    console.close()


@pytest.mark.parametrize('with_request_id', [False, True])
@pytest.mark.parametrize('terminal', ['finished', 'failed', 'stopped'])
def test_speak_accepts_legacy_and_correlated_tts_events(monkeypatch, with_request_id, terminal):
    closed = []

    def runtime(_synthesizer, _player_factory, on_status):
        suffix = ('turn-request',) if with_request_id else ()

        def submit(_text, *, validate=None):
            on_status('other-playback', 'finished', False, *suffix)
            on_status('selected-playback', 'playing', False, *suffix)
            on_status('selected-playback', terminal, False, *suffix)
            return 'selected-playback'

        return SimpleNamespace(submit=submit, close=lambda: closed.append(True))

    monkeypatch.setattr(audio, 'SpeechRuntime', runtime)
    console = audio.ConsoleAudio(None)
    try:
        assert console.speak('안녕하세요') is (terminal == 'finished')
        assert closed == [True]
    finally:
        console.close()


@pytest.mark.parametrize('failure', ['synthesis', 'write', 'drain', 'validation', 'cleanup'])
def test_speak_retry_after_failure(monkeypatch, failure):
    players, runtimes = [], []
    original_runtime = audio.SpeechRuntime

    def runtime(*args, **kwargs):
        value = original_runtime(*args, **kwargs)
        runtimes.append(value)
        return value

    def maybe_fail(stage):
        if len(players) == 1 and failure == stage:
            raise RuntimeError('fake first ' + stage + ' failure')

    def player(*, on_state, cancel_event, **_kwargs):
        value = SimpleNamespace(close_called=False)

        def close():
            value.close_called = True
            maybe_fail('cleanup')

        value.write = lambda *_: (maybe_fail('write'), on_state('playing'))
        value.finish = lambda: maybe_fail('drain')
        value.stop = lambda: cancel_event.set()
        value.close = close
        players.append(value)
        return value

    def generate(*_args):
        maybe_fail('synthesis')
        yield [0.0] * 320, 24000

    monkeypatch.setattr(audio, 'SpeechRuntime', runtime)
    monkeypatch.setattr(audio, 'StreamingPlayer', player)
    console = audio.ConsoleAudio(SimpleNamespace(generate=generate), emit=lambda _: None)
    try:
        assert console.speak('첫 요청', validate=lambda: maybe_fail('validation')) is False
        assert players[0].close_called and not runtimes[0]._worker.is_alive()
        assert console.speak('재시도') is True
        assert len(players) == len(runtimes) == 2
        assert all(player.close_called for player in players)
        assert all(runtime._closed and not runtime._worker.is_alive() for runtime in runtimes)
        assert console._transcriber is None
    finally:
        console.close()


@pytest.mark.parametrize('fails', [False, True])
def test_fall_delegates_and_always_closes(monkeypatch, fails):
    cleanup = []
    outcome = object()
    engine_factory = object()
    console = audio.ConsoleAudio(None, emit=lambda _: None)
    monkeypatch.setattr(console, '_prepare_input', lambda: 'shared-model')

    def run():
        if fails:
            raise RuntimeError('synthetic fall failure')

    def factory(*args, **kwargs):
        assert args[0] == 'shared-model' and args[-1] is engine_factory
        return SimpleNamespace(run=run, close=lambda: cleanup.append(True),
                               status='succeeded', result=outcome)

    monkeypatch.setattr(audio._fall, 'VoiceDemo', factory)
    if fails:
        with pytest.raises(RuntimeError):
            console.fall(engine_factory)
    else:
        assert console.fall(engine_factory) == ('succeeded', outcome)
    assert cleanup == [True]
