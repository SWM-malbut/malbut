"""Existing detector contract -> real monitor; no ROS graph or inference."""

import asyncio
import json

import pytest

from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from test_cloud_fall_monitor import make, frame


def make_input():
    monitor, clock, provider = make()
    adapter = FallDetectorInput(monitor, max_source_age_s=2)
    adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    return adapter, monitor, clock, provider


def poses(people=None, **changes):
    return json.dumps(dict(schemaVersion=1, status='ok', frameId='rgb_optical',
                           captureStamp={'sec': 1000, 'nanosec': 0},
                           persons=people or [], unassigned=[], **changes))


def candidates(*items):
    return json.dumps(dict(schemaVersion=1, status='ok', timeBase='ros_image_stamp',
                           frameId='rgb_optical', candidates=list(items)))


def item(track='p1', cid='c1', revision=1, end=1000):
    return dict(candidateId=cid, revision=revision, targetTrackId=track,
                source='yolo_pose', candidateKind='found_down', requiresVerification=True,
                evidenceStartSec=end-1, evidenceEndSec=end,
                evidence={'pose': {'floor_height_m': 0.15}})


def test_capture_conversion_and_same_frame_does_not_refresh_observation():
    adapter, monitor, clock, _ = make_input()
    assert adapter.rgb(frame(0).jpeg, capture=999, frame_id='rgb_optical',
                       source_now=1000, now=clock())
    assert monitor.buffer.window(end=100, duration_s=10, max_images=12,
                                 max_age_s=2).frames[0].captured_at == 99
    with pytest.raises(ValueError, match='duplicate'):
        adapter.rgb(frame(0).jpeg, capture=999, frame_id='rgb_optical',
                    source_now=1000, now=clock())
    for capture in (997, 1001):
        with pytest.raises(ValueError, match='capture'):
            adapter.rgb(frame(0).jpeg, capture=capture, frame_id='rgb_optical',
                        source_now=1000, now=clock())


def test_pose_health_and_weak_observations_control_period_without_claiming_people():
    adapter, monitor, clock, _ = make_input()
    adapter.poses(poses(), source_now=1000, now=clock())
    assert monitor.periodic_interval_s() == 300
    clock.value += 1
    data = json.loads(poses([{'observed': True, 'pose': {}, 'confidenceLevel': 'weak'}]))
    data['captureStamp']['sec'] += 1
    adapter.poses(json.dumps(data), source_now=1001, now=clock())
    assert monitor.periodic_interval_s() == 60
    clock.value += 121
    data['captureStamp']['sec'] += 121
    data['persons'] = [{'observed': False, 'pose': None, 'state': 'lost'}]
    adapter.poses(json.dumps(data), source_now=1122, now=clock())
    assert monitor.periodic_interval_s() == 300
    clock.value += 1
    adapter.poses('{"schemaVersion":1,"status":"inference_error"}',
                  source_now=1123, now=clock())
    assert monitor.periodic_interval_s() == 60


def test_multiple_people_revision_dedup_sensor_and_cloud_request():
    adapter, monitor, clock, provider = make_input()
    adapter.rgb(frame(0).jpeg, capture=1000, frame_id='rgb_optical',
                source_now=1000, now=clock())
    found = adapter.candidates(candidates(item(), item('p2', 'c2')),
                               source_now=1000, now=clock())
    assert len(found) == 2 and found[0] != found[1]
    assert monitor.incident(found[0]).subject_key != monitor.incident(found[1]).subject_key
    assert adapter.candidates(candidates(item()), source_now=1000, now=clock()) == ()
    assert asyncio.run(monitor.run_once())
    assert provider.calls[0].sensors.floor_distance_m == 0.15
    clock.value += 1
    revised = item(revision=2, end=1001)
    revised['candidateKind'] = 'fall_suspected'
    assert adapter.candidates(candidates(revised), source_now=1001, now=clock()) == (found[0],)
    assert monitor.incident(found[0]).revision == 2


def test_privacy_off_ignores_frames_and_candidates():
    adapter, monitor, clock, provider = make_input()
    adapter.configure(enabled=False, camera_enabled=False, cloud_consent=False, connected=False)
    assert not adapter.rgb(frame(0).jpeg, capture=1000, frame_id='rgb_optical',
                           source_now=1000, now=clock())
    assert not adapter.candidates(candidates(item()), source_now=1000, now=clock())
    assert not asyncio.run(monitor.run_once()) and not provider.calls


def test_clock_reset_separates_targets_and_discards_old_images():
    adapter, monitor, clock, _ = make_input()
    adapter.rgb(frame(0).jpeg, capture=1000, frame_id='rgb_optical',
                source_now=1000, now=clock())
    first = adapter.candidates(candidates(item()), source_now=1000, now=clock())[0]
    clock.value += 1
    adapter.rgb(frame(0).jpeg, capture=10, frame_id='rgb_optical',
                source_now=10, now=clock())
    second = adapter.candidates(candidates(item(end=10)), source_now=10, now=clock())[0]
    assert first != second
    assert monitor.incident(first).subject_key != monitor.incident(second).subject_key
    assert len(monitor.buffer.window(end=101, duration_s=10, max_images=12,
                                     max_age_s=2).frames) == 1


def test_malformed_messages_cannot_create_partial_questions():
    adapter, monitor, clock, _ = make_input()
    bad = item('p2', 'c2')
    bad['evidence']['pose']['floor_height_m'] = -1
    with pytest.raises(ValueError):
        adapter.candidates(candidates(item(), bad), source_now=1000, now=clock())
    assert not monitor.drain_events()
    with pytest.raises(ValueError):
        adapter.candidates('{"schemaVersion":1,"schemaVersion":1}',
                           source_now=1000, now=clock())
