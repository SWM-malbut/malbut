"""Exercise real confirmation, STT and TTS control with deterministic audio I/O."""

from collections import deque
from concurrent.futures import Future
from queue import Empty, Queue
from threading import Event
import time
from types import SimpleNamespace

import pytest

pytest.importorskip('malbut_agent_server.situation_session')
pytest.importorskip('malbut_stt.dialogue_pipeline')
pytest.importorskip('malbut_tts.runtime')
from malbut_agent_server.situation_dialogue import (  # noqa: E402
    MockSituationProvider, SituationDialogue, SituationResult,
)
from malbut_agent_server.situation_session import SituationSpeechSession  # noqa: E402
from malbut_stt.dialogue_pipeline import DialoguePipeline  # noqa: E402
from malbut_tts.runtime import CONFIRMATION, SpeechRuntime  # noqa: E402


VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


class AudioRig:
    """Replace only devices/ASR/model/ROS transport; retain runtime state machines."""

    def __init__(self, *, aec=True):
        self.now = 0.0
        self.events = Queue()
        self.recorder_frames = Queue()
        self.players = {}
        self.responses = deque()
        self.results = []
        self.finishes = []
        self.spoken = []
        self.playbacks = []
        self.controls = []
        self.reports = []
        self.wakes = []
        self.asr_started = Event()
        self.asr_allowed = Event()
        self.asr_allowed.set()
        self.tts = SpeechRuntime(
            SimpleNamespace(generate=self.synthesize), self.player,
            lambda pid, state: self.events.put(('playback', pid, state)),
        )
        self.stt = DialoguePipeline(
            recorder_factory=lambda: SimpleNamespace(
                sample_rate=16000, start=lambda: None,
                read=self.recorder_frames.get,
                stop=lambda: self.recorder_frames.put([0] * 320), delete=lambda: None,
            ),
            wake=SimpleNamespace(transcribe=self.wake),
            transcriber=SimpleNamespace(transcribe=self.transcribe),
            is_speech=lambda frame, rate: frame[:2] != b'\x00\x00',
            publish_transcript=lambda uid, text: self.events.put(
                ('transcript', self.stt.session.session_id, uid, text)),
            publish_control=self.stop_playback,
            publish_interruption=lambda *_args: pytest.fail('confirmation asked for addressee'),
            publish_input_status=lambda *args: self.events.put(('input', *args)),
            report=self.reports.append, clock=lambda: self.now, input_has_aec=aec,
        )
        self.stt.start()
        self.session = SituationSpeechSession(
            SimpleNamespace(request_id='audio-confirmation', situation_type='fall',
                            summary='영상에서 낙상이 의심됨'),
            lambda: SituationDialogue(MockSituationProvider()), self,
            lambda *args: self.finishes.append(args), clock=lambda: self.now,
            on_result=self.results.append,
        )

    def synthesize(self, text, cancel):
        yield QUIET, 24000

    def player(self, *, on_state, cancel_event):
        # Device drain is explicitly controlled, while TTS owns scheduling.
        drain = Event()
        device = SimpleNamespace(drain=drain, cancel=cancel_event)

        def write(audio, rate):
            assert audio == QUIET and rate == 24000
            on_state('playing')

        def finish():
            assert drain.wait(8), 'audio test failed to drain its output device'

        def stop():
            cancel_event.set()
            drain.set()

        device.write, device.finish, device.stop = write, finish, stop
        device.close = lambda: None
        self.players[self.session.playback_id] = device
        return device

    def wake(self, pcm, rate):
        self.wakes.append(pcm)
        return '제이크야'

    def transcribe(self, pcm, rate):
        assert rate == 16000 and VOICE in pcm
        self.asr_started.set()
        assert self.asr_allowed.wait(5), 'audio test failed to release ASR'
        answer = self.responses.popleft()
        if isinstance(answer, Exception):
            raise answer
        return answer

    def open_session(self, sid):
        future = Future()
        future.set_result(SimpleNamespace(
            accepted=self.stt.start_session(sid), barge_in_available=self.stt.input_has_aec,
        ))
        return future

    def close_session(self, sid):
        self.stt.stop_session(sid)

    def verify_session(self, sid):
        future = Future()
        future.set_result(SimpleNamespace(accepted=(
            self.stt.session.active and self.stt.session.session_id == sid)))
        return future

    def speak(self, text, pid):
        self.spoken.append((pid, text))
        return self.tts.submit(text, CONFIRMATION, playback_id=pid) is not None

    def stop_playback(self, pid, command):
        self.controls.append((pid, command))
        return self.tts.control(pid, command)

    def stop(self, pid):
        self.stop_playback(pid, 'stop')

    def poll(self):
        self.stt.poll()
        for _ in range(100):
            try:
                kind, *args = self.events.get_nowait()
            except Empty:
                break
            if kind == 'playback':
                self.playbacks.append(tuple(args))
                self.stt.on_playback_status(*args)
                self.session.playback(*args)
            elif kind == 'input':
                self.session.input_status(*args)
            else:
                self.session.transcript(*args)
        self.session.tick()

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.poll()
            if predicate():
                return
            time.sleep(0.001)
        raise AssertionError((self.session.phase, self.playbacks, self.reports))

    def start(self):
        self.session.start()
        self.until(lambda: (self.session.playback_id, 'playing') in self.playbacks)
        return self.session.playback_id

    def drain_question(self):
        self.players[self.session.playback_id].drain.set()
        self.until(lambda: self.session.phase == 'listening')
        self.now += 0.5  # Allow the existing no-AEC speaker-tail guard to pass.

    def answer(self, text):
        self.responses.append(text)
        self.stt.feed(VOICE + QUIET * 100)

    def finish(self):
        self.until(lambda: self.results and self.session.playback_id in self.players)
        self.players[self.session.playback_id].drain.set()
        self.until(lambda: self.session.done)
        assert self.finishes == [('succeeded', self.results[0])]
        assert not self.stt.session.active

    def close(self):
        self.asr_allowed.set()
        self.session.cancel()
        self.stt.close()
        self.tts.close()


@pytest.fixture
def audio_rig():
    rigs = []

    def make(**options):
        rig = AudioRig(**options)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.close()


def test_aec_barge_in_stops_real_tts_and_resolves_without_wake(audio_rig):
    rig = audio_rig()
    question = rig.start()
    rig.answer('그냥 누워 있는 거야')
    rig.until(lambda: bool(rig.results))
    assert (question, 'stop') in rig.controls
    assert (question, 'stopped') in rig.playbacks
    assert (question, 'finished') not in rig.playbacks
    assert rig.results == [SituationResult('resolved', False)]
    rig.finish()
    assert len(rig.spoken) == 2 and not rig.wakes


def test_no_aec_blocks_speaker_echo_then_accepts_wake_free_answer(audio_rig):
    rig = audio_rig(aec=False)
    rig.start()
    rig.stt.feed(VOICE + QUIET * 100)
    rig.poll()
    assert not rig.asr_started.is_set() and not rig.results
    rig.drain_question()
    rig.answer('그냥 누워 있는 거야')
    rig.finish()
    assert rig.results == [SituationResult('resolved', False)]
    assert not rig.wakes


def test_started_answer_survives_ten_seconds_and_moves_to_help_question(audio_rig):
    rig = audio_rig()
    rig.start()
    rig.drain_question()
    first_sid = rig.session.session_id
    rig.now = 9.9
    rig.asr_allowed.clear()
    rig.answer('넘어졌어')
    rig.until(lambda: rig.session.phase == 'hearing' and rig.asr_started.is_set())
    rig.now = 12
    rig.poll()
    assert rig.results == [] and rig.session.phase == 'hearing'
    rig.asr_allowed.set()
    rig.until(lambda: len(rig.spoken) == 2
              and (rig.session.playback_id, 'playing') in rig.playbacks)
    assert rig.session.session_id != first_sid
    rig.drain_question()
    rig.answer('도움 필요 없어')
    rig.finish()
    assert rig.results == [SituationResult('confirmed_incident', False)]
    assert len(rig.spoken) == 3 and not rig.wakes


def test_microphone_asr_failure_never_becomes_user_silence(audio_rig):
    rig = audio_rig()
    rig.start()
    rig.drain_question()
    rig.answer(RuntimeError('ASR unavailable'))
    rig.until(lambda: rig.session.done)
    assert rig.finishes == [('aborted', None)] and rig.results == []
    assert not rig.stt.session.active


def test_real_playback_finish_starts_silence_deadline(audio_rig):
    rig = audio_rig()
    rig.start()
    rig.now = 12
    rig.poll()
    assert not rig.results and rig.session.phase == 'speaking'
    rig.drain_question()
    rig.now = 21.99
    rig.poll()
    assert not rig.results
    rig.now = 22
    rig.finish()
    assert rig.results == [SituationResult('unknown', True)]


def test_lost_stt_session_does_not_turn_into_a_help_judgment(audio_rig):
    rig = audio_rig()
    rig.start()
    rig.drain_question()
    rig.stt.stop_session(rig.session.session_id)
    rig.now = 10
    rig.until(lambda: rig.session.done)
    assert rig.finishes == [('aborted', None)] and rig.results == []
    assert len(rig.spoken) == 1
