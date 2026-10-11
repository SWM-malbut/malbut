"""Only part of the body seen from 1 m: look at the whole body before asking.

2026-10-10 20:43 and 10-11 03:39: someone sitting on a bed showed only legs to
the 12 cm camera, so the close look said "person" and the robot asked.
"""

import asyncio
import json

import pytest

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (
    POSTURE_CHECK_PROMPT, build_posture_check_payload, parse_posture_check,
)
from malbut_agent_server.application.fall_detector_input import whole_body_posture
from malbut_agent_server.domain.fall_monitoring import (
    IncidentState, PersonCheckReply, PostureCheckReply, PostureCheckRequest, SubjectCheckState,
    SubjectFrame, SubjectPose,
)
from malbut_agent_server.fall_runtime import apply_decision
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_cloud_fall_monitor import enable, frame, make
from test_fall_approach_runtime import BAG, SPOT, Looker, Places, ids, kinds
from test_fall_cloud_association import finding, reply
from test_fall_unidentified_verification import scan
from test_ollama_cloud_fall import jpeg, response

LEGS = (.40, .55, .60, .98)
SITTER = (.38, .10, .62, .98)


class Viewer(Looker):
    def __init__(self):
        super().__init__()
        self.look = PersonCheckReply('person', '사람의 다리와 발이 보입니다.')
        self.posture = PostureCheckReply('raised', '침대 가장자리에 걸터앉아 있습니다.')
        self.postures = []

    async def check_posture(self, request):
        self.postures.append(request)
        if isinstance(self.posture, Exception):
            raise self.posture
        return self.posture


def setup(**switches):
    monitor, clock, _ = make()
    provider = Viewer()
    monitor._provider = provider
    monitor._place = Places({BAG: SPOT, LEGS: SPOT, SITTER: SPOT})
    monitor.approach_enabled = True
    monitor.full_view_enabled = switches.get('full_view', True)
    monitor.set_running_missions(('patrol',))
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(BAG)))
    question = next(e for e in events if e.kind == 'question_requested')
    monitor.drain_events()
    assert monitor.approach_result(**ids(question), outcome='arrived')
    return monitor, clock, provider, question


def see(monitor, clock, box, *, torso=None, upright=True, whole=True, count=1, strong=True):
    for _ in range(count):
        clock.value += 0.2
        monitor.ingest_subject_frame(SubjectFrame(clock.value, (SubjectPose(
            'pose:0:p', box, SubjectCheckState.UNKNOWN, True, strong,
            whole_body=whole if torso is not None else None,
            upright=upright if torso is not None else None, torso_floor_m=torso),), .5))
    monitor.maintain_associations()


def cloud(monitor, clock):
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    assert asyncio.run(monitor.run_once())


def legs_only(monitor, clock):
    """No Pose person in 3 s; Cloud says a person (legs): move to see the whole body."""
    clock.value += 3.5
    cloud(monitor, clock)


def view(monitor, question, outcome='arrived'):
    assert apply_decision(monitor, json.dumps(dict(
        action='view_result', boot_id=monitor.boot_id, **ids(question), outcome=outcome)))


def test_legs_only_moves_to_the_whole_body_and_a_person_sitting_on_a_bed_is_not_asked():
    monitor, clock, provider, question = setup()
    legs_only(monitor, clock)
    assert kinds(monitor.drain_events()) == [('approach_completed', 'arrived'),
                                             ('person_check_completed', 'partial_body')]
    view(monitor, question)
    see(monitor, clock, SITTER, torso=0.75, count=3)
    cloud(monitor, clock)
    events = monitor.drain_events()
    assert kinds(events) == [('view_completed', 'arrived'),
                             ('posture_check_completed', 'sitting_off_floor'),
                             ('incident_resolved', 'off_floor_verified')]
    check = next(e for e in events if e.kind == 'posture_check_completed')
    assert check.analysis.purpose == 'posture_check'
    assert check.analysis.explanation == '침대 가장자리에 걸터앉아 있습니다.'
    assert len(provider.postures) == 1 and len(provider.postures[0].window.frames) <= 3
    incident = monitor.incident(question.incident_id)
    assert incident.state is IncidentState.RESOLVED
    assert incident.close_reason == 'off_floor_verified'
    assert not any(e.kind == 'notification_requested' for e in events)


def test_standing_up_during_the_check_is_also_not_asked():
    monitor, clock, provider, question = setup()
    legs_only(monitor, clock)
    view(monitor, question)
    see(monitor, clock, SITTER, torso=1.1, count=3)
    cloud(monitor, clock)
    assert ('posture_check_completed', 'standing') in kinds(monitor.drain_events())


@pytest.mark.parametrize('answer,reason', [
    (PostureCheckReply('floor', '바닥에 무릎을 꿇고 있습니다.'), 'judgements_differ'),
    (PostureCheckReply('unclear', '엉덩이가 가려져 있습니다.'), 'posture_unconfirmed'),
    (CloudFallProviderError('cloud_timeout'), 'posture_unconfirmed'),
])
def test_cloud_must_agree_before_it_closes(answer, reason):
    monitor, clock, provider, question = setup()
    provider.posture = answer
    legs_only(monitor, clock)
    view(monitor, question)
    see(monitor, clock, SITTER, torso=0.8, count=3)
    cloud(monitor, clock)
    events = monitor.drain_events()
    assert ('posture_check_completed', reason) in kinds(events)
    assert monitor.incident(question.incident_id).state is not IncidentState.RESOLVED


@pytest.mark.parametrize('torso,upright,reason', [
    (0.35, True, 'on_floor'),      # Sitting on the floor: ask without a Cloud call.
    (0.10, False, 'on_floor'),     # Lying on the floor.
    (0.70, False, 'posture_unconfirmed'),  # High but slumped, e.g. over the bed edge.
])
def test_the_robot_measurement_alone_sends_it_to_a_question(torso, upright, reason):
    monitor, clock, provider, question = setup()
    legs_only(monitor, clock)
    view(monitor, question)
    see(monitor, clock, SITTER, torso=torso, upright=upright, count=3)
    assert ('posture_check_completed', reason) in kinds(monitor.drain_events())
    assert provider.postures == []


@pytest.mark.parametrize('outcome', ['no_path', 'timeout', 'rejected'])
def test_not_reaching_the_view_asks(outcome):
    monitor, clock, provider, question = setup()
    legs_only(monitor, clock)
    view(monitor, question, outcome)
    assert kinds(monitor.drain_events())[-2:] == [('view_completed', outcome),
                                                  ('posture_check_completed', 'body_unseen')]


def test_the_whole_body_not_seen_in_time_asks():
    monitor, clock, provider, question = setup()
    legs_only(monitor, clock)
    view(monitor, question)
    see(monitor, clock, LEGS, count=2)  # Legs again, no shoulders or hips.
    see(monitor, clock, SITTER, torso=0.75, count=2)  # Too few whole-body samples.
    clock.value += 5.0
    monitor.maintain_associations()
    assert ('posture_check_completed', 'body_unseen') in kinds(monitor.drain_events())
    assert provider.postures == []


def test_the_whole_body_already_in_view_from_1m_is_checked_there_without_moving():
    monitor, clock, provider, question = setup()
    see(monitor, clock, SITTER, torso=0.75, count=3)
    assert ('person_check_completed', 'whole_body') in kinds(monitor.drain_events())
    see(monitor, clock, SITTER, torso=0.75, count=3)
    cloud(monitor, clock)
    assert kinds(monitor.drain_events())[-2:] == [('posture_check_completed', 'sitting_off_floor'),
                                                  ('incident_resolved', 'off_floor_verified')]
    assert provider.looks == []  # Pose saw the whole body: no person check call.


def test_without_the_switch_a_person_is_asked_as_before():
    monitor, clock, provider, question = setup(full_view=False)
    legs_only(monitor, clock)
    assert kinds(monitor.drain_events())[-1] == ('person_check_completed', 'person')
    see(monitor, clock, SITTER, torso=0.75, count=3)
    assert kinds(monitor.drain_events()) == []
    with pytest.raises(ValueError):
        monitor.view_result(**ids(question), outcome='guess')
    assert not monitor.view_result(**ids(question), outcome='arrived')


def test_detector_features_give_whole_body_only_with_both_shoulders_and_hips():
    anchors = ['left_hip', 'left_shoulder', 'right_hip', 'right_shoulder']
    features = dict(usable=True, anchor_names=anchors, torso_angle_deg=8.0,
                    body_axis_angle_deg=15.0, horizontal=False, compact_body=False,
                    floor_height_m=0.74)
    assert whole_body_posture(features) == (True, True, 0.74)
    assert whole_body_posture(features | {'anchor_names': anchors[:3]}) == (False, False, 0.74)
    assert whole_body_posture(features | {'torso_angle_deg': 50.0})[1] is False
    assert whole_body_posture(features | {'floor_height_m': None})[2] is None
    assert whole_body_posture(features | {'floor_height_m': 'x'})[2] is None
    assert whole_body_posture(features | {'usable': False}) == (None, None, None)
    assert whole_body_posture(None) == (None, None, None)


def test_posture_check_wire_is_the_narrow_prompt_and_a_strict_reply():
    from malbut_agent_server.domain.fall_monitoring import FrameWindow, RgbFrame
    window = FrameWindow((RgbFrame(10.0, jpeg()), RgbFrame(10.5, jpeg())), 10.0, 10.5, False)
    body = json.loads(build_posture_check_payload(PostureCheckRequest('posture-1', window),
                                                  model='gemma'))
    assert body['messages'][0]['content'] == POSTURE_CHECK_PROMPT
    assert len(body['messages'][1]['images']) == 2
    reply_ = parse_posture_check(response(json.dumps(
        dict(verdict='raised', explanation='침대에 앉아 있습니다.'))))
    assert reply_ == PostureCheckReply('raised', '침대에 앉아 있습니다.')
    for bad in (dict(verdict='sitting', explanation='x'),
                dict(verdict='raised', explanation='x', extra=1), dict(verdict='floor')):
        with pytest.raises(CloudFallProviderError):
            parse_posture_check(response(json.dumps(bad)))


def test_the_journal_keeps_the_posture_explanation_and_the_closure(tmp_path):
    from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
    monitor, clock, provider, question = setup()
    journal = SqliteFallJournal(tmp_path / 'events.sqlite', device_id='robot')
    monitor._journal = journal
    legs_only(monitor, clock)
    view(monitor, question)
    see(monitor, clock, SITTER, torso=0.75, count=3)
    cloud(monitor, clock)
    payloads = [json.loads(row[0]) for row in journal._db.execute(
        'SELECT payload FROM incident_events ORDER BY sequence').fetchall()]
    steps = [(p['eventKind'], p['reason']) for p in payloads]
    assert steps == [('person_check_completed', 'partial_body'), ('view_completed', 'arrived'),
                     ('posture_check_completed', 'sitting_off_floor'),
                     ('incident_resolved', 'off_floor_verified')]
    posture = payloads[2]
    assert posture['analysis']['purpose'] == 'posture_check'
    assert payloads[-1]['state'] == 'resolved'
    journal.close()
