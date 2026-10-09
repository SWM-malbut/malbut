"""Public Action correlation and cancellation without a ROS graph."""

from concurrent.futures import Future, ThreadPoolExecutor
import json
import sys
from threading import Barrier
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from malbut_agent_server import manager_client


class GoalUUID:
    """Mimic only the generated UUID field used on the wire."""

    def __init__(self, uuid):
        self.uuid = uuid


class GoalHandle:
    """Allow result and cancellation responses to arrive independently."""

    def __init__(self, goal_id, accepted=True):
        self.goal_id = goal_id
        self.accepted = accepted
        self.result_future = Future()
        self.cancel_futures = []
        self.result_error = None
        self.cancel_error = None

    def get_result_async(self):
        if self.result_error:
            raise self.result_error
        return self.result_future

    def cancel_goal_async(self):
        if self.cancel_error:
            raise self.cancel_error
        future = Future()
        self.cancel_futures.append(future)
        return future


class FakeActionClient:
    """Capture actual Goal payloads and independently delay acceptance."""

    def __init__(self, node, action_type, action_name):
        self.ready = True
        self.requests = []
        self.destroyed = False
        self.send_error = None
        node.client = self
        assert action_name == '/malbut/mission/execute'

    def server_is_ready(self):
        return self.ready

    def send_goal_async(self, goal, *, goal_uuid, feedback_callback):
        record = SimpleNamespace(
            goal=goal, goal_id=goal_uuid, feedback=feedback_callback,
            future=Future(),
        )
        self.requests.append(record)
        if self.send_error:
            raise self.send_error
        return record.future

    def destroy(self):
        self.destroyed = True


class FakeNode:
    """Expose one manually ticked timer and lifecycle observations."""

    def __init__(self):
        self.errors = []
        self.timer_destroyed = False
        self.timer_canceled = False

    def create_timer(self, period, callback):
        self.tick = callback
        self.timer_period = period
        return SimpleNamespace(cancel=self.cancel_timer)

    def cancel_timer(self):
        self.timer_canceled = True

    def destroy_timer(self, timer):
        self.timer_destroyed = True

    def get_logger(self):
        return SimpleNamespace(error=self.errors.append)


@pytest.fixture
def harness(monkeypatch):
    """Supply just the ROS symbols imported by the opt-in client."""
    modules = {
        name: ModuleType(name) for name in (
            'action_msgs', 'action_msgs.msg', 'malbut_interfaces',
            'malbut_interfaces.action', 'rclpy', 'rclpy.action',
            'unique_identifier_msgs', 'unique_identifier_msgs.msg',
        )
    }
    modules['action_msgs.msg'].GoalStatus = SimpleNamespace(
        STATUS_SUCCEEDED=4, STATUS_CANCELED=5, STATUS_ABORTED=6,
    )
    modules['malbut_interfaces.action'].ExecuteMission = SimpleNamespace(
        Goal=SimpleNamespace,
    )
    modules['rclpy.action'].ActionClient = FakeActionClient
    modules['unique_identifier_msgs.msg'].UUID = GoalUUID
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    now = [10.0]
    monkeypatch.setattr(manager_client.time, 'monotonic', lambda: now[0])
    events = []
    node = FakeNode()
    client = manager_client.ManagerClient(node, on_event=events.append)
    value = SimpleNamespace(client=client, node=node, events=events, now=now)
    yield value
    client.close()


def submit(harness, request_id='one', **arguments):
    """Submit an explicit development request, without inferred identity."""
    harness.client.submit('follow_person', arguments, request_id)
    return harness.node.client.requests[-1]


def accept(request, accepted=True):
    handle = GoalHandle(request.goal_id, accepted)
    request.future.set_result(handle)
    return handle


def feedback(
    request, state='RUNNING', mission_id=None, payload='state: TRACKING',
):
    request.feedback(SimpleNamespace(
        goal_id=request.goal_id,
        feedback=SimpleNamespace(
            mission_id=(mission_id if mission_id is not None
                        else bytes(request.goal_id.uuid).hex()),
            state=state, feedback_yaml=payload,
        ),
    ))


def result(
    handle, status=4, mission_id=None, payload='success: true', message='',
):
    handle.result_future.set_result(SimpleNamespace(
        status=status,
        result=SimpleNamespace(
            mission_id=(mission_id if mission_id is not None
                        else bytes(handle.goal_id.uuid).hex()),
            result_yaml=payload, message=message,
        ),
    ))


def cancel_response(handle, return_code=0, goal_id=None):
    handle.cancel_futures[-1].set_result(SimpleNamespace(
        return_code=return_code,
        goals_canceling=(
            [SimpleNamespace(goal_id=goal_id or handle.goal_id)]
            if return_code == 0 else []
        ),
    ))


def test_goal_fields_identity_progress_and_result_are_separate(harness):
    request = submit(harness, target_mode=1, target_person_id='등록된 사람',
                     desired_distance_m=1.5)
    assert vars(request.goal).keys() == {'capability_id', 'arguments_yaml'}
    assert yaml.safe_load(request.goal.arguments_yaml) == {
        'target_mode': 1, 'target_person_id': '등록된 사람',
        'desired_distance_m': 1.5,
    }
    snapshot = harness.client.snapshot('one')
    assert snapshot['state'] == 'SUBMITTING'
    assert snapshot['goal_id'] == bytes(request.goal_id.uuid).hex()
    assert snapshot['goal_id'] != snapshot['request_id']
    handle = accept(request)
    assert harness.client.snapshot('one')['state'] == 'ACCEPTED'
    assert not harness.client.snapshot('one')['terminal']
    for state in ('PENDING', 'RUNNING', 'CANCELING', 'SUSPENDED', 'RUNNING'):
        feedback(request, state)
        assert harness.client.snapshot('one')['state'] == state
    raw = 'success: true\nfinal_state: STOPPED\nmessage: 완료\n'
    result(handle, payload=raw)
    snapshot = harness.client.snapshot('one')
    assert snapshot['kind'] == 'succeeded'
    assert snapshot['result_yaml'] == raw
    assert snapshot['ros_status'] == 4
    assert snapshot['terminal']
    assert 'physical_stopped' not in snapshot
    assert len(harness.node.client.requests) == 1
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_feedback_before_acceptance_does_not_regress_state(harness):
    request = submit(harness)
    feedback(request)
    accept(request)
    assert harness.client.snapshot('one')['state'] == 'RUNNING'


@pytest.mark.parametrize('status,kind', [
    (4, 'succeeded'), (5, 'canceled'), (6, 'failed'),
])
def test_final_outcomes_preserve_reason_and_ignore_late_feedback(
    harness, status, kind,
):
    request = submit(harness)
    handle = accept(request)
    result(handle, status=status, message='reason from Manager')
    before = harness.client.snapshot('one')
    feedback(request, 'RUNNING')
    assert harness.client.snapshot('one') == before
    assert before['kind'] == kind
    assert before['reason'] == 'reason from Manager'


def test_accepted_goal_can_later_fail_manager_input_validation(harness):
    request = submit(harness, unsupported_input='bad')
    handle = accept(request)
    result(handle, status=6, payload='', message=(
        'Unknown input field(s): unsupported_input'
    ))
    assert [event['kind'] for event in harness.events] == [
        'submitted', 'accepted', 'failed',
    ]
    assert harness.client.snapshot('one')['accepted'] is True


@pytest.mark.parametrize('ready,expected', [
    (False, 'unavailable'), (True, 'rejected'),
])
def test_no_server_and_goal_rejection_are_distinct(harness, ready, expected):
    harness.node.client.ready = ready
    harness.client.submit('follow_person', {}, 'one')
    if ready:
        accept(harness.node.client.requests[0], accepted=False)
    else:
        assert harness.node.client.requests == []
    assert harness.client.snapshot('one')['kind'] == expected
    assert harness.client.snapshot('one')['terminal']


def test_duplicate_submission_is_idempotent_and_conflicts_fail(harness):
    submit(harness, target_mode=1, desired_distance_m=1.0)
    assert harness.client.submit('follow_person', {
        'desired_distance_m': 1.0, 'target_mode': 1,
    }, 'one') == 'one'
    for capability, arguments in [('patrol', {}), ('follow_person', {})]:
        with pytest.raises(ValueError, match='different input'):
            harness.client.submit(capability, arguments, 'one')
    assert len(harness.node.client.requests) == 1


def test_competing_submissions_send_only_one_goal(harness):
    barrier = Barrier(2)

    def send():
        barrier.wait(timeout=5)
        return harness.client.submit('follow_person', {}, 'one')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send), pool.submit(send)]
        results = [future.result(timeout=5) for future in futures]
    assert results == ['one', 'one']
    assert len(harness.node.client.requests) == 1


@pytest.mark.parametrize('capability,arguments,request_id', [
    ('', {}, 'id'), (' \n', {}, 'id'), ('follow_person', [], 'id'),
    ('follow_person', {1: 'bad'}, 'id'), ('follow_person', {}, ''),
    ('follow_person', {'value': object()}, 'id'),
])
def test_invalid_local_input_never_sends(
    harness, capability, arguments, request_id,
):
    with pytest.raises(ValueError):
        harness.client.submit(capability, arguments, request_id)
    assert not harness.node.client.requests


def test_goal_timeout_retains_cancel_for_late_acceptance(harness):
    request = submit(harness)
    harness.now[0] += 6
    harness.node.tick()
    harness.node.tick()
    kinds = [event['kind'] for event in harness.events]
    assert kinds == ['submitted', 'unknown']
    assert not harness.client.snapshot('one')['terminal']
    harness.client.cancel('one')
    harness.client.cancel('one')
    feedback(request)
    assert harness.client.snapshot('one')['state'] == 'UNKNOWN'
    handle = accept(request)
    assert len(handle.cancel_futures) == 1
    cancel_response(handle)
    assert harness.client.snapshot('one')['kind'] == 'cancel_accepted'
    assert not harness.client.snapshot('one')['terminal']
    result(handle, status=5)
    assert harness.client.snapshot('one')['kind'] == 'canceled'
    assert len(harness.node.client.requests) == 1


def test_cancel_is_not_terminal_and_waits_for_its_own_goal(harness):
    first = accept(submit(harness))
    second = accept(submit(harness, request_id='two'))
    harness.client.cancel('one')
    assert len(first.cancel_futures) == 1
    assert not second.cancel_futures
    cancel_response(first)
    snapshot = harness.client.snapshot('one')
    assert snapshot['kind'] == 'cancel_accepted'
    assert not snapshot['terminal']
    assert snapshot['state'] == 'ACCEPTED'
    result(first, status=5)
    assert harness.client.snapshot('two')['state'] == 'ACCEPTED'


@pytest.mark.parametrize('cancel_first', [True, False])
def test_cancel_and_success_race_reports_final_status(harness, cancel_first):
    handle = accept(submit(harness))
    harness.client.cancel('one')
    if cancel_first:
        cancel_response(handle)
    result(handle)
    if not cancel_first:
        cancel_response(handle)
    assert harness.client.snapshot('one')['kind'] == 'succeeded'
    assert harness.client.snapshot('one')['cancel_requested']


def test_rejected_cancellation_may_be_explicitly_requested_again(harness):
    handle = accept(submit(harness))
    harness.client.cancel('one')
    cancel_response(handle, return_code=1)
    assert harness.client.snapshot('one')['kind'] == 'cancel_rejected'
    assert not harness.client.snapshot('one')['terminal']
    harness.client.cancel('one')
    assert len(handle.cancel_futures) == 2
    cancel_response(handle)
    harness.client.cancel('one')
    assert len(handle.cancel_futures) == 2


def test_cancel_timeout_retains_same_goal_without_automatic_resend(harness):
    handle = accept(submit(harness))
    harness.client.cancel('one')
    harness.now[0] += 6
    harness.node.tick()
    assert harness.client.snapshot('one')['kind'] == 'cancel_unknown'
    harness.client.cancel('one')
    assert len(handle.cancel_futures) == 1
    cancel_response(handle)
    assert harness.client.snapshot('one')['kind'] == 'cancel_accepted'


def test_foreign_cancel_response_is_not_accepted(harness):
    handle = accept(submit(harness))
    harness.client.cancel('one')
    cancel_response(handle, goal_id=GoalUUID(list(uuid4().bytes)))
    assert harness.client.snapshot('one')['kind'] == 'cancel_unknown'


@pytest.mark.parametrize('status,mission_id,payload', [
    (4, 'another-mission', 'success: true'),
    (0, None, ''),
    (4.0, None, ''),
    (4, None, {'success': True}),
])
def test_invalid_final_result_never_succeeds_or_accepts_late_progress(
    harness, status, mission_id, payload,
):
    request = submit(harness)
    handle = accept(request)
    result(handle, status=status, mission_id=mission_id, payload=payload)
    before = harness.client.snapshot('one')
    assert before['state'] == 'UNKNOWN'
    assert not before['terminal']
    feedback(request)
    assert harness.client.snapshot('one') == before
    # A malformed result does not prevent canceling our known Goal handle.
    harness.client.cancel('one')
    assert len(handle.cancel_futures) == 1


def test_mismatched_feedback_does_not_become_current_mission_state(harness):
    request = submit(harness)
    accept(request)
    feedback(request, mission_id='another-mission')
    assert harness.client.snapshot('one')['state'] == 'UNKNOWN'
    assert harness.client.snapshot('one')['mission_id'] is None


def test_invalid_acceptance_handle_is_not_used_to_cancel_another_goal(harness):
    request = submit(harness)
    harness.client.cancel('one')
    handle = GoalHandle(GoalUUID(list(uuid4().bytes)))
    request.future.set_result(handle)
    assert harness.client.snapshot('one')['state'] == 'UNKNOWN'
    assert not handle.cancel_futures


@pytest.mark.parametrize('failure', [
    'send', 'goal_response', 'get_result', 'result', 'cancel',
])
def test_async_errors_never_retry_or_claim_completion(harness, failure):
    if failure == 'send':
        harness.node.client.send_error = RuntimeError('transport failed')
    request = submit(harness)
    if failure == 'goal_response':
        request.future.set_exception(RuntimeError('lost response'))
    elif failure not in ('send', 'goal_response'):
        handle = GoalHandle(request.goal_id)
        if failure == 'get_result':
            handle.result_error = RuntimeError('result unavailable')
        request.future.set_result(handle)
        if failure == 'result':
            handle.result_future.set_exception(RuntimeError('result lost'))
        elif failure == 'cancel':
            handle.cancel_error = RuntimeError('cancel lost')
            harness.client.cancel('one')
    snapshot = harness.client.snapshot('one')
    assert snapshot['kind'] in ('unknown', 'cancel_unknown')
    assert not snapshot['terminal']
    harness.client.submit('follow_person', {}, 'one')
    assert len(harness.node.client.requests) == 1


def test_close_releases_ros_without_requesting_cancellation(harness):
    request = submit(harness)
    handle = accept(request)
    harness.client.close()
    harness.client.close()
    before = list(harness.events)
    feedback(request)
    result(handle)
    harness.node.tick()
    assert harness.events == before
    assert not handle.cancel_futures
    assert harness.node.client.destroyed
    assert harness.node.timer_destroyed and harness.node.timer_canceled
    with pytest.raises(RuntimeError, match='closed'):
        harness.client.submit('follow_person', {})


def test_snapshot_and_event_consumers_cannot_change_record_state(harness):
    submit(harness)
    harness.events[-1]['state'] = 'SUCCEEDED'
    harness.client.snapshot('one')['state'] = 'SUCCEEDED'
    assert harness.client.snapshot('one')['state'] == 'SUBMITTING'


def test_event_callback_failure_cannot_hide_result_observation(harness):
    def fail(event):
        raise ValueError('consumer failed')

    harness.client._on_event = fail
    handle = accept(submit(harness))
    result(handle)
    assert harness.client.snapshot('one')['state'] == 'SUCCEEDED'
    assert len(harness.node.errors) == 3


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf')])
def test_invalid_timeout_is_rejected_before_ros_import(timeout):
    with pytest.raises(ValueError, match='positive and finite'):
        manager_client.ManagerClient(None, on_event=lambda event: None,
                                     goal_response_timeout_s=timeout)
