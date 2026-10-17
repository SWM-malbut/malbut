"""The VLM in the one fall check (SWM25-261): it reports, runs asked Cloud looks and
writes the fall rules' records. Fake Cloud and places, a real journal file; these are
unit tests, not robot checks."""

import asyncio
import json
from pathlib import Path

import pytest

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.application.fall_flow_sensor import FallFlowSensor
from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import (
    ArrivalCheckReply, CandidateKind, CloudFallReply, CloudPersonFinding, CloudPersonRegion,
    DepartureCheckReply, FallCandidate, FallRuntimePolicy, PersonCheckReply, PostureCheckReply,
    RgbFrame, SubjectCheckState, SubjectFrame, SubjectPose, VideoAssessment,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError

CONTRACT = json.loads((Path(__file__).resolve().parents[2]
                       / 'malbut_interfaces/contracts/fall_events.json').read_text())
BOX = (0.3, 0.5, 0.7, 0.9)
SPOT = (3.0, 1.0)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class Cloud:
    execution_target = 'cloud'

    def __init__(self):
        self.calls = []
        self.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, '바닥에 누워 있습니다.')
        self.arrival = ArrivalCheckReply('same_person', '회색 상의가 같습니다.')
        self.person = PersonCheckReply('person', '사람의 다리가 보입니다.')
        self.departure = DepartureCheckReply('person_walked', '일어나 걸어 나갔습니다.')
        self.posture = PostureCheckReply('floor', '바닥에 누워 있습니다.', 'fall')
        self.error = None

    async def _answer(self, name, request, value):
        self.calls.append((name, request))
        if self.error is not None:
            raise self.error
        return value

    async def analyze(self, request):
        return await self._answer('analyze', request, self.reply)

    async def check_arrival(self, request):
        return await self._answer('arrival', request, self.arrival)

    async def check_person(self, request):
        return await self._answer('person', request, self.person)

    async def check_departure(self, request):
        return await self._answer('departure', request, self.departure)

    async def check_posture(self, request):
        return await self._answer('posture', request, self.posture)


class Places:
    """Every box is at SPOT; the camera sits at the origin facing +x."""

    def __init__(self, height=0.0):
        self.height = height

    def locate_near(self, observed_at, box, *, tolerance_s=0.25):
        return SPOT

    def locate(self, captured_at, box):
        return SPOT

    def locate3d(self, captured_at, box):
        return (*SPOT, self.height)

    def camera_near(self, observed_at, *, tolerance_s=0.25):
        return ((0.0, 0.0), (1.0, 0.0))

    def clear(self):
        pass


def policy():
    return FallRuntimePolicy.agreed(
        retry_interval_s=3, max_person_observation_age_s=2, clip_window_s=10,
        max_frame_age_s=2, max_calls_per_minute=20, max_incidents=10, max_images=12)


def frame(t):
    return RgbFrame(t, b'\xff\xd8test\xff\xd9')


@pytest.fixture
def robot(tmp_path):
    clock, cloud = Clock(), Cloud()
    journal = SqliteFallJournal(tmp_path / 'events.sqlite', device_id='robot',
                                wall_clock=lambda: 1_760_000_000.0)
    sensor = FallFlowSensor(
        device_id='robot', boot_id='boot-1', policy=policy(),
        buffer=FallFrameBuffer(retention_s=30, max_bytes=100000, max_frames=200),
        provider=cloud, journal=journal, clock=clock, place_locator=Places())
    sensor.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    yield sensor, clock, cloud, journal
    journal.close()


def advance(sensor, clock, seconds, step=0.5):
    end = clock.value + seconds
    while clock.value < end - 1e-9:
        clock.value = round(clock.value + step, 6)
        sensor.ingest_rgb(frame(clock.value))
        sensor.maintain_associations()


def messages(sensor, kind=None):
    return [m for m in sensor.drain_events() if kind is None or m['kind'] == kind]


def candidate(clock, kind=CandidateKind.ALREADY_DOWN, reason='pose_sustained_horizontal_posture'):
    return FallCandidate('c1:1', 'pose:1:a', 'yolo_pose', kind, clock.value,
                         evidence_started_at=clock.value - 1, pose_reason=reason)


def poses(sensor, clock, state=SubjectCheckState.SUSPECTED, box=BOX, key='pose:1:a'):
    sensor.ingest_subject_frame(SubjectFrame(
        clock.value, (SubjectPose(key, box, state, True, True),), 1.0, camera_stationary=True))


def look(sensor, what, **fields):
    sensor.apply(dict(action='look', what=what, look_id='look-' + what, **fields))
    asyncio.run(sensor.run_once())
    result, = messages(sensor, 'fall_look_result')
    return result


def stored(journal):
    return [json.loads(row['payload']) for row in journal._db.execute(
        'SELECT payload FROM incident_events ORDER BY sequence')]


def web_accepts(payload):
    """The web contract's checks that a robot can get wrong (fall-contract.ts)."""
    kind, reason = payload['eventKind'], payload['reason']
    assert kind in CONTRACT['eventKinds'] or kind == 'notification_requested', kind
    if kind in CONTRACT['reasonsByKind']:
        assert reason in CONTRACT['reasonsByKind'][kind], (kind, reason)
    if 'analysis' in payload:
        assert payload['analysis']['purpose'] in CONTRACT['analysisPurposes'][kind]
    if kind == 'notification_requested':
        assert CONTRACT['notificationReasons'][reason] == payload['notificationLevel']
        assert payload['state'] != 'resolved'
    if payload['state'] == 'resolved':
        assert kind in ('incident_resolved', 'confirmation_completed', 'approach_returned')
    assert payload['checkRound'] >= 1 and payload['evidenceRevision'] == payload['checkRound']


# ------------------------------------------------------------ sightings


def test_a_pose_candidate_is_reported_with_its_place_photo_and_view(robot):
    sensor, clock, _, _ = robot
    advance(sensor, clock, 2)
    poses(sensor, clock)
    messages(sensor)
    sighting_id = sensor.candidate(candidate(clock))
    sighting, = (m['sighting'] for m in messages(sensor, 'fall_sighting'))
    assert sighting['sighting_id'] == sighting_id and sighting['kind'] == 'lying'
    assert sighting['track'] == 'pose:1:a' and sighting['box'] == list(BOX)
    assert sighting['point'] == list(SPOT) and sighting['robot'] == [0.0, 0.0]
    assert sighting['photo'] is not None and sighting['fall_moment'] is None


def test_a_fall_motion_keeps_its_moment_from_4_s_before_to_1_s_after(robot):
    sensor, clock, _, _ = robot
    advance(sensor, clock, 6)
    sensor.candidate(candidate(clock, CandidateKind.MOTION_SEEN, 'pose_rapid_posture_change'))
    sighting, = (m['sighting'] for m in messages(sensor, 'fall_sighting'))
    moment = sighting['fall_moment']
    assert sighting['kind'] == 'motion' and moment not in sensor._moments
    advance(sensor, clock, 1)
    frames = sensor._moments[moment].frames
    assert 1 < len(frames) <= 12 and frames[-1].captured_at <= clock.value


def test_people_in_view_are_reported_every_2_s_and_standing_up_at_once(robot):
    sensor, clock, _, _ = robot
    for _ in range(5):
        poses(sensor, clock)
        clock.value += 0.5
    seen = [m['sighting']['kind'] for m in messages(sensor, 'fall_sighting')]
    assert seen == ['seen', 'seen']
    poses(sensor, clock, SubjectCheckState.CLEAR)
    assert [m['sighting']['kind'] for m in messages(sensor, 'fall_sighting')] == ['up']


def test_a_periodic_cloud_find_is_reported_but_not_one_resting_on_a_bed(robot):
    sensor, clock, cloud, _ = robot
    advance(sensor, clock, 4)
    finding = CloudPersonFinding(VideoAssessment.OBSERVED_FALL, CandidateKind.MOTION_SEEN,
                                 (CloudPersonRegion(0, BOX), CloudPersonRegion(1, BOX)))
    cloud.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, '넘어졌습니다.', (finding,))
    sensor._scan_anchor = clock.value - 61
    assert asyncio.run(sensor.run_once())
    sighting, = (m['sighting'] for m in messages(sensor, 'fall_sighting'))
    assert sighting['kind'] == 'cloud' and sighting['fall_moment'] in sensor._moments
    assert sensor._analyses[sighting['sighting_id']].purpose == 'crosscheck'
    sensor._place.height = 0.5
    cloud.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, '누워 있습니다.', (
        CloudPersonFinding(VideoAssessment.SUSPECTED_FALL, CandidateKind.ALREADY_DOWN,
                           (CloudPersonRegion(0, BOX), CloudPersonRegion(1, BOX))),))
    advance(sensor, clock, 1)
    sensor._scan_anchor = clock.value - 61
    assert asyncio.run(sensor.run_once())
    assert messages(sensor, 'fall_sighting') == []  # 2026-10-09: on a bed, as before.


# ------------------------------------------------------------ asked looks


def test_the_first_filter_is_the_incident_cloud_check(robot):
    sensor, clock, cloud, _ = robot
    advance(sensor, clock, 4)
    result = look(sensor, 'filter', sighting_id='s1', track='pose:1:a')
    assert result['verdict'] == 'suspect' and result['sighting_id'] == 's1'
    assert cloud.calls[-1][1].purpose == 'incident'
    cloud.reply = CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, '침대에 누워 쉬고 있습니다.')
    assert look(sensor, 'filter', sighting_id='s2')['verdict'] == 'ordinary'


def test_consent_off_answers_guard_without_a_cloud_call(robot):
    sensor, clock, cloud, _ = robot
    advance(sensor, clock, 2)
    sensor.configure(enabled=True, camera_enabled=True, cloud_consent=False, connected=True)
    sensor.apply(dict(action='look', what='arrival', look_id='l1', incident_id='i1'))
    result, = messages(sensor, 'fall_look_result')
    assert (result['verdict'], result['reason']) == ('guard', 'cloud_consent_missing')
    assert cloud.calls == []


def test_a_cloud_failure_is_failed_and_its_reason_kept(robot):
    sensor, clock, cloud, _ = robot
    advance(sensor, clock, 2)
    cloud.error = CloudFallProviderError('cloud_timeout')
    result = look(sensor, 'posture', incident_id='i1')
    assert (result['verdict'], result['reason']) == ('failed', 'cloud_timeout')


def test_arrival_compares_the_first_sight_or_asks_only_for_a_person(robot):
    sensor, clock, cloud, _ = robot
    advance(sensor, clock, 2)
    photo = sensor._sight('low', clock.value, box=BOX, photo=True)
    photo_id = messages(sensor, 'fall_sighting')[0]['sighting']['photo']
    assert photo
    result = look(sensor, 'arrival', incident_id='i1', before=photo_id, box=list(BOX))
    name, request = cloud.calls[-1]
    assert result['verdict'] == 'same_person' and name == 'arrival'
    assert request.before_box == BOX and len(request.after.frames) == 2
    cloud.person = PersonCheckReply('not_person', '사람이 없습니다.')
    assert look(sensor, 'arrival', incident_id='i1', before='lost')['verdict'] == 'gone'
    assert cloud.calls[-1][0] == 'person'


def test_what_left_needs_frames_of_the_spot_from_the_way(robot):
    sensor, clock, cloud, _ = robot
    advance(sensor, clock, 2)
    sensor._sight('low', clock.value, box=BOX, point=SPOT, photo=True)
    sighting = messages(sensor, 'fall_sighting')[0]['sighting']
    sensor.apply(dict(action='record', event='incident_opened', incident_id='i1',
                      sighting_id=sighting['sighting_id'], reason='pose_sustained_low_posture',
                      state='verifying', fall_seen=False, answer=None, check_round=1))
    assert look(sensor, 'departure', incident_id='i1',
                before=sighting['photo'])['verdict'] == 'not_seen'
    assert cloud.calls == []  # Nothing from the way yet: no Cloud call.
    advance(sensor, clock, 20)  # The spot is in view (straight ahead) once a second.
    result = look(sensor, 'departure', incident_id='i1', before=sighting['photo'])
    name, request = cloud.calls[-1]
    assert result['verdict'] == 'person_walked' and name == 'departure'
    assert 1 <= len(request.middle.frames) <= 12


def test_posture_sends_the_fall_moments_and_says_unknown_when_they_are_lost(robot):
    sensor, clock, cloud, _ = robot
    advance(sensor, clock, 6)
    sensor.candidate(candidate(clock, CandidateKind.MOTION_SEEN, 'pose_rapid_posture_change'))
    moment = messages(sensor, 'fall_sighting')[0]['sighting']['fall_moment']
    advance(sensor, clock, 2)
    result = look(sensor, 'posture', incident_id='i1', fall_moments=[moment])
    request = cloud.calls[-1][1]
    assert (result['verdict'], result['earlier']) == ('floor', 'fall')
    assert len(request.fall_moments) == 1 and len(request.window.frames) <= 3
    cloud.posture = PostureCheckReply('standing', '서 있습니다.')
    result = look(sensor, 'posture', incident_id='i1', fall_moments=['gone-after-restart'])
    assert result['earlier'] == 'unknown' and cloud.calls[-1][1].fall_moments == ()


# ------------------------------------------------------------ the journal


def record(sensor, event, reason=None, **fields):
    data = dict(action='record', event=event, incident_id='i1', reason=reason,
                state='verifying', fall_seen=False, answer=None, check_round=1)
    data.update(fields)
    sensor.apply(data)


def test_records_are_written_as_the_web_takes_them(robot):
    sensor, clock, cloud, journal = robot
    advance(sensor, clock, 4)
    sensor.set_running_missions(('patrol',))
    sensor._sight('low', clock.value, box=BOX, point=SPOT, photo=True)
    sighting = messages(sensor, 'fall_sighting')[0]['sighting']
    look(sensor, 'filter', sighting_id=sighting['sighting_id'])
    record(sensor, 'incident_opened', 'pose_sustained_low_posture',
           sighting_id=sighting['sighting_id'])
    record(sensor, 'analysis_completed', look_id='look-filter')
    record(sensor, 'approach_started')
    look(sensor, 'arrival', incident_id='i1', before=sighting['photo'])
    record(sensor, 'arrival_compared', 'same_person', look_id='look-arrival')
    record(sensor, 'posture_check_completed', 'on_floor', look_id='look-filter')  # Wrong look.
    sensor.apply(dict(action='notify', incident_id='i1', reason='person_may_differ',
                      level='check', state='verifying', fall_seen=False, answer=None,
                      check_round=1))
    record(sensor, 'confirmation_completed', 'resolved', state='resolved', answer='okay')
    record(sensor, 'incident_resolved', 'response_completed', state='resolved', answer='okay')
    rows = stored(journal)
    for payload in rows:
        web_accepts(payload)
    by_kind = {p['eventKind']: p for p in rows}
    assert by_kind['incident_opened']['bootId'] == 'boot-1'
    assert by_kind['analysis_completed']['analysis']['purpose'] == 'incident'
    assert by_kind['approach_started']['reason'] == 'patrol_stopped'
    assert by_kind['arrival_compared']['analysis']['purpose'] == 'arrival_check'
    assert 'analysis' not in by_kind['posture_check_completed']
    assert journal.open_flow_incidents() == []  # Closed.
    assert journal.pending() is not None and journal.clip_status()  # Uploads and the clip.


def test_a_recheck_reopens_and_the_round_goes_on_the_record(robot):
    sensor, clock, _, journal = robot
    record(sensor, 'incident_opened', 'cloud_crosscheck', sighting_id='unknown')
    record(sensor, 'incident_resolved', 'off_floor_verified', state='resolved')
    record(sensor, 'recheck_started', 'floor_recheck', check_round=2)
    open_, = journal.open_flow_incidents()
    assert open_['check_round'] == 2
    assert stored(journal)[-1]['checkRound'] == 2


def test_a_dropped_candidate_stays_local(robot):
    sensor, _, _, journal = robot
    sensor.apply(dict(action='record', event='candidate_dropped', incident_id=None,
                      reason='reflection', sighting_id='s1'))
    assert stored(journal) == []
    assert journal._db.execute('SELECT count(*) FROM flow_notes').fetchone()[0] == 1


def test_an_unknown_record_or_state_is_refused(robot):
    sensor, _, _, _ = robot
    with pytest.raises(ValueError):
        record(sensor, 'incident_opened', state='closed')
    with pytest.raises(ValueError):
        sensor.apply(dict(action='record', event='drive_now', incident_id='i1'))


# ------------------------------------------------------------ restart and the guardian


def test_a_restarted_agent_gets_the_open_incidents_and_recent_suspicions(robot):
    sensor, clock, _, _ = robot
    advance(sensor, clock, 2)
    sensor._sight('low', clock.value, box=BOX, point=SPOT, photo=True)
    sighting = messages(sensor, 'fall_sighting')[0]['sighting']
    record(sensor, 'incident_opened', 'pose_sustained_low_posture',
           sighting_id=sighting['sighting_id'])
    sensor.apply(dict(action='flow_hello', flow=True))
    ready, = messages(sensor, 'flow_ready')
    incident, = ready['open_incidents']
    assert incident['incident_id'] == 'i1' and incident['first']['point'] == list(SPOT)
    assert [s['sighting_id'] for s in ready['recent']] == [sighting['sighting_id']]


def test_a_guardian_close_is_passed_on_now_and_then(robot):
    sensor, clock, _, _ = robot
    record(sensor, 'incident_opened', 'cloud_crosscheck', sighting_id='unknown')
    sensor.close_by_guardian('i1')
    sensor.close_by_guardian('i1')
    sensor.close_by_guardian('someone-elses')
    assert messages(sensor) == [dict(kind='guardian_closed', incident_id='i1')]
    clock.value += 11
    sensor.close_by_guardian('i1')
    assert messages(sensor, 'guardian_closed')
