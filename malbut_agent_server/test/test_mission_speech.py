"""Keep communication facts separate from physical outcomes."""

import pytest

from malbut_agent_server.mission_speech import MissionAnnouncer, event_speech


def event(kind, state='', request_id='first', **extra):
    """Construct an event with a stable request binding."""
    return dict(
        request_id=request_id, capability_id='follow_person',
        kind=kind, state=state, **extra,
    )


def test_acceptance_and_cancellation_are_not_execution_completion():
    """Acceptance cannot turn into success or physical stop speech."""
    assert '접수' in event_speech(event('accepted'))
    assert '성공' not in event_speech(event('accepted'))
    assert event_speech(event('cancel_accepted')) == '취소를 요청했어요.'
    assert event_speech(event('canceled')) == '작업이 취소됐어요.'
    assert '멈췄' not in event_speech(event('canceled'))
    assert '정지' not in event_speech(event('canceled'))


def test_success_describes_manager_status_not_unverified_result_fields():
    """Opaque YAML cannot supply a claim of robot arrival or stopping."""
    text = event_speech(event(
        'succeeded', result_yaml='arrived: true\nphysically_stopped: true',
    ))
    assert text == '요청하신 작업이 완료됐어요.'
    assert '도착' not in text and '정지' not in text


def test_manager_failure_keeps_internal_reasons_out_of_speech():
    """Handle failures after Goal acceptance without inventing their cause."""
    supplied = 'unknown capability: missing'
    assert event_speech(event('failed', reason=supplied)) == '작업을 완료하지 못했어요.'
    assert '사유' not in event_speech(event('failed'))
    assert event_speech(event('submitted')) is None
    assert event_speech(event('progress', state='made_up_state')) is None


def test_routine_transition_bursts_do_not_generate_automatic_speech():
    """Initial dialogue already acknowledges the command; keep updates quiet."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    transitions = [
        event('submitted'), event('accepted'),
        event('progress', 'PENDING'), event('progress', 'RUNNING'),
        event('progress', 'RUNNING', feedback_yaml='different: data'),
        event('progress', 'SUSPENDED'), event('progress', 'RUNNING'),
        event('cancel_requested'), event('progress', 'CANCELING'),
        event('cancel_accepted'),
    ]
    for update in transitions * 3:
        assert announcer.handle(update) is None
    assert spoken == []
    terminal = event('failed', reason='mission preempted by a replacement request')
    assert announcer.handle(terminal) == event_speech(terminal)
    assert announcer.handle(terminal) is None
    assert spoken == [event_speech(terminal)]


@pytest.mark.parametrize('kind,state', [
    ('accepted', ''), ('cancel_requested', ''), ('cancel_accepted', ''),
    ('progress', 'PENDING'), ('progress', 'RUNNING'),
    ('progress', 'CANCELING'), ('progress', 'SUSPENDED'),
])
def test_routine_events_remain_available_for_direct_command_responses(kind, state):
    """Filtering automatic notices must not remove explicit status vocabulary."""
    assert event_speech(event(kind, state))


def test_admission_rejection_does_not_claim_a_supplied_reason():
    """A standard Goal rejection has no Manager-provided reason field."""
    text = event_speech(event(
        'rejected', reason='Manager did not accept the Action Goal',
    ))
    assert text == '작업을 완료하지 못했어요.'
    assert '전달받은 사유' not in text
    assert 'did not accept' not in text


def test_cancel_rejection_does_not_promise_new_result_observation():
    """Result observation may already have failed with an unknown state."""
    text = event_speech(event('cancel_rejected', state='UNKNOWN'))
    assert text == '취소 요청을 처리하지 못했어요.'
    assert '취소됐어요' not in text
    assert '계속 확인' not in text


def test_independent_requests_do_not_suppress_each_other():
    """Track announcement history by the original request identifier."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    for request_id in ('first', 'second'):
        assert announcer.handle(event('succeeded', request_id=request_id))
        assert announcer.handle(event('succeeded', request_id=request_id)) is None
    assert len(spoken) == 2


@pytest.mark.parametrize('terminal', [
    'succeeded', 'failed', 'canceled', 'rejected', 'unavailable',
])
def test_late_progress_cannot_overwrite_terminal_speech(terminal):
    """A finished request cannot be described as executing again."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    announcer.handle(event(terminal))
    assert announcer.handle(event('progress', 'RUNNING')) is None
    assert announcer.handle(event('cancel_accepted')) is None
    assert announcer.handle(event('unknown')) is None
    assert announcer.handle(event('cancel_unknown')) is None
    assert announcer.handle(event(terminal)) is None
    assert len(spoken) == 1


@pytest.mark.parametrize('problem', ['unknown', 'cancel_unknown', 'cancel_rejected'])
@pytest.mark.parametrize('terminal', ['succeeded', 'failed', 'canceled'])
def test_problem_is_announced_once_and_later_result_is_preserved(problem, terminal):
    """Uncertain execution and failed cancellation still need user attention."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    problem_event = event(problem)
    assert announcer.handle(problem_event) == event_speech(problem_event)
    assert announcer.handle(problem_event) is None
    assert announcer.handle(event('progress', 'RUNNING')) is None
    assert announcer.handle(event('accepted')) is None
    assert announcer.handle(problem_event) is None
    terminal_event = event(terminal)
    assert announcer.handle(terminal_event) == event_speech(terminal_event)
    assert announcer.handle(terminal_event) is None
    assert spoken == [event_speech(problem_event), event_speech(terminal_event)]


@pytest.mark.parametrize('kind', ['unknown', 'failed'])
def test_unsent_speech_is_not_recorded_as_already_announced(kind):
    """Shutdown before publication must not consume the speech event."""
    announcer = MissionAnnouncer(lambda _: False)
    assert announcer.handle(event(kind)) is None
    announcer._speak = lambda _: True
    assert announcer.handle(event(kind)) is not None
