"""Only part of the body seen: move to see all of it before anyone is asked."""

from types import SimpleNamespace

import pytest

from malbut_agent_server.fall_rules.fall_confirmation import CHECK_WAIT_S, POSTURE_WAIT_S
from malbut_agent_server.fall_rules.fall_confirmation_link import VIEW_STANDOFF_M
from test_fall_rules_approach import (  # noqa: F401
    approaching, finish, link, looked, scene, sent,
)
from test_fall_rules_confirmation import event


def posture(reason):
    return event(kind='posture_check_completed', confirmation_scope='scene',
                 subject_key=None, reason=reason)


def looking():
    coordinator, request = approaching()
    coordinator.approach_done(request, 'arrived', 10.0)
    coordinator.drain_commands()
    return coordinator, request


def test_a_partial_body_moves_to_the_whole_body_view_then_waits_for_the_posture():
    coordinator, request = looking()
    assert coordinator.receive(looked('partial_body'))
    assert coordinator.next_work(11.0) == ('view', request)
    assert coordinator.view_done(request, 'arrived', 30.0)
    (decision,) = coordinator.drain_commands()
    assert decision == dict(action='view_result', boot_id='boot-1', incident_id='incident',
                            question_id='question', evidence_revision=1, outcome='arrived')
    assert coordinator.next_work(30.0 + POSTURE_WAIT_S - 1) is None
    assert coordinator.receive(posture('on_floor'))
    assert coordinator.next_work(31.0) == ('confirm', request)


@pytest.mark.parametrize('reason', ['sitting_off_floor', 'standing'])
def test_off_the_floor_closes_without_a_question_and_drives_back(reason):
    coordinator, request = looking()
    coordinator.receive(looked('partial_body'))
    coordinator.view_done(request, 'arrived', 30.0)
    coordinator.drain_commands()
    assert coordinator.receive(posture(reason))
    assert not coordinator.requests
    kind, job = coordinator.next_work(31.0)
    assert kind == 'return' and job.reason == 'off_floor'
    assert coordinator.receive(event(kind='incident_resolved', confirmation_scope='scene',
                                     subject_key=None, reason='off_floor_verified'))
    assert coordinator.return_done(job, 'returned')
    assert coordinator.next_work(32.0) is None


@pytest.mark.parametrize('reason', ['on_floor', 'body_unseen', 'posture_unconfirmed'])
def test_anything_but_off_the_floor_asks(reason):
    coordinator, request = looking()
    coordinator.receive(looked('partial_body'))
    coordinator.view_done(request, 'no_path', 20.0)
    assert coordinator.receive(posture(reason))
    assert coordinator.next_work(21.0) == ('confirm', request)


def test_no_posture_in_time_asks_anyway_and_late_results_are_ignored():
    coordinator, request = looking()
    coordinator.receive(looked('partial_body'))
    coordinator.view_done(request, 'arrived', 30.0)
    assert coordinator.next_work(30.0 + POSTURE_WAIT_S) == ('confirm', request)
    assert coordinator.receive(posture('sitting_off_floor'))
    assert coordinator.requests and not coordinator.returns


def test_posture_results_are_strict_and_only_count_after_the_look():
    coordinator, request = approaching()
    assert coordinator.receive(posture('sitting_off_floor'))  # Still driving there.
    assert coordinator.requests and not coordinator.returns
    assert not coordinator.receive(posture('sitting'))
    assert not coordinator.receive(event(kind='posture_check_completed', confirmation_scope='scene',
                                         subject_key=None, reason=None))
    coordinator.approach_done(request, 'arrived', 0.0)
    assert coordinator.next_work(CHECK_WAIT_S) == ('confirm', request)


def test_link_moves_to_the_view_then_drives_back_with_the_reason(link):  # noqa: F811
    link.on_event(SimpleNamespace(data=scene()))
    finish(link, 'arrived')
    link.on_event(SimpleNamespace(data=looked('partial_body')))
    goal = finish(link, 'arrived')
    assert goal == dict(capability='fall_approach', request_id='question', phase='view',
                        x=2.5, y=1.0, standoff_m=VIEW_STANDOFF_M)
    assert [d['action'] for d in sent(link)] == ['approach_result', 'view_result']
    link.on_event(SimpleNamespace(data=posture('sitting_off_floor')))
    goal = finish(link, 'returned')
    assert goal['phase'] == 'return' and goal['reason'] == 'off_floor'
    assert all(g['capability'] != 'fall_confirmation' for g, _ in link.goals)


def test_link_asks_when_the_view_cannot_be_reached(link):  # noqa: F811
    link.on_event(SimpleNamespace(data=scene()))
    finish(link, 'arrived')
    link.on_event(SimpleNamespace(data=looked('partial_body')))
    finish(link, None, status=6, message='Action server /malbut/falls/approach is unavailable')
    assert sent(link)[-1] == dict(action='view_result', boot_id='boot-1', incident_id='incident',
                                  question_id='question', evidence_revision=1,
                                  outcome='failed')
    link.on_event(SimpleNamespace(data=posture('body_unseen')))
    link.tick()
    assert link.goals[-1][0]['capability'] == 'fall_confirmation'
