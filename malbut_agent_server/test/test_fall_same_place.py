"""2026-10-12 21:13-21:17 on the robot: one fall became two cases, and a place
the robot had just found no fall at was suspected again from afar."""

import asyncio

from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, FallCandidate, IncidentState, SubjectCheckState, SubjectFrame, SubjectPose,
    VideoAssessment,
)
from test_cloud_fall_monitor import candidate, enable, frame, make
from test_fall_approach_runtime import BAG, arrive, ids, kinds, lying, see
from test_fall_approach_runtime import setup as checked_spot
from test_fall_cloud_association import BOX, finding, pose, reply
from test_fall_pending_association import advance, feed
from test_fall_unidentified_verification import scan

PLACE = (2.0, 1.0)
FAR = (6.0, 1.0)
ELSEWHERE = (0.5, 0.05, 0.6, 0.3)


class Places:
    def __init__(self, points):
        self.points = points

    def locate(self, captured_at, box):
        return self.points.get(box)

    def locate_near(self, observed_at, box, *, tolerance_s=0.25):
        return self.points.get(box)

    def clear(self):
        pass


def pose_case(points=None):
    """A Pose case for person-1 at PLACE, its question still out (like 21:15:20)."""
    m, clock, provider = make(clip_window_s=5)
    m._place = Places(points or {BOX: PLACE})
    enable(m)
    feed(m, clock, 100)
    m.candidate(candidate())
    provider.reply = reply(finding(assessment=VideoAssessment.OBSERVED_FALL))
    assert asyncio.run(m.run_once())
    question, = [e for e in m.drain_events() if e.kind == 'question_requested']
    advance(m, clock, 105, (pose(),))
    m.drain_events()
    return m, clock, provider, question


def cloud_finds(m, clock, provider, value, *, at=160, until=190):
    """A Cloud look with no Pose box to match (the box filled the frame), then time."""
    provider.reply = value
    advance(m, clock, at)
    assert asyncio.run(m.run_once())
    events = list(m.drain_events())
    advance(m, clock, until)
    asyncio.run(m.run_once())
    m.maintain_associations()
    return events + list(m.drain_events())


def test_a_cloud_finding_at_the_open_cases_place_joins_that_case():
    m, clock, provider, question = pose_case()
    m.drain_clip_ranges()
    events = cloud_finds(m, clock, provider, reply(finding()))
    assert len(m._incidents) == 1
    assert not any(e.kind in {'incident_opened', 'question_requested'} for e in events)
    final = [e.discovery for e in events if e.discovery][-1]
    assert final.reason == 'same_place_open_incident'
    assert final.association_review.status == 'cancelled' and final.incident_id is None
    # Its window becomes part of the case's clips, not of another case.
    assert any(c.incident_id == question.incident_id for c in m.drain_clip_ranges())


def test_elsewhere_later_or_a_fall_seen_happening_is_still_its_own_case():
    m, clock, provider, _ = pose_case({BOX: PLACE, ELSEWHERE: FAR})
    cloud_finds(m, clock, provider, reply(finding(ELSEWHERE)))
    assert len(m._incidents) == 2, 'four metres away'
    m, clock, provider, _ = pose_case()
    cloud_finds(m, clock, provider, reply(finding()), at=240, until=270)
    assert len(m._incidents) == 2, 'more than two minutes after the case was seen'
    m, clock, provider, _ = pose_case()
    cloud_finds(m, clock, provider, reply(finding(assessment=VideoAssessment.OBSERVED_FALL)))
    assert len(m._incidents) == 2, 'a fall seen happening'


def cleared_spot():
    """The robot looked from 1 m, found no person, closed the case and drove back."""
    monitor, clock, provider, question, _ = checked_spot()
    arrive(monitor, question)
    clock.value += 3.5
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    assert asyncio.run(monitor.run_once())
    assert monitor.return_result(**ids(question), outcome='returned')
    monitor.drain_events()
    return monitor, clock, provider


def opened(monitor):
    return [i for i in monitor._incidents.values() if i.state is not IncidentState.RESOLVED]


def test_at_a_cleared_place_only_a_fall_seen_happening_opens_a_case():
    monitor, clock, provider = cleared_spot()
    see(monitor, clock, BAG)
    assert monitor.candidate(lying(clock)) is None
    events = scan(monitor, clock, provider, reply(finding(BAG)), stamp=clock.value + 300)
    assert not opened(monitor)
    assert ('cloud_discovery', 'cleared_place') in kinds(events)
    events = scan(monitor, clock, provider,
                  reply(finding(BAG, VideoAssessment.OBSERVED_FALL)), stamp=clock.value + 300)
    assert ('incident_opened', 'target_unidentified') in kinds(events)


def test_a_fall_seen_happening_by_pose_also_opens_a_case_there():
    monitor, clock, _ = cleared_spot()
    see(monitor, clock, BAG)
    assert monitor.candidate(lying(clock, kind=CandidateKind.MOTION_SEEN)) is not None
    # That fall took the place back: lying there is a case again.
    clock.value += 0.5
    see(monitor, clock, BAG, 'pose:0:c')
    assert monitor.candidate(FallCandidate('low:2', 'pose:0:c', 'yolo_pose',
                                           CandidateKind.ALREADY_DOWN, clock.value)) is not None


def test_standing_up_there_or_half_an_hour_returns_the_usual_rules():
    monitor, clock, _ = cleared_spot()
    monitor.ingest_subject_frame(SubjectFrame(clock.value, (SubjectPose(
        'pose:0:s', BAG, SubjectCheckState.CLEAR, True, True),), .5))
    clock.value += 0.5
    see(monitor, clock, BAG)
    assert monitor.candidate(lying(clock)) is not None, 'stood up there'
    monitor, clock, _ = cleared_spot()
    clock.value += 1801
    see(monitor, clock, BAG)
    assert monitor.candidate(lying(clock)) is not None, 'after half an hour'


def test_a_person_saying_they_are_fine_clears_their_place_too():
    m, clock, _, question = pose_case()
    assert m.confirmation_result(
        incident_id=question.incident_id, question_id=question.question_id,
        subject_key=question.subject_key, evidence_revision=question.evidence_revision,
        situation_assessment='resolved', help_needed=False)
    m.drain_events()
    feed(m, clock, 110, (pose('person-2'),))
    assert m.candidate(FallCandidate('c2', 'person-2', 'yolo_pose',
                                     CandidateKind.ALREADY_DOWN, 110)) is None
    assert kinds(m.drain_events()) == [('candidate_rejected', 'cleared_place')]
