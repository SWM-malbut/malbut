"""Real ROS messages and Agent dialogue with simulated detection, microphone and TTS."""

from concurrent.futures import Future
from datetime import datetime
import json
import time
from threading import Event
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

rclpy = pytest.importorskip('rclpy')
pytest.importorskip('yolo_msgs.msg')
pytest.importorskip('malbut_interfaces.msg')

from malbut_interfaces.msg import (  # noqa: E402
    SpeechInputStatus, SpeechPlaybackStatus, SpeechRequest, SpeechTranscript, SystemState,
)
from malbut_interfaces.srv import ControlSpeechPlayback, ControlSpeechSession  # noqa: E402
from nav_msgs.msg import Odometry  # noqa: E402
from std_msgs.msg import String  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.parameter import Parameter  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data  # noqa: E402
from yolo_msgs.msg import Detection, DetectionArray  # noqa: E402

from malbut_agent_server.config import Settings  # noqa: E402
from malbut_agent_server.patrol_memory import PatrolMemoryStore  # noqa: E402
from malbut_agent_server.patrol_query import configure_patrol_queries  # noqa: E402
from malbut_agent_server.ros_communication import create_communication_node  # noqa: E402
from malbut_agent_server import patrol_conversation_ros as adapter  # noqa: E402
from test_patrol_conversation import NOW, remember
from malbut_agent_server.patrol_conversation import PatrolConversationTopics  # noqa: E402
from test_speech_dialogue import RuntimeFactory  # noqa: E402


@pytest.fixture
def robot(tmp_path, monkeypatch):
    monkeypatch.setenv('ROS_DOMAIN_ID', '198')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    clock = [0.]
    original = adapter.RosPatrolConversation
    monkeypatch.setattr(adapter, 'RosPatrolConversation',
                        lambda node: original(node, clock=lambda: clock[0], wall_clock=lambda: NOW))
    memory_path = tmp_path / 'patrol'
    memory_path.mkdir()
    store = PatrolMemoryStore(memory_path, 'owner')
    remember(store, at=time.time() - 60)
    runtime = RuntimeFactory()

    def factory():
        instance = runtime()
        configure_patrol_queries(instance, store)
        return instance

    rclpy.init()
    agent = create_communication_node(
        speech_db_path=str(tmp_path / 'receipts.sqlite3'),
        dialogue_settings=Settings(user_id='owner'), dialogue_factory=factory)
    io = Node('patrol_conversation_test_io')
    executor = SingleThreadedExecutor()
    executor.add_node(agent)
    executor.add_node(io)
    speech, controls, stops = [], [], []
    accepting = [True]

    def control(request, response):
        controls.append((request.session_id, request.active))
        response.accepted = accepting[0]
        return response

    def stop(request, response):
        stops.append(request.playback_id)
        response.accepted = True
        return response

    io.create_service(ControlSpeechSession, '/malbut/speech/session_control', control)
    io.create_service(ControlSpeechPlayback, '/malbut/speech/playback_control', stop)
    io.create_subscription(SpeechRequest, '/malbut/speech/response', speech.append, 10)
    detections = io.create_publisher(DetectionArray, '/yolo/detections', qos_profile_sensor_data)
    poses = io.create_publisher(String, '/homecam/person_poses', qos_profile_sensor_data)
    odom = io.create_publisher(Odometry, '/odom', qos_profile_sensor_data)
    states = io.create_publisher(SystemState, '/malbut/state', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    playback = io.create_publisher(SpeechPlaybackStatus, '/malbut/speech/playback_status', 10)
    inputs = io.create_publisher(SpeechInputStatus, '/malbut/speech/input_status', 10)
    transcripts = io.create_publisher(SpeechTranscript, '/malbut/speech/transcript', 10)

    def pump(predicate, timeout=5):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            executor.spin_once(timeout_sec=.01)
        assert predicate(), (agent._patrol_chat.phase, controls)

    chat = agent._patrol_chat
    pump(lambda: agent._speech_ready and detections.get_subscription_count() > 0
         and chat._input.service_is_ready())

    def pulse(now, *, count=1, moving=False, idle=True, stale=False, source='yolo', score=.9):
        clock[0] = now
        stamp = io.get_clock().now().to_msg()
        if stale:
            stamp.sec -= 10
        message = DetectionArray()
        message.header.stamp = stamp
        message.detections = [Detection(class_name='person', score=score) for _ in range(count)]
        key = 'yolo' if source == 'yolo' else 'homecam_pose'
        previous = chat._presence_stamps.get(key)
        if source == 'yolo':
            detections.publish(message)
        else:
            poses.publish(String(data=json.dumps({
                'schemaVersion': 1, 'status': 'ok',
                'captureStamp': {'sec': stamp.sec, 'nanosec': stamp.nanosec},
                'persons': [{'observed': True, 'pose': {'present': True, 'boxConfidence': score}}
                            for _ in range(count)], 'unassigned': []})))
        motion = Odometry()
        motion.header.stamp = io.get_clock().now().to_msg()
        motion.twist.twist.linear.y = .2 if moving else 0.
        odom.publish(motion)
        states.publish(SystemState(system_state=SystemState.IDLE if idle else SystemState.EMERGENCY,
                                   control_mode=SystemState.AUTONOMOUS))
        pump(lambda: chat._odom_at == now and chat._system_idle == idle
             and (stale or chat._presence_stamps.get(key) != previous))
        chat.tick()

    def approach():
        for second in range(31, 36):
            pulse(float(second))
        pump(lambda: bool(speech))
        pump(lambda: speech[0].playback_id in chat._playing)
        return speech[0]

    def finish(message):
        playback.publish(SpeechPlaybackStatus(playback_id=message.playback_id,
                                             request_id=message.request_id, state='finished'))
        pump(lambda: chat.phase == 'listening')

    try:
        yield SimpleNamespace(**locals())
    finally:
        executor.remove_node(agent)
        agent.destroy_node()
        executor.remove_node(io)
        io.destroy_node()
        executor.shutdown()
        rclpy.shutdown()


def test_detection_to_opener_to_wake_free_ordinary_dialogue(robot):
    r = robot
    question = r.approach()
    assert '식물' in question.text
    assert r.runtime.provider.calls == []  # No LLM call to select or speak the opener.
    r.finish(question)
    first_session = r.chat.session_id
    r.transcripts.publish(SpeechTranscript(session_id=first_session,
                                          utterance_id='answer-1', text='응 식물 좋아해'))
    r.pump(lambda: any(s.request_id == 'answer-1' for s in r.speech))
    request = r.runtime.provider.calls[0][0]
    assert request.scene_context['conversation_opener']['topic'] == 'plants'
    assert request.utterance == '응 식물 좋아해'
    assert not r.chat.session_id
    assert (first_session, False) in r.controls
    reply = next(s for s in r.speech if s.request_id == 'answer-1')
    r.pump(lambda: r.chat._playback_id == reply.playback_id)
    r.finish(reply)
    assert r.chat.session_id != first_session  # Retired microphone session is not reused.
    r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
                                          utterance_id='answer-2', text='요즘은 산책도 좋아해'))
    r.pump(lambda: len(r.runtime.provider.calls) == 2)
    second, history, _ = r.runtime.provider.calls[1]
    assert 'conversation_opener' not in (second.scene_context or {})
    assert history[0].user_content == '응 식물 좋아해'


def test_silence_ends_listening_without_repeating_the_question(robot):
    r = robot
    r.finish(r.approach())
    session = r.chat.session_id
    r.pulse(56.)
    r.pump(lambda: (session, False) in r.controls)
    assert r.chat.phase == 'idle' and r.chat.gate.used
    for t in range(57, 65):
        r.pulse(float(t))
    assert len(r.speech) == 1 and not r.runtime.provider.calls


def test_decline_and_late_answer_do_not_start_inference(robot):
    r = robot
    r.finish(r.approach())
    session = r.chat.session_id
    r.transcripts.publish(SpeechTranscript(session_id=session, utterance_id='no', text='나중에'))
    r.pump(lambda: r.chat.phase == 'idle')
    r.transcripts.publish(SpeechTranscript(session_id=session, utterance_id='late', text='응'))
    for _ in range(10):
        r.executor.spin_once(timeout_sec=.01)
    assert not r.runtime.provider.calls and len(r.speech) == 1


def test_negative_answer_is_a_normal_reply_not_a_request_to_end(robot):
    r = robot
    r.finish(r.approach())
    r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
                                          utterance_id='negative', text='아니요'))
    r.pump(lambda: bool(r.runtime.provider.calls))
    assert r.runtime.provider.calls[0][0].utterance == '아니요'
    assert r.runtime.provider.calls[0][0].scene_context['conversation_opener']['topic'] == 'plants'


def test_app_confirmation_to_summary_storage_then_visitor_revokes_identity(robot, tmp_path):
    from malbut_agent_server.companion_memory import CompanionMemory
    from malbut_agent_server.companion_service import CompanionService

    r = robot
    cloud = dict(ownerId='owner', revision=1, eraseRevision=0, categories=['daily'],
                 externalProcessing=True, confirmedSessionId=None, remainingSeconds=0)
    reports = []
    def sync(report):
        reports.append(report)
        return dict(cloud)
    summary = dict(summary='내일 친구와 산책하기로 했다고 말함.', categories=['daily'],
                   event_state='planned', event_date='2026-10-30')
    service = CompanionService(CompanionMemory(tmp_path / 'daily.sqlite3', clock=lambda: NOW),
                               SimpleNamespace(sync=sync), SimpleNamespace(extract=lambda *args: [summary]),
                               clock=lambda: r.clock[0], wall=lambda: NOW)
    r.agent.dialogue._companion.service = service
    service.on_change = r.agent.dialogue._companion.clear_history
    r.finish(r.approach())
    assert service.session == r.chat.request_id
    # The app confirms this exact robot encounter; transcript text cannot do it.
    service.sync()
    assert service.binding(r.chat.request_id) is None
    cloud.update(revision=2, confirmedSessionId=service.session, remainingSeconds=300)
    r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
                                          utterance_id='daily-plan', text='내일 친구와 산책하기로 했어'))
    r.pump(lambda: any(s.request_id == 'daily-plan' and not s.interim for s in r.speech))
    assert service.memory.recall('owner')[0]['summary'] == summary['summary']
    assert r.runtime.provider.calls[-1][0].scene_context['companion_verified']
    assert r.runtime.handled == []  # Bypass durable ordinary conversation/history processing.
    reply = next(s for s in r.speech if s.request_id == 'daily-plan' and not s.interim)
    r.pump(lambda: r.chat._playback_id == reply.playback_id)
    r.finish(reply)
    r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
                                          utterance_id='daily-recall', text='내일 뭐 하기로 했지?'))
    r.pump(lambda: len(r.runtime.provider.calls) == 2)
    assert r.runtime.provider.calls[-1][0].scene_context['daily_memories'][0]['event_state'] == 'planned'
    r.pulse(36., count=2)
    assert r.chat.phase == 'idle'
    assert service.session is None
    assert service.binding(cloud['confirmedSessionId']) is None


@pytest.mark.parametrize('time_text,quiet', [
    ('2026-11-01T21:59:00+09:00', False), ('2026-11-01T22:00:00+09:00', True),
    ('2026-11-02T07:59:00+09:00', True), ('2026-11-02T08:00:00+09:00', False),
])
def test_quiet_hours_use_configured_timezone(robot, time_text, quiet):
    r = robot
    r.chat.wall_clock = lambda: datetime.fromisoformat(time_text).timestamp()
    assert r.chat._quiet() is quiet


def test_live_quiet_hours_delay_opener_without_consuming_encounter(robot):
    r = robot
    r.agent.set_parameters([Parameter('patrol_chat_quiet_start_hour', value=0),
                            Parameter('patrol_chat_quiet_end_hour', value=23)])
    for t in range(31, 37):
        r.pulse(float(t))
    assert not r.speech and not r.chat.gate.used
    r.agent.set_parameters([Parameter('patrol_chat_quiet_end_hour', value=0)])
    r.pulse(37.)
    r.pump(lambda: bool(r.speech))


def test_opt_out_discards_already_selected_opener(robot):
    r = robot
    opener = PatrolConversationTopics(r.store).reserve()
    selection = Future()
    selection.set_result(opener)
    r.chat._future = selection
    r.transcripts.publish(SpeechTranscript(utterance_id='mute-pending', text='먼저 말 걸지 마'))
    r.pump(lambda: 'mute-pending' in r.chat._preference_ids)
    r.pump(lambda: any(s.request_id == 'mute-pending' for s in r.speech))
    for t in range(31, 37):
        r.pulse(float(t))
    assert all(s.request_id == 'mute-pending' for s in r.speech)
    assert not r.runtime.provider.calls


@pytest.mark.parametrize('block', ['motion', 'emergency', 'people', 'stale', 'ordinary_input'])
def test_no_spontaneous_speech_when_unavailable(robot, block):
    r = robot
    for t in range(31, 37):
        if block == 'ordinary_input':
            r.inputs.publish(SpeechInputStatus(utterance_id='user-speaking', state='started'))
            r.pump(lambda: r.agent._input_utterance_id == 'user-speaking')
        r.pulse(float(t), moving=block == 'motion', idle=block != 'emergency',
                count=2 if block == 'people' else 1, stale=block == 'stale')
    assert r.speech == [] and not r.runtime.provider.calls


def test_failed_playback_never_opens_the_microphone(robot):
    r = robot
    question = r.approach()
    r.playback.publish(SpeechPlaybackStatus(playback_id=question.playback_id,
                                           request_id=question.request_id, state='failed'))
    r.pump(lambda: r.chat.phase == 'idle')
    assert not any(active for _, active in r.controls)


def test_fall_preemption_closes_only_our_listening_session(robot):
    r = robot
    r.finish(r.approach())
    session = r.chat.session_id
    r.agent._begin_situation()
    r.pump(lambda: (session, False) in r.controls)
    assert r.chat.phase == 'idle'
    assert all(identity == session for identity, _ in r.controls)


def test_spoken_opt_out_persists_and_can_be_reenabled_without_llm(robot):
    r = robot
    r.finish(r.approach())
    r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
                                          utterance_id='mute', text='먼저 말 걸지 마'))
    r.pump(lambda: any(s.request_id == 'mute' for s in r.speech))
    with r.store.connect() as connection:
        assert connection.execute('SELECT enabled FROM patrol_conversation_preferences').fetchone()[0] == 0
    r.inputs.publish(SpeechInputStatus(utterance_id='unmute', state='started'))
    r.pump(lambda: r.agent._input_utterance_id == 'unmute')
    r.transcripts.publish(SpeechTranscript(utterance_id='unmute', text='아니, 먼저 말을 걸어도 괜찮아'))
    r.pump(lambda: any(s.request_id == 'unmute' for s in r.speech))
    assert r.agent._input_utterance_id is None
    with r.store.connect() as connection:
        assert connection.execute('SELECT enabled FROM patrol_conversation_preferences').fetchone()[0] == 1
    assert not r.runtime.provider.calls


@pytest.mark.parametrize('cause', ['person_left', 'input_failed'])
def test_person_leaving_and_input_failure_close_listening(robot, cause):
    r = robot
    r.finish(r.approach())
    session = r.chat.session_id
    if cause == 'person_left':
        r.pulse(36., count=0)
    else:
        r.inputs.publish(SpeechInputStatus(session_id=session, utterance_id='failed', state='failed'))
    r.pump(lambda: (session, False) in r.controls)
    assert r.chat.phase == 'idle'


def test_rejected_microphone_start_does_not_accept_an_answer(robot):
    r = robot
    question = r.approach()
    r.accepting[0] = False
    r.playback.publish(SpeechPlaybackStatus(playback_id=question.playback_id,
                                           request_id=question.request_id, state='finished'))
    r.pump(lambda: bool(r.controls) and r.chat.phase == 'idle')
    assert not r.chat.session_id and not r.runtime.provider.calls


def test_late_microphone_ack_cannot_reopen_a_timed_out_session(robot, monkeypatch):
    from rclpy.task import Future

    r = robot
    pending = Future()
    call = r.chat._input.call_async
    monkeypatch.setattr(r.chat._input, 'call_async',
                        lambda request: pending if request.active else call(request))
    question = r.approach()
    r.playback.publish(SpeechPlaybackStatus(playback_id=question.playback_id,
                                           request_id=question.request_id, state='finished'))
    r.pump(lambda: r.chat.phase == 'opening')
    session = r.chat.session_id
    r.pulse(36.)
    r.pulse(37.)
    r.pulse(38.)
    r.pump(lambda: (session, False) in r.controls)
    pending.set_result(ControlSpeechSession.Response(accepted=True))
    r.chat.tick()
    assert r.chat.phase == 'idle' and not r.chat.session_id


def test_camera_permission_revocation_ends_only_our_session(robot, monkeypatch):
    r = robot
    r.finish(r.approach())
    session = r.chat.session_id
    monkeypatch.setattr(r.agent._scene._images, 'permitted', lambda: False)
    r.chat.tick()
    r.pump(lambda: (session, False) in r.controls)
    assert r.chat.phase == 'idle'


def test_late_answer_after_departure_is_suppressed_without_blocking_new_dialogue(robot):
    r = robot
    release = Event()
    answer = r.runtime.provider.respond

    def slow_answer(request, history):
        assert release.wait(5)
        return answer(request, history)

    r.runtime.provider.respond = slow_answer
    try:
        r.finish(r.approach())
        r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
                                              utterance_id='slow-reply', text='응 좋아해'))
        r.pump(lambda: bool(r.runtime.provider.calls))
        r.pulse(36., count=0)
        release.set()
        r.pump(lambda: not r.agent.dialogue.has_pending)
        assert not any(s.request_id == 'slow-reply' for s in r.speech)
        r.transcripts.publish(SpeechTranscript(utterance_id='ordinary-new', text='안녕'))
        r.pump(lambda: any(s.request_id == 'ordinary-new' for s in r.speech))
    finally:
        release.set()


def test_own_interim_ack_does_not_end_conversation_or_open_microphone(robot):
    r = robot
    r.approach()
    r.chat.phase, r.chat.reply_id = 'responding', 'user-question'
    r.chat._playback_id = ''
    identity = 'ack:' + str(uuid5(NAMESPACE_URL, 'user-question'))
    r.agent.say('날씨를 확인할게요.', interim=True, request_id=identity, playback_id='notice')
    r.pump(lambda: 'notice' in r.chat._playing)
    r.playback.publish(SpeechPlaybackStatus(playback_id='notice', request_id=identity,
                                           state='finished', interim=True))
    r.pump(lambda: 'notice' not in r.chat._playing)
    assert r.chat.phase == 'responding' and not r.controls


def test_idle_pose_stream_starts_default_greeting_without_patrol_or_app_confirmation(robot):
    r = robot
    with r.store.connect() as connection:
        connection.execute('DELETE FROM observations')
    for second in range(31, 36):
        r.pulse(float(second), source='pose')
    r.pump(lambda: bool(r.speech))
    question = r.speech[0]
    assert question.text == '오늘 하루는 어떠셨어요?'
    assert r.chat._last_detection_stamp == -float('inf')  # Tracking YOLO stays asleep.
    assert not r.runtime.provider.calls
    r.finish(question)
    r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
                                          utterance_id='daily-answer', text='오늘 산책했어'))
    r.pump(lambda: bool(r.runtime.provider.calls))
    request, history, memories = r.runtime.provider.calls[-1]
    assert request.user_id == 'unverified-companion'
    assert request.scene_context['conversation_opener']['source'] == 'current_encounter'
    assert not history and not memories


@pytest.mark.parametrize('count,score,stale', [(0, .9, False), (2, .9, False),
                                             (1, .1, False), (1, .9, True)])
def test_pose_empty_multiple_weak_and_stale_frames_do_not_start_chat(robot, count, score, stale):
    r = robot
    for second in range(31, 37):
        r.pulse(float(second), source='pose', count=count, score=score, stale=stale)
    assert not r.speech and r.chat.phase == 'idle'


def test_second_person_in_pose_stream_ends_chat_even_when_yolo_says_one(robot):
    r = robot
    r.finish(r.approach())
    session = r.chat.session_id
    r.pulse(36., source='pose', count=2)
    r.pump(lambda: (session, False) in r.controls)
    assert r.chat.phase == 'idle'
    r.pulse(36.5, count=1)
    assert r.chat.gate.present_since is None  # A second model cannot erase the visitor.


def test_same_person_in_two_detectors_is_not_counted_twice(robot):
    r = robot
    for second in range(31, 36):
        r.pulse(float(second), source='pose')
        r.pulse(float(second) + .1)
    r.pump(lambda: bool(r.speech))
    assert r.chat.gate.present_since is not None


@pytest.mark.parametrize('payload', [
    '[]', '{', '{}', '{"captureStamp": null}',
    json.dumps({'captureStamp': {'sec': 10**400, 'nanosec': 0}}),
    json.dumps({'captureStamp': {'sec': 1, 'nanosec': -1}}),
])
def test_malformed_pose_input_does_not_start_or_crash_conversation(robot, payload):
    r = robot
    r.chat._person_poses(String(data=payload))
    assert not r.chat._presence and not r.speech
