"""Person boxes for the clip overlay: positions only, no pixels, no Cloud calls."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.adapters.outbound.homecam_fall_events import (
    FallPeopleUploader, FallUploadError, HomecamFallPeopleClient,
)
from malbut_agent_server.application.fall_people_recorder import (
    FallPeopleRecorder, PeopleTrack, RecordedPeople,
)
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, FallCandidate, SubjectCheckState, SubjectFrame, SubjectPose,
)
from test_cloud_fall_monitor import enable, frame, make
from test_fall_cloud_association import BOX, finding, reply
from test_fall_journal import attach

TARGET = (0.1, 0.2, 0.3, 0.8)
OTHER = (0.6, 0.2, 0.7, 0.7)


def recorder(**changes):
    return FallPeopleRecorder(**{'boot_id': 'boot-1', **changes})


def feed(rec, start, end, people=(('pose:0:a', TARGET),), step=0.2):
    t = start
    while t <= end + 1e-9:
        rec.observe(round(t, 3), people)
        t += step


def test_segment_takes_pre_roll_and_later_frames_relative_to_its_start():
    rec = recorder()
    feed(rec, 90.0, 100.0, (('pose:0:a', TARGET), ('pose:0:b', OTHER)))
    rec.segment('incident-1', 'pose:0:a', 0, 95.0, 110.0)
    feed(rec, 100.2, 112.0)
    assert rec.due(111.9) == ()  # settles 2 s after the segment end
    (people,) = rec.due(112.0)
    assert (people.incident_id, people.segment_index, people.revision) == ('incident-1', 0, 1)
    target, other = people.tracks
    assert target.target and not other.target
    assert target.samples[0] == (0, 100, 200, 300, 800)
    assert target.samples[-1][0] == 15000 and len(target.samples) == 76
    assert other.samples[-1][0] == 5000  # left the scene at 100 s
    assert not people.cloud and not people.truncated
    assert rec.due(200.0) == ()  # unchanged: not sent again


def test_track_key_is_a_stable_one_way_hash_per_boot():
    rec = recorder()
    feed(rec, 100.0, 101.0)
    rec.segment('incident-1', 'pose:0:a', 0, 100.0, 101.0)
    rec.segment('incident-2', None, 0, 100.0, 101.0)
    first, second = rec.due(110.0)
    assert first.tracks[0].key == second.tracks[0].key
    assert 'pose' not in first.tracks[0].key and len(first.tracks[0].key) == 12
    assert not second.tracks[0].target  # scene incident: no target person
    other = recorder(boot_id='boot-2')
    feed(other, 100.0, 101.0)
    other.segment('incident-1', 'pose:0:a', 0, 100.0, 101.0)
    assert other.due(110.0)[0].tracks[0].key != first.tracks[0].key


def test_late_cloud_box_or_extension_sends_a_new_revision():
    rec = recorder()
    feed(rec, 100.0, 110.0)
    rec.segment('incident-1', 'pose:0:a', 0, 100.0, 110.0)
    assert rec.due(112.0)[0].revision == 1
    rec.cloud('incident-1', 104.0, BOX)
    rec.cloud('incident-2', 104.0, BOX)  # another incident's location
    (people,) = rec.due(112.1)
    assert people.revision == 2 and people.cloud == ((4000, 100, 400, 700, 900),)
    rec.segment('incident-1', 'pose:0:a', 0, 100.0, 115.0)
    feed(rec, 110.2, 115.0)
    (people,) = rec.due(117.0)
    assert people.revision == 3 and people.tracks[0].samples[-1][0] == 15000


def test_empty_segment_is_not_sent():
    rec = recorder()
    rec.segment('incident-1', 'pose:0:a', 0, 100.0, 110.0)
    assert rec.due(120.0) == ()


def test_limits_keep_the_target_and_flag_truncation():
    rec = recorder(max_tracks=2, max_frames=3)
    people = [('pose:0:a', TARGET)] + [(f'pose:0:{n}', OTHER) for n in 'bcd']
    rec.segment('incident-1', 'pose:0:a', 0, 100.0, 101.0)
    feed(rec, 100.0, 101.0, people)
    (result,) = rec.due(110.0)
    assert result.truncated and len(result.tracks) == 2
    assert any(t.target for t in result.tracks)
    assert all(len(t.samples) == 3 for t in result.tracks)


def test_released_segment_is_never_resent_partially():
    rec = recorder(keep_s=60.0)
    feed(rec, 100.0, 110.0)
    rec.segment('incident-1', 'pose:0:a', 0, 100.0, 110.0)
    assert rec.due(112.0)
    assert rec.due(170.0) == ()  # released from memory
    feed(rec, 170.2, 171.0)
    rec.segment('incident-1', 'pose:0:a', 0, 100.0, 171.0)
    assert rec.due(200.0) == ()


def test_detection_off_drops_the_pre_roll():
    rec = recorder()
    feed(rec, 90.0, 100.0)
    rec.clear_history()
    rec.segment('incident-1', 'pose:0:a', 0, 95.0, 100.0)
    assert rec.due(110.0) == ()


def subject_frame(t, *people):
    return SubjectFrame(t, tuple(SubjectPose(key, box, SubjectCheckState.UNKNOWN, box is not None)
                                 for key, box in people), 0.5)


def test_monitor_records_boxes_of_the_pose_incident_and_everyone_in_view():
    monitor, clock, _ = make()
    enable(monitor)
    for step in range(11):
        clock.value = 95.0 + step * 0.5
        monitor.ingest_rgb(frame(clock.value))
        monitor.ingest_subject_frame(subject_frame(
            clock.value, ('pose:0:a', TARGET), ('pose:0:b', OTHER), ('pose:0:c', None)))
    iid = monitor.candidate(FallCandidate('c1', 'pose:0:a', 'yolo_pose',
                                          CandidateKind.MOTION_SEEN, 100.0,
                                          evidence_started_at=99.0))
    clock.value = 122.0  # 20 s after the evidence plus the settle time
    monitor.flush_people()
    (people,) = monitor.drain_people()
    assert people.incident_id == iid
    assert [t.target for t in people.tracks] == [True, False]  # no box: not recorded
    assert people.tracks[0].samples[0][0] == 6000  # clip starts at 89 s, first frame 95 s


def test_monitor_adds_cloud_locations_of_a_crosscheck_finding():
    monitor, clock, provider = make()
    enable(monitor)
    for t in (159.5, 160.0):
        clock.value = t
        monitor.ingest_rgb(frame(t))
        monitor.ingest_subject_frame(subject_frame(t, ('person-1', BOX)))
    provider.reply = reply(finding())
    asyncio.run(monitor.run_once())
    clock.value = 200.0
    monitor.flush_people()
    (people,) = monitor.drain_people()
    assert people.tracks[0].target and len(people.cloud) == 2


def test_box_storage_failure_does_not_stop_the_incident():
    monitor, clock, _ = make()
    enable(monitor)

    def broken(**kwargs):
        raise OSError('disk full')
    monitor._journal = SimpleNamespace(append_people=broken, append=lambda **k: None)
    monitor.ingest_rgb(frame(clock()))
    monitor.ingest_subject_frame(subject_frame(clock(), ('pose:0:a', TARGET)))
    iid = monitor.candidate(FallCandidate('c1', 'pose:0:a', 'yolo_pose',
                                          CandidateKind.MOTION_SEEN, 100.0))
    clock.value = 130.0
    monitor.flush_people()
    assert monitor.incident(iid) and monitor.people_storage_failures == 1
    assert monitor.drain_people() == ()


def boxes(revision=1):
    return RecordedPeople('incident-1', 'boot-1', 0, revision, (
        PeopleTrack('0123456789ab', True, ((0, 100, 200, 300, 800), (200, 101, 200, 300, 800))),),
        ((4000, 100, 400, 700, 900),), False)


def test_journal_people_payload_contract_and_newest_revision(tmp_path):
    _, _, _, journal, _ = attach(tmp_path)
    try:
        journal.append_people(device_id='robot', people=boxes(revision=2))
        journal.append_people(device_id='robot', people=boxes(revision=1))  # late, older
        pending = journal.pending_people()
        assert (pending['segment_index'], pending['revision']) == (0, 2)
        assert json.loads(pending['payload']) == dict(
            schemaVersion=1, incidentId='incident-1', bootId='boot-1', segmentIndex=0,
            revision=2, truncated=False, tracks=[dict(
                key='0123456789ab', target=True,
                samples=[[0, 100, 200, 300, 800], [200, 101, 200, 300, 800]])],
            cloud=[[4000, 100, 400, 700, 900]])
        assert journal.pending_clip() is None  # separate outbox
        with pytest.raises(ValueError):
            journal.append_people(device_id='other', people=boxes())
    finally:
        journal.close()


def test_people_uploader_retries_until_the_clip_exists(tmp_path):
    _, _, _, journal, now = attach(tmp_path)
    try:
        journal.append_people(device_id='robot', people=boxes())

        def clip_missing(payload):
            raise FallUploadError('http_503')
        uploader = FallPeopleUploader(journal, SimpleNamespace(store=clip_missing))
        assert uploader.run_once()
        status = journal.people_status()[0]
        assert (status['status'], status['last_error']) == ('pending', 'http_503')
        now[0] += 301
        uploader._client = SimpleNamespace(store=lambda payload: None)
        assert uploader.run_once()
        assert journal.people_status()[0]['status'] == 'stored'
    finally:
        journal.close()


def test_people_client_posts_to_its_endpoint_with_a_larger_body_limit():
    client = HomecamFallPeopleClient(
        base_url='https://homecam.example.com', device_id='robot',
        allowed_hosts={'homecam.example.com'}, device_token='secret-token')
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        response = io.BytesIO(b'{"stored":true,"incidentId":"i1","segmentIndex":0,"revision":1}')
        response.status = 201
        return response
    client._opener.open = open_request
    payload = json.dumps(dict(incidentId='i1', segmentIndex=0, revision=1, pad='x' * 100000))
    client.store(payload)
    assert requests[0].full_url == 'https://homecam.example.com/api/device/v1/fall-incident-people'
    with pytest.raises(FallUploadError) as error:
        client.store(json.dumps(dict(incidentId='i1', segmentIndex=0, revision=1,
                                     pad='x' * 270000)))
    assert error.value.code == 'http_413' and error.value.blocked


def test_worker_sends_boxes_after_the_clip_and_only_with_the_clip_flag(tmp_path, monkeypatch):
    from malbut_agent_server import fall_upload_worker as worker
    from test_fall_clip_journal import clip
    _, _, _, journal, _ = attach(tmp_path)
    journal.append_clip(device_id='robot', clip=clip())
    journal.append_people(device_id='robot', people=boxes())
    journal.close()
    calls = []
    monkeypatch.setattr(worker.HomecamFallClipClient, 'store',
                        lambda self, payload: calls.append('clip'))
    monkeypatch.setattr(worker.HomecamFallPeopleClient, 'store',
                        lambda self, payload: calls.append('people'))
    token = tmp_path / 'token'
    token.write_text('test-token\n')
    token.chmod(0o600)
    args = ['--journal', str(tmp_path / 'private' / 'fall.sqlite'), '--device-id', 'robot',
            '--base-url', 'https://homecam.example.com', '--allow-host', 'homecam.example.com',
            '--token-file', str(token), '--execute', '--once']
    assert worker.main(args) == 0 and calls == []
    for _ in range(2):
        assert worker.main(args + ['--upload-clips']) == 0
    assert calls == ['clip', 'people']
