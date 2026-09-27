"""Two failure cases are evidence availability, never inferred person identity."""
import asyncio
from dataclasses import replace
import json

import pytest

from malbut_agent_server.adapters.outbound.sqlite_fall_journal import SqliteFallJournal
from malbut_agent_server.application.fall_cloud_association import association_evidence
from malbut_agent_server.domain.fall_monitoring import (
    CloudAssociationEvidence, CloudDiscovery, CloudFallReply, IncidentState, VideoAssessment,
)
from malbut_agent_server.fall_runtime import event_metadata
from test_cloud_fall_monitor import candidate, enable, make
from test_fall_cloud_association import BOX, HELPER, feed, finding, pose, reply


def snapshot(people):
    return tuple(tuple((p.subject_key, 'token' if p.association_usable else None, p)
                       for p in people) for _ in range(2))


@pytest.mark.parametrize('people,expected,boxes,usable', [
    ((), 'pose_evidence_missing', 0, 0),
    ((pose(usable=False),), 'pose_evidence_missing', 2, 0),
    ((pose(),), 'identity_unverified', 2, 2),
    ((pose('helper', HELPER),), 'identity_unverified', 2, 2),
    ((pose(), pose('competitor')), 'identity_unverified', 2, 2),
])
def test_availability_does_not_claim_pose_found_cloud_person(people, expected, boxes, usable):
    evidence = association_evidence(finding(), snapshot(people))
    assert evidence.unlinked_case == expected
    assert evidence.metadata() == dict(scope='finding_frames', samples=2,
        pose_box_samples=boxes, usable_pose_samples=usable)


def test_counts_only_cloud_referenced_samples_not_newer_or_unrelated_frames():
    evidence = association_evidence(finding(), ((), (), snapshot((pose(),))[0]))
    assert evidence.unlinked_case == 'pose_evidence_missing' and evidence.samples == 2
    evidence = association_evidence(finding(), (snapshot((pose(),))[0], ()))
    assert evidence.unlinked_case == 'identity_unverified' and evidence.usable_pose_samples == 1


def test_missing_cloud_location_uses_window_without_inventing_person_match():
    value = replace(finding(), regions=())
    evidence = association_evidence(value, snapshot((pose(),)))
    assert evidence.scope == 'request_window'
    assert evidence.unlinked_case == 'identity_unverified'


def test_invalid_cloud_index_is_unknown_not_detector_absence():
    evidence = association_evidence(finding(), ())
    assert evidence.scope == 'invalid_sample_reference'
    assert evidence.unlinked_case == 'identity_unverified'


@pytest.mark.parametrize('scope,samples,boxes,usable', [
    ('invented',2,2,2), ('finding_frames',True,1,1),
    ('finding_frames',2,3,1), ('finding_frames',2,0,1), ('finding_frames',-1,0,0),
])
def test_bad_counts_are_rejected(scope,samples,boxes,usable):
    with pytest.raises(ValueError): CloudAssociationEvidence(scope,samples,boxes,usable)


def test_legacy_discovery_does_not_guess_case():
    d = CloudDiscovery('d','r',0,finding(),(1.,1.5),'no_matching_track')
    assert d.metadata()['association_case'] == 'unknown'
    assert d.metadata()['association_evidence'] is None


@pytest.mark.parametrize('people,expected', [
    ((), 'pose_evidence_missing'),
    ((pose(usable=False),), 'pose_evidence_missing'),
    ((pose('helper', HELPER),), 'identity_unverified'),
    ((pose(), pose('other')), 'identity_unverified'),
])
def test_both_unlinked_cases_start_verification_without_more_cloud_calls(people,expected,tmp_path):
    monitor, clock, provider = make(); enable(monitor)
    path = tmp_path/'private'/'falls.sqlite'
    journal = SqliteFallJournal(path,device_id='robot'); monitor._journal = journal
    for stamp in (159.5,160): feed(monitor,clock,stamp,people)
    provider.reply = reply(finding())
    assert asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    d = next(e.discovery for e in events if e.discovery)
    q, = [e for e in events if e.kind == 'question_requested']
    assert d.metadata()['association_case'] == expected
    assert d.subject_key is None and d.incident_id == q.incident_id
    assert q.confirmation_scope == 'scene'
    assert monitor.incident(q.incident_id).auto_normal_blocked
    assert len(provider.calls) == 1 and not asyncio.run(monitor.run_once())
    assert event_metadata(next(e for e in events if e.discovery))['discovery']['association_case'] == expected
    for stamp in (219.5,220): feed(monitor,clock,stamp,people)
    assert asyncio.run(monitor.run_once())
    repeated = monitor.drain_events()
    assert not any(e.kind in ('incident_opened','question_requested') for e in repeated)
    assert monitor.incident(q.incident_id).rechecks == 0
    journal.close()
    reopened = SqliteFallJournal(path,device_id='robot')
    try:
        records = reopened.discoveries()
        assert len(records) == 2 and all(r['association_case']==expected for r in records)
        assert all(r['association_evidence']['samples']==2 for r in records)
        assert 'jpeg' not in json.dumps(records) and 'private-model-text' not in json.dumps(records)
    finally: reopened.close()


def test_confirmed_match_is_not_reported_as_pending_identity():
    monitor,clock,provider = make(); enable(monitor)
    for stamp in (159.5,160): feed(monitor,clock,stamp)
    provider.reply = reply(finding()); asyncio.run(monitor.run_once())
    d = next(e.discovery for e in monitor.drain_events() if e.discovery)
    assert d.metadata()['association_case'] == 'matched'
    assert d.subject_key == 'person-1' and d.association_evidence.usable_pose_samples == 2


def test_pose_appearing_late_does_not_rewrite_dispatch_availability_or_merge():
    async def run():
        monitor,clock,provider = make(); enable(monitor)
        for stamp in (159.5,160): feed(monitor,clock,stamp,())
        provider.reply = reply(finding()); provider.release = asyncio.Event()
        task = asyncio.create_task(monitor.run_once()); await provider.started.wait()
        feed(monitor,clock,160.5,(pose('later',BOX),))
        provider.release.set(); await task
        events = monitor.drain_events()
        d = next(e.discovery for e in events if e.discovery)
        assert d.metadata()['association_case'] == 'pose_evidence_missing'
        assert d.subject_key is None
        scene_id = d.incident_id
        later_id = monitor.candidate(candidate(160.5,subject='later'))
        assert later_id != scene_id and monitor.incident(scene_id).subject_key is None
        assert monitor.incident(scene_id).state is IncidentState.VERIFYING
    asyncio.run(run())


def test_pose_only_candidate_starts_case_without_waiting_for_cloud_agreement():
    monitor,clock,provider = make(); enable(monitor)
    feed(monitor,clock,100)
    iid = monitor.candidate(candidate())
    assert iid is not None and not provider.calls
    assert monitor.incident(iid).candidate_sources == ('yolo_pose',)
    assert any(e.kind=='incident_opened' for e in monitor.drain_events())
    # A scene-level normal verdict cannot resolve this person's case.
    monitor._record_crosscheck(None,CloudFallReply(VideoAssessment.NORMAL_ACTIVITY,'normal'),(),{})
    assert monitor.incident(iid).state is not IncidentState.RESOLVED
