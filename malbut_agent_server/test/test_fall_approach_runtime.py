"""An uncertain suspicion is looked at from 1 m before anyone is asked or alerted."""

import asyncio
import json

import pytest

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, FallCandidate, IncidentState, PersonCheckReply, SubjectCheckState,
    SubjectFrame, SubjectPose, VideoAssessment,
)
from malbut_agent_server.fall_runtime import apply_decision, event_metadata
from test_cloud_fall_monitor import Provider, enable, frame, make
from test_fall_cloud_association import finding, reply
from test_fall_unidentified_verification import scan

BAG = (.112, .274, .261, .496)
SPOT = (2.0, 1.0)
CHAIR = (.70, .30, .85, .60)


class Places:
    def __init__(self, points):
        self.points = points

    def locate(self, captured_at, box):
        return self.points.get(box)

    def locate_near(self, observed_at, box, *, tolerance_s=0.25):
        return self.points.get(box)

    def clear(self):
        pass


class Looker(Provider):
    def __init__(self):
        super().__init__()
        self.look = PersonCheckReply('not_person', '가방과 옷가지만 보입니다.')
        self.looks = []

    async def check_person(self, request):
        self.looks.append(request)
        if isinstance(self.look, Exception):
            raise self.look
        return self.look


def setup(*, enabled=True, running=('patrol',)):
    monitor, clock, _ = make()
    provider = Looker()
    monitor._provider = provider
    monitor._place = Places({BAG: SPOT})
    monitor.approach_enabled = enabled
    monitor.set_running_missions(running)
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(BAG)))
    question = next(e for e in events if e.kind == 'question_requested')
    return monitor, clock, provider, question, events


def ids(question):
    return dict(incident_id=question.incident_id, question_id=question.question_id,
                evidence_revision=question.evidence_revision)


def arrive(monitor, question):
    assert monitor.approach_result(**ids(question), outcome='arrived')


def kinds(events):
    return [(e.kind, e.reason) for e in events]


def test_an_uncertain_scene_question_carries_the_spot_and_records_the_stopped_patrol():
    monitor, _, _, question, events = setup()
    assert question.approach_target == SPOT
    assert event_metadata(question)['approach_target'] == dict(x=2.0, y=1.0, frame='map')
    assert ('approach_started', 'patrol_stopped') in kinds(events)
    replay, = [e for e in monitor.pending_questions() if e.question_id == question.question_id]
    assert replay.approach_target == SPOT


@pytest.mark.parametrize('strong', [True, False, None])
def test_a_pose_fall_question_requests_approach_regardless_of_person_confidence(strong):
    monitor, clock, provider = make()
    monitor._place = Places({BAG: SPOT})
    monitor.approach_enabled = True
    enable(monitor)
    monitor.ingest_rgb(frame(clock.value))
    monitor.ingest_subject_frame(SubjectFrame(clock.value, (SubjectPose(
        'pose:0:b', BAG, SubjectCheckState.SUSPECTED, True, strong),), .5))
    incident_id = monitor.candidate(lying(clock))
    provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, '낙상 의심')
    assert asyncio.run(monitor.run_once())
    question, = [e for e in monitor.drain_events() if e.kind == 'question_requested']
    assert question.incident_id == incident_id
    assert question.subject_key == 'pose:0:b'
    assert event_metadata(question)['approach_target'] == dict(x=2.0, y=1.0, frame='map')
    replay, = monitor.pending_questions()
    assert replay.approach_target == SPOT


@pytest.mark.parametrize('enabled,points', [(False, {BAG: SPOT}), (True, {})])
def test_without_the_switch_or_a_map_point_the_question_goes_out_as_before(enabled, points):
    monitor, clock, provider = make()
    monitor._place = Places(points)
    monitor.approach_enabled = enabled
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(BAG)))
    question = next(e for e in events if e.kind == 'question_requested')
    assert question.approach_target is None
    assert not any(e.kind == 'approach_started' for e in events)


def test_no_person_from_close_range_closes_the_case_without_asking_and_drives_back():
    monitor, clock, provider, question, _ = setup()
    monitor.drain_events()
    arrive(monitor, question)
    clock.value += 3.5
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    assert asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    assert kinds(events)[:3] == [('approach_completed', 'arrived'),
                                 ('person_check_completed', 'not_a_person'),
                                 ('incident_resolved', 'not_a_person')]
    check = next(e for e in events if e.kind == 'person_check_completed')
    assert check.question_id == question.question_id
    assert check.analysis.purpose == 'person_check'
    assert check.analysis.explanation == '가방과 옷가지만 보입니다.'
    assert len(provider.looks) == 1 and len(provider.looks[0].window.frames) <= 3
    incident = monitor.incident(question.incident_id)
    assert incident.state is IncidentState.RESOLVED and incident.close_reason == 'not_a_person'
    assert not any(e.kind == 'notification_requested' for e in events)
    assert monitor.return_result(**ids(question), outcome='returned')
    assert kinds(monitor.drain_events()) == [('approach_returned', 'returned')]


def test_a_clear_pose_person_at_the_spot_is_a_person_without_a_cloud_call():
    monitor, clock, provider, question, _ = setup()
    arrive(monitor, question)
    clock.value += 0.5
    monitor.ingest_subject_frame(SubjectFrame(clock.value, (SubjectPose(
        'pose:0:a', BAG, SubjectCheckState.SUSPECTED, True, True),), .5))
    monitor.maintain_associations()
    events = monitor.drain_events()
    assert ('person_check_completed', 'person') in kinds(events)
    assert not provider.looks
    assert monitor.incident(question.incident_id).state is not IncidentState.RESOLVED


@pytest.mark.parametrize('look', [PersonCheckReply('person', '사람 다리가 보입니다.'),
                                  PersonCheckReply('unclear', '어두워서 알 수 없습니다.'),
                                  RuntimeError('cloud down')])
def test_a_person_unclear_or_failed_look_asks_rather_than_closes(look):
    monitor, clock, provider, question, _ = setup()
    provider.look = look
    arrive(monitor, question)
    clock.value += 3.5
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    assert asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    assert ('person_check_completed', 'person') in kinds(events)
    assert monitor.incident(question.incident_id).state is not IncidentState.RESOLVED


def test_not_arriving_is_recorded_and_nothing_is_looked_at():
    monitor, clock, provider, question, _ = setup()
    monitor.drain_events()
    assert monitor.approach_result(**ids(question), outcome='no_path')
    clock.value += 10
    monitor.maintain_associations()
    assert kinds(monitor.drain_events()) == [('approach_completed', 'no_path')]
    assert not provider.looks
    with pytest.raises(ValueError):
        monitor.approach_result(**ids(question), outcome='teleported')


def test_decisions_are_exact_and_reach_the_monitor():
    monitor, _, _, question, _ = setup()
    base = dict(boot_id=monitor.boot_id, incident_id=question.incident_id,
                question_id=question.question_id, evidence_revision=1)
    good = dict(base, action='approach_result', outcome='no_map')
    assert apply_decision(monitor, json.dumps(good))
    for bad in (dict(base, action='approach_result'),
                dict(base, action='approach_result', outcome='no_map', extra=1),
                dict(base, action='return_result', outcome=3),
                dict(base, action='approach_result', outcome='no_map', boot_id='old')):
        with pytest.raises(ValueError):
            apply_decision(monitor, json.dumps(bad))


def test_the_journal_keeps_the_close_range_explanation(tmp_path):
    monitor, clock, provider, question, _ = setup()
    journal = SqliteFallJournal(tmp_path / 'events.sqlite', device_id='robot')
    monitor._journal = journal
    arrive(monitor, question)
    clock.value += 3.5
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    asyncio.run(monitor.run_once())
    payloads = [json.loads(row[0]) for row in journal._db.execute(
        'SELECT payload FROM incident_events ORDER BY sequence').fetchall()]
    check = next(p for p in payloads if p.get('eventKind') == 'person_check_completed')
    assert check['reason'] == 'not_a_person'
    assert check['analysis']['purpose'] == 'person_check'
    resolved = next(p for p in payloads if p.get('eventKind') == 'incident_resolved')
    assert resolved['reason'] == 'not_a_person' and resolved['state'] == 'resolved'
    journal.close()


# ---------------------------------------------------------------- Cloud wire

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (  # noqa: E402
    PERSON_CHECK_PROMPT, OllamaCloudFallProvider, build_person_check_payload, parse_person_check,
)
from malbut_agent_server.domain.fall_monitoring import (  # noqa: E402
    FrameWindow, PersonCheckRequest, RgbFrame,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError  # noqa: E402
from test_ollama_cloud_fall import jpeg, response  # noqa: E402


def close_look(count=3):
    frames = tuple(RgbFrame(100 + i * .4, jpeg()) for i in range(count))
    return PersonCheckRequest('look-1', FrameWindow(frames, frames[0].captured_at,
                                                    frames[-1].captured_at, False))


def test_person_check_sends_only_close_frames_and_the_narrow_question():
    body = json.loads(build_person_check_payload(close_look(), model='gemma4:31b'))
    assert body['messages'][0]['content'] == PERSON_CHECK_PROMPT
    assert 'bags, clothes, bedding' in PERSON_CHECK_PROMPT
    assert len(body['messages'][1]['images']) == 3
    assert body['options'] == {'temperature': 0, 'num_predict': 256}
    with pytest.raises(CloudFallProviderError):
        build_person_check_payload(close_look(5), model='gemma4:31b')


@pytest.mark.parametrize('content,verdict', [
    ('{"verdict": "not_person", "explanation": "가방만 보입니다."}', 'not_person'),
    ('```json\n{"verdict": "person", "explanation": "다리가 보입니다."}\n```', 'person'),
])
def test_person_check_reply_is_strict(content, verdict):
    assert parse_person_check(response(content)).verdict == verdict
    for bad in ('{"verdict": "maybe", "explanation": "x"}', '{"verdict": "person"}',
                '{"verdict": "person", "explanation": "x", "fall": true}', 'not json'):
        with pytest.raises(CloudFallProviderError):
            parse_person_check(response(bad))


def test_live_provider_uses_the_person_check_wire():
    async def run():
        provider = OllamaCloudFallProvider(model='gemma4:31b', api_key='test-only')
        sent = []

        async def post(body):
            sent.append(json.loads(body))
            return response('{"verdict": "unclear", "explanation": "어둡습니다."}')

        provider._post = post
        result = await provider.check_person(close_look(2))
        assert result.verdict == 'unclear'
        assert sent[0]['messages'][0]['content'] == PERSON_CHECK_PROMPT

    asyncio.run(run())


def see(monitor, clock, box, subject='pose:0:b'):
    """A weak Pose person: the close look itself still asks Cloud."""
    monitor.ingest_subject_frame(SubjectFrame(clock.value, (SubjectPose(
        subject, box, SubjectCheckState.SUSPECTED, True, False),), .5))


def lying(clock, subject='pose:0:b', kind=CandidateKind.ALREADY_DOWN):
    reason = ('pose_rapid_posture_change' if kind is CandidateKind.MOTION_SEEN
              else 'pose_sustained_low_posture')
    return FallCandidate('low:1', subject, 'yolo_pose', kind, clock.value,
                         pose_reason=reason)


def test_pose_at_the_spot_being_checked_is_left_to_that_check():
    """2026-10-09: from 1 m Pose read the bag pile as a person and opened a second case."""
    monitor, clock, provider, question, _ = setup()
    arrive(monitor, question)
    clock.value += 0.5
    see(monitor, clock, BAG)
    monitor.drain_events()
    assert monitor.candidate(lying(clock)) is None
    assert monitor.candidate(lying(clock, 'pose:0:new')) is None, 'not measured yet'
    assert kinds(monitor.drain_events()) == [('candidate_rejected', 'spot_under_check')] * 2
    clock.value += 3.5
    monitor.ingest_rgb(frame(clock.value - .5))
    monitor.ingest_rgb(frame(clock.value))
    monitor.maintain_associations()
    assert asyncio.run(monitor.run_once())
    assert ('incident_resolved', 'not_a_person') in kinds(monitor.drain_events())
    see(monitor, clock, BAG)
    assert monitor.candidate(lying(clock)) is None, 'still the check while driving back'
    assert monitor.return_result(**ids(question), outcome='returned')
    # The detector sends it again: a person still there is a case of its own now.
    assert monitor.candidate(lying(clock)) is not None


@pytest.mark.parametrize('box,kind', [(CHAIR, CandidateKind.ALREADY_DOWN),
                                      (BAG, CandidateKind.MOTION_SEEN)])
def test_a_person_farther_than_1m_or_a_seen_fall_opens_its_own_case(box, kind):
    monitor, clock, _, question, _ = setup()
    monitor._place = Places({BAG: SPOT, CHAIR: (5.0, 1.0)})
    arrive(monitor, question)
    clock.value += 0.5
    see(monitor, clock, box)
    assert monitor.candidate(lying(clock, kind=kind)) is not None


def test_a_check_ends_with_its_question_or_after_a_bound():
    monitor, clock, _, question, _ = setup()
    assert monitor.approach_result(**ids(question), outcome='no_path')
    assert monitor.confirmation_failed(**ids(question)) is not None
    clock.value += 0.5
    see(monitor, clock, BAG)
    assert monitor.candidate(lying(clock)) is not None
    monitor, clock, _, question, _ = setup()
    clock.value += 241
    see(monitor, clock, BAG)
    assert monitor.candidate(lying(clock)) is not None, 'a lost return report'


class Heights(Places):
    def __init__(self, spots):
        super().__init__({box: spot[:2] for box, spot in spots.items()})
        self.spots = spots

    def locate3d(self, captured_at, box):
        return self.spots.get(box)

    def locate3d_near(self, observed_at, box, *, tolerance_s=0.25):
        return self.spots.get(box)


def test_suspected_spots_log_their_height_above_the_floor():
    """Numbers to choose a bed/sofa threshold from, before any decision uses them."""
    monitor, clock, _ = make()
    provider = Looker()
    monitor._provider = provider
    lines = []
    monitor._place_log = lines.append
    monitor._place = Heights({BAG: (*SPOT, 0.52), CHAIR: (5.0, 1.0, 0.03)})
    enable(monitor)
    scan(monitor, clock, provider, reply(finding(BAG)))
    assert lines[0] == 'fall place: Cloud box at map (2.00, 1.00), 52 cm above the floor'
    monitor.ingest_subject_frame(SubjectFrame(clock.value, (SubjectPose(
        'pose:0:c', CHAIR, SubjectCheckState.SUSPECTED, True, True),), .5))
    iid = monitor.candidate(lying(clock, 'pose:0:c'))
    assert lines[-1] == f'fall place: Pose case {iid[:8]} at map (5.00, 1.00), 3 cm above the floor'


class Resting(Places):
    """Every box measures at the same height; its lowest band is what is asked."""

    def __init__(self, height):
        super().__init__({BAG: SPOT})
        self.height, self.asked = height, []

    def locate3d(self, captured_at, box):
        self.asked.append(box)
        return None if self.height is None else (*SPOT, self.height)


@pytest.mark.parametrize('height,assessment,opened', [
    (0.52, VideoAssessment.SUSPECTED_FALL, False),
    (0.10, VideoAssessment.SUSPECTED_FALL, True),
    (0.52, VideoAssessment.OBSERVED_FALL, True),
    (None, VideoAssessment.SUSPECTED_FALL, True),
])
def test_a_body_resting_on_a_bed_is_not_a_cloud_scene_case(height, assessment, opened):
    """2026-10-09 18:27: a person on the bed was a fall case every minute."""
    monitor, clock, _ = make()
    provider = Looker()
    monitor._provider = provider
    lines = []
    monitor._place_log = lines.append
    monitor._place = place = Resting(height)
    enable(monitor)
    events = scan(monitor, clock, provider, reply(finding(BAG, assessment)))
    assert any(e.kind == 'incident_opened' for e in events) is opened
    if height is not None and assessment is not VideoAssessment.OBSERVED_FALL:
        band = place.asked[0]
        assert band[3] == BAG[3] and band[1] == pytest.approx(BAG[3] - (BAG[3] - BAG[1]) / 4)
    if not opened:
        discovery, = [e for e in events if e.kind == 'cloud_discovery']
        assert discovery.reason == 'on_furniture' and discovery.incident_id is None
        assert lines[-1] == ('fall place: Cloud box rests 52 cm above the floor, '
                             'on a bed or sofa: not a fall case')
