"""Measured target association and runtime integration, with no model calls."""

import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.application.fall_subject_evidence import FallSubjectEvidence
from malbut_agent_server.domain.fall_monitoring import (
    FrameWindow, IncidentState, SubjectCheckState, SubjectFrame, SubjectPose,
    SubjectVideoTarget, VoiceAnswer,
)
from malbut_agent_server.adapters.outbound.ollama_cloud_fall import build_payload
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_cloud_fall_monitor import answer, frame
from test_fall_detector_input import make_input, candidates, item
from test_ollama_cloud_fall import request


def subject(key='person', state=SubjectCheckState.CLEAR, usable=True):
    return SubjectPose(key, (0.1, 0.2, 0.4, 0.9), state, usable)


def test_target_boxes_require_exact_rgb_times_and_uninterrupted_association():
    evidence = FallSubjectEvidence(retention_s=10, max_frames=50)
    window = FrameWindow((frame(100), frame(100.4)), 100, 100.4, False)
    for stamp in (100, 100.2, 100.4):
        evidence.append(SubjectFrame(stamp, (subject(),), 0.5))
    target = evidence.target('person', window)
    assert target.sample_times == (100, 100.4)
    assert evidence.target('other-person', window) is None
    assert evidence.target('person', replace(window, frames=(frame(100.1),))) is None
    # Losing this person, even between sampled RGB frames, breaks continuity.
    evidence.append(SubjectFrame(100.6, (), 0.5))
    evidence.append(SubjectFrame(100.8, (subject(),), 0.5))
    assert evidence.latest('person')[1] != target.association_token
    assert evidence.target('person', replace(window, frames=(frame(100), frame(100.8)))) is None


@pytest.mark.parametrize('bad', ['weak', 'gap', 'reset'])
def test_weak_gap_or_reset_do_not_reuse_old_target_token(bad):
    evidence = FallSubjectEvidence(retention_s=10, max_frames=50)
    evidence.append(SubjectFrame(100, (subject(),), 0.5))
    token = evidence.latest('person')[1]
    if bad == 'weak':
        evidence.append(SubjectFrame(100.2, (subject(usable=False),), 0.5))
    elif bad == 'reset':
        evidence.clear()
    evidence.append(SubjectFrame(101, (subject(),), 0.5))
    assert evidence.latest('person')[1] != token


def test_target_metadata_is_per_frame_and_does_not_send_ids():
    req = request()
    target = SubjectVideoTarget(req.subject_key, 'private-token', (90, 100),
                                ((0.1, 0.2, 0.4, 0.9), (0.2, 0.3, 0.6, 0.8)))
    body = build_payload(replace(req, target=target), model='gemma4:31b')
    payload = json.loads(body)
    assert b'private-' not in body
    assert 'target_box' in payload['messages'][1]['content']
    assert 'upright helper' in payload['messages'][0]['content']
    assert 'no target region is supplied' not in payload['messages'][0]['content']
    for bad in (replace(target, subject_key='other'), replace(target, sample_times=(89, 100))):
        with pytest.raises(CloudFallProviderError):
            build_payload(replace(req, target=bad), model='gemma4:31b')


def checked_message(capture, *, with_candidate=False, state='clear', usable=True,
                    track='p1', helper=False):
    data = json.loads(candidates(*([item(end=capture)] if with_candidate else [])))
    row = dict(targetTrackId=track, trackingState='tracked', associationUsable=usable,
               box=[0.1, 0.2, 0.4, 0.9], features={'usable': True},
               subjectCheck={'state': state, 'reason': 'stable_upright' if state == 'clear'
                             else 'posture_suspected'})
    data.update(subjectCheckVersion=1, subjectCheckMaxGapSec=0.5, captureTimeSec=capture,
                robotMotion='stationary', unassignedCount=0, tracks=[row])
    if helper:
        data['tracks'].append(dict(row, targetTrackId='helper', box=[0.6, 0.1, 0.9, 0.9]))
    return json.dumps(data)


def feed(adapter, clock, n, *, first=False, **changes):
    # Same ROS capture with deliberate callback delay. Join must remain exact.
    capture = 1000 + n * 0.2
    clock.value = 100 + n * 0.2
    adapter.rgb(frame(0).jpeg, capture=capture, source_now=capture,
                frame_id='rgb_optical', now=clock())
    clock.value += 0.001
    return adapter.candidates(checked_message(capture, with_candidate=first, **changes),
                              source_now=capture + 0.001, now=clock())


@pytest.mark.parametrize('has_answer', [False, True])
def test_input_to_targeted_cloud_to_closure_requires_real_agent_boundary(has_answer):
    async def run():
        adapter, monitor, clock, provider = make_input()
        iid = feed(adapter, clock, 0, first=True, helper=True)[0]
        if has_answer:
            answer(monitor, iid, VoiceAnswer.OKAY)  # Test fixture, not a runtime fake reply.
        for n in range(1, 11):
            feed(adapter, clock, n, helper=True)
        await monitor.run_once()
        assert provider.calls[-1].target is not None
        assert provider.calls[-1].target.subject_key == monitor.incident(iid).subject_key
        assert provider.calls[-1].target.boxes[-1] == (0.1, 0.2, 0.4, 0.9)
        if not has_answer:
            monitor.request_recheck(iid)
        for n in range(11, 27):
            feed(adapter, clock, n, helper=True)
        await monitor.run_once()
        incident = monitor.incident(iid)
        assert len(provider.calls) == 2
        assert (incident.state is IncidentState.RESOLVED) == has_answer
        assert not any(e.kind == 'notification_requested' for e in monitor.drain_events())
    asyncio.run(run())


def test_missing_target_cannot_be_cleared_by_visible_helper():
    async def run():
        adapter, monitor, clock, provider = make_input()
        iid = feed(adapter, clock, 0, first=True)[0]
        answer(monitor, iid, VoiceAnswer.OKAY)
        for n in range(1, 11):
            feed(adapter, clock, n)
        await monitor.run_once()
        for n in range(11, 27):
            feed(adapter, clock, n, track='helper')
        await monitor.run_once()
        assert provider.calls[-1].target is None
        assert monitor.incident(iid).state is not IncidentState.RESOLVED
    asyncio.run(run())


def test_invalid_subject_payload_is_atomic_and_drops_previous_clearance():
    adapter, monitor, clock, _ = make_input()
    data = json.loads(checked_message(1000, with_candidate=True))
    data['tracks'].append(data['tracks'][0])
    with pytest.raises(ValueError):
        adapter.candidates(json.dumps(data), source_now=1000, now=clock())
    assert not monitor.drain_events()
    assert monitor._subject_evidence.latest('pose:0:p1') is None


def test_evidence_memory_is_bounded():
    evidence = FallSubjectEvidence(retention_s=1, max_frames=2)
    for stamp in (100, 100.2, 100.4):
        evidence.append(SubjectFrame(stamp, (subject(),), 0.5))
    assert evidence.target('person', FrameWindow((frame(100),), 100, 100, False)) is None
    assert len(evidence._frames) == 2
