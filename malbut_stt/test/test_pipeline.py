"""Verify local wake-to-publication behavior without hardware or cloud services."""

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


def wake_frames():
    return [[7] * 320] + [[0] * 320] * 20


def command_frames():
    return [[1] * 320] + [[0] * 320] * 50


def make_pipeline(recordings, responses, wake_responses=None, manual=False):
    recordings = list(recordings)
    recorders, calls, wake_calls, messages, events = [], [], [], [], []
    answers = iter(responses)
    wake_answers = iter(wake_responses) if wake_responses is not None else None
    stopped = [False]

    def factory():
        recorder = Recorder(recordings.pop(0))
        recorders.append(recorder)
        return recorder

    def transcribe(pcm, sample_rate):
        assert recorders[-1].closed and not recorders[-1].active
        calls.append((pcm, sample_rate))
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    def recognize(pcm, sample_rate):
        assert recorders[-1].closed and not recorders[-1].active
        wake_calls.append((pcm, sample_rate))
        value = next(wake_answers) if wake_answers is not None else '제이크야'
        if isinstance(value, Exception):
            raise value
        return value

    def report(event):
        events.append(event)
        if event.startswith((
            'published:', 'empty_transcript', 'transcription_failed:',
            'no_speech', 'too_long', 'not_wake', 'wake_no_speech', 'wake_too_long',
        )) and not recordings:
            stopped[0] = True

    pipeline = SpeechPipeline(
        factory, None if manual else SimpleNamespace(transcribe=recognize),
        lambda frame, _: frame[:2] in (b'\x01\x00', b'\x07\x00'),
        SimpleNamespace(transcribe=transcribe),
        lambda uid, text: messages.append((uid, text)),
        lambda: stopped[0], report, CaptureSettings(),
    )
    return SimpleNamespace(
        pipeline=pipeline, recorders=recorders, calls=calls, wake_calls=wake_calls,
        messages=messages, events=events, stopped=stopped,
    )


def test_wake_audio_never_reaches_api_and_first_command_frame_is_preserved():
    original = '  거실로 가줘.\n'
    run = make_pipeline([wake_frames(), command_frames()], [original])
    run.pipeline.run()
    assert run.wake_calls == [(b'\x07\x00' * 320 + bytes(640 * 20), 16000)]
    assert run.calls == [(b'\x01\x00' * 320 + bytes(640 * 50), 16000)]
    assert len(run.messages) == 1
    assert run.messages[0][1] == original
    assert str(UUID(run.messages[0][0])) == run.messages[0][0]
    assert run.events[:5] == [
        'waiting_for_wake', 'recognizing_wake', 'wake_detected', 'listening', 'transcribing',
    ]
    assert len(run.recorders) == 2
    assert all(recorder.closed for recorder in run.recorders)


@pytest.mark.parametrize('text', ['안녕', '제이크야 거실로 가줘', '제이크', '', '로봇 이름은 제이크입니다.'])
def test_non_wake_and_combined_command_never_open_command_capture_or_call_api(text):
    run = make_pipeline([wake_frames()], [], [text])
    run.pipeline.run()
    assert run.events[-1] == 'not_wake'
    assert len(run.recorders) == 1
    assert run.calls == run.messages == []


def test_same_sentence_spoken_twice_gets_different_ids():
    run = make_pipeline([wake_frames(), command_frames()] * 2, ['안녕', '안녕'])
    run.pipeline.run()
    assert len(run.calls) == len(run.messages) == 2
    assert run.messages[0][0] != run.messages[1][0]


@pytest.mark.parametrize('failure', ['', ' \n', TimeoutError('secret'), RuntimeError('secret')])
def test_api_failure_discards_audio_and_requires_a_fresh_wake(failure):
    run = make_pipeline([wake_frames(), command_frames()] * 2, [failure, '다시 말함'])
    run.pipeline.run()
    assert len(run.calls) == 2
    assert [text for _, text in run.messages] == ['다시 말함']
    assert run.events.count('waiting_for_wake') == 2
    assert all(recorder.closed for recorder in run.recorders)
    assert 'secret' not in ' '.join(run.events)


@pytest.mark.parametrize('frames, outcome', [
    ([[0] * 320] * 250, 'no_speech'),
    ([[1] * 320] * 1001, 'too_long'),
])
def test_empty_or_overlong_command_never_calls_api(frames, outcome):
    run = make_pipeline([wake_frames(), frames], [])
    run.pipeline.run()
    assert outcome in run.events
    assert run.calls == run.messages == []


@pytest.mark.parametrize('frames, outcome', [
    ([[0] * 320] * 250, 'wake_no_speech'),
    ([[7] * 320] * 301, 'wake_too_long'),
])
def test_empty_or_overlong_wake_skips_both_local_inference_and_api(frames, outcome):
    run = make_pipeline([frames], [])
    run.pipeline.run()
    assert outcome in run.events
    assert run.wake_calls == run.calls == run.messages == []
    assert run.recorders[0].closed


def test_manual_capture_does_not_need_a_wake_model_or_drop_the_first_frame():
    run = make_pipeline([command_frames()], ['안녕'], manual=True)
    run.pipeline.run()
    assert run.wake_calls == []
    assert run.calls == [(b'\x01\x00' * 320 + bytes(640 * 50), 16000)]
    assert run.events[:2] == ['listening', 'transcribing']


def test_shutdown_during_api_response_suppresses_late_publication():
    run = make_pipeline([wake_frames(), command_frames()], [])

    def late_result(*_):
        run.stopped[0] = True
        return '늦은 결과'

    run.pipeline.transcriber.transcribe = late_result
    run.pipeline.run()
    assert run.messages == []
    assert all(recorder.closed for recorder in run.recorders)


def test_stop_after_wake_detection_never_opens_command_microphone():
    run = make_pipeline([wake_frames()], [])
    report = run.pipeline.report

    def stop(event):
        report(event)
        if event == 'wake_detected':
            run.stopped[0] = True

    run.pipeline.report = stop
    run.pipeline.run()
    assert len(run.recorders) == 1
    assert run.calls == run.messages == []
    assert 'listening' not in run.events


def test_shutdown_while_reading_releases_device_without_inference():
    run = make_pipeline([[]], [])
    factory = run.pipeline.recorder_factory

    def closing_factory():
        recorder = factory()

        def read():
            run.stopped[0] = True
            return [7] * 320

        recorder.read = read
        return recorder

    run.pipeline.recorder_factory = closing_factory
    run.pipeline.run()
    assert run.recorders[0].closed
    assert run.wake_calls == run.calls == run.messages == []


def test_local_model_failure_does_not_fall_back_to_cloud():
    run = make_pipeline([wake_frames()], [], [RuntimeError('local error')])
    with pytest.raises(RuntimeError):
        run.pipeline.run()
    assert run.pipeline.phase == 'recognizing_wake'
    assert run.recorders[0].closed
    assert run.calls == run.messages == []


def test_microphone_failure_exits_without_an_automatic_retry():
    run = make_pipeline([[]], [])
    with pytest.raises(StopIteration):
        run.pipeline.run()
    assert run.events == ['waiting_for_wake']
    assert run.recorders[0].closed
    assert run.wake_calls == run.calls == run.messages == []
