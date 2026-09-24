"""Recover replayed VLM handoffs after losing the Manager Action client."""

import json

import pytest

from test_situation_action_ros import rig as _confirmation_rig

rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')
from action_msgs.msg import GoalStatus  # noqa: E402
from malbut_interfaces.action import ConfirmSituation  # noqa: E402
from malbut_interfaces.msg import SpeechInputStatus, SpeechPlaybackStatus  # noqa: E402
from rclpy.action import ActionClient  # noqa: E402
from rclpy.node import Node  # noqa: E402
from std_msgs.msg import String  # noqa: E402


# Register locally even when pytest already collected the source test module
# as a package module; a plugin registration can otherwise inherit its scope.
rig = _confirmation_rig


def replay(rig, request_id, *, summary='거실 바닥에 누워 있는 사람이 관측됨',
           situation_type='fall'):
    """A new Manager client has no original Goal UUID or audio readiness gate."""
    client = ActionClient(rig.voice, ConfirmSituation, '/malbut/agent/confirm_situation')
    try:
        rig.spin_until(client.server_is_ready)
        sent = client.send_goal_async(ConfirmSituation.Goal(
            request_id=request_id, situation_type=situation_type, summary=summary,
        ))
        rig.spin_until(sent.done)
        handle = sent.result()
        if not handle.accepted:
            return False, None
        result = handle.get_result_async()
        rig.spin_until(result.done)
        return True, result.result()
    finally:
        client.destroy()


@pytest.mark.parametrize('restart_after_result', [False, True])
def test_manager_restart_recovers_same_handoff_without_repeating_dialogue(
    rig, restart_after_result,
):
    from malbut_system_manager.fall_confirmation_link import FallConfirmationLink
    from malbut_agent_server.fall_runtime import apply_decision, event_metadata
    from test_fall_confirmation_result import assessed

    monitor, _, _, iid = assessed()
    question = next(e for e in monitor.drain_events() if e.kind == 'question_requested')
    payload = String(data=json.dumps(dict(
        event_metadata(question), boot_id=monitor.boot_id, runtime_id='recovery-vlm',
    )))
    events = rig.voice.create_publisher(String, '/malbut/falls/runtime/events', 50)
    received = []
    rig.voice.create_subscription(
        String, '/malbut/falls/runtime/decision', lambda msg: received.append(msg.data), 10,
    )
    manager = Node('confirmation_manager_before_restart')
    link = FallConfirmationLink(manager, runtime_id='recovery-vlm')
    rig.executor.add_node(manager)
    try:
        rig.spin_until(lambda: events.get_subscription_count() > 0
                       and link.client.server_is_ready() and rig.agent.dialogue.ready
                       and link.decisions.get_subscription_count() > 0)
        events.publish(payload)
        rig.spin_until(lambda: rig.agent.situation._session is not None
                       and rig.agent.situation._session.phase == 'listening')
        if restart_after_result:
            rig.answer('그냥 누워 있는 거야')
            rig.spin_until(lambda: bool(received) and not rig.agent.situation.active)
            # Simulate losing the final topic publication before the VLM
            # applies it. The original question therefore remains pending.
            received.clear()
        # A crashed Manager cannot issue cancellation during teardown. Drop
        # its ROS entities directly, retaining the Agent's current dialogue.
        link.timer.cancel()
        rig.executor.remove_node(manager)
        link.client.destroy()
        manager.destroy_node()
        manager = Node('confirmation_manager_after_restart')
        link = FallConfirmationLink(manager, runtime_id='recovery-vlm')
        rig.executor.add_node(manager)
        rig.spin_until(lambda: link.client.server_is_ready()
                       and manager.count_publishers('/malbut/falls/runtime/events') > 0
                       and link.decisions.get_subscription_count() > 0)
        events.publish(payload)
        rig.spin_until(lambda: bool(link.coordinator.requests) or bool(received))
        if not restart_after_result:
            rig.answer('그냥 누워 있는 거야')
        rig.spin_until(lambda: bool(received), timeout=5)
        command = json.loads(received[-1])
        assert command['action'] == 'confirmation_result'
        assert command['situation_assessment'] == 'resolved'
        assert command['help_needed'] is False
        assert apply_decision(monitor, received[-1])
        assert monitor.incident(iid).situation_assessment == 'resolved'
        assert not monitor.pending_questions()
        rig.spin_until(lambda: not rig.agent.situation.active)
        assert len(rig.spoken) == 2  # One question and one closing sentence.
    finally:
        link.destroy()
        rig.executor.remove_node(manager)
        manager.destroy_node()


def test_replayed_completed_request_returns_original_result_without_audio(rig):
    handle = rig.start('completed-replay')
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    rig.answer('도와줘')
    original = handle.get_result_async()
    rig.spin_until(lambda: original.done() and not rig.agent.situation.active)
    spoken, controls = len(rig.spoken), len(rig.controls)
    replay = rig.start('completed-replay')
    assert replay.accepted
    result = replay.get_result_async()
    rig.spin_until(result.done)
    assert result.result().status == GoalStatus.STATUS_SUCCEEDED
    assert result.result().result == original.result().result
    assert len(rig.spoken) == spoken
    assert len(rig.controls) == controls
    assert rig.agent.dialogue.has_capacity()


def test_replay_during_closing_preserves_the_active_audio_lifecycle(rig, monkeypatch):
    handle = rig.start('closing-replay')
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    rig.finish = False
    rig.answer('그냥 누워 있는 거야')
    original = handle.get_result_async()
    rig.spin_until(lambda: original.done() and len(rig.spoken) == 2)
    session = rig.agent.situation._session
    owner = rig.agent.situation._active_goal
    monkeypatch.setattr(rig.agent.situation._input, 'service_is_ready', lambda: False)
    monkeypatch.setattr(rig.agent.situation._playback, 'service_is_ready', lambda: False)
    accepted, result = replay(rig, 'closing-replay')
    assert accepted and result.status == GoalStatus.STATUS_SUCCEEDED
    assert result.result == original.result().result
    assert rig.agent.situation._session is session
    assert rig.agent.situation._active_goal is owner
    assert rig.agent.situation.active and not rig.agent.dialogue.has_capacity()
    assert len(rig.spoken) == 2
    # Closing audio failure does not invalidate the judgment already sent.
    rig.playback.publish(SpeechPlaybackStatus(
        playback_id=rig.spoken[-1].playback_id, state='failed',
    ))
    rig.spin_until(lambda: not rig.agent.situation.active)
    accepted, result = replay(rig, 'closing-replay')
    assert accepted and result.status == GoalStatus.STATUS_SUCCEEDED
    assert result.result.situation_assessment == 'resolved'


@pytest.mark.parametrize('change', [
    {'summary': '다른 사람의 다른 상황'}, {'situation_type': 'smoke'},
])
def test_same_id_with_different_payload_cannot_receive_a_cached_judgment(rig, change):
    handle = rig.start('conflicting-id')
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    rig.answer('도와줘')
    result = handle.get_result_async()
    rig.spin_until(lambda: result.done() and not rig.agent.situation.active)
    accepted, _ = replay(rig, 'conflicting-id', **change)
    assert not accepted
    assert len(rig.spoken) == 2


@pytest.mark.parametrize('terminal', ['aborted', 'canceled'])
def test_replay_of_operational_failure_remains_an_unsuccessful_action(rig, terminal):
    handle = rig.start('failed-replay')
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    if terminal == 'aborted':
        rig.inputs.publish(SpeechInputStatus(
            session_id=rig.session_id, utterance_id='', state='failed',
        ))
    else:
        canceled = handle.cancel_goal_async()
        rig.spin_until(canceled.done)
    original = handle.get_result_async()
    rig.spin_until(lambda: original.done() and not rig.agent.situation.active)
    accepted, result = replay(rig, 'failed-replay')
    assert accepted
    # A fresh Goal cannot transition directly from EXECUTING to CANCELED.
    assert result.status == GoalStatus.STATUS_ABORTED
    assert result.result.situation_assessment == ''
    assert len(rig.spoken) == 1


def test_in_progress_duplicate_cannot_start_another_conversation(rig):
    handle = rig.start('pending-replay')
    rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
    owner = rig.agent.situation._session
    accepted, _ = replay(rig, 'pending-replay')
    assert not accepted
    assert rig.agent.situation._session is owner
    assert len(rig.spoken) == 1
    canceled = handle.cancel_goal_async()
    rig.spin_until(canceled.done)


def test_result_receipts_evict_oldest_entries_at_the_configured_bound(rig, monkeypatch):
    monkeypatch.setattr('malbut_agent_server.ros_situation.MAX_CONFIRMATION_RECEIPTS', 2)
    for request_id in ('first', 'second', 'third'):
        handle = rig.start(request_id)
        rig.spin_until(lambda: rig.agent.situation._session.phase == 'listening')
        rig.answer('그냥 누워 있는 거야')
        result = handle.get_result_async()
        rig.spin_until(lambda: result.done() and not rig.agent.situation.active)
    assert list(rig.agent.situation._recent) == ['second', 'third']
    assert len(rig.spoken) == 6
