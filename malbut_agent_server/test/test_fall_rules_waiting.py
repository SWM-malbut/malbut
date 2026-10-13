"""A fall check yields to 직접 움직이기 and never ends unnoticed (2026-10-12).

The Manager refuses or interrupts a NORMAL fall step while the robot is driven by
hand or busy; the step goes again. Only that waiting counts: a minute alerts the
guardian once and the robot goes back to the spot before asking; ten minutes in
all stop trying.
"""

from concurrent.futures import Future
import json
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_agent_server.fall_rules.fall_confirmation import (
    CHECK_WAIT_S, FallConfirmationCoordinator, WAIT_ALERT_S, WAIT_LIMIT_S, WAIT_NOTE_S,
)
from malbut_agent_server.fall_rules import fall_confirmation_link as link_module
from malbut_agent_server.fall_rules.fall_confirmation_link import FallConfirmationLink
from test_fall_rules_confirmation import event

SPOT = dict(x=2.0, y=3.0, frame='map')


def question(**changes):
    return event(revisit_target=SPOT, **changes)


def queued(*events):
    coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    for item in events or (question(),):
        assert coordinator.receive(item)
    return coordinator


def actions(coordinator):
    return [(c['action'], c.get('reason')) for c in coordinator.drain_commands()]


# ------------------------------------------------------------ coordinator


def test_only_waiting_counts_and_a_minute_alerts_once_then_goes_back():
    coordinator = queued()
    request = coordinator.requests['question']
    assert request.revisit_target == (2.0, 3.0) and request.approach_target is None
    coordinator.wait(0.0, 'manual_drive')
    coordinator.wait(1.0, 'manual_drive')
    coordinator.check_waits(WAIT_NOTE_S - 0.1)
    assert actions(coordinator) == []  # A passing refusal leaves no note.
    coordinator.check_waits(WAIT_NOTE_S)
    coordinator.check_waits(WAIT_NOTE_S + 1)
    assert actions(coordinator) == [('check_waiting', 'manual_drive')]  # Once per reason.
    coordinator.check_waits(WAIT_ALERT_S - 0.1)
    assert actions(coordinator) == [] and coordinator.next_work(59.9) == ('confirm', request)
    coordinator.check_waits(WAIT_ALERT_S)
    assert actions(coordinator) == [('check_not_confirmed', 'waited_manual_drive')]
    # The person may have moved: go back to the spot before asking.
    assert coordinator.next_work(WAIT_ALERT_S) == ('revisit', request)
    coordinator.check_waits(WAIT_ALERT_S + 30)
    assert actions(coordinator) == []


def test_time_in_progress_is_not_waiting():
    coordinator = queued()
    coordinator.wait(0.0, 'busy')
    coordinator.resume(40.0)  # 40 s waited; then the question runs ten minutes.
    coordinator.check_waits(640.0)
    assert actions(coordinator) == []
    coordinator.wait(640.0, 'busy')
    coordinator.check_waits(659.9)
    assert actions(coordinator) == [('check_waiting', 'busy')]
    coordinator.check_waits(660.0)  # 40 + 20 s.
    assert actions(coordinator) == [('check_not_confirmed', 'waited_busy')]


def test_every_queued_incident_counts_its_own_minute():
    coordinator = queued(question(), question(incident_id='other', question_id='other-q'))
    coordinator.wait(0.0, 'manual_drive')
    coordinator.check_waits(WAIT_ALERT_S)
    commands = coordinator.drain_commands()
    assert [(c['action'], c['question_id']) for c in commands] == [
        ('check_waiting', 'question'), ('check_not_confirmed', 'question'),
        ('check_not_confirmed', 'other-q')]
    assert coordinator.stages == {'question': 'revisit', 'other-q': 'revisit'}


def test_without_a_spot_it_alerts_and_asks_when_it_can():
    coordinator = queued(event())
    request = coordinator.requests['question']
    coordinator.wait(0.0, 'manual_drive')
    coordinator.check_waits(WAIT_ALERT_S)
    assert actions(coordinator)[-1] == ('check_not_confirmed', 'waited_manual_drive')
    coordinator.resume(WAIT_ALERT_S)
    assert coordinator.next_work(WAIT_ALERT_S) == ('confirm', request)


def test_revisit_reports_and_goes_again_after_another_minute_without_a_second_alert():
    coordinator = queued()
    request = coordinator.requests['question']
    coordinator.wait(0.0, 'manual_drive')
    coordinator.check_waits(60.0)
    coordinator.resume(70.0)
    assert coordinator.revisit_started(request)
    assert coordinator.revisit_done(request, 'arrived', 80.0)
    assert actions(coordinator)[-2:] == [
        ('revisit_started', 'manual_drive'), ('revisit_result', None)]
    assert coordinator.next_work(80.0) is None  # Waits for the runtime's look.
    assert coordinator.receive(event(kind='person_check_completed', reason='person'))
    assert coordinator.next_work(81.0) == ('confirm', request)
    coordinator.wait(81.0, 'busy')
    coordinator.check_waits(140.9)
    assert coordinator.stages['question'] == 'confirm'
    coordinator.check_waits(141.0)
    assert coordinator.stages['question'] == 'revisit'
    assert ('check_not_confirmed', 'waited_busy') not in actions(coordinator)


@pytest.mark.parametrize('kind,reason', [
    ('person_check_completed', 'nobody_there'),
    ('posture_check_completed', 'standing'),
    ('posture_check_completed', 'sitting_off_floor'),
])
def test_revisit_results_drive_back_with_the_revisit_reason(kind, reason):
    coordinator = queued()
    request = coordinator.requests['question']
    coordinator.wait(0.0, 'manual_drive')
    coordinator.check_waits(60.0)
    coordinator.resume(61.0)
    coordinator.revisit_done(request, 'arrived', 70.0)
    if kind == 'posture_check_completed':
        coordinator.receive(event(kind='person_check_completed', reason='partial_body'))
        coordinator.view_done(request, 'arrived', 75.0)
    assert coordinator.receive(event(kind=kind, reason=reason))
    assert not coordinator.requests
    kind_, job = coordinator.next_work(80.0)
    assert kind_ == 'return' and job.reason == 'revisit' and job.target == (2.0, 3.0)


def test_ten_minutes_of_waiting_stop_trying_but_never_cut_a_step():
    coordinator = queued()
    request = coordinator.requests['question']
    coordinator.wait(0.0, 'busy')
    coordinator.resume(500.0)
    coordinator.check_waits(2000.0)  # Running: nothing is cut.
    assert coordinator.requests
    coordinator.wait(2000.0, 'busy')
    coordinator.check_waits(2099.9)
    assert coordinator.requests
    coordinator.check_waits(2100.0)
    assert not coordinator.requests and coordinator.waiting_for is None
    commands = coordinator.drain_commands()
    last = commands[-1]
    assert (last['action'], last['reason']) == ('confirmation_failed', 'time_limit')
    assert not coordinator.fail(request, 'time_limit')
    assert WAIT_LIMIT_S == 600.0


def test_guardian_close_drops_the_wait_and_the_request():
    coordinator = queued()
    coordinator.wait(0.0, 'manual_drive')
    assert coordinator.receive(event(kind='incident_resolved', reason='guardian_closed'))
    assert not coordinator.requests and coordinator.waiting_for is None
    coordinator.check_waits(1000.0)
    assert actions(coordinator) == []
    assert coordinator.status()['active'] is False


@pytest.mark.parametrize('reason', ['speech_unavailable', 'question_error'])
def test_failures_carry_their_reason(reason):
    coordinator = queued()
    assert coordinator.fail(coordinator.requests['question'], reason)
    assert actions(coordinator) == [('confirmation_failed', reason)]


def test_status_covers_the_whole_check_including_gaps():
    near = dict(x=1.0, y=1.0, frame='map')
    coordinator = queued(event(approach_target=near, revisit_target=SPOT))
    request = coordinator.requests['question']
    off = dict(active=False, phase=None, waiting_for=None, alerted=False, target=None)
    assert FallConfirmationCoordinator(runtime_id='vlm').status() == off
    assert coordinator.status() == dict(active=True, phase='approach', waiting_for=None,
                                        alerted=False, target=dict(x=2.0, y=3.0))
    coordinator.approach_done(request, 'arrived', 0.0)
    assert coordinator.status()['phase'] == 'look'  # Standing still, still checking.
    assert coordinator.next_work(CHECK_WAIT_S) == ('confirm', request)
    assert coordinator.status()['phase'] == 'ask'
    coordinator.wait(20.0, 'manual_drive')
    assert coordinator.status() == dict(active=True, phase='wait', waiting_for='manual_drive',
                                        alerted=False, target=dict(x=2.0, y=3.0))
    coordinator.check_waits(80.0)
    assert coordinator.status()['alerted'] is True
    coordinator.resume(81.0)
    assert coordinator.status(current='revisit')['phase'] == 'approach'
    coordinator.fail(request, 'question_error')
    assert coordinator.status() == off


def test_malformed_revisit_spot_is_refused():
    coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    assert not coordinator.receive(event(revisit_target=dict(x=1, y=2)))
    assert not coordinator.requests


# ------------------------------------------------------------ link


@pytest.fixture
def link(monkeypatch):
    monkeypatch.setitem(sys.modules, 'action_msgs.msg', SimpleNamespace(
        GoalStatus=SimpleNamespace(STATUS_SUCCEEDED=4, STATUS_ABORTED=6)))
    now = SimpleNamespace(value=0.0)
    item = object.__new__(FallConfirmationLink)
    item.node, item.lock, item.clock = Mock(), RLock(), lambda: now.value
    item.goal_response_timeout_s, item.result_timeout_s = 5.0, 610.0
    item.server_loss_timeout_s = 5.0
    item.coordinator = FallConfirmationCoordinator(runtime_id='vlm')
    item.action_type = SimpleNamespace(Goal=SimpleNamespace)
    item.message_type = SimpleNamespace
    item.client, item.decisions, item.timer, item.agent_presence = Mock(), Mock(), Mock(), Mock()
    item.status_publisher = Mock()
    item.client.server_is_ready.return_value = True
    item.agent_presence.server_is_ready.return_value = True
    item.goals = []

    def send(goal, feedback_callback=None):
        future = Future()
        fields = json.loads(goal.arguments_yaml) | {'capability': goal.capability_id}
        item.goals.append((fields, future, feedback_callback))
        return future
    item.client.send_goal_async.side_effect = send
    item.manager_state = SimpleNamespace(
        active_foreground_missions=[], active_background_missions=[],
        pending_missions=[], suspended_missions=[])
    item.request = item.goal_future = item.handle = None
    item.sent_at = item.next_attempt = 0.0
    item.accepted_at = item.server_missing_since = None
    item.now = now
    yield item
    item.close()


def respond(link, *, status=6, message='', result_yaml='', running=False):
    fields, future, feedback = link.goals[-1]
    result = Future()
    handle = Mock(accepted=True)
    handle.get_result_async.return_value = result
    future.set_result(handle)
    if running:
        feedback(SimpleNamespace(feedback=SimpleNamespace(state='RUNNING')))
    if status is not None:
        result.set_result(SimpleNamespace(status=status, result=SimpleNamespace(
            message=message, result_yaml=result_yaml)))
    return fields


def decisions(link):
    return [(d['action'], d.get('reason', d.get('outcome')))
            for d in (json.loads(c.args[0].data) for c in link.decisions.publish.call_args_list)]


def statuses(link):
    return [json.loads(c.args[0].data) for c in link.status_publisher.publish.call_args_list]


MANUAL = SimpleNamespace(capability_id='manual_drive')


@pytest.mark.parametrize('message,why', [
    ('conflicting mission 1f2e has higher priority HIGH', 'busy'),
    ('conflicting mission 1f2e has higher priority URGENT', 'busy'),
    ('manual recovery is in progress', 'busy'),
    ('system manager is still booting', 'busy'),
    ('emergency stop is active', 'busy'),
    ('foreground missions are blocked while recharging', 'busy'),
    ('superseded before execution by 9a8b', 'busy'),
    ('mission preempted by a replacement request', 'busy'),
    ('Downstream Action server rejected the goal', 'busy'),
])
def test_question_refusals_that_pass_wait_and_go_again(link, message, why):
    link.on_event(SimpleNamespace(data=question()))
    respond(link, message=message)
    assert decisions(link) == [] and link.coordinator.waiting_for == why
    assert link.request is None and link.coordinator.requests
    # The Agent refusing the question leaves nothing holding the wheels: a HIGH
    # hold goes in while waiting (2026-10-13). The Manager's own refusals keep
    # them held (or blocked) already.
    holds = 1 if message == 'Downstream Action server rejected the goal' else 0
    if holds:
        assert link.goals[-1][0]['capability'] == 'fall_approach'
        assert link.goals[-1][0]['phase'] == 'hold'
    link.now.value = 0.9
    link.tick()
    assert len(link.goals) == 1 + holds
    link.now.value = 1.0
    link.tick()
    assert len(link.goals) == 2 + holds and link.goals[-1][0]['capability'] == 'fall_confirmation'
    respond(link, message=message)
    link.now.value = 3.0  # Still refused three seconds on: the history notes it.
    link.tick()
    assert decisions(link) == [('check_waiting', why)]


def test_interruption_by_manual_driving_is_named_from_the_manager_state(link):
    link.on_event(SimpleNamespace(data=question()))
    respond(link, status=None, running=True)
    link.manager_state.active_foreground_missions = [MANUAL]
    fields, future, _ = link.goals[-1]
    future.result().get_result_async.return_value.set_result(SimpleNamespace(
        status=6, result=SimpleNamespace(message='mission preempted by a replacement request',
                                         result_yaml='')))
    assert link.coordinator.waiting_for == 'manual_drive'


def test_while_driven_by_hand_nothing_is_sent_and_the_wait_is_counted(link):
    link.manager_state.pending_missions = [MANUAL]
    link.on_event(SimpleNamespace(data=question()))
    assert not link.goals and decisions(link) == []
    assert statuses(link)[-1] == dict(active=True, phase='wait', waiting_for='manual_drive',
                                      alerted=False, target=dict(x=2.0, y=3.0))
    for second in range(1, 61):
        link.now.value = float(second)
        link.tick()
    assert not link.goals
    assert decisions(link) == [('check_waiting', 'manual_drive'),
                               ('check_not_confirmed', 'waited_manual_drive')]
    assert len(statuses(link)) >= 60  # Every second while it lasts.
    assert statuses(link)[-1]['alerted'] is True
    link.manager_state.pending_missions = []
    link.now.value = 62.0
    link.tick()
    assert link.goals[-1][0] == dict(capability='fall_approach', request_id='question',
                                     phase='approach', x=2.0, y=3.0, standoff_m=1.0)
    respond(link, status=4, result_yaml=json.dumps(dict(outcome='arrived')), running=True)
    assert decisions(link)[-2:] == [
        ('revisit_started', 'manual_drive'), ('revisit_result', 'arrived')]


def test_no_manager_counts_as_busy(link):
    link.client.server_is_ready.return_value = False
    link.on_event(SimpleNamespace(data=question()))
    assert link.coordinator.waiting_for == 'busy' and not link.goals
    for second in range(1, 4):
        link.now.value = float(second)
        link.tick()
    assert decisions(link) == [('check_waiting', 'busy')] and not link.goals


def test_running_ends_the_wait_so_a_long_question_is_not_counted(link):
    link.on_event(SimpleNamespace(data=question()))
    respond(link, message='system manager is still booting')
    link.now.value = 30.0
    link.tick()
    respond(link, status=None, running=True)
    assert link.coordinator.waiting_for is None
    link.now.value = 600.0
    link.tick()
    assert ('check_not_confirmed', 'waited_busy') not in decisions(link)
    assert statuses(link)[-1]['phase'] == 'ask'


@pytest.mark.parametrize('message,reason', [
    ('Action server /malbut/agent/confirm_situation is unavailable', 'speech_unavailable'),
    ('Downstream goal canceled without a manager request', 'question_error'),
    ('', 'question_error'),
])
def test_question_failures_that_waiting_cannot_fix_alert_at_once(link, message, reason):
    link.on_event(SimpleNamespace(data=question()))
    respond(link, message=message, running=True)
    assert decisions(link) == [('confirmation_failed', reason)]
    assert not link.coordinator.requests


def test_a_drive_refused_by_its_own_server_asks_in_place(link):
    link.on_event(SimpleNamespace(data=event(approach_target=SPOT)))
    respond(link, message='Downstream Action server rejected the goal')
    assert decisions(link) == [('approach_result', 'failed')]
    link.tick()
    assert link.goals[-1][0]['capability'] == 'fall_confirmation'


def test_a_drive_interrupted_by_manual_driving_waits_and_drives_again(link):
    link.on_event(SimpleNamespace(data=event(approach_target=SPOT)))
    link.manager_state.active_foreground_missions = [MANUAL]
    respond(link, message='conflicting mission x has higher priority URGENT')
    assert link.coordinator.waiting_for == 'manual_drive' and decisions(link) == []
    link.manager_state.active_foreground_missions = []
    link.now.value = 1.0
    link.tick()
    assert link.goals[-1][0]['phase'] == 'approach' and len(link.goals) == 2


def test_close_publishes_that_checking_stopped(link):
    link.on_event(SimpleNamespace(data=question()))
    link.close()
    assert statuses(link)[-1]['active'] is False


def test_manager_words_are_the_managers_own():
    """The waiting list must match what the Manager really says."""
    root = Path(__file__).resolve().parents[2] / 'malbut_system_manager' / 'malbut_system_manager'
    if not root.is_dir():
        pytest.skip('malbut_system_manager is not in this checkout')
    source = ''.join((root / name).read_text()
                     for name in ('mission_scheduler.py', 'mission_executor.py'))
    for message in link_module.WAIT_MESSAGES:
        assert f"'{message}'" in source, message
    for prefix in link_module.WAIT_PREFIXES:
        assert f"f'{prefix}" in source, prefix
    assert f"'{link_module.DOWNSTREAM_REJECTED}'" in source
    assert not any(message.startswith('localization is ') for message in link_module.WAIT_MESSAGES)
    assert "f'priority {blocker.priority.name}'" in source
    assert "f'Action server {execution.command_name} is unavailable'" in source
    capability = (root.parent.parent / 'malbut_interfaces' / 'capabilities'
                  / 'fall_confirmation.yaml')
    assert 'name: /malbut/agent/confirm_situation' in capability.read_text()
