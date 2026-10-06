"""No devices or API: exercise the console fall harness's actual runtime wiring."""

from collections import deque
from queue import Queue
from threading import Event
import time
from types import SimpleNamespace

import pytest

import test_agent_console_support  # noqa: F401

import fall_voice as demo_module
from fall_voice import VoiceDemo
from malbut_agent_server.situation_dialogue import MockSituationProvider, SituationDialogue


VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


@pytest.mark.parametrize('stage', ['DialoguePipeline', 'SituationSpeechSession'])
@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_initialization_failure_closes_started_tts_worker(monkeypatch, stage, failure):
    runtimes = []
    real_runtime = demo_module.SpeechRuntime

    def runtime(*args, **kwargs):
        value = real_runtime(*args, **kwargs)
        runtimes.append(value)
        return value

    def fail(*_args, **_kwargs):
        raise failure('interrupted initialization')

    monkeypatch.setattr(demo_module, 'SpeechRuntime', runtime)
    monkeypatch.setattr(demo_module, stage, fail)
    try:
        with pytest.raises(failure, match='interrupted initialization'):
            VoiceDemo(
                SimpleNamespace(), SimpleNamespace(),
                lambda: pytest.fail('initialization opened a microphone'),
                lambda **_: pytest.fail('initialization opened a speaker'),
                lambda *_: False, lambda: None, emit=lambda _: None,
            )
        assert len(runtimes) == 1
        assert runtimes[0]._closed and not runtimes[0]._worker.is_alive()
    finally:
        for value in runtimes:
            value.close()


class Rig:
    def __init__(self):
        self.now = 0.0
        self.frames = Queue()
        self.answers = deque()
        self.players = []
        self.cleaned = []
        self.recorder = SimpleNamespace(
            sample_rate=16000, start=lambda: None, read=self.frames.get,
            stop=lambda: (self.cleaned.append('mic_stop'), self.frames.put([0] * 320)),
            delete=lambda: self.cleaned.append('mic_delete'),
        )
        self.demo = VoiceDemo(
            SimpleNamespace(transcribe=self.transcribe),
            SimpleNamespace(generate=lambda *_: iter([([0.0] * 320, 24000)])),
            lambda: self.recorder, self.player,
            lambda frame, rate: frame[:2] != b'\x00\x00',
            lambda: SituationDialogue(MockSituationProvider()),
            clock=lambda: self.now, emit=lambda _: None,
        )

    def transcribe(self, pcm, rate):
        assert rate == 16000 and VOICE in pcm
        answer = self.answers.popleft()
        if isinstance(answer, Exception):
            raise answer
        return answer

    def player(self, *, on_state, cancel_event):
        drained = Event()

        def finish():
            assert drained.wait(5), 'test did not drain playback'

        def stop():
            cancel_event.set()
            drained.set()

        player = SimpleNamespace(
            drained=drained, write=lambda *_: on_state('playing'), finish=finish,
            stop=stop, close=lambda: self.cleaned.append('player_close'),
        )
        self.players.append(player)
        return player

    def until(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.demo.poll()
            if predicate():
                return
            time.sleep(0.001)
        raise AssertionError((self.demo.session.phase, self.demo.status, self.demo.result))

    def start(self):
        self.demo.stt.start()
        self.demo.session.start()
        self.until(lambda: self.demo.stt.session.playback_state == 'playing')

    def listen(self):
        self.players[-1].drained.set()
        self.until(lambda: self.demo.session.phase == 'listening')
        self.now += 0.5  # The production non-AEC speaker-tail guard is 0.3 seconds.

    def answer(self, text):
        self.answers.append(text)
        # The current production gate requires at least 80 ms of speech.
        self.demo.stt.feed(VOICE * 4 + QUIET * 100)

    def finish(self):
        self.until(lambda: self.demo.result is not None and
                   self.demo.stt.session.playback_id == self.demo.session.playback_id)
        self.players[-1].drained.set()
        self.until(lambda: self.demo.session.done)
        assert self.demo.status == 'succeeded'


@pytest.mark.parametrize('with_request_id', [False, True])
def test_fall_accepts_legacy_and_correlated_tts_events(monkeypatch, with_request_id):
    original_runtime = demo_module.SpeechRuntime

    def runtime(synthesizer, player_factory, on_status, *args, **kwargs):
        def status(pid, state, interim, *_request_id):
            suffix = ('fall-request',) if with_request_id else ()
            on_status(pid, state, interim, *suffix)
        return original_runtime(synthesizer, player_factory, status, *args, **kwargs)

    monkeypatch.setattr(demo_module, 'SpeechRuntime', runtime)
    rig = Rig()
    try:
        rig.start()
        assert rig.demo.session.phase == 'speaking'
        rig.listen()
        assert rig.demo.session.phase == 'listening'
    finally:
        rig.demo.close()


def test_closing_audio_failure_does_not_deny_already_delivered_silence_result():
    rig = Rig()
    messages = []
    rig.demo.emit = messages.append
    synthesize = rig.demo.tts._synthesizer.generate

    def fail_closing(text, cancel):
        if rig.demo.result is not None:
            raise RuntimeError('synthetic closing speech failure')
        return synthesize(text, cancel)

    rig.demo.tts._synthesizer.generate = fail_closing
    try:
        rig.start()
        rig.listen()
        rig.now = 10.0
        rig.until(lambda: rig.demo.session.done)
        assert rig.demo.status == 'aborted'
        assert rig.demo.result.situation_assessment == 'unknown'
        assert rig.demo.result.help_needed is True
        assert any('판단 결과는 유지' in text for text in messages)
        assert not any('사용자 무응답으로 판단하지 않았습니다' in text for text in messages)
    finally:
        rig.demo.close()


@pytest.mark.parametrize('case', [
    'two_turns', 'silence', 'lost_session', 'asr_failure', 'microphone_failure',
])
def test_demo_audio_wiring_and_cleanup(case):
    rig = Rig()
    demo = rig.demo
    try:
        rig.start()
        first_pid = demo.session.playback_id
        # Playing the prompt is never itself the user's ten-second silence.
        rig.now = 12.0
        demo.stt.feed(VOICE + QUIET * 100)  # Raw speaker audio must stay gated.
        demo.poll()
        assert demo.result is None and demo.session.phase == 'speaking'
        rig.listen()
        first_sid = demo.session.session_id
        if case == 'two_turns':
            rig.answer('넘어졌어')
            rig.until(lambda: demo.session.playback_id != first_pid
                      and demo.stt.session.playback_id == demo.session.playback_id)
            demo.events.put(('transcript', first_sid, 'late-answer', '도와줘'))
            demo.poll()
            assert demo.result is None
            rig.listen()
            rig.answer('도움 필요 없어')
            rig.finish()
            assert (demo.result.situation_assessment, demo.result.help_needed) == (
                'confirmed_incident', False)
        elif case == 'silence':
            rig.now = 21.99
            demo.poll()
            assert demo.result is None
            rig.now = 22.0
            rig.finish()
            assert (demo.result.situation_assessment, demo.result.help_needed) == (
                'unknown', True)
        else:
            if case == 'lost_session':
                demo.stt.stop_session(first_sid)
                rig.now = 22.0
            elif case == 'microphone_failure':
                demo.stt.capture_error = OSError('synthetic microphone failure')
                with pytest.raises(OSError):
                    demo.poll()
                demo.stt.capture_error = None
            else:
                rig.answer(RuntimeError('synthetic ASR failure'))
            rig.until(lambda: demo.session.done)
            assert demo.status == 'aborted' and demo.result is None
        assert not demo.stt.session.active
    finally:
        demo.close()
    assert rig.cleaned.count('mic_stop') == rig.cleaned.count('mic_delete') == 1
    assert rig.cleaned.count('player_close') == len(rig.players)
    assert not demo.stt.capture_thread.is_alive()
    assert not demo.stt.asr_thread.is_alive()
    assert not demo.tts._worker.is_alive()
