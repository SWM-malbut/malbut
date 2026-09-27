"""Exercise transport callbacks with fake futures, without ROS or a DDS graph."""

from concurrent.futures import Future
import json
import sys
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from malbut_fall_coordinator.fall_confirmation_link import FallConfirmationLink
from test_fall_confirmation import event


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setitem(sys.modules, 'action_msgs.msg', SimpleNamespace(
        GoalStatus=SimpleNamespace(STATUS_SUCCEEDED=4, STATUS_ABORTED=6),
    ))
    now = SimpleNamespace(value=0.0)
    link = object.__new__(FallConfirmationLink)
    link.node, link.lock, link.clock = Mock(), RLock(), lambda: now.value
    link.goal_response_timeout_s, link.result_timeout_s = 5.0, 610.0
    link.server_loss_timeout_s = 5.0
    link.coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    link.action_type = SimpleNamespace(Goal=SimpleNamespace)
    link.message_type = SimpleNamespace
    link.client, link.decisions, link.timer = Mock(), Mock(), Mock()
    link.client.server_is_ready.return_value = True
    link.client.send_goal_async.side_effect = lambda goal: Future()
    link.agent_presence = Mock()
    link.agent_presence.server_is_ready.return_value = True
    link.manager_state = SimpleNamespace(
        active_foreground_missions=[], active_background_missions=[],
        pending_missions=[], suspended_missions=[],
    )
    link.request = link.goal_future = link.handle = None
    link.sent_at = link.next_attempt = 0.0
    link.accepted_at = link.server_missing_since = None
    result = Future()
    handle = Mock(accepted=True)
    handle.get_result_async.return_value = result
    link.on_event(SimpleNamespace(data=event()))
    yield SimpleNamespace(link=link, now=now, handle=handle, result=result)
    link.close()


def commands(link):
    return [json.loads(call.args[0].data) for call in link.decisions.publish.call_args_list]


def success():
    return SimpleNamespace(status=4, result=SimpleNamespace(
        message='', result_yaml=json.dumps(dict(
            situation_assessment='resolved', help_needed=False,
        )),
    ))


@pytest.mark.parametrize('late_by', [0.0, 0.001])
@pytest.mark.parametrize('phase', ['acceptance', 'result', 'manager_loss', 'agent_loss'])
def test_late_callback_cannot_bypass_transport_deadline(rig, phase, late_by):
    link = rig.link
    if phase != 'acceptance':
        link.goal_future.set_result(rig.handle)
    if phase in ('manager_loss', 'agent_loss'):
        peer = link.client if phase == 'manager_loss' else link.agent_presence
        peer.server_is_ready.return_value = False
        link.tick()
    rig.now.value = (610.0 if phase == 'result' else 5.0) + late_by
    if phase == 'acceptance':
        link.goal_future.set_result(rig.handle)
    else:
        rig.result.set_result(success())
    assert [item['action'] for item in commands(link)] == ['confirmation_failed']
    assert link.request is None and not link.coordinator.requests
    rig.handle.cancel_goal_async.assert_called_once()
    assert all('help_needed' not in item for item in commands(link))


def test_result_before_deadline_is_forwarded_once(rig):
    goal = rig.link.client.send_goal_async.call_args.args[0]
    assert goal.capability_id == 'fall_confirmation'
    assert json.loads(goal.arguments_yaml) == dict(
        request_id=rig.link.request.request_id, situation_type='fall',
        summary=rig.link.request.summary,
    )
    rig.now.value = 4.999
    rig.link.goal_future.set_result(rig.handle)
    rig.now.value += 609.999
    rig.result.set_result(success())
    assert [item['action'] for item in commands(rig.link)] == ['confirmation_result']
    assert commands(rig.link)[0]['help_needed'] is False
    rig.handle.cancel_goal_async.assert_not_called()


def test_obsolete_goal_cancel_failure_does_not_affect_replacement(rig):
    old_future = rig.link.goal_future
    rig.link.on_event(SimpleNamespace(data=event(question_id='next', evidence_revision=2)))
    current_future, current_request = rig.link.goal_future, rig.link.request
    rig.handle.cancel_goal_async.side_effect = RuntimeError('transport unavailable')
    old_future.set_result(rig.handle)
    rig.handle.cancel_goal_async.assert_called_once()
    rig.link.node.get_logger().warning.assert_called_once_with(
        'confirmation_cancel_transport_failed',
    )
    assert rig.link.request == current_request
    assert rig.link.goal_future is current_future
    assert commands(rig.link) == []


def test_late_result_does_not_finish_replacement_request(rig):
    rig.link.goal_future.set_result(rig.handle)
    rig.link.on_event(SimpleNamespace(data=event(question_id='next', evidence_revision=2)))
    current_future, current_request = rig.link.goal_future, rig.link.request
    rig.result.set_result(success())
    assert rig.link.request == current_request
    assert rig.link.goal_future is current_future
    assert commands(rig.link) == []


def test_rejected_goal_can_retry_same_request_without_old_deadline(rig):
    request = rig.link.request
    rig.now.value = 4.0
    rig.link.goal_future.set_result(SimpleNamespace(accepted=False))
    rig.now.value = 4.999
    rig.link.tick()
    assert rig.link.request is None
    rig.now.value = 5.0
    rig.link.tick()
    assert rig.link.request == request
    rig.link.goal_future.set_result(rig.handle)
    rig.result.set_result(success())
    assert [item['action'] for item in commands(rig.link)] == ['confirmation_result']
    rig.handle.cancel_goal_async.assert_not_called()


def test_downstream_rejection_retries_through_manager(rig):
    request = rig.link.request
    rig.link.goal_future.set_result(rig.handle)
    rig.result.set_result(SimpleNamespace(status=6, result=SimpleNamespace(
        message='Downstream Action server rejected the goal', result_yaml='',
    )))
    assert rig.link.request is None
    assert commands(rig.link) == []
    rig.now.value = 1.0
    rig.link.tick()
    assert rig.link.request == request
    assert rig.link.client.send_goal_async.call_count == 2
    rig.link.agent_presence.send_goal_async.assert_not_called()


@pytest.mark.parametrize('accepted', [False, True])
def test_close_returns_transport_future_and_cancels_late_goal(rig, accepted):
    pending = rig.link.goal_future
    cancellation = Future()
    rig.handle.cancel_goal_async.return_value = cancellation
    if accepted:
        pending.set_result(rig.handle)
    assert rig.link.close() is (cancellation if accepted else pending)
    assert rig.link.request is rig.link.handle is rig.link.goal_future is None
    if not accepted:
        rig.handle.cancel_goal_async.assert_not_called()
        pending.set_result(rig.handle)
    rig.handle.cancel_goal_async.assert_called_once()
    rig.result.set_result(success())
    assert commands(rig.link) == []
    rig.link.agent_presence.cancel_goal_async.assert_not_called()
