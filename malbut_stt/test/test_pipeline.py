"""Verify wake-to-publication behavior without microphone or cloud services."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.pipeline import SpeechPipeline


class Recorder:
    """Represent one finite recording device lifetime."""

    sample_rate = 16000

    def __init__(self, frames):
        self.frames = iter(frames)
        self.closed = False
        self.active = False

    def start(self):
        self.active = True

    def read(self):
        return next(self.frames)

    def stop(self):
        self.active = False

    def delete(self):
        self.closed = True


def frames_for_utterance():
    """Use one wake frame, one speech frame, and one second of silence."""
    return [[7] * 320, [1] * 320] + [[0] * 320] * 50


def make_pipeline(recordings, responses):
    """Provide deterministic boundaries and close the run after the fixtures."""
    recordings = list(recordings)
    recorders = []
    answers = iter(responses)
    calls = []
    messages = []
    events = []
    stopped = [False]

    def factory():
        recorder = Recorder(recordings.pop(0))
        recorders.append(recorder)
        return recorder

    def transcribe(pcm, sample_rate):
        assert recorders[-1].closed
        assert not recorders[-1].active
        calls.append((pcm, sample_rate))
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    def report(event):
        events.append(event)
        if event.startswith((
            'published:', 'empty_transcript', 'transcription_failed:',
            'no_speech', 'too_long',
        )) and not recordings:
            stopped[0] = True

    pipeline = SpeechPipeline(
        factory,
        SimpleNamespace(sample_rate=16000, process=lambda frame: 0 if frame[0] == 7 else -1),
        lambda frame, _: frame[:2] == b'\x01\x00',
        SimpleNamespace(transcribe=transcribe),
        lambda uid, text: messages.append((uid, text)),
        lambda: stopped[0], report, CaptureSettings(),
    )
    return pipeline, recorders, calls, messages, events, stopped


def test_waiting_audio_never_reaches_api_and_final_text_is_unchanged():
    """Only post-wake audio is uploaded, with one final ID/text publication."""
    original = '  거실로 가줘.\n'
    pipeline, recorders, calls, messages, events, _ = make_pipeline(
        [[[9] * 320] * 10 + frames_for_utterance()], [original],
    )
    pipeline.run()
    assert len(calls) == 1
    pcm, rate = calls[0]
    assert rate == 16000
    assert pcm == b'\x01\x00' * 320 + bytes(640 * 50)
    assert len(messages) == 1
    assert messages[0][1] == original
    assert str(UUID(messages[0][0])) == messages[0][0]
    assert events[:3] == ['waiting_for_wake', 'listening', 'transcribing']
    assert recorders[0].closed


def test_same_sentence_spoken_twice_gets_different_ids():
    """Repeated words are independent utterances, not retransmissions."""
    pipeline, _, calls, messages, _, _ = make_pipeline(
        [frames_for_utterance(), frames_for_utterance()], ['안녕', '안녕'],
    )
    pipeline.run()
    assert len(calls) == len(messages) == 2
    assert messages[0][0] != messages[1][0]


@pytest.mark.parametrize('failure', ['', ' \n', TimeoutError('secret'), RuntimeError('secret')])
def test_failure_discards_audio_and_returns_to_wake_waiting(failure):
    """Do not retry a failed recording or publish an error as user speech."""
    pipeline, recorders, calls, messages, events, _ = make_pipeline(
        [frames_for_utterance(), frames_for_utterance()], [failure, '다시 말함'],
    )
    pipeline.run()
    assert len(calls) == 2
    assert [text for _, text in messages] == ['다시 말함']
    assert events.count('waiting_for_wake') == 2
    assert all(recorder.closed for recorder in recorders)
    assert 'secret' not in ' '.join(events)


@pytest.mark.parametrize('frames, outcome', [
    ([[7] * 320] + [[0] * 320] * 250, 'no_speech'),
    ([[7] * 320] + [[1] * 320] * 1001, 'too_long'),
])
def test_empty_or_overlong_capture_never_calls_transcription(frames, outcome):
    """Discard incomplete input before the network boundary."""
    pipeline, _, calls, messages, events, _ = make_pipeline([frames], [])
    pipeline.run()
    assert outcome in events
    assert calls == messages == []


def test_shutdown_during_api_response_suppresses_late_publication():
    """A completed API call cannot revive a shutting-down node."""
    pipeline, recorders, _, messages, _, stopped = make_pipeline(
        [frames_for_utterance()], [],
    )

    def late_result(*_):
        stopped[0] = True
        return '늦은 결과'

    pipeline.transcriber.transcribe = late_result
    pipeline.run()
    assert messages == []
    assert recorders[0].closed


def test_shutdown_while_reading_releases_device_without_api_call():
    """Stop capture even when a read returns after shutdown begins."""
    pipeline, recorders, calls, messages, _, stopped = make_pipeline([[]], [])
    factory = pipeline.recorder_factory

    def closing_factory():
        recorder = factory()

        def read():
            stopped[0] = True
            return [7] * 320

        recorder.read = read
        return recorder

    pipeline.recorder_factory = closing_factory
    pipeline.run()
    assert recorders[0].closed
    assert calls == messages == []


def test_microphone_failure_exits_instead_of_reporting_ready_again():
    """Broken hardware must not enter an automatic retry loop."""
    pipeline, recorders, calls, messages, events, _ = make_pipeline([[]], [])
    with pytest.raises(StopIteration):
        pipeline.run()
    assert events == ['waiting_for_wake']
    assert recorders[0].closed
    assert calls == messages == []
