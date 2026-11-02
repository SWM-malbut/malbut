"""Real ROS Agent orchestration with simulated motion, camera, odometry and speech."""

import json
import math
from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')
pytest.importorskip('yolo_msgs.msg')

from malbut_interfaces.action import ExecuteMission  # noqa: E402
from malbut_interfaces.msg import SpeechInputStatus, SpeechPlaybackStatus, SpeechTranscript  # noqa: E402
from rclpy.action import ActionServer, CancelResponse  # noqa: E402
from rclpy.task import Future  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile  # noqa: E402
from std_msgs.msg import String  # noqa: E402

from malbut_agent_server.speech_missions import SpeechMissions  # noqa: E402
from navigation_user_map import room, write_user_map, rooms, rewrite  # noqa: E402
from test_patrol_conversation_ros import robot  # noqa: E402


@pytest.fixture
def proactive_enabled():
    # Exercise normal startup defaults, not a switch changed after construction.
    return False


@pytest.fixture
def called(robot, tmp_path):
    r = robot
    assert r.agent.get_parameter('patrol_conversation_enabled').value is False
    assert r.agent.get_parameter('called_conversation_enabled').value is True
    assert not r.chat.proactive_enabled and r.chat.called.enabled
    r.agent.speech_missions = SpeechMissions(r.agent.missions)
    catalog, selected, _, document = write_user_map(tmp_path, rooms=[room('거실', 0, 0)])
    r.agent.speech_missions.observe_localization({'mode': 'LOCALIZATION', 'map': str(selected)})
    controller = r.chat.called
    goals, cancellations, terminals = [], [], []
    runtime = r.io.create_publisher(String, '/malbut/runtime/state', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def resources(active):
        revision = r.chat.position.runtime_revision
        runtime.publish(String(data=json.dumps(dict(ready=True, error=None,
            active_groups=['tracking'] if active else [],
            capabilities=['follow_person'] if active else []))))
        r.pump(lambda: r.chat.position.runtime_revision > revision)

    async def execute(handle):
        goals.append(handle)
        terminal = Future()
        terminals.append(terminal)
        success = await terminal
        if handle.is_cancel_requested:
            handle.canceled()
        elif success:
            handle.succeed()
        else:
            handle.abort()
        result = ({} if handle.request.capability_id == 'navigate_to_pose'
                  else dict(total_elapsed_time=dict(sec=1, nanosec=0))
                  if handle.request.capability_id == 'spin'
                  else dict(success=success, final_state='STOPPED'))
        return ExecuteMission.Result(result_yaml=json.dumps(result))

    def cancel(handle):
        cancellations.append(handle)
        return CancelResponse.ACCEPT

    server = ActionServer(r.io, ExecuteMission, '/malbut/mission/execute', execute,
                          cancel_callback=cancel)
    r.pump(lambda: r.chat.position.client.server_is_ready())
    resources(False)

    def wake(count=0):
        r.pulse(1., count=count)
        r.pulse(2., count=count)
        r.pulse(3., count=count)
        r.inputs.publish(SpeechInputStatus(state='wake', utterance_id='wake-one'))
        r.pump(lambda: len(goals) == 1)
        assert controller.phase == 'acquiring'
        assert r.controls[0] == (controller.session_id, True)
        assert goals[0].request.idle_only
        assert json.loads(goals[0].request.arguments_yaml)['approach_only'] is True
        resources(True)

    def finish_motion(success, count=0, *, release=True):
        terminals[-1].set_result(success)
        r.pump(lambda: controller.phase == 'settling')
        if release:
            resources(False)
        for _ in range(6):
            r.pulse(r.clock[0] + .5, count=count)

    def finish_speech():
        r.pump(lambda: bool(r.speech) and r.speech[-1].playback_id in r.chat._playing)
        message = r.speech[-1]
        r.playback.publish(SpeechPlaybackStatus(playback_id=message.playback_id,
            request_id=message.request_id, state='finished'))
        r.pump(lambda: message.playback_id not in r.chat._playing)

    def answer(text, uid='location-one'):
        r.transcripts.publish(SpeechTranscript(session_id=controller.session_id,
            utterance_id=uid, text=text))

    try:
        yield SimpleNamespace(**locals())
    finally:
        for terminal in terminals:
            if not terminal.done():
                terminal.set_result(False)
        server.destroy()


def test_default_person_presence_does_not_start_spontaneous_conversation(called):
    r = called.r
    for second in range(31, 36):
        r.pulse(float(second))
    assert r.chat.gate.ready()  # A stable, fresh encounter would otherwise qualify.
    assert not r.speech and not called.goals and not r.runtime.provider.calls
    assert r.chat.phase == 'idle' and not called.controller.active


def test_visible_caller_approaches_releases_tracking_and_listens(called):
    c, r = called, called.r
    c.wake(count=1)
    assert not r.runtime.provider.calls and not r.speech
    c.finish_motion(True, count=1, release=False)
    assert c.controller.phase == 'settling' and not r.speech
    c.resources(False)
    r.pulse(r.clock[0] + .1)
    r.pump(lambda: any(s.text == '왔어요. 말씀하세요.' for s in r.speech))
    assert not c.controller.active and len(c.goals) == 1
    c.finish_speech()
    r.pump(lambda: r.chat.phase == 'listening')
    r.transcripts.publish(SpeechTranscript(session_id=r.chat.session_id,
        utterance_id='daily-chat', text='오늘 산책했어'))
    r.pump(lambda: len(r.runtime.provider.calls) == 1)
    assert len(c.goals) == 1  # No following during dialogue.


def direction_wake(c, *, yaw=1.0, age=0., moving=False):
    r = c.r
    for second in (1., 2., 3.):
        r.pulse(second, count=0, moving=moving and second == 2.)
    from rclpy.duration import Duration
    stamp = (r.io.get_clock().now() - Duration(seconds=age)).to_msg()
    r.inputs.publish(SpeechInputStatus(state='wake', utterance_id='direction-wake',
        direction_valid=True, direction_yaw=yaw, direction_stamp=stamp))
    r.pump(lambda: bool(c.goals))
    return c.goals[-1].request


def test_wake_rotates_then_reuses_follower_only_after_standstill(called):
    c, r = called, called.r
    request = direction_wake(c)
    assert request.capability_id == 'spin' and request.idle_only
    arguments = json.loads(request.arguments_yaml)
    assert arguments == dict(target_yaw=1., time_allowance=dict(sec=20, nanosec=0))
    assert c.controller.phase == 'orienting' and not r.runtime.provider.calls
    c.resources(True)
    c.finish_motion(True, release=False)
    assert len(c.goals) == 1 and c.controller.phase == 'settling'
    c.resources(False)
    r.pulse(r.clock[0] + .1)
    r.pump(lambda: len(c.goals) == 2)
    assert c.goals[1].request.capability_id == 'follow_person'
    assert json.loads(c.goals[1].request.arguments_yaml)['approach_only'] is True
    c.resources(True)
    c.finish_motion(True, count=1)
    r.pump(lambda: any(s.text == '왔어요. 말씀하세요.' for s in r.speech))
    assert len(c.goals) == 2 and not r.runtime.provider.calls


@pytest.mark.parametrize('yaw,age', [(float('nan'), 0.), (4., 0.), (1., 5.),
                                    (1., -2.), (.01, 0.), (1., 3.)])
def test_invalid_stale_future_small_or_pre_standstill_angle_skips_rotation(called, yaw, age):
    request = direction_wake(called, yaw=yaw, age=age)
    assert request.capability_id == 'follow_person'


@pytest.mark.parametrize('yaw', [-math.pi, math.pi])
def test_rear_direction_survives_float32_ros_rounding(called, yaw):
    request = direction_wake(called, yaw=yaw)
    assert request.capability_id == 'spin'
    angle = json.loads(request.arguments_yaml)['target_yaw']
    assert angle == pytest.approx(yaw) and abs(angle) <= math.pi


def test_blocked_rotation_never_starts_person_approach(called):
    c = called
    direction_wake(c)
    c.resources(True)
    c.finish_motion(False)
    c.r.pump(lambda: not c.controller.active)
    assert len(c.goals) == 1
    c.r.pump(lambda: any('회전하지 못했어요' in s.text for s in c.r.speech))


def test_speech_during_rotation_cancels_and_replies_after_stop(called):
    c, r = called, called.r
    direction_wake(c)
    c.resources(True)
    r.inputs.publish(SpeechInputStatus(session_id=c.controller.session_id,
        state='started', utterance_id='during-spin'))
    r.pump(lambda: bool(c.cancellations))
    c.answer('그냥 여기서 이야기하자', uid='during-spin')
    r.pump(lambda: c.controller.pending is not None)
    assert not r.runtime.provider.calls
    c.terminals[-1].set_result(False)
    r.pump(lambda: not r.chat.position.busy)
    c.resources(False)
    for _ in range(6):
        r.pulse(r.clock[0] + .5, count=1)
    r.pump(lambda: len(r.runtime.provider.calls) == 1)
    assert len(c.goals) == 1


def test_unseen_caller_room_navigation_reacquisition_and_stationary_dialogue(called):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    r.pump(lambda: c.controller.phase == 'asking' and bool(r.speech))
    assert '어디 계세요' in r.speech[-1].text
    c.finish_speech()
    c.answer('거실 소파 옆이야')
    r.pump(lambda: c.controller.phase == 'announcing')
    assert len(c.goals) == 1  # Announce before dispatching navigation.
    c.finish_speech()
    r.pump(lambda: len(c.goals) == 2)
    assert c.goals[1].request.capability_id == 'navigate_to_pose'
    pose = json.loads(c.goals[1].request.arguments_yaml)['pose']['pose']['position']
    assert pose == dict(x=0., y=0., z=0.)  # Saved map point, not the sofa's guessed position.
    c.resources(True)
    c.finish_motion(True)
    r.pump(lambda: len(c.goals) == 3)
    assert c.goals[2].request.capability_id == 'follow_person'
    c.resources(True)
    c.finish_motion(True, count=1)
    r.pump(lambda: any(s.text == '왔어요. 말씀하세요.' for s in r.speech))
    assert not r.runtime.provider.calls  # No LLM/model added for search or room matching.
    assert not r.chat.position.busy and r.chat.position.released


@pytest.mark.parametrize('answer', ['여기!', '거실에 없어', '거실 말고 안방', '창고', '0,0'])
def test_ambiguous_location_never_dispatches_coordinates(called, answer):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    c.answer(answer)
    r.pump(lambda: c.controller.questions == 2)
    assert len(c.goals) == 1 and not r.runtime.provider.calls


def test_speech_during_approach_cancels_and_waits_for_terminal_before_reply(called):
    c, r = called, called.r
    c.wake(count=1)
    r.inputs.publish(SpeechInputStatus(session_id=c.controller.session_id,
        state='started', utterance_id='early-speech'))
    r.pump(lambda: bool(c.cancellations))
    c.answer('오늘 산책했어', uid='early-speech')
    r.pump(lambda: c.controller.pending is not None)
    assert not r.runtime.provider.calls and r.chat.position.busy
    c.terminals[-1].set_result(False)
    r.pump(lambda: not r.chat.position.busy)
    c.resources(False)
    for _ in range(6):
        r.pulse(r.clock[0] + .5, count=1)
    r.pump(lambda: len(r.runtime.provider.calls) == 1)
    assert len(c.goals) == 1


def test_map_edit_between_answer_and_departure_prevents_navigation(called):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    c.answer('거실')
    r.pump(lambda: c.controller.phase == 'announcing')
    rooms(c.document)[0]['properties']['representative_point'] = [1., 1.]
    rewrite(c.catalog, c.document)
    c.finish_speech()
    r.pump(lambda: not c.controller.active)
    assert len(c.goals) == 1


def test_failed_room_navigation_does_not_claim_to_have_found_person(called):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    c.answer('거실')
    r.pump(lambda: c.controller.phase == 'announcing')
    c.finish_speech()
    r.pump(lambda: len(c.goals) == 2)
    c.resources(True)
    c.finish_motion(False)
    r.pump(lambda: not c.controller.active)
    assert len(c.goals) == 2
    assert not any(s.text == '왔어요. 말씀하세요.' for s in r.speech)


def test_no_person_at_destination_does_not_roam_through_more_rooms(called):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    c.answer('거실')
    r.pump(lambda: c.controller.phase == 'announcing')
    c.finish_speech()
    r.pump(lambda: len(c.goals) == 2)
    c.resources(True)
    c.finish_motion(True)
    r.pump(lambda: len(c.goals) == 3)
    c.resources(True)
    c.finish_motion(False)
    r.pump(lambda: not c.controller.active)
    assert len(c.goals) == 3
    assert not any(s.text == '왔어요. 말씀하세요.' for s in r.speech)


@pytest.mark.parametrize('target_x', [2., 2.5, 2.9])
def test_kinematic_approach_uses_real_standoff_policy_before_dialogue(called, target_x):
    """Drive simulated odometry using the production follower's completion policy."""
    from malbut_tracking.conversation_approach import ConversationApproach
    from malbut_tracking.geometry import Point2D

    c, r = called, called.r
    c.wake(count=1)
    policy = ConversationApproach(r.clock[0], 1.5)
    target, x, yaw_error = Point2D(target_x, 0.), 0., .3
    reached = False
    for _ in range(90):
        now = r.clock[0] + .2
        moving = x < target_x - 1.5 or yaw_error > .05
        x = min(x + .05, target_x - 1.5)
        if x == target_x - 1.5:
            yaw_error = max(.0, yaw_error - .05)
        assert policy.observe(now, target, Point2D(x, 0.), 'person-1', metric=True) is None
        r.pulse(now, count=1, moving=moving, mission_id=r.chat.position.mission_id)
        reached = policy.reached(now, target_x - x, yaw_error)
        assert not r.speech
        if reached:
            break
    assert reached and abs(target_x - x - 1.5) <= .2 and yaw_error <= .1
    c.finish_motion(True, count=1)
    r.pump(lambda: any(s.text == '왔어요. 말씀하세요.' for s in r.speech))
    assert r.chat.position.released


@pytest.mark.parametrize('text', ['멈춰', '오지 마'])
def test_cancel_during_navigation_waits_for_actual_result(called, text):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    c.answer('거실')
    r.pump(lambda: c.controller.phase == 'announcing')
    c.finish_speech()
    r.pump(lambda: len(c.goals) == 2)
    c.resources(True)
    session = c.controller.session_id
    c.answer(text, 'stop-navigation')
    r.pump(lambda: bool(c.cancellations))
    assert c.controller.active and r.chat.position.busy
    r.pump(lambda: (session, False) in r.controls)
    c.terminals[-1].set_result(False)
    r.pump(lambda: not r.chat.position.busy)
    c.resources(False)
    for _ in range(6):
        r.pulse(r.clock[0] + .5, count=0)
    r.pump(lambda: not c.controller.active)
    assert len(c.goals) == 2 and not r.runtime.provider.calls


def test_silence_closes_location_question_without_another_mission(called):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    session = c.controller.session_id
    for _ in range(70):
        r.pulse(r.clock[0] + .5, count=0)
    r.pump(lambda: not c.controller.active)
    assert (session, False) in r.controls and len(c.goals) == 1
    assert sum('어디 계세요' in s.text for s in r.speech) == 1


def test_map_change_while_navigating_cancels_owned_navigation(called):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    c.answer('거실')
    r.pump(lambda: c.controller.phase == 'announcing')
    c.finish_speech()
    r.pump(lambda: len(c.goals) == 2)
    c.resources(True)
    r.agent.speech_missions.observe_localization({'mode': 'MAPPING', 'map': str(c.selected)})
    for _ in range(6):
        r.pulse(r.clock[0] + .5, count=0, mission_id=r.chat.position.mission_id)
    r.pump(lambda: bool(c.cancellations))
    assert len(c.goals) == 2 and not r.runtime.provider.calls


def test_late_started_event_after_location_transcript_does_not_stall_navigation(called):
    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    c.answer('거실')
    r.pump(lambda: c.controller.phase == 'announcing')
    r.inputs.publish(SpeechInputStatus(session_id=c.controller.session_id,
        state='started', utterance_id='location-one'))
    c.finish_speech()
    r.pump(lambda: len(c.goals) == 2)
    assert not c.controller.input_started


def test_duplicate_wake_does_not_start_another_tracker(called):
    c, r = called, called.r
    c.wake(count=1)
    for uid in ('wake-one', 'another-wake'):
        r.inputs.publish(SpeechInputStatus(state='wake', utterance_id=uid))
    r.pulse(r.clock[0] + .5)
    assert len(c.goals) == 1


def test_explicit_wake_still_works_in_quiet_hours(called, monkeypatch):
    monkeypatch.setattr(called.r.chat, '_quiet', lambda: True)
    called.wake(count=1)
    assert len(called.goals) == 1


@pytest.mark.parametrize('blocker', ['disabled', 'camera', 'other_mission'])
def test_ineligible_wake_leaves_existing_listening_alone(called, monkeypatch, blocker):
    c, r = called, called.r
    for now in (1., 2., 3.):
        r.pulse(now)
    if blocker == 'disabled':
        c.controller.enabled = False
    elif blocker == 'camera':
        monkeypatch.setattr(r.agent._scene._images, 'permitted', lambda: False)
    else:
        r.pulse(3.1, mission_id='someone-elses-mission')
    r.inputs.publish(SpeechInputStatus(state='wake', utterance_id='blocked-wake'))
    r.pump(lambda: 'blocked-wake' in c.controller.seen)
    assert not c.controller.active and not c.goals and not r.controls


def test_manager_busy_rejection_needs_no_nonexistent_runtime_transition():
    from concurrent.futures import Future as DoneFuture
    from unittest.mock import Mock
    from action_msgs.msg import GoalStatus
    from malbut_agent_server.conversation_position_ros import ConversationPosition

    p = ConversationPosition.__new__(ConversationPosition)
    p.node, p.cancel_requested, p.navigation = Mock(), False, False
    p.node.count_publishers.return_value = 1
    p.runtime_idle, p.runtime_revision, p.start_revision = True, 1, 1
    result = DoneFuture()
    result.set_result(SimpleNamespace(status=GoalStatus.STATUS_ABORTED,
        result=ExecuteMission.Result(message='conversation positioning skipped: manager is busy')))
    p._finished(result)
    assert not p.success and p.released


@pytest.mark.parametrize('accepted', [True, False])
def test_fallback_waits_for_microphone_close_acknowledgement(called, monkeypatch, accepted):
    from concurrent.futures import Future as DoneFuture

    c, r = called, called.r
    c.wake()
    c.finish_motion(False)
    c.finish_speech()
    closed = DoneFuture()
    session = c.controller.session_id
    original = r.chat._input.call_async

    def delayed_close(request):
        if request.session_id == session and not request.active:
            return closed
        return original(request)

    monkeypatch.setattr(r.chat._input, 'call_async', delayed_close)
    before = len(r.speech)
    c.controller._finish('여기서 말씀을 들을게요.')
    r.pulse(r.clock[0] + .1, count=0)
    assert c.controller.phase == 'finishing' and len(r.speech) == before
    closed.set_result(SimpleNamespace(accepted=accepted))
    r.pump(lambda: not c.controller.active)
    if accepted:
        r.pump(lambda: len(r.speech) == before + 1)
        assert r.speech[-1].request_id.startswith('conversation-opener:')
    else:
        assert len(r.speech) == before
