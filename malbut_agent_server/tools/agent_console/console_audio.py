"""Local Metal speech input and current production audio runtimes for the console."""

from pathlib import Path
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import fall_voice as _fall

from malbut_stt.audio import SoundDeviceRecorder
from malbut_stt.cpp_transcription import CppWhisperTranscriber
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_tts.audio import StreamingPlayer
from malbut_tts.runtime import SpeechRuntime, TERMINAL_STATES
import webrtcvad


class ConsoleAudio:
    def __init__(self, synthesizer, *, device_index=-1, output_device=None, emit=print,
                 model_path=None, library_path=None):
        self.synthesizer = synthesizer
        self.model_path = Path(model_path) if model_path is not None else None
        self.library_path = Path(library_path) if library_path is not None else None
        self.device_index = device_index
        self.output_device = output_device
        self.emit = emit
        self._transcriber = None
        self._vad = webrtcvad.Vad(2)

    def _prepare_input(self):
        if self._transcriber is None:
            if (self.model_path is None or not self.model_path.is_file()
                    or self.library_path is None or not self.library_path.is_file()):
                raise FileNotFoundError('Configure --stt-model and --stt-library for voice input')
            self.emit('로컬 음성 인식 모델을 준비하고 있어요…')
            self._transcriber = CppWhisperTranscriber(
                self.model_path, self.library_path, use_gpu=True, n_threads=6)
        return self._transcriber

    def _recorder(self):
        return SoundDeviceRecorder(device_index=self.device_index)

    def _player(self, **kwargs):
        return StreamingPlayer(device=self.output_device, **kwargs)

    def listen_once(self):
        """Record one wake-free utterance; only healthy microphone silence returns None."""
        transcriber = self._prepare_input()
        text = None
        started = False

        def transcript(_uid, value):
            nonlocal text
            text = value

        def input_status(_sid, _uid, state):
            nonlocal started
            if state == 'failed':
                raise RuntimeError('음성 인식에 실패했습니다.')
            if state == 'started' and not started:
                started = True
                self.emit('🎙 듣고 있어요… 말을 마치면 잠깐 쉬세요.')

        pipeline = DialoguePipeline(
            recorder_factory=self._recorder, wake=transcriber, transcriber=transcriber,
            is_speech=self._vad.is_speech, publish_transcript=transcript,
            publish_control=lambda *_: None, publish_interruption=lambda *_: None,
            publish_input_status=input_status, report=lambda _: None,
            input_has_aec=False, clock=monotonic,
        )
        try:
            pipeline.start()
            if not pipeline.start_session('console-' + uuid4().hex):
                raise RuntimeError('마이크 세션을 열지 못했습니다.')
            last_frame = monotonic()
            deadline = last_frame + 15.0
            self.emit('🎤 지금 말하세요. 호출어 없이 15초 안에 시작하면 됩니다.')
            while text is None:
                if not pipeline.audio.empty():
                    last_frame = monotonic()
                pipeline.poll()
                now = monotonic()
                if now - last_frame >= 5.0:
                    raise RuntimeError('마이크 입력이 중단되었습니다.')
                if not started and now >= deadline:
                    self.emit('음성이 없어 입력을 마쳤습니다.')
                    return None
                sleep(0.01)
            return text
        finally:
            pipeline.close()

    def speak(self, text, *, validate=None):
        """Return True only after the output device reports a completed drain."""
        events = Queue()
        runtime = SpeechRuntime(
            self.synthesizer, self._player,
            lambda *args: events.put(args),
        )
        try:
            playback_id = runtime.submit(text, validate=validate)
            if playback_id is None:
                return False
            while True:
                # Newer TTS runtimes append the originating request ID.
                pid, state, _interim, *_correlation = events.get()
                if pid == playback_id and state in TERMINAL_STATES:
                    return state == 'finished'
        finally:
            runtime.close()

    def fall(self, engine_factory):
        """Reuse the verified fall harness and return (status, result)."""
        demo = _fall.VoiceDemo(
            self._prepare_input(), self.synthesizer, self._recorder, self._player,
            self._vad.is_speech, engine_factory, emit=self.emit,
        )
        try:
            demo.run()
            return demo.status, demo.result
        finally:
            demo.close()

    def close(self):
        if self._transcriber is not None:
            transcriber, self._transcriber = self._transcriber, None
            transcriber.close()
