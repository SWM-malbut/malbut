"""A caller can speak immediately: adopting wake input must not reset capture."""

import pytest

from test_dialogue_pipeline import harness, pump, QUIET, VOICE


def test_wake_event_precedes_chime_and_command_capture(harness):
    events = []
    pipeline = harness.create(publish_input_status=lambda *a: events.append(a),
                              on_wake=lambda: events.append('chime'))
    pipeline.feed(VOICE * 4 + QUIET * 20)
    pump(pipeline, lambda: pipeline.session.active)
    assert events[0][0] == '' and events[0][2] == 'wake' and events[0][1]
    assert events[1] == 'chime'
    assert not harness.transcripts


@pytest.mark.parametrize('decoding', [False, True])
def test_adoption_preserves_pcm_id_and_inflight_recognition(harness, decoding):
    pipeline = harness.create()
    pipeline.session.activate()
    harness.allow_asr.clear()
    pipeline.feed(VOICE * 20)
    uid = pipeline._capture_id
    if decoding:
        pipeline.feed(QUIET * 110)
        assert harness.entered_asr.wait(1)
    generation = pipeline._generation
    assert pipeline.start_session('patrol-chat-called-test', preserve_input=True)
    assert pipeline._generation == generation
    assert pipeline.session.session_id == 'patrol-chat-called-test'
    if not decoding:
        assert pipeline._capture_id == uid
        pipeline.feed(QUIET * 110)
    harness.allow_asr.set()
    pump(pipeline, lambda: len(harness.transcripts) == 1)
    assert harness.transcripts[0][0] == uid
    assert pipeline.session.active  # Listening stays open while the robot approaches.


def test_adoption_cannot_resurrect_finished_input_or_replace_another_owner(harness):
    pipeline = harness.create()
    assert not pipeline.start_session('patrol-chat-called-test', preserve_input=True)
    assert pipeline.start_session('fall-check')
    assert not pipeline.start_session('patrol-chat-called-test', preserve_input=True)
    assert pipeline.session.session_id == 'fall-check'
    pipeline.stop_session('patrol-chat-called-test')
    pipeline.stop_session('fall-check')
    pipeline.session.activate()
    assert not pipeline.start_session('patrol-chat-called-test', preserve_input=True)


def test_direction_window_uses_capture_time_not_asr_completion(harness):
    from types import SimpleNamespace
    pipeline = harness.create()
    pipeline._event_time = 11.
    pipeline._complete_capture('wake', SimpleNamespace(
        revision=1, pcm=bytes(64000), voiced_s=1., silence_s=.5))
    assert pipeline.wake_audio_window == (9., 10.5)
    pipeline.clock = lambda: 20.
    pipeline._accept_result('wake', pipeline._generation, None, '제이크', None)
    assert pipeline.wake_audio_window == (9., 10.5)
