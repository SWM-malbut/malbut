"""Regress input timing; historical baseline is in the diagnostic JUnit output.

Production input adapter, frame buffer, association and incident monitor; only
RGB bytes, measured boxes, time and Cloud replies are fixtures. No ROS graph,
camera, model, network, voice or notification is used here.
"""

import asyncio
import json

import pytest

from malbut_agent_server.application.fall_cloud_association import associate_timed_finding
from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, CloudPersonFinding, CloudPersonRegion,
    VideoAssessment,
)
from test_cloud_fall_monitor import frame, make
from test_fall_subject_evidence import checked_message


BOX = (0.1, 0.2, 0.4, 0.9)


def pose_payload(capture, *, initial=False, mode='same_frame', stationary=True):
    data = json.loads(checked_message(
        capture, with_candidate=initial, state='unknown',
        usable=mode != 'pose_unusable'))
    data['robotMotion'] = 'stationary' if stationary else 'unknown'
    if mode == 'pose_empty':
        data['tracks'] = []
    elif mode == 'ambiguous_people':
        data['tracks'].append(dict(data['tracks'][0], targetTrackId='other'))
    return json.dumps(data)


def rgb(adapter, clock, capture, *, received_at=None):
    received_at = capture + .001 if received_at is None else received_at
    clock.value = received_at - 900
    assert adapter.rgb(frame(0).jpeg, capture=capture, frame_id='rgb_optical',
                       source_now=received_at, now=clock())


def pose(adapter, clock, capture, *, received_at=None, **kwargs):
    received_at = capture + .02 if received_at is None else received_at
    clock.value = received_at - 900
    return adapter.candidates(pose_payload(capture, **kwargs),
                              source_now=received_at, now=clock())


async def prepare(mode, *, camera_stationary=True):
    monitor, clock, provider = make(clip_window_s=5)
    adapter = FallDetectorInput(monitor, max_source_age_s=2)
    adapter.configure(enabled=True, camera_enabled=True,
                      cloud_consent=True, connected=True)
    rgb(adapter, clock, 1000)
    original = pose(adapter, clock, 1000, initial=True, stationary=camera_stationary)[0]
    provider.reply = CloudFallReply(VideoAssessment.OBSERVED_FALL, 'fixture')
    assert await monitor.run_once()
    first = monitor.drain_events()
    question = monitor.incident(original).question_id
    token = monitor.incident(original).subject_association_token
    assert question and token
    assert sum(e.kind == 'question_requested' for e in first) == 1

    # A synthetic 10 Hz camera. For the last ten seconds, select opposite
    # 5 Hz frame sets in the mismatch case. Pose continues detecting p1.
    # Other cases keep identical RGB/Pose source stamps and change arrival.
    for n in range(1, 601):
        capture = 1000 + n / 10
        offset_rgb = mode == 'different_rgb_frames' and n >= 500
        if n % 2 == int(offset_rgb):
            rgb(adapter, clock, capture)
        if n % 2 == 0:
            if n == 600 and mode.startswith('late_'):
                continue
            pose(adapter, clock, capture,
                 mode=mode if n >= 500 else 'same_frame', stationary=camera_stationary)

    clock.value = 160.1
    window = monitor.buffer.window(end=clock(), duration_s=5,
                                   max_images=12, max_age_s=2)
    assert len(window.frames) == 12
    finding = CloudPersonFinding(
        VideoAssessment.SUSPECTED_FALL, CandidateKind.ALREADY_DOWN,
        (CloudPersonRegion(0, BOX), CloudPersonRegion(11, BOX)))
    provider.reply = CloudFallReply(
        VideoAssessment.SUSPECTED_FALL, 'fixture', (finding,))
    provider.started.clear()
    return adapter, monitor, clock, provider, original, question, token, finding


@pytest.mark.parametrize('mode,reason,box_samples', [
    ('same_frame', 'matched', 2),
    ('different_rgb_frames', 'matched', 2),
    ('late_before_dispatch', 'matched', 2),
    ('late_during_cloud', 'matched', 2),
    ('late_after_cloud', 'no_matching_track', 1),
    ('pose_empty', 'no_matching_track', 0),
    ('pose_unusable', 'track_unusable', 2),
    ('ambiguous_people', 'ambiguous_tracks', 2),
])
def test_input_timing_incident_outcome(mode, reason, box_samples, record_property):
    async def run():
        (adapter, monitor, clock, provider, original, question,
         token, finding) = await prepare(mode)

        if mode == 'late_before_dispatch':
            pose(adapter, clock, 1060, received_at=1060.15)
        if mode == 'late_during_cloud':
            provider.release = asyncio.Event()
            task = asyncio.create_task(monitor.run_once())
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            try:
                pose(adapter, clock, 1060, received_at=1060.15)
            finally:
                provider.release.set()
            assert await task
        else:
            assert await monitor.run_once()

        events = list(monitor.drain_events())
        discovery = next(e.discovery for e in events if e.discovery)
        assert discovery.reason == reason
        evidence = discovery.association_evidence
        assert evidence.scope == 'finding_frames' and evidence.samples == 2
        assert evidence.pose_box_samples == box_samples

        matched = reason == 'matched'
        waiting = mode in {'late_after_cloud', 'pose_empty', 'pose_unusable'}
        assert (discovery.incident_id == original) == matched
        new_case = not matched and not waiting
        assert sum(e.kind == 'incident_opened' for e in events) == int(new_case)
        assert sum(e.kind == 'question_requested' for e in events) == int(new_case)
        if waiting:
            assert discovery.incident_id is None
            assert discovery.association_review.status == 'pending'
        assert monitor.incident(original).question_id == question
        assert monitor.incident(original).subject_association_token == token
        assert monitor.incident(original).attempts == 1
        assert monitor.incident(original).rechecks == 0
        assert [r.purpose for r in provider.calls] == ['incident', 'crosscheck']

        if mode == 'late_after_cloud':
            pose(adapter, clock, 1060, received_at=1060.15)
            later = monitor.drain_events()
            linked, = [e.discovery for e in later if e.kind == 'cloud_discovery_linked']
            assert linked.reason == 'matched_after_association_wait'
            assert linked.incident_id == original
            assert linked.association_link.source_incident_id == original
            assert monitor.incident(original).question_id == question
            # The deferred observation never became a scene/question at all.
            assert len(monitor._incidents) == 1
            assert not any(e.kind == 'question_requested' for e in later)
            assert {e.question_id for e in monitor.pending_questions()
                    if e.kind == 'question_requested'} == {question}

        # Show what is known locally AFTER the reply, without modifying the
        # dispatch snapshot or invoking deferred visual tracking.
        fresh = monitor._subject_evidence.association_samples(
            tuple(f.captured_at for f in provider.calls[-1].window.frames))
        available_now = associate_timed_finding(finding, fresh)
        if mode.startswith('late_'):
            assert available_now.reason == 'matched'
        if mode == 'different_rgb_frames':
            latest = monitor._subject_evidence.latest('pose:0:p1')
            assert latest[1] == token and latest[2].box == BOX
            assert latest[2].association_usable
            assert discovery.metadata()['association_case'] == 'matched'
            proof = discovery.association_link
            assert proof.metadata()['method'] == 'measured_pose_timestamps_v1'
            assert all(len(times) == 2 for times in proof.pose_times)

        result = dict(
            mode=mode, reason=reason,
            pose_box_samples=box_samples, finding_samples=2,
            selected_rgb_frames=len(provider.calls[-1].window.frames),
            incident_count=1 + int(new_case),
            total_question_requests=1 + int(new_case),
            association_waiting=waiting and mode != 'late_after_cloud',
            extra_incident_cloud_calls=0,
            reassociate_with_current_evidence=available_now.reason,
            linked_after_response=(mode == 'late_after_cloud'),
        )
        record_property('diagnosis', json.dumps(result))
        print(json.dumps(result, sort_keys=True))
        if mode in {'pose_empty', 'pose_unusable'}:
            clock.value = discovery.association_review.deadline
            monitor.maintain_associations()
            expired = monitor.drain_events()
            assert sum(e.kind == 'question_requested' for e in expired) == 1
            assert len(monitor._incidents) == 2 and len(provider.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize('order', ['rgb_first', 'pose_first'])
def test_same_source_stamp_survives_callback_delay_and_clock_rounding(order):
    monitor, clock, _ = make()
    adapter = FallDetectorInput(monitor, max_source_age_s=2)
    adapter.configure(enabled=True, camera_enabled=True,
                      cloud_consent=True, connected=True)
    capture = 1791100360.1234567

    def deliver(kind, source_offset, local_offset):
        clock.value = 100 + local_offset
        args = dict(source_now=capture + source_offset, now=clock())
        if kind == 'rgb':
            assert adapter.rgb(frame(0).jpeg, capture=capture,
                               frame_id='rgb_optical', **args)
        else:
            adapter.candidates(pose_payload(capture), **args)

    kinds = ('rgb', 'pose') if order == 'rgb_first' else ('pose', 'rgb')
    deliver(kinds[0], .02, .0202)
    deliver(kinds[1], .25, .2507)
    window = monitor.buffer.window(end=clock(), duration_s=5,
                                   max_images=12, max_age_s=2)
    target = monitor._subject_evidence.target('pose:0:p1', window)
    assert target is not None
    assert target.sample_times == (window.frames[0].captured_at,)
    assert target.boxes == (BOX,)
