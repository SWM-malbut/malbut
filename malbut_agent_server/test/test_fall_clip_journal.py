"""Clip ranges are wall-clock metadata; no recording, media upload or playback here."""

import io
import json
from types import SimpleNamespace
import urllib.error

import pytest

from malbut_agent_server.adapters.outbound.homecam_fall_events import (
    FallClipUploader, FallUploadError, HomecamFallClipClient,
)
from malbut_agent_server.application.fall_clip_planner import RecordedClip
from malbut_agent_server.domain.fall_monitoring import CandidateKind, FallCandidate
from test_cloud_fall_monitor import make, enable, frame, candidate
from test_fall_journal import attach

WALL = 1789689600.0  # 2026-09-18T00:00:00Z


def with_wall(monitor, clock, offset=WALL - 100.0):
    """Wall clock = monitor clock + offset, so expected ranges are exact."""
    shift = [offset]
    monitor._wall_clock = lambda: clock() + shift[0]
    return shift


def iso(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(
        timespec='milliseconds').replace('+00:00', 'Z')


def motion(t, started, cid='c1', subject='person-1'):
    return FallCandidate(cid, subject, 'yolo_pose', CandidateKind.MOTION_SEEN, t,
                         evidence_started_at=started)


def test_pose_candidate_records_ten_before_start_and_twenty_after_observation():
    monitor, clock, _ = make()
    enable(monitor)
    with_wall(monitor, clock)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(motion(100.0, 98.5))
    (clip,) = monitor.drain_clip_ranges()
    assert clip.incident_id == iid and clip.boot_id == 'boot-1'
    assert (clip.segment_index, clip.revision) == (0, 1)
    assert clip.start_at == WALL - 100.0 + 88.5
    assert clip.end_at == WALL - 100.0 + 120.0
    assert clip.anchor_kinds == ('pose_motion',)
    assert not clip.found_down and not clip.clock_stepped


def test_candidate_without_start_falls_back_to_observation_time():
    monitor, clock, _ = make()
    enable(monitor)
    with_wall(monitor, clock)
    monitor.ingest_rgb(frame(clock()))
    monitor.candidate(candidate())
    (clip,) = monitor.drain_clip_ranges()
    assert clip.end_at - clip.start_at == 30.0


def test_already_down_discovery_anchors_on_the_discovery_and_is_flagged():
    monitor, clock, _ = make()
    enable(monitor)
    with_wall(monitor, clock)
    monitor.ingest_rgb(frame(clock()))
    monitor.candidate(FallCandidate('c1', 'person-1', 'yolo_pose',
                                    CandidateKind.ALREADY_DOWN, 100.0))
    (clip,) = monitor.drain_clip_ranges()
    assert clip.found_down and clip.anchor_kinds == ('pose_found_down',)
    assert clip.end_at - clip.start_at == 30.0


def test_clock_step_during_an_incident_is_flagged_not_hidden():
    monitor, clock, _ = make()
    enable(monitor)
    shift = with_wall(monitor, clock)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(motion(100.0, 100.0))
    monitor.drain_clip_ranges()
    shift[0] += 3600.0  # e.g. NTP sync after boot
    clock.value = 200.0
    monitor.ingest_rgb(frame(clock()))
    monitor.candidate(motion(200.0, 199.0, cid='c2'))
    clips = [c for c in monitor.drain_clip_ranges() if c.incident_id == iid]
    assert clips and all(c.clock_stepped for c in clips)


def test_clip_storage_failure_does_not_stop_the_incident():
    monitor, clock, _ = make()
    enable(monitor)
    with_wall(monitor, clock)

    def broken(**kwargs):
        raise OSError('disk full')
    monitor._journal = SimpleNamespace(append_clip=broken, append=lambda **k: None)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(motion(100.0, 99.0))
    assert iid is not None and monitor.incident(iid)
    assert monitor.clip_storage_failures == 1
    assert monitor.drain_clip_ranges() == ()


def clip(revision=1, segment=0, start=WALL, end=WALL + 30):
    return RecordedClip('incident-1', 'boot-1', segment, revision, start, end,
                        ('pose_motion',), False, False)


def test_journal_keeps_newest_revision_and_payload_contract(tmp_path):
    monitor, _, _, journal, now = attach(tmp_path)
    try:
        journal.append_clip(device_id='robot', clip=clip(revision=2, end=WALL + 40))
        journal.append_clip(device_id='robot', clip=clip(revision=1))  # late, older
        pending = journal.pending_clip()
        assert (pending['segment_index'], pending['revision']) == (0, 2)
        payload = json.loads(pending['payload'])
        assert payload == dict(
            schemaVersion=1, incidentId='incident-1', bootId='boot-1', segmentIndex=0,
            revision=2, startAt=iso(WALL), endAt=iso(WALL + 40),
            anchorKinds=['pose_motion'], foundDown=False, clockSource='wall',
            clockStepped=False)
        with pytest.raises(ValueError):
            journal.append_clip(device_id='other', clip=clip())
    finally:
        journal.close()


def test_ack_of_an_old_revision_keeps_the_newer_one_pending(tmp_path):
    _, _, _, journal, _ = attach(tmp_path)
    try:
        journal.append_clip(device_id='robot', clip=clip(revision=1))
        sent = journal.pending_clip()
        journal.append_clip(device_id='robot', clip=clip(revision=2, end=WALL + 50))
        journal.acknowledge_clip(sent['incident_id'], sent['segment_index'], sent['revision'])
        assert journal.clip_status()[0]['status'] == 'pending'
        assert journal.pending_clip()['revision'] == 2
    finally:
        journal.close()


def test_clip_uploader_backs_off_on_not_supported_then_stores(tmp_path):
    _, _, _, journal, now = attach(tmp_path)
    try:
        journal.append_clip(device_id='robot', clip=clip())
        received = []

        def missing(payload):
            received.append(payload)
            raise FallUploadError('not_supported')

        client = SimpleNamespace(store=missing)
        uploader = FallClipUploader(journal, client)
        assert uploader.run_once()
        status = journal.clip_status()[0]
        assert (status['status'], status['last_error']) == ('pending', 'not_supported')
        assert not uploader.run_once()  # backing off
        now[0] += 301
        client.store = received.append
        assert uploader.run_once()
        assert received[0] == received[1]
        assert journal.clip_status()[0]['status'] == 'stored'
    finally:
        journal.close()


def test_clip_uploader_blocks_on_conflict(tmp_path):
    _, _, _, journal, _ = attach(tmp_path)
    try:
        journal.append_clip(device_id='robot', clip=clip())

        def conflict(payload):
            raise FallUploadError('http_409', blocked=True)
        FallClipUploader(journal, SimpleNamespace(store=conflict)).run_once()
        assert journal.clip_status()[0]['status'] == 'blocked'
    finally:
        journal.close()


def clip_client():
    return HomecamFallClipClient(
        base_url='https://homecam.example.com', device_id='robot',
        allowed_hosts={'homecam.example.com'}, device_token='secret-token')


def test_clip_client_posts_to_clip_endpoint_and_checks_the_ack():
    instance = clip_client()
    payload = json.dumps(dict(incidentId='i1', segmentIndex=0, revision=3))
    requests, bodies = [], []

    def open_request(request, timeout):
        requests.append(request)
        response = io.BytesIO(bodies.pop(0))
        response.status = 201
        return response

    instance._opener.open = open_request
    bodies.append(b'{"stored":true,"incidentId":"i1","segmentIndex":0,"revision":3}')
    instance.store(payload)
    assert requests[0].full_url == 'https://homecam.example.com/api/device/v1/fall-incident-clips'
    assert requests[0].get_header('Authorization') == 'Bearer secret-token'
    for body in (b'{"stored":true,"incidentId":"i1","segmentIndex":0,"revision":2}',
                 b'{"stored":false,"incidentId":"i1","segmentIndex":0,"revision":3}', b'[]'):
        bodies.append(body)
        with pytest.raises(FallUploadError):
            instance.store(payload)


def test_clip_client_maps_404_to_not_supported_without_blocking():
    instance = clip_client()
    for code, expected, blocked in ((404, 'not_supported', False), (409, 'http_409', True)):
        def failure(*args, **kwargs):
            raise urllib.error.HTTPError('secret-url', code, 'secret-token', {}, None)
        instance._opener.open = failure
        with pytest.raises(FallUploadError) as caught:
            instance.store('{"incidentId":"i1","segmentIndex":0,"revision":1}')
        assert (caught.value.code, caught.value.blocked) == (expected, blocked)
        assert 'secret' not in str(caught.value)


def run_worker(tmp_path, monkeypatch, *extra):
    from malbut_agent_server import fall_upload_worker as worker
    calls = []
    monkeypatch.setattr(worker.HomecamFallEventClient, 'store',
                        lambda self, payload: calls.append('event'))
    monkeypatch.setattr(worker.HomecamFallClipClient, 'store',
                        lambda self, payload: calls.append('clip'))
    token = tmp_path / 'token'
    token.write_text('test-token\n')
    token.chmod(0o600)
    args = ['--journal', str(tmp_path / 'private' / 'fall.sqlite'), '--device-id', 'robot',
            '--base-url', 'https://homecam.example.com', '--allow-host', 'homecam.example.com',
            '--token-file', str(token), '--execute', '--once', *extra]
    assert worker.main(args) == 0
    return calls


def test_worker_uploads_clips_only_when_enabled_and_events_go_first(tmp_path, monkeypatch):
    monitor, _, _, journal, _ = attach(tmp_path)
    monitor.candidate(candidate())
    journal.append_clip(device_id='robot', clip=clip())
    journal.close()
    assert run_worker(tmp_path, monkeypatch) == ['event']
    assert run_worker(tmp_path, monkeypatch) == []  # clip waits for the flag
    assert run_worker(tmp_path, monkeypatch, '--upload-clips') == ['clip']


def test_revised_segment_keeps_its_original_wall_offset_after_a_clock_step():
    monitor, clock, _ = make()
    enable(monitor)
    shift = with_wall(monitor, clock)
    monitor.ingest_rgb(frame(clock()))
    iid = monitor.candidate(motion(100.0, 100.0))
    (first,) = monitor.drain_clip_ranges()
    shift[0] += 3600.0
    clock.value = 105.0
    monitor.ingest_rgb(frame(clock()))
    monitor.candidate(motion(105.0, 104.0, cid='c2'))
    revised = [c for c in monitor.drain_clip_ranges()
               if c.incident_id == iid and c.segment_index == 0]
    assert revised and revised[-1].start_at == first.start_at
    assert revised[-1].end_at == first.end_at + 5.0 and revised[-1].clock_stepped
