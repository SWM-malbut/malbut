"""Real DDS confirmation Action with fake audio and the offline dialogue engine."""

import time
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')
from action_msgs.msg import GoalStatus  # noqa: E402
from malbut_interfaces.action import ConfirmSituation  # noqa: E402
from malbut_interfaces.msg import (  # noqa: E402
    SpeechInputStatus, SpeechPlaybackStatus, SpeechRequest, SpeechTranscript,
)
from malbut_interfaces.srv import ControlSpeechPlayback, ControlSpeechSession  # noqa: E402
from rclpy.action import ActionClient  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402

from malbut_agent_server.config import Settings  # noqa: E402
from malbut_agent_server.ros_communication import create_communication_node  # noqa: E402


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr('malbut_agent_server.manager_client.ManagerClient',
                        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None))
    rclpy.init()
    executor = SingleThreadedExecutor()
    agent = create_communication_node(
        speech_db_path=str(tmp_path / 'receipts.sqlite3'),
        dialogue_settings=Settings(
            user_id='speaker', database_path=str(tmp_path / 'dialogue.sqlite3'),
        ),
    )
    voice = Node('confirmation_audio_test')
    executor.add_node(agent)
    executor.add_node(voice)
    state = SimpleNamespace(
        agent=agent, spoken=[], controls=[], session_id='', finish=True, feedback=[],
        voice=voice, executor=executor,
    )
    playback = voice.create_publisher(SpeechPlaybackStatus, '/malbut/speech/playback_status', 10)
    inputs = voice.create_publisher(SpeechInputStatus, '/malbut/speech/input_status', 10)
    transcripts = voice.create_publisher(SpeechTranscript, '/malbut/speech/transcript', 10)

    def speak(message):
        state.spoken.append(message)
        playback.publish(SpeechPlaybackStatus(playback_id=message.playback_id, state='playing'))
        if state.finish:
            playback.publish(SpeechPlaybackStatus(
                playback_id=message.playback_id, state='finished',
            ))

    def session(request, response):
        if getattr(request, 'check_only', False):
            response.accepted = bool(request.session_id and request.session_id == state.session_id)
            response.barge_in_available = True
            return response
        if request.active:
            state.session_id = request.session_id
        elif request.session_id == state.session_id:
            state.session_id = ''
        response.accepted = True
        response.barge_in_available = True
        return response

    def control(request, response):
        state.controls.append(request)
        response.accepted = True
        return response

    voice.create_subscription(SpeechRequest, '/malbut/speech/response', speak, 10)
    voice.create_service(ControlSpeechSession, '/malbut/speech/session_control', session)
    voice.create_service(ControlSpeechPlayback, '/malbut/speech/playback_control', control)
    client = ActionClient(voice, ConfirmSituation, '/malbut/agent/confirm_situation')

    def spin_until(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            executor.spin_once(timeout_sec=0.01)
        raise AssertionError('confirmation did not progress')

    def start(request_id=None):
        spin_until(lambda: client.server_is_ready() and agent.dialogue.ready
                   and agent.situation._input.service_is_ready()
                   and agent.situation._playback.service_is_ready()
                   and inputs.get_subscription_count() > 0
                   and transcripts.get_subscription_count() > 0
                   and playback.get_subscription_count() > 0
                   and agent.situation._speech.get_subscription_count() > 0)
        future = client.send_goal_async(ConfirmSituation.Goal(
            request_id=request_id or uuid4().hex,
            situation_type='fall', summary='거실 바닥에 누워 있는 사람이 관측됨',
        ), feedback_callback=state.feedback.append)
        spin_until(future.done)
        handle = future.result()
        return handle

    def answer(text, session_id=None):
        uid = uuid4().hex
        sid = session_id or state.session_id
        inputs.publish(SpeechInputStatus(session_id=sid, utterance_id=uid, state='started'))
        transcripts.publish(SpeechTranscript(session_id=sid, utterance_id=uid, text=text))

    state.spin_until, state.start, state.answer = spin_until, start, answer
    state.inputs, state.playback = inputs, playback
    try:
        yield state
    finally:
        agent.destroy_node()
        client.destroy()
        voice.destroy_node()
        executor.shutdown(timeout_sec=0)
        if rclpy.ok():
            rclpy.shutdown()


def test_user_rest_resolves_and_reports_before_closing_playback(rig):
    handle = rig.start('rest-confirmation')
    assert handle.accepted
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    assert rig.controls[0].command == 'stop_all'
    assert rig.spoken[0].request_type == SpeechRequest.CONFIRMATION
    assert not rig.agent.say('일반 대화 답변')
    result = handle.get_result_async()
    rig.finish = False
    rig.answer('그냥 누워 있는 거야')
    rig.spin_until(result.done)
    assert result.result().status == GoalStatus.STATUS_SUCCEEDED
    assert result.result().result.situation_assessment == 'resolved'
    assert result.result().result.help_needed is False
    assert rig.feedback == []
    rig.spin_until(lambda: len(rig.spoken) == 2)
    assert rig.agent.situation.active
    rig.playback.publish(SpeechPlaybackStatus(
        playback_id=rig.spoken[-1].playback_id, state='finished',
    ))
    rig.spin_until(lambda: not rig.agent.situation.active)
    assert rig.agent.dialogue.has_capacity()
    duplicate = rig.start('rest-confirmation')
    assert duplicate.accepted
    replay = duplicate.get_result_async()
    rig.spin_until(replay.done)
    assert replay.result().result == result.result().result
    assert len(rig.spoken) == 2


def test_lost_stt_session_is_aborted_instead_of_reported_as_user_silence(rig):
    handle = rig.start()
    assert handle.accepted
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    result = handle.get_result_async()
    # A reachable, restarted STT no longer owns the old microphone session.
    rig.session_id = ''
    rig.agent.situation._session._deadline = time.monotonic() - 1
    rig.spin_until(result.done)
    assert result.result().status == GoalStatus.STATUS_ABORTED
    assert len(rig.spoken) == 1 and rig.feedback == []
    rig.spin_until(lambda: not rig.agent.situation.active)


@pytest.mark.parametrize('reply, help_needed', [('도와줘', True), ('도움 필요 없어', False)])
def test_confirm_fall_then_assistance_answer(rig, reply, help_needed):
    handle = rig.start()
    assert handle.accepted
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    first_session = rig.session_id
    rig.answer('넘어졌어')
    rig.spin_until(lambda: len(rig.spoken) >= 2
                   and rig.agent.situation._session.phase == 'listening')
    assert rig.session_id != first_session
    rig.answer('그냥 누웠어', session_id=first_session)
    rig.answer(reply)
    result = handle.get_result_async()
    rig.spin_until(result.done)
    assert result.result().status == GoalStatus.STATUS_SUCCEEDED
    assert result.result().result.situation_assessment == 'confirmed_incident'
    assert result.result().result.help_needed is help_needed


def test_audio_failure_is_an_aborted_action_not_user_silence(rig):
    handle = rig.start()
    assert handle.accepted
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    rig.inputs.publish(SpeechInputStatus(
        session_id=rig.session_id, utterance_id='', state='failed',
    ))
    result = handle.get_result_async()
    rig.spin_until(result.done)
    assert result.result().status == GoalStatus.STATUS_ABORTED
    assert result.result().result.situation_assessment == ''


def test_cancel_closes_proactive_input_and_releases_ordinary_dialogue(rig):
    handle = rig.start()
    assert handle.accepted
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    cancel = handle.cancel_goal_async()
    result = handle.get_result_async()
    rig.spin_until(lambda: cancel.done() and result.done())
    assert result.result().status == GoalStatus.STATUS_CANCELED
    rig.spin_until(lambda: rig.session_id == '')
    assert rig.agent.dialogue.has_capacity()


@pytest.mark.parametrize('answer,assessment,help_needed', [
    ('그냥 누워 있는 거야', 'resolved', False),
    ('도와줘', 'unknown', True),
])
def test_vlm_manager_real_agent_round_trip_applies_final_user_judgment(
    rig, answer, assessment, help_needed,
):
    from std_msgs.msg import String
    from malbut_system_manager.fall_confirmation_link import FallConfirmationLink
    from malbut_agent_server.fall_runtime import apply_decision, event_metadata
    from test_fall_confirmation_result import assessed

    monitor, _, _, iid = assessed()
    question = next(e for e in monitor.drain_events() if e.kind == 'question_requested')
    manager = Node('confirmation_round_trip_manager')
    link = FallConfirmationLink(manager, runtime_id='integration-vlm')
    rig.executor.add_node(manager)
    events = rig.voice.create_publisher(String, '/malbut/falls/runtime/events', 50)
    received = []

    def decision(message):
        received.append(json.loads(message.data))
        assert apply_decision(monitor, message.data)

    rig.voice.create_subscription(String, '/malbut/falls/runtime/decision', decision, 10)
    try:
        rig.spin_until(lambda: events.get_subscription_count() > 0
                       and link.client.server_is_ready() and rig.agent.dialogue.ready
                       and link.decisions.get_subscription_count() > 0)
        events.publish(String(data=json.dumps(dict(
            event_metadata(question), boot_id=monitor.boot_id, runtime_id='integration-vlm',
        ))))
        rig.spin_until(lambda: rig.agent.situation._session is not None
                       and rig.agent.situation._session.phase == 'listening')
        rig.answer(answer)
        rig.spin_until(lambda: bool(received))
        assert received[0]['action'] == 'confirmation_result'
        assert received[0]['situation_assessment'] == assessment
        assert received[0]['help_needed'] is help_needed
        assert monitor.incident(iid).situation_assessment == assessment
        assert monitor.incident(iid).help_needed is help_needed
        assert not monitor.pending_questions()
    finally:
        link.destroy()
        rig.executor.remove_node(manager)
        manager.destroy_node()
