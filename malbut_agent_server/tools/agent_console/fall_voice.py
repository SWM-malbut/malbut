"""Reusable fall voice dialogue harness with no device initialization at import."""

from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import asdict
import json
from queue import Empty, Queue
import time
from types import SimpleNamespace
from uuid import uuid4

from malbut_agent_server.situation_session import SituationSpeechSession
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_tts.runtime import CONFIRMATION, SpeechRuntime


class VoiceDemo:
    """Wire existing session, STT and TTS callbacks on one owner thread."""

    def __init__(self, transcriber, synthesizer, recorder_factory, player_factory,
                 is_speech, engine_factory, *, clock=time.monotonic, emit=print):
        self.events = Queue()
        self.emit = emit
        self.result = None
        self.status = None
        with ExitStack() as cleanup:
            self.tts = SpeechRuntime(synthesizer, player_factory,
                                    lambda pid, state, interim, request_id='':
                                    self.events.put(('playback', pid, state)))
            cleanup.callback(self.tts.close)
            self.stt = DialoguePipeline(
                recorder_factory=recorder_factory, wake=transcriber, transcriber=transcriber,
                is_speech=is_speech, input_has_aec=False, clock=clock,
                publish_transcript=self.transcript,
                publish_control=self.tts.control,
                publish_interruption=lambda *_: None,
                publish_input_status=lambda *args: self.events.put(('input', *args)),
                report=self.report,
            )
            cleanup.callback(self.stt.close)
            self.session = SituationSpeechSession(
                SimpleNamespace(request_id=uuid4().hex, situation_type='fall',
                                summary='거실 바닥에 사람이 누워 있어 낙상이 의심됩니다.'),
                engine_factory, self, self.finished, clock=clock, on_result=self.decided,
            )
            cleanup.callback(self.session.cancel)
            self._phase = None
            cleanup.pop_all()

    def transcript(self, uid, text):
        self.events.put(('transcript', self.stt.session.session_id, uid, text))

    def report(self, event):
        if any(word in event for word in ('error', 'failed', 'overflow', 'discarded')):
            self.emit(f'[STT] {event}')

    def open_session(self, sid):
        future = Future()
        future.set_result(SimpleNamespace(accepted=self.stt.start_session(sid),
                                          barge_in_available=False))
        return future

    def verify_session(self, sid):
        future = Future()
        future.set_result(SimpleNamespace(accepted=self.stt.session_is_active(sid)))
        return future

    def close_session(self, sid):
        future = Future()
        future.set_result(SimpleNamespace(accepted=self.stt.stop_session(sid)))
        return future

    def speak(self, text, pid):
        self.emit(f'\n🤖 {text}')
        return self.tts.submit(text, CONFIRMATION, playback_id=pid) is not None

    def stop(self, pid):
        return self.tts.control(pid, 'stop')

    def decided(self, result):
        self.result = result
        labels = {'confirmed_incident': '낙상 확인', 'resolved': '상황 해소',
                  'unknown': '발생 여부 미확인'}
        self.emit(f'\n결과: {labels[result.situation_assessment]} / '
                  f'도움 필요: {"예" if result.help_needed else "아니요"}')
        self.emit(json.dumps(asdict(result), ensure_ascii=False))

    def finished(self, status, result):
        self.status = status
        if status == 'aborted':
            if self.result is not None:
                self.emit('판단 결과는 유지됩니다. 마무리 음성 처리를 완료하지 못했습니다.')
            else:
                self.emit('처리 실패: 음성/모델 오류입니다. 사용자 무응답으로 판단하지 않았습니다.')

    def poll(self):
        try:
            self.stt.poll()
        except Exception:
            self.session.abort()
            raise
        while True:
            try:
                kind, *args = self.events.get_nowait()
            except Empty:
                break
            if kind == 'playback':
                self.stt.on_playback_status(*args)
                self.session.playback(*args)
                if args[1] == 'failed':
                    self.emit('[TTS] 재생 실패. runtime.log를 확인하세요.')
            elif kind == 'input':
                self.session.input_status(*args)
            else:
                if self.session.transcript(*args):
                    self.emit(f'🗣 인식: {args[-1]}')
        self.session.tick()
        if self.session.phase != self._phase:
            self._phase = self.session.phase
            if self._phase == 'listening':
                self.emit('🎤 질문이 끝났어요. 잠깐 쉬고 답하세요. (답변 시작까지 최대 10초)')
            elif self._phase == 'hearing':
                self.emit('🎙 듣고 있어요…')
            elif self._phase == 'thinking':
                self.emit('판단 중…')

    def run(self):
        self.stt.start()
        deadline = time.monotonic() + 5
        while self.stt.audio.empty():
            self.poll()
            if time.monotonic() >= deadline:
                raise RuntimeError('microphone first frame timeout')
            time.sleep(0.01)
        self.emit('마이크 입력 확인. 낙상 의심 상황을 시작합니다.')
        self.session.start()
        deadline = time.monotonic() + 600
        while not self.session.done:
            self.poll()
            if time.monotonic() >= deadline:
                self.session.abort()
            time.sleep(0.01)

    def close(self):
        with ExitStack() as cleanup:
            cleanup.callback(self.tts.close)
            cleanup.callback(self.stt.close)
            self.session.cancel()
