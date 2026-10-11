"""A person seen from 1 m: look at the whole body before asking.

2026-10-10 20:43 and 10-11 03:39: someone sitting on a bed showed only legs to
the 12 cm camera, so the close look said "person" and the robot asked. Cloud is
asked twice from where the whole body is visible; Pose keypoints are not used.
"""

import asyncio
import json

import pytest

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (
    POSTURE_CHECK_PROMPT, build_posture_check_payload, parse_posture_check,
)
from malbut_agent_server.domain.fall_monitoring import (
    FrameWindow, IncidentState, PersonCheckReply, PostureCheckReply, PostureCheckRequest,
    RgbFrame, SubjectCheckState, SubjectFrame, SubjectPose,
)
from malbut_agent_server.fall_runtime import apply_decision
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_cloud_fall_monitor import enable, frame, make
from test_fall_approach_runtime import BAG, SPOT, Looker, Places, ids, kinds
from test_fall_cloud_association import finding, reply
from test_fall_unidentified_verification import scan
from test_ollama_cloud_fall import jpeg, response

SITTER = (.38, .10, .62, .98)
ON_BED = PostureCheckReply('furniture', '침대 가장자리에 걸터앉아 있습니다.')
STANDS = PostureCheckReply('standing', '두 발로 서 있습니다.')
FLOOR = PostureCheckReply('floor', '바닥에 무릎을 꿇고 있습니다.')
UNCLEAR = PostureCheckReply('unclear', '엉덩이가 가려져 있습니다.')


class Viewer(Looker):
    def __init__(self):
        super().__init__()
        self.look = PersonCheckReply('person', '사람의 다리와 발이 보입니다.')
        self.answers = [ON_BED, ON_BED]
        self.postures = []

    async def check_posture(self, request):
        self.postures.append(request)
        answer = self.answers[len(self.postures) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


def setup(full_view=True):
    monitor, clock, _ = make()
    provider = Viewer()
    monitor._provider = provider
    monitor._place = Places({BAG: SPOT, SITTER: SPOT})
    monitor.approach_enabled = True
    monitor.full_view_enabled = full_view
    monitor.set_running_missions(('patrol',))
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(BAG)))
    question = next(e for e in events if e.kind == 'question_requested')
    monitor.drain_events()
    assert monitor.approach_result(**ids(question), outcome='arrived')
    return monitor, clock, provider, question


def cloud(monitor, clock, after):
    clock.value += after
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    return asyncio.run(monitor.run_once())


def legs_only(monitor, clock):
    """No Pose person in 3 s; Cloud says a person (legs): move to see the whole body."""
    assert cloud(monitor, clock, 3.5)


def view(monitor, question, outcome='arrived'):
    assert apply_decision(monitor, json.dumps(dict(
        action='view_result', boot_id=monitor.boot_id, **ids(question), outcome=outcome)))


def looks(monitor, clock, count):
    cloud(monitor, clock, 1.1)  # Settled after the move: first look.
    for _ in range(count - 1):
        cloud(monitor, clock, 2.6)  # A few seconds later, on new frames.


def test_a_person_sitting_on_a_bed_seen_twice_from_the_view_is_not_asked():
    monitor, clock, provider, question = setup()
    legs_only(monitor, clock)
    assert kinds(monitor.drain_events()) == [('approach_completed', 'arrived'),
                                             ('person_check_completed', 'partial_body')]
    view(monitor, question)
    cloud(monitor, clock, 1.1)
    assert kinds(monitor.drain_events()) == [('view_completed', 'arrived')]  # One look only.
    cloud(monitor, clock, 2.6)
    events = monitor.drain_events()
    assert kinds(events) == [('posture_check_completed', 'sitting_off_floor'),
                             ('incident_resolved', 'off_floor_verified')]
    check = events[0]
    assert check.analysis.purpose == 'posture_check'
    assert check.analysis.explanation == '침대 가장자리에 걸터앉아 있습니다.'
    first, second = provider.postures
    assert min(f.captured_at for f in second.window.frames) > max(
        f.captured_at for f in first.window.frames)
    incident = monitor.incident(question.incident_id)
    assert incident.state is IncidentState.RESOLVED
    assert incident.close_reason == 'off_floor_verified'
    assert not any(e.kind == 'notification_requested' for e in events)


@pytest.mark.parametrize('answers,reason', [
    ([STANDS, STANDS], 'standing'),
    ([ON_BED, STANDS], 'sitting_off_floor'),  # Got up during the check: still off the floor.
])
def test_both_looks_off_the_floor_close_it(answers, reason):
    monitor, clock, provider, question = setup()
    provider.answers = answers
    legs_only(monitor, clock)
    view(monitor, question)
    looks(monitor, clock, 2)
    assert ('posture_check_completed', reason) in kinds(monitor.drain_events())


@pytest.mark.parametrize('answers,reason,calls', [
    ([FLOOR], 'on_floor', 1),
    ([UNCLEAR], 'posture_unconfirmed', 1),
    ([CloudFallProviderError('cloud_timeout')], 'posture_unconfirmed', 1),
    ([ON_BED, FLOOR], 'on_floor', 2),
    ([ON_BED, UNCLEAR], 'posture_unconfirmed', 2),
    ([STANDS, CloudFallProviderError('cloud_timeout')], 'posture_unconfirmed', 2),
])
def test_anything_short_of_two_off_floor_looks_asks(answers, reason, calls):
    monitor, clock, provider, question = setup()
    provider.answers = answers
    legs_only(monitor, clock)
    view(monitor, question)
    looks(monitor, clock, calls)
    events = monitor.drain_events()
    assert ('posture_check_completed', reason) in kinds(events)
    assert len(provider.postures) == calls
    assert monitor.incident(question.incident_id).state is not IncidentState.RESOLVED


@pytest.mark.parametrize('outcome', ['no_path', 'timeout', 'rejected'])
def test_not_reaching_the_view_asks(outcome):
    monitor, clock, provider, question = setup()
    legs_only(monitor, clock)
    view(monitor, question, outcome)
    assert kinds(monitor.drain_events())[-2:] == [('view_completed', outcome),
                                                  ('posture_check_completed', 'body_unseen')]
    assert provider.postures == []


def test_a_clear_pose_person_at_1m_also_moves_back_before_anyone_is_asked():
    monitor, clock, provider, question = setup()
    clock.value += 0.5
    monitor.ingest_subject_frame(SubjectFrame(clock.value, (SubjectPose(
        'pose:0:p', SITTER, SubjectCheckState.UNKNOWN, True, True),), .5))
    monitor.maintain_associations()
    assert kinds(monitor.drain_events())[-1] == ('person_check_completed', 'partial_body')
    assert provider.looks == []  # Pose saw the person: no person check call.


def test_without_the_switch_a_person_is_asked_as_before():
    monitor, clock, provider, question = setup(full_view=False)
    legs_only(monitor, clock)
    assert kinds(monitor.drain_events())[-1] == ('person_check_completed', 'person')
    with pytest.raises(ValueError):
        monitor.view_result(**ids(question), outcome='guess')
    assert not monitor.view_result(**ids(question), outcome='arrived')
    assert provider.postures == []


def test_the_journal_keeps_the_posture_explanation_and_the_closure(tmp_path):
    from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
    monitor, clock, provider, question = setup()
    journal = SqliteFallJournal(tmp_path / 'events.sqlite', device_id='robot')
    monitor._journal = journal
    legs_only(monitor, clock)
    view(monitor, question)
    looks(monitor, clock, 2)
    payloads = [json.loads(row[0]) for row in journal._db.execute(
        'SELECT payload FROM incident_events ORDER BY sequence').fetchall()]
    steps = [(p['eventKind'], p['reason']) for p in payloads]
    assert steps == [('person_check_completed', 'partial_body'), ('view_completed', 'arrived'),
                     ('posture_check_completed', 'sitting_off_floor'),
                     ('incident_resolved', 'off_floor_verified')]
    assert payloads[2]['analysis']['purpose'] == 'posture_check'
    assert payloads[-1]['state'] == 'resolved'
    journal.close()


def test_the_decision_is_logged_with_both_looks():
    monitor, clock, provider, question = setup()
    lines = []
    monitor._place_log = lines.append
    legs_only(monitor, clock)
    view(monitor, question)
    looks(monitor, clock, 2)
    assert any('fall posture:' in line and 'Cloud looks furniture, furniture: sitting_off_floor'
               in line for line in lines)


def test_posture_check_wire_is_the_narrow_prompt_and_a_strict_reply():
    window = FrameWindow((RgbFrame(10.0, jpeg()), RgbFrame(10.5, jpeg())), 10.0, 10.5, False)
    body = json.loads(build_posture_check_payload(PostureCheckRequest('posture-1', window),
                                                  model='gemma'))
    assert body['messages'][0]['content'] == POSTURE_CHECK_PROMPT
    assert len(body['messages'][1]['images']) == 2
    for verdict in ('furniture', 'standing', 'floor', 'unclear'):
        assert parse_posture_check(response(json.dumps(
            dict(verdict=verdict, explanation='설명')))).verdict == verdict
    for bad in (dict(verdict='raised', explanation='x'), dict(verdict='sitting', explanation='x'),
                dict(verdict='floor', explanation='x', extra=1), dict(verdict='floor')):
        with pytest.raises(CloudFallProviderError):
            parse_posture_check(response(json.dumps(bad)))
