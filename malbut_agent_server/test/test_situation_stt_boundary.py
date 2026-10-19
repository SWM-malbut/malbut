"""Run the actual STT/session boundary with PCM and stubbed recognizer results."""

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from malbut_agent_server.situation_dialogue import MockSituationProvider, SituationDialogue
from test_situation_session import Harness


@pytest.mark.parametrize('recognition', [
    'silence', 'non_speech', 'quality_rejected', 'empty', 'answer', 'failure',
])
def test_confirmation_survives_rejected_noise_but_preserves_real_failures(monkeypatch, recognition):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / 'malbut_stt'))
    from malbut_stt.dialogue_pipeline import DialoguePipeline

    h = Harness()
    h.engine = SituationDialogue(MockSituationProvider())
    statuses = []

    def status(*event):
        statuses.append(event)
        h.session.input_status(*event)

    pipeline = DialoguePipeline(
        recorder_factory=None, wake=None, transcriber=None,
        is_speech=lambda frame, rate: frame[:2] != b'\x00\x00',
        publish_transcript=lambda uid, text: h.session.transcript(
            pipeline.session.session_id, uid, text),
        publish_control=lambda *args: None, publish_interruption=lambda *args: None,
        publish_input_status=status, report=lambda message: None, clock=lambda: h.now,
        partial_interval_s=None, endpoint_predecode_s=None,
    )

    def response(accepted):
        future = Future()
        future.set_result(SimpleNamespace(accepted=accepted))
        return future

    h.open_session = lambda sid: response(pipeline.start_session(sid))
    h.close_session = lambda sid: response(pipeline.stop_session(sid))
    h.verify_session = lambda sid: response(pipeline.session_is_active(sid))
    h.begin_question()
    h.session.playback(h.session.playback_id, 'finished')
    h.session.tick()
    assert h.session.phase == 'listening'
    sid = h.session.session_id

    def captured_result(text=None, error='SileroNonSpeech'):
        # Simulated VAD-positive noise; no microphone, network or model is used.
        pipeline.feed(b'\x01\x00' * (320 * 30) + bytes(640 * 100))
        kind, generation, uid, _ = pipeline.jobs.get_nowait()
        assert kind == 'command' and h.session.phase == 'hearing'
        pipeline.results.put((kind, generation, uid, text, error, 0.0))
        pipeline.poll()
        return uid

    if recognition != 'silence':
        for h.now in (1.0, 4.0, 8.0):
            if recognition == 'empty':
                captured_result('', None)
            else:
                captured_result(error=('InsufficientSpeechEvidence' if recognition == 'quality_rejected'
                                       else 'SileroNonSpeech'))
            assert h.session.phase == 'listening'
            assert h.session._deadline == 10.0
            assert pipeline.session_is_active(sid)
            assert not h.finishes and not h.outcomes
        h.now = 9.0
        if recognition == 'answer':
            captured_result('그냥 누워 있었어', None)
        elif recognition == 'failure':
            captured_result(error='SileroGateError')
            assert h.finishes == [('aborted', None)]
            assert not h.outcomes
            assert statuses[-1] == (sid, '', 'failed')
            return
    if recognition != 'answer':
        h.now = 10.0
        h.session.tick()
    h.complete_work()
    assert len(h.outcomes) == 1
    outcome = h.outcomes[0]
    assert outcome.help_needed is (recognition != 'answer')
    assert outcome.situation_assessment == ('resolved' if recognition == 'answer' else 'unknown')
    assert not pipeline.session.active
    assert all(uid for _, uid, state in statuses if state == 'failed')
    if recognition != 'answer':
        assert h.events[-1][1] == '답변을 확인하지 못했어요. 도움이 필요한 상황으로 판단할게요.'
    h.session.playback(h.session.playback_id, 'finished')
    assert h.finishes == [('succeeded', outcome)]

    # The same completed result must not create the web's question-error event.
    from malbut_agent_server.fall_runtime import apply_decision
    from test_fall_confirmation_result import assessed, result

    monitor, _, _, iid = assessed()
    monitor.drain_events()
    assert apply_decision(monitor, result(
        monitor, iid, situation_assessment=outcome.situation_assessment,
        help_needed=outcome.help_needed))
    events = monitor.drain_events()
    assert all(event.kind != 'agent_check_failed' for event in events)
    assert all(event.reason != 'question_error' for event in events)
