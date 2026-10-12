"""Could not ask for a minute: alert, go back, look again, never close (2026-10-12).

The fall rules wait while the robot is driven by hand or busy. A minute of it
alerts the guardian (urgent, once). Then the robot goes back to the spot before
asking; what it sees there is reported, compared by clothing with the first
scene for reference, and the case is left for the guardian to close.
"""

import asyncio
import json

import pytest

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (
    SAME_PERSON_PROMPT, build_same_person_payload, parse_same_person,
)
from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import (
    FrameWindow, IncidentState, PersonCheckReply, RgbFrame, SamePersonCheckReply,
    SamePersonCheckRequest,
)
from malbut_agent_server.fall_runtime import apply_decision, event_metadata
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_cloud_fall_monitor import enable, frame, make
from test_fall_approach_runtime import BAG, SPOT, Places, ids
from test_fall_cloud_association import finding, reply
from test_fall_full_view import FLOOR, ON_BED, STANDS, Viewer
from test_fall_unidentified_verification import scan
from test_ollama_cloud_fall import jpeg, response


class Comparer(Viewer):
    def __init__(self):
        super().__init__()
        self.same = SamePersonCheckReply('same', '회색 상의와 검은 바지가 같아 보입니다.')
        self.compared = []

    async def compare_person(self, request):
        self.compared.append(request)
        if isinstance(self.same, Exception):
            raise self.same
        return self.same


def setup(*, provider=None, approach=True):
    monitor, clock, _ = make()
    provider = provider or Comparer()
    monitor._provider = provider
    monitor._place = Places({BAG: SPOT})
    monitor.approach_enabled = approach
    monitor.full_view_enabled = True
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(BAG)))
    question = next(e for e in events if e.kind == 'question_requested')
    monitor.drain_events()
    return monitor, clock, provider, question


def decide(monitor, question, action, **fields):
    return apply_decision(monitor, json.dumps(dict(
        action=action, boot_id=monitor.boot_id, **ids(question), **fields)))


def waited_and_went_back(monitor, question):
    assert decide(monitor, question, 'check_waiting', reason='manual_drive')
    assert decide(monitor, question, 'check_not_confirmed', reason='waited_manual_drive')
    assert decide(monitor, question, 'revisit_started', reason='manual_drive')
    assert decide(monitor, question, 'revisit_result', outcome='arrived')


def cloud(monitor, clock, after):
    clock.value += after
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    return asyncio.run(monitor.run_once())


def seen(monitor):
    return [(e.kind, e.reason, e.notification_level.value if e.notification_level else None)
            for e in monitor.drain_events()]


def test_the_question_carries_the_spot_to_go_back_to():
    monitor, _, _, question = setup()
    assert question.revisit_target == SPOT
    assert event_metadata(question)['revisit_target'] == dict(x=2.0, y=1.0, frame='map')
    replay, = monitor.pending_questions()
    assert replay.revisit_target == SPOT
    # Kept for the comparison: the scene that raised the question, with the box.
    window, box = monitor._first_scenes[question.incident_id]
    assert 1 <= len(window.frames) <= 2 and box == BAG


def test_without_the_approach_switch_there_is_no_spot_and_it_just_asks_later():
    monitor, _, _, question = setup(approach=False)
    assert question.revisit_target is None and 'revisit_target' not in event_metadata(question)


def test_a_minute_of_waiting_alerts_urgently_once_and_keeps_the_case_open():
    monitor, _, _, question = setup()
    assert decide(monitor, question, 'check_waiting', reason='manual_drive')
    assert seen(monitor) == [('check_waiting', 'manual_drive', None)]
    assert decide(monitor, question, 'check_not_confirmed', reason='waited_manual_drive')
    assert seen(monitor) == [('check_not_confirmed', 'waited_manual_drive', None),
                             ('notification_requested', 'robot_could_not_confirm', 'urgent')]
    assert monitor.incident(question.incident_id).state is IncidentState.RECHECK_REQUIRED
    assert decide(monitor, question, 'check_not_confirmed', reason='waited_busy')
    assert seen(monitor) == [('check_not_confirmed', 'waited_busy', None)]


@pytest.mark.parametrize('action,fields', [
    ('check_waiting', dict(reason='walking')),
    ('check_not_confirmed', dict(reason='manual_drive')),
    ('revisit_started', dict(reason='waited_busy')),
    ('revisit_result', dict(outcome='somewhere')),
    ('confirmation_failed', dict(reason='bored')),
    ('check_waiting', dict(reason='busy', extra=1)),
    ('check_waiting', dict()),
])
def test_waiting_decisions_are_strict(action, fields):
    monitor, _, _, question = setup()
    with pytest.raises(ValueError):
        decide(monitor, question, action, **fields)
    assert seen(monitor) == []


def test_nobody_there_after_going_back_alerts_and_does_not_close():
    monitor, clock, provider, question = setup()
    provider.look = PersonCheckReply('not_person', '가방과 옷가지만 보입니다.')
    waited_and_went_back(monitor, question)
    assert question.incident_id in monitor._spot_checks
    monitor.drain_events()
    assert cloud(monitor, clock, 3.5)
    assert seen(monitor) == [('person_check_completed', 'nobody_there', None),
                             ('notification_requested', 'revisit_nobody_there', 'check')]
    incident = monitor.incident(question.incident_id)
    assert incident.state is IncidentState.RECHECK_REQUIRED and incident.close_reason is None
    assert not provider.compared  # Nobody to compare.
    assert monitor.return_result(**ids(question), outcome='returned')
    assert seen(monitor) == [('approach_returned', 'returned', None)]


def off_floor(monitor, clock, question, answers):
    monitor._provider.answers = answers
    waited_and_went_back(monitor, question)
    monitor.drain_events()
    assert cloud(monitor, clock, 3.5)  # Legs only: move to see the whole body.
    assert decide(monitor, question, 'view_result', outcome='arrived')
    assert cloud(monitor, clock, 1.1)
    assert cloud(monitor, clock, 2.6)


@pytest.mark.parametrize('verdict,notice,level', [
    ('same', 'revisit_same_person_off_floor', 'info'),
    ('different', 'revisit_person_unclear_off_floor', 'check'),
    ('unclear', 'revisit_person_unclear_off_floor', 'check'),
])
def test_off_the_floor_after_going_back_compares_then_tells_the_guardian(verdict, notice, level):
    monitor, clock, provider, question = setup()
    provider.same = SamePersonCheckReply(verdict, '처음 장면과 옷을 비교했습니다.')
    off_floor(monitor, clock, question, [STANDS, STANDS])
    assert seen(monitor)[-1] == ('posture_check_completed', 'standing', None)
    assert monitor.incident(question.incident_id).state is not IncidentState.RESOLVED
    assert cloud(monitor, clock, 1.0)  # The comparison.
    events = monitor.drain_events()
    compared = events[0]
    assert (compared.kind, compared.reason) == ('same_person_compared', verdict)
    assert compared.analysis.purpose == 'same_person_check'
    assert compared.analysis.explanation == '처음 장면과 옷을 비교했습니다.'
    assert [(e.kind, e.reason, e.notification_level.value) for e in events[1:]] == [
        ('notification_requested', notice, level)]
    request, = provider.compared
    assert request.before_box == BAG and 1 <= len(request.before.frames) <= 2
    assert 1 <= len(request.after.frames) <= 2
    incident = monitor.incident(question.incident_id)
    assert incident.state is IncidentState.RECHECK_REQUIRED and incident.close_reason is None


@pytest.mark.parametrize('trouble', ['error', 'no_method', 'consent_off'])
def test_a_comparison_that_cannot_run_is_unclear_and_still_alerts(trouble):
    provider = Comparer()
    if trouble == 'error':
        provider.same = CloudFallProviderError('cloud_timeout')
    monitor, clock, provider, question = setup(provider=provider)
    if trouble == 'no_method':
        provider.compare_person = None
    off_floor(monitor, clock, question, [ON_BED, ON_BED])
    if trouble == 'consent_off':
        monitor.configure(enabled=True, camera_enabled=True, cloud_consent=False, connected=True)
    monitor.maintain_associations()
    asyncio.run(monitor.run_once())
    tail = seen(monitor)[-2:]
    assert tail == [('same_person_compared', 'unclear', None),
                    ('notification_requested', 'revisit_person_unclear_off_floor', 'check')]


def test_a_comparison_left_too_long_is_recorded_unclear():
    monitor, clock, provider, question = setup()
    off_floor(monitor, clock, question, [STANDS, STANDS])
    monitor.drain_events()
    monitor.configure(enabled=False, camera_enabled=True, cloud_consent=True, connected=True)
    clock.value += 61
    monitor.maintain_associations()
    assert seen(monitor) == [
        ('same_person_compared', 'unclear', None),
        ('notification_requested', 'revisit_person_unclear_off_floor', 'check')]


def test_on_the_floor_it_asks_at_once_and_compares_meanwhile_and_help_still_alerts():
    monitor, clock, provider, question = setup()
    off_floor(monitor, clock, question, [FLOOR, FLOOR])
    events = seen(monitor)
    # One floor look is enough to ask; the comparison follows, for reference only.
    assert events[-2:] == [('posture_check_completed', 'on_floor', None),
                           ('same_person_compared', 'same', None)]
    assert not any(kind == 'notification_requested' and reason.startswith('revisit')
                   for kind, reason, _ in events)
    # Asked: the answer decides as before. 도움 요청 goes out after 직접 확인하지 못함.
    assert decide(monitor, question, 'confirmation_result', subject_key=None,
                  situation_assessment='confirmed_incident', help_needed=True)
    assert ('notification_requested', 'confirmation_help_required', 'urgent') in seen(monitor)


def test_failures_alert_once_with_their_reason():
    monitor, _, _, question = setup()
    assert decide(monitor, question, 'confirmation_failed', reason='speech_unavailable')
    assert seen(monitor) == [('agent_check_failed', 'speech_unavailable', None),
                             ('notification_requested', 'robot_could_not_confirm', 'urgent')]
    monitor, _, _, question = setup()
    decide(monitor, question, 'check_not_confirmed', reason='waited_busy')
    monitor.drain_events()
    assert decide(monitor, question, 'confirmation_failed', reason='time_limit')
    assert seen(monitor) == [('agent_check_failed', 'time_limit', None)]  # Already alerted.


def test_older_alerts_keep_their_own_rule_beside_the_new_ones():
    monitor, _, _, question = setup()
    decide(monitor, question, 'check_not_confirmed', reason='waited_manual_drive')
    monitor.drain_events()
    incident = monitor.incident(question.incident_id)
    assert incident.notification_level.value == 'urgent'
    monitor._notify(incident, incident.notification_level.__class__('check'), 'person_no_response')
    monitor._notify(incident, incident.notification_level.__class__('info'),
                    'fall_observed_person_okay')
    assert seen(monitor) == [('notification_requested', 'person_no_response', 'check')]
    assert incident.notification_level.value == 'urgent'


def test_guardian_close_stops_checking_and_repeats_are_ignored():
    monitor, clock, provider, question = setup()
    waited_and_went_back(monitor, question)
    monitor.drain_events()
    assert monitor.close_by_guardian(question.incident_id)
    assert seen(monitor) == [('incident_resolved', 'guardian_closed', None)]
    incident = monitor.incident(question.incident_id)
    assert incident.state is IncidentState.RESOLVED and incident.close_reason == 'guardian_closed'
    assert question.incident_id not in monitor._person_checks
    assert not monitor.close_by_guardian(question.incident_id)
    assert not monitor.close_by_guardian('11111111-1111-4111-8111-111111111111')
    assert not decide(monitor, question, 'revisit_result', outcome='arrived')
    assert not decide(monitor, question, 'confirmation_failed', reason='time_limit')
    assert seen(monitor) == []
    assert not monitor.pending_questions()


def test_guardian_close_during_its_analysis_waits_for_the_result():
    monitor, clock, provider, question = setup()
    monitor._active_incident = question.incident_id
    assert monitor.close_by_guardian(question.incident_id)
    assert seen(monitor) == [] and monitor.incident(question.incident_id).state is not \
        IncidentState.RESOLVED
    monitor._active_incident = None
    monitor._close_deferred()
    assert seen(monitor) == [('incident_resolved', 'guardian_closed', None)]


def test_the_journal_keeps_the_comparison_explanation(tmp_path):
    monitor, clock, provider, question = setup()
    journal = SqliteFallJournal(tmp_path / 'private' / 'fall.sqlite', device_id='robot')
    monitor._journal = journal
    off_floor(monitor, clock, question, [STANDS, STANDS])
    cloud(monitor, clock, 1.0)
    payloads = [json.loads(p) for p in journal._db.execute(
        'SELECT payload FROM incident_events ORDER BY sequence').fetchall() for p in p]
    compared = next(p for p in payloads if p['eventKind'] == 'same_person_compared')
    assert compared['reason'] == 'same' and compared['analysis']['purpose'] == 'same_person_check'
    notice = next(p for p in payloads if p['reason'] == 'revisit_same_person_off_floor')
    assert notice['notificationLevel'] == 'info' and notice['state'] == 'recheck_required'


def test_same_person_request_and_reply_are_bounded_and_strict():
    frames = FrameWindow((RgbFrame(1.0, jpeg()), RgbFrame(1.5, jpeg())), 0.0, 2.0, False)
    request = SamePersonCheckRequest('same-1', frames, (.1, .2, .5, .9), frames)
    payload = json.loads(build_same_person_payload(request, model='gemma4:31b'))
    user = payload['messages'][1]
    assert len(user['images']) == 4
    assert '"earlier_target_box":[0.1,0.2,0.5,0.9]' in user['content']
    assert payload['messages'][0]['content'] == SAME_PERSON_PROMPT
    assert 'Do not judge or describe faces' in SAME_PERSON_PROMPT
    for bad in (SamePersonCheckRequest('same-1', frames, (.5, .2, .1, .9), frames),
                SamePersonCheckRequest('same-1', frames, (.1, .2, 1.5, .9), frames),
                SamePersonCheckRequest('same-1', FrameWindow(frames.frames * 2, 0.0, 2.0, False),
                                       None, frames)):
        with pytest.raises(CloudFallProviderError, match='cloud_input_invalid'):
            build_same_person_payload(bad, model='gemma4:31b')
    no_box = json.loads(build_same_person_payload(
        SamePersonCheckRequest('same-1', frames, None, frames), model='gemma4:31b'))
    assert '"earlier_target_box":null' in no_box['messages'][1]['content']
    reply_ = parse_same_person(response(json.dumps(dict(verdict='same', explanation='같은 옷'))))
    assert reply_ == SamePersonCheckReply('same', '같은 옷')
    for content in (json.dumps(dict(verdict='identical', explanation='x')),
                    json.dumps(dict(verdict='same', explanation='x', name='kim'))):
        with pytest.raises(CloudFallProviderError, match='cloud_invalid_response'):
            parse_same_person(response(content))
