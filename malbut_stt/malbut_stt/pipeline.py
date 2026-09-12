"""Run wake, capture, transcription, and publication sequentially."""

from array import array
import sys
from typing import Any, Callable, Optional
from uuid import uuid4

from malbut_stt.audio import CaptureResult, CaptureSettings, UtteranceCollector
from malbut_stt.wake import is_wake_phrase


def pcm_bytes(samples: Any) -> bytes:
    """Encode signed recorder samples as little-endian PCM16."""
    values = array('h', samples)
    if sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes()


class SpeechPipeline:
    """Keep cloud processing outside microphone capture and ROS callbacks."""

    def __init__(
        self,
        recorder_factory: Callable[[], Any],
        wake: Any,
        is_speech: Callable[[bytes, int], bool],
        transcriber: Any,
        publish: Callable[[str, str], None],
        should_stop: Callable[[], bool],
        report: Callable[[str], None],
        settings: CaptureSettings,
    ) -> None:
        """Accept hardware and network boundaries independently for tests."""
        self.recorder_factory = recorder_factory
        self.wake = wake
        self.is_speech = is_speech
        self.transcriber = transcriber
        self.publish = publish
        self.should_stop = should_stop
        self.report = report
        self.settings = settings
        self.wake_settings = CaptureSettings(silence_timeout_s=0.4, max_utterance_s=6.0)
        self.phase = 'idle'

    def _listen(self, settings, event) -> Optional[CaptureResult]:
        """Collect one utterance and close the microphone before any inference."""
        self.phase = 'opening_microphone'
        recorder = self.recorder_factory()
        started = False
        try:
            if recorder.sample_rate != 16000:
                raise ValueError('speech capture requires 16kHz PCM')
            if self.should_stop():
                return None
            self.phase = 'starting_microphone'
            recorder.start()
            started = True
            self.report(event)
            collector = UtteranceCollector(16000, self.is_speech, settings)
            while not self.should_stop():
                self.phase = 'reading_microphone'
                samples = recorder.read()
                if self.should_stop():
                    return None
                self.phase = 'collecting_utterance'
                result = collector.feed(pcm_bytes(samples))
                if result is not None:
                    return result
            return None
        finally:
            try:
                if started:
                    recorder.stop()
            finally:
                recorder.delete()

    def run(self) -> None:
        """Publish valid final results once; every new capture gets a fresh ID."""
        while not self.should_stop():
            if self.wake is not None:
                capture = self._listen(self.wake_settings, 'waiting_for_wake')
                if capture is None or self.should_stop():
                    return
                if capture.status != 'complete':
                    self.report('wake_' + capture.status)
                    continue
                self.phase = 'recognizing_wake'
                self.report('recognizing_wake')
                try:
                    text = self.wake.transcribe(capture.pcm, 16000)
                finally:
                    capture = None
                if self.should_stop():
                    return
                if not is_wake_phrase(text):
                    self.report('not_wake')
                    continue
                self.report('wake_detected')
                if self.should_stop():
                    return
            capture = self._listen(self.settings, 'listening')
            if capture is None or self.should_stop():
                return
            if capture.status != 'complete':
                self.report(capture.status)
                continue
            self.report('transcribing')
            self.phase = 'transcribing'
            try:
                text = self.transcriber.transcribe(capture.pcm, 16000)
            except Exception as error:
                # SDK messages can include request content: log only the class.
                self.report('transcription_failed:' + type(error).__name__)
                continue
            finally:
                # No raw recording is retained for retry or later processing.
                capture = None
            if self.should_stop():
                return
            if not isinstance(text, str) or not text.strip():
                self.report('empty_transcript')
                continue
            utterance_id = str(uuid4())
            self.phase = 'publishing'
            self.publish(utterance_id, text)
            if self.should_stop():
                return
            self.report('published:' + utterance_id)
