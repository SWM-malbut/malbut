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
    assert '종료 확인 전' in event_speech(event('cancel_accepted'))
    assert '취소 상태로 종료' in event_speech(event('canceled'))
    assert '멈췄' not in event_speech(event('canceled'))
    assert '정지' not in event_speech(event('canceled'))


def test_success_describes_manager_status_not_unverified_result_fields():
    """Opaque YAML cannot supply a claim of robot arrival or stopping."""
    text = event_speech(event(
        'succeeded', result_yaml='arrived: true\nphysically_stopped: true',
    ))
    assert 'Manager' in text and '성공 상태로 종료' in text
    assert '도착' not in text and '정지' not in text


def test_manager_failure_reports_only_its_supplied_reason():
    """Handle failures after Goal acceptance without inventing their cause."""
    supplied = 'unknown capability: missing'
    assert supplied in event_speech(event('failed', reason=supplied))
    assert '사유' not in event_speech(event('failed'))
    assert event_speech(event('submitted')) is None
    assert event_speech(event('progress', state='made_up_state')) is None


def test_progress_repeats_are_suppressed_but_resume_is_announced():
    """Repeated Feedback payload changes cannot flood TTS."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    assert announcer.handle(event('progress', 'RUNNING')) is not None
    assert announcer.handle(event(
        'progress', 'RUNNING', feedback_yaml='different: data',
    )) is None
    assert announcer.handle(event('progress', 'SUSPENDED')) is not None
    assert announcer.handle(event('progress', 'RUNNING')) is not None
    assert len(spoken) == 3


def test_admission_rejection_does_not_claim_a_supplied_reason():
    """A standard Goal rejection has no Manager-provided reason field."""
    text = event_speech(event(
        'rejected', reason='Manager did not accept the Action Goal',
    ))
    assert '접수를 거절' in text
    assert '전달받은 사유' not in text
    assert 'did not accept' not in text


def test_cancel_rejection_does_not_promise_new_result_observation():
    """Result observation may already have failed with an unknown state."""
    text = event_speech(event('cancel_rejected', state='UNKNOWN'))
    assert '접수되지 않았어요' in text
    assert '종료된 것으로 판단하지 않을게요' in text
    assert '계속 확인' not in text


def test_independent_requests_do_not_suppress_each_other():
    """Track announcement history by the original request identifier."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    for request_id in ('first', 'second'):
        assert announcer.handle(event('accepted', request_id=request_id))
    assert len(spoken) == 2


@pytest.mark.parametrize('terminal', [
    'succeeded', 'failed', 'canceled', 'rejected',
])
def test_late_progress_cannot_overwrite_terminal_speech(terminal):
    """A finished request cannot be described as executing again."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    announcer.handle(event(terminal))
    assert announcer.handle(event('progress', 'RUNNING')) is None
    assert announcer.handle(event('cancel_accepted')) is None
    assert announcer.handle(event(terminal)) is None
    assert len(spoken) == 1


def test_unknown_can_later_report_verified_result_without_restarting():
    """An eventual verified result does not authorize a restart."""
    spoken = []
    announcer = MissionAnnouncer(lambda text: spoken.append(text) or True)
    assert announcer.handle(event('unknown'))
    assert announcer.handle(event('succeeded'))
    assert len(spoken) == 2


def test_unsent_speech_is_not_recorded_as_already_announced():
    """Shutdown before publication must not consume the speech event."""
    announcer = MissionAnnouncer(lambda _: False)
    assert announcer.handle(event('accepted')) is None
    announcer._speak = lambda _: True
    assert announcer.handle(event('accepted')) is not None
